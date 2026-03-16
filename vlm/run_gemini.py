"""
Run Gemini on sequences of panoramic images to detect navigation landmarks.

Usage:
    python vlm/run_gemini.py \
        --input frames.json \
        --output results.json \
        --system-prompt vlm/prompt_engineering/system_prompt1.md \
        --max-frames 10

The input JSON should be a dict of {segment_id: [frame dicts]}.
Each segment is processed independently. Frames within a segment are sorted
by frame_order and batched in consecutive windows of --window-size.
--max-frames caps the total number of frames processed across all segments.
Panoramas are rotated so the walking direction faces center before sending.
"""

import argparse
import io
import json
import os
import random
from pathlib import Path

from dotenv import load_dotenv
load_dotenv() 

import numpy as np
from PIL import Image
from google import genai
from google.genai import types


from geo_utils import load_orientation_lookup, rotate_panorama


def rotate_and_encode(pano_path, frame_stem, orientation_lookup, is_reverse,
                      crop_width=None, turn_angle=0.0):
    """Load a panorama, rotate so walking direction is centered, return JPEG bytes and PIL images.

    Args:
        crop_width: if set, a float in (0, 1] indicating what fraction of the
            width to keep (centered). E.g. 0.5 keeps the middle 50%.
        turn_angle: additional rotation in degrees for junction frames, applied
            after the walking-direction rotation so the turn direction is centered.

    Returns:
        (jpeg_bytes, full_pil_img, cropped_pil_img)
        full_pil_img is the rotated panorama (always full width).
        cropped_pil_img is the center-cropped version (same as full if no crop).
        jpeg_bytes are encoded from cropped_pil_img.
    """
    img = np.array(Image.open(pano_path).convert("RGB"))
    img = rotate_panorama(img, frame_stem, orientation_lookup, is_reverse)
    if turn_angle:
        shift_x = -round(turn_angle / 360.0 * img.shape[1])
        img = np.roll(img, shift_x, axis=1)
    full_pil_img = Image.fromarray(img.astype(np.uint8))

    if crop_width is not None and crop_width < 1.0:
        w = full_pil_img.width
        crop_px = int(w * crop_width)
        left = (w - crop_px) // 2
        right = left + crop_px
        cropped_pil_img = full_pil_img.crop((left, 0, right, full_pil_img.height))
    else:
        cropped_pil_img = full_pil_img

    buf = io.BytesIO()
    cropped_pil_img.save(buf, format="JPEG", quality=95)
    return buf.getvalue(), full_pil_img, cropped_pil_img


def build_response_schema(n_images):
    """Build the Gemini response schema for n_images."""
    detection_schema = types.Schema(
        type="OBJECT",
        properties={
            "bbox": types.Schema(
                type="ARRAY",
                items=types.Schema(type="INTEGER"),
                description="[y_min, x_min, y_max, x_max] normalized 0-1000",
            ),
            "description": types.Schema(type="STRING"),
            "landmark_score": types.Schema(type="NUMBER"),
        },
        required=["bbox", "description", "landmark_score"],
    )

    detection_list = types.Schema(
        type="ARRAY",
        items=detection_schema,
    )

    image_keys = [f"image{i + 1}" for i in range(n_images)]
    return types.Schema(
        type="OBJECT",
        properties={key: detection_list for key in image_keys},
        required=image_keys,
    )


def substitute_prompt(template, n_images):
    """Replace $NUM_IMAGES placeholder in prompt template."""
    return template.replace("$NUM_IMAGES", str(n_images))


def remap_bboxes_to_full(detections, crop_width):
    """Remap bbox x-coordinates from cropped image space back to full panorama space.

    The crop is centered, so the left edge of the crop starts at (1 - crop_width) / 2
    of the full image width. x-coordinates in the cropped image (0-1000) are mapped
    back to the full panorama (0-1000).

    y-coordinates are unchanged (full height is always kept).
    """
    if crop_width is None or crop_width >= 1.0:
        return detections

    crop_start = (1.0 - crop_width) / 2.0

    remapped = []
    for det in detections:
        bbox = det["bbox"]  # [y_min, x_min, y_max, x_max]
        y_min, x_min, y_max, x_max = bbox

        # Map x from cropped (0-1000) to full (0-1000)
        x_min_full = int(round(crop_start * 1000 + (x_min / 1000.0) * crop_width * 1000))
        x_max_full = int(round(crop_start * 1000 + (x_max / 1000.0) * crop_width * 1000))

        remapped.append({
            **det,
            "bbox": [y_min, x_min_full, y_max, x_max_full],
        })
    return remapped


def process_batch(client, batch, orientation_lookup, system_prompt_template,
                  crop_width=None, processed_images=None):
    """Send a batch of frames to Gemini and return parsed results.

    Args:
        crop_width: if set, crop to middle fraction of width before sending
        processed_images: if provided, a list to append (frame_id, pil_img) tuples to
    Returns:
        dict mapping frame_id -> list of detections
    Raises:
        Exception: if the API call fails (caller handles error recording)
    """
    n_images = len(batch)
    system_prompt = substitute_prompt(system_prompt_template, n_images)

    content_parts = []

    for frame in batch:
        is_reverse = frame["direction"] == "reverse"
        frame_stem = frame["frame_id"]
        turn_angle = (frame.get("turn_angle", 0.0) or 0.0) if frame.get("_apply_turn_angle") else 0.0
        image_bytes, full_pil_img, cropped_pil_img = rotate_and_encode(
            frame["pano_path"], frame_stem, orientation_lookup, is_reverse,
            crop_width=crop_width, turn_angle=turn_angle,
        )
        if processed_images is not None:
            # Always save the full panorama for sample images
            processed_images.append((frame_stem, full_pil_img))
        content_parts.append(
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
        )

    response = client.models.generate_content(
        model="gemini-3.1-pro-preview",
        contents=content_parts,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.0,
            max_output_tokens=65536,
            response_mime_type="application/json",
            response_schema=build_response_schema(n_images),
        ),
    )

    raw = json.loads(response.text)

    # Map image1..imageN keys back to frame_ids, remapping bboxes if cropped
    results = {}
    for i, frame in enumerate(batch):
        key = f"image{i + 1}"
        detections = raw.get(key, [])
        results[frame["frame_id"]] = remap_bboxes_to_full(detections, crop_width)

    return results


def load_existing_output(output_path):
    """Load existing output file if it exists, otherwise return empty structure."""
    if os.path.exists(output_path):
        with open(output_path, "r") as f:
            return json.load(f)
    return {"metadata": {}, "batches": {}}


def save_output(output_path, output_data):
    """Write output data to disk."""
    with open(output_path, "w") as f:
        json.dump(output_data, f, indent=2)


def batch_needs_run(batch_id, frame_ids, existing_output):
    """Determine whether a batch needs to be (re)run.

    Returns:
        (needs_run: bool, reason: str)
    """
    batches = existing_output.get("batches", {})
    if batch_id not in batches:
        return True, "new"

    entry = batches[batch_id]

    # If frame composition changed, rerun
    if entry.get("frame_ids") != frame_ids:
        return True, "frame_ids changed"

    status = entry.get("status")
    if status == "error":
        return True, f"previous error: {entry.get('error', 'unknown')}"

    if status == "completed":
        return False, "already completed"

    return True, f"unknown status: {status}"


def main():
    parser = argparse.ArgumentParser(description="Run Gemini on panorama sequences")
    parser.add_argument("--input", required=True, help="Path to input JSON ({segment_id: [frame dicts]})")
    parser.add_argument("--output", required=True, help="Path to output JSON")
    parser.add_argument("--system-prompt", required=True, help="Path to system prompt .md file")
    parser.add_argument("--window-size", type=int, default=5, help="Number of images per API call (default: 5)")
    parser.add_argument("--max-frames", type=int, default=None, help="Max number of frames to process")
    parser.add_argument("--save-samples", type=int, default=0, help="Save N random processed (post-rotation) images for testing")
    parser.add_argument("--save-samples-dir", default=None, help="Directory to save sample images (default: <output_dir>/sample_images)")
    parser.add_argument("--crop-width", type=float, default=None,
                        help="Fraction of panorama width to keep (centered), e.g. 0.5 for middle 50%%. "
                             "Output bboxes are remapped to full panorama coordinates.")
    parser.add_argument("--project", default=None, help="Google Cloud project ID (or set GOOGLE_CLOUD_PROJECT)")
    parser.add_argument("--location", default="global", help="Google Cloud region")
    args = parser.parse_args()

    # Validate crop-width
    if args.crop_width is not None and not (0.0 < args.crop_width <= 1.0):
        parser.error("--crop-width must be between 0 (exclusive) and 1 (inclusive)")

    # Load segments and sort each by frame_order
    with open(args.input, "r") as f:
        segments = json.load(f)
    for seg_id in segments:
        segments[seg_id].sort(key=lambda x: x["frame_order"])
        # Only apply turn_angle if the previous frame had is_turn_warning set
        for i, frame in enumerate(segments[seg_id]):
            prev_warned = (i > 0 and segments[seg_id][i - 1].get("is_turn_warning", False))
            frame["_apply_turn_angle"] = prev_warned

    # Apply global max-frames budget across segments in order
    remaining = args.max_frames
    if remaining is not None:
        for seg_id in segments:
            if remaining <= 0:
                segments[seg_id] = []
            else:
                segments[seg_id] = segments[seg_id][:remaining]
                remaining -= len(segments[seg_id])

    total_frames = sum(len(frames) for frames in segments.values())
    crop_msg = f" (crop: {args.crop_width:.0%} center)" if args.crop_width else ""
    print(f"Processing {total_frames} frames across {len(segments)} segments{crop_msg}")

    # Load orientation lookup for panorama rotation
    orientation_lookup = load_orientation_lookup()
    print(f"Orientation lookup: {len(orientation_lookup)} frames")

    # Load prompt template (contains $NUM_IMAGES placeholder)
    with open(args.system_prompt, "r") as f:
        system_prompt_template = f.read()

    # Initialize Vertex AI client
    client = genai.Client(
        vertexai=True,
        project=args.project or os.getenv("GOOGLE_CLOUD_PROJECT"),
        location=args.location,
    )

    # Process each segment independently, batching within each
    ws = args.window_size

    # Load existing output for resumption
    existing_output = load_existing_output(args.output)
    output_data = {
        "metadata": {
            "window_size": ws,
            "crop_width": args.crop_width,
        },
        "batches": dict(existing_output.get("batches", {})),
    }
    batch_num = 0
    skipped = 0
    errored = 0
    processed_images = [] if args.save_samples > 0 else None

    for seg_id, frames in segments.items():
        if not frames:
            continue
        num_batches = (len(frames) + ws - 1) // ws
        print(f"\nSegment '{seg_id}': {len(frames)} frames, {num_batches} batch(es)")

        for batch_idx in range(num_batches):
            start = batch_idx * ws
            end = min(start + ws, len(frames))
            batch = frames[start:end]
            batch_num += 1

            batch_id = f"{seg_id}_b{batch_idx}"
            frame_ids = [f["frame_id"] for f in batch]

            needs_run, reason = batch_needs_run(batch_id, frame_ids, existing_output)
            if not needs_run:
                skipped += 1
                print(f"  Batch {batch_idx + 1}/{num_batches} ({batch_id}): skipped ({reason})")
                continue

            print(f"  Batch {batch_idx + 1}/{num_batches} ({batch_id}): running ({reason})")
            print(f"    Frames: {frame_ids}")

            try:
                batch_results = process_batch(
                    client, batch, orientation_lookup, system_prompt_template,
                    crop_width=args.crop_width,
                    processed_images=processed_images,
                )
                output_data["batches"][batch_id] = {
                    "segment_id": seg_id,
                    "batch_index": batch_idx,
                    "frame_ids": frame_ids,
                    "status": "completed",
                    "results": batch_results,
                }
            except Exception as e:
                errored += 1
                print(f"    ERROR: {e}")
                output_data["batches"][batch_id] = {
                    "segment_id": seg_id,
                    "batch_index": batch_idx,
                    "frame_ids": frame_ids,
                    "status": "error",
                    "error": str(e),
                    "results": {},
                }

            # Save after every batch
            save_output(args.output, output_data)

    # Save random sample images for testing
    if processed_images:
        n_samples = min(args.save_samples, len(processed_images))
        samples = random.sample(processed_images, n_samples)
        sample_dir = Path(args.save_samples_dir) if args.save_samples_dir else Path(args.output).parent / "sample_images"
        sample_dir.mkdir(parents=True, exist_ok=True)
        for frame_id, pil_img in samples:
            pil_img.save(sample_dir / f"{frame_id}.jpg", format="JPEG", quality=85)
        print(f"\nSaved {n_samples} sample images to {sample_dir}")

    # Final summary
    total_batches = batch_num
    completed = sum(1 for b in output_data["batches"].values() if b["status"] == "completed")
    all_results = {}
    for b in output_data["batches"].values():
        if b["status"] == "completed":
            all_results.update(b.get("results", {}))

    print(f"\nResults written to {args.output}")
    print(f"Batches: {completed} completed, {errored} errored, {skipped} skipped (of {total_batches} total)")
    print(f"Total frames with detections: {sum(1 for v in all_results.values() if v)}/{len(all_results)}")


if __name__ == "__main__":
    main()
