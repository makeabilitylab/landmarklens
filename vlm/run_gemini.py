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
from PIL import Image, ImageDraw
from google import genai
from google.genai import types


from geo_utils import load_orientation_lookup, rotate_panorama


def rotate_and_encode(pano_path, frame_stem, orientation_lookup, is_reverse,
                      crop_width=None, crop_height=None, turn_angle=0.0, line_width_deg=None):
    """Load a panorama, rotate so walking direction is centered, return JPEG bytes and PIL images.

    Args:
        crop_width: if set, a float in (0, 1] indicating what fraction of the
            panorama width to keep (centered). E.g. 0.5 keeps the middle 50%.
        crop_height: if set, a float in (0, 1] indicating what fraction of the
            panorama height to keep (centered). E.g. 0.8 keeps the middle 80%.
        turn_angle: additional rotation in degrees for junction frames, applied
            after the walking-direction rotation so the turn direction is centered.
        line_width_deg: if set, draw two vertical lines on the cropped image at
            this many degrees from center (total span = line_width_deg).

    Returns:
        (jpeg_bytes, full_pil_img, cropped_pil_img)
        full_pil_img is the rotated panorama (always full width/height).
        cropped_pil_img is the center-cropped version (same as full if no crop).
        jpeg_bytes are encoded from cropped_pil_img.
    """
    img = np.array(Image.open(pano_path).convert("RGB"))
    img = rotate_panorama(img, frame_stem, orientation_lookup, is_reverse)
    if turn_angle:
        shift_x = -round(turn_angle / 360.0 * img.shape[1])
        img = np.roll(img, shift_x, axis=1)
    full_pil_img = Image.fromarray(img.astype(np.uint8))

    # Apply width and height cropping (both centered)
    crop_left = 0
    crop_right = full_pil_img.width
    crop_top = 0
    crop_bottom = full_pil_img.height

    if crop_width is not None and crop_width < 1.0:
        w = full_pil_img.width
        crop_px = int(w * crop_width)
        crop_left = (w - crop_px) // 2
        crop_right = crop_left + crop_px

    if crop_height is not None and crop_height < 1.0:
        h = full_pil_img.height
        crop_px = int(h * crop_height)
        crop_top = (h - crop_px) // 2
        crop_bottom = crop_top + crop_px

    if crop_left > 0 or crop_top > 0 or crop_right < full_pil_img.width or crop_bottom < full_pil_img.height:
        cropped_pil_img = full_pil_img.crop((crop_left, crop_top, crop_right, crop_bottom))
    else:
        cropped_pil_img = full_pil_img

    # Draw vertical guide lines if requested
    if line_width_deg is not None:
        # line_width_deg is the total span in degrees centered on the image center.
        # The cropped image spans crop_width * 360 degrees (or 360 if no crop).
        crop_degrees = (crop_width if crop_width is not None and crop_width < 1.0 else 1.0) * 360.0
        line_fraction = line_width_deg / crop_degrees  # fraction of cropped image width
        cw = cropped_pil_img.width
        ch = cropped_pil_img.height
        center_x = cw / 2.0
        half_line_px = (line_fraction / 2.0) * cw
        left_line_x = int(round(center_x - half_line_px))
        right_line_x = int(round(center_x + half_line_px))

        draw = ImageDraw.Draw(cropped_pil_img)
        line_color = (255, 0, 0)  # red
        line_thickness = max(4, cw // 500)
        draw.line([(left_line_x, 0), (left_line_x, ch - 1)], fill=line_color, width=line_thickness)
        draw.line([(right_line_x, 0), (right_line_x, ch - 1)], fill=line_color, width=line_thickness)

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
            "category": types.Schema(type="INTEGER"),
            "category_label": types.Schema(type="STRING"),
            "color_prominence": types.Schema(type="NUMBER"),
            "shape_prominence": types.Schema(type="NUMBER"),
            "semantic_familiarity": types.Schema(type="NUMBER"),
            "navigational_context": types.Schema(type="STRING"),
        },
        required=["bbox", "description", "category", "category_label",
                   "color_prominence", "shape_prominence", "semantic_familiarity",
                   "navigational_context"],
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


def remap_bboxes_to_full(detections, crop_width, crop_height=None):
    """Remap bbox coordinates from cropped image space back to full panorama space.

    Both crops are centered, so offsets are (1 - crop_fraction) / 2.
    Coordinates in the cropped image (0-1000) are mapped back to the full panorama (0-1000).
    """
    do_x = crop_width is not None and crop_width < 1.0
    do_y = crop_height is not None and crop_height < 1.0
    if not do_x and not do_y:
        return detections

    x_offset = (1.0 - crop_width) / 2.0 if do_x else 0.0
    x_scale = crop_width if do_x else 1.0
    y_offset = (1.0 - crop_height) / 2.0 if do_y else 0.0
    y_scale = crop_height if do_y else 1.0

    remapped = []
    for det in detections:
        bbox = det["bbox"]  # [y_min, x_min, y_max, x_max]
        y_min, x_min, y_max, x_max = bbox

        x_min_full = int(round(x_offset * 1000 + (x_min / 1000.0) * x_scale * 1000))
        x_max_full = int(round(x_offset * 1000 + (x_max / 1000.0) * x_scale * 1000))
        y_min_full = int(round(y_offset * 1000 + (y_min / 1000.0) * y_scale * 1000))
        y_max_full = int(round(y_offset * 1000 + (y_max / 1000.0) * y_scale * 1000))

        remapped.append({
            **det,
            "bbox": [y_min_full, x_min_full, y_max_full, x_max_full],
        })
    return remapped


def process_batch(client, batch, orientation_lookup, system_prompt_template,
                  crop_width=None, crop_height=None, line_width_deg=None,
                  intersection_crop_width=None, intersection_line_width_deg=None,
                  processed_images=None, dry_run=False):
    """Send a batch of frames to Gemini and return parsed results.

    Args:
        crop_width: if set, crop to middle fraction of width before sending
        crop_height: if set, crop to middle fraction of height before sending
        line_width_deg: if set, draw vertical guide lines at this degree span
        intersection_crop_width: if set, override crop_width for junction frames (fraction)
        intersection_line_width_deg: if set, override line_width_deg for junction frames
        processed_images: if provided, a list to append (frame_id, pil_img) tuples to
        dry_run: if True, skip the API call and return empty detections
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

        # Junction frames get a wider crop and wider guide lines
        is_intersection_frame = frame.get("is_junction")
        frame_crop_width = crop_width
        frame_line_width_deg = line_width_deg
        if is_intersection_frame and intersection_crop_width is not None:
            frame_crop_width = intersection_crop_width
        if is_intersection_frame and intersection_line_width_deg is not None:
            frame_line_width_deg = intersection_line_width_deg

        image_bytes, full_pil_img, cropped_pil_img = rotate_and_encode(
            frame["pano_path"], frame_stem, orientation_lookup, is_reverse,
            crop_width=frame_crop_width, crop_height=crop_height,
            turn_angle=turn_angle, line_width_deg=frame_line_width_deg,
        )
        if processed_images is not None:
            processed_images.append((frame_stem, cropped_pil_img))

        content_parts.append(
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
        )

    if dry_run:
        return {frame["frame_id"]: [] for frame in batch}

    response = client.models.generate_content(
        model="gemini-3.1-pro-preview",
        contents=content_parts,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.0,
            max_output_tokens=65536,
            response_mime_type="application/json",
            response_schema=build_response_schema(n_images),
            thinking_config=types.ThinkingConfig(
                thinking_level=types.ThinkingLevel.MEDIUM
            ),
        ),
    )

    raw = json.loads(response.text)

    # Map image1..imageN keys back to frame_ids, remapping bboxes if cropped
    results = {}
    for i, frame in enumerate(batch):
        key = f"image{i + 1}"
        detections = raw.get(key, [])
        frame_crop_width = crop_width
        if frame.get("is_junction") and intersection_crop_width is not None:
            frame_crop_width = intersection_crop_width
        results[frame["frame_id"]] = remap_bboxes_to_full(detections, frame_crop_width, crop_height)

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
                        help="Degrees of panorama width to keep (centered), e.g. 180 for middle 180°. "
                             "Panorama is assumed to be 360°. Output bboxes are remapped to full panorama coordinates.")
    parser.add_argument("--crop-height", type=float, default=None,
                        help="Fraction of panorama height to keep (centered), e.g. 0.8 keeps the middle 80%% "
                             "(removing 10%% from top and bottom). Output bboxes are remapped to full panorama coordinates.")
    parser.add_argument("--line-width", type=float, default=None,
                        help="Draw two vertical red guide lines on each image spanning this many degrees "
                             "(centered). Used to indicate the region where detected object centers should lie. "
                             "Requires a prompt that references the lines.")
    parser.add_argument("--intersection-crop-width", type=float, default=None,
                        help="Override --crop-width for intersection/junction frames (degrees). "
                             "If not set, intersection frames use the same --crop-width as other frames.")
    parser.add_argument("--intersection-line-width", type=float, default=None,
                        help="Override --line-width for intersection/junction frames (degrees). "
                             "If not set, intersection frames use the same --line-width as other frames.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run the full pipeline (loading, batching, image processing) but skip "
                             "the Gemini API call. Returns empty detections for each frame.")
    parser.add_argument("--frames", nargs="+", default=None,
                        help="Only process these specific frame IDs (e.g. --frames GS010418_0023 GS010418_0027)")
    parser.add_argument("--project", default=None, help="Google Cloud project ID (or set GOOGLE_CLOUD_PROJECT)")
    parser.add_argument("--location", default="global", help="Google Cloud region")
    args = parser.parse_args()

    # Convert crop-width from degrees to fraction (internal representation)
    if args.crop_width is not None:
        if not (0.0 < args.crop_width <= 360.0):
            parser.error("--crop-width must be between 0 (exclusive) and 360 (inclusive) degrees")
        args.crop_width = args.crop_width / 360.0

    # Validate crop-height (already a fraction)
    if args.crop_height is not None:
        if not (0.0 < args.crop_height <= 1.0):
            parser.error("--crop-height must be between 0 (exclusive) and 1 (inclusive)")

    # Validate line-width
    if args.line_width is not None:
        crop_deg = (args.crop_width * 360.0) if args.crop_width is not None else 360.0
        if not (0.0 < args.line_width <= crop_deg):
            parser.error(f"--line-width must be between 0 (exclusive) and {crop_deg:.0f} degrees "
                         f"(the crop width)")

    # Convert intersection-crop-width from degrees to fraction
    if args.intersection_crop_width is not None:
        if not (0.0 < args.intersection_crop_width <= 360.0):
            parser.error("--intersection-crop-width must be between 0 (exclusive) and 360 (inclusive) degrees")
        args.intersection_crop_width = args.intersection_crop_width / 360.0

    # Validate intersection-line-width against the intersection crop
    if args.intersection_line_width is not None:
        int_crop_deg = (args.intersection_crop_width * 360.0) if args.intersection_crop_width is not None else \
                       (args.crop_width * 360.0) if args.crop_width is not None else 360.0
        if not (0.0 < args.intersection_line_width <= int_crop_deg):
            parser.error(f"--intersection-line-width must be between 0 (exclusive) and "
                         f"{int_crop_deg:.0f} degrees (the intersection crop width)")

    # Load segments and sort each by frame_order
    with open(args.input, "r") as f:
        segments = json.load(f)
    for seg_id in segments:
        segments[seg_id].sort(key=lambda x: x["frame_order"])
        # Only apply turn_angle if the previous frame had is_turn_warning set
        for i, frame in enumerate(segments[seg_id]):
            prev_warned = (i > 0 and segments[seg_id][i - 1].get("is_turn_warning", False))
            frame["_apply_turn_angle"] = prev_warned

    # Filter to specific frames if requested
    if args.frames:
        frame_set = set(args.frames)
        for seg_id in segments:
            segments[seg_id] = [f for f in segments[seg_id] if f["frame_id"] in frame_set]
        matched = sum(len(v) for v in segments.values())
        print(f"Filtered to {matched}/{len(frame_set)} requested frames")

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
    crop_h_msg = f" (crop-height: {args.crop_height:.0%})" if args.crop_height else ""
    crop_msg = f" (crop: {args.crop_width * 360:.0f}°)" if args.crop_width else ""
    line_msg = f" (lines: ±{args.line_width / 2:.0f}°)" if args.line_width else ""
    int_crop_msg = f" (intersection crop: {args.intersection_crop_width * 360:.0f}°)" if args.intersection_crop_width else ""
    int_line_msg = f" (intersection lines: ±{args.intersection_line_width / 2:.0f}°)" if args.intersection_line_width else ""
    print(f"Processing {total_frames} frames across {len(segments)} segments{crop_msg}{crop_h_msg}{line_msg}{int_crop_msg}{int_line_msg}")

    # Load orientation lookup for panorama rotation
    orientation_lookup = load_orientation_lookup()
    print(f"Orientation lookup: {len(orientation_lookup)} frames")

    # Load prompt template (contains $NUM_IMAGES placeholder)
    with open(args.system_prompt, "r") as f:
        system_prompt_template = f.read()

    # Initialize Vertex AI client (skip in dry-run mode)
    if args.dry_run:
        client = None
        print("DRY RUN: skipping Gemini API calls")
    else:
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
            "crop_height": args.crop_height,
            "line_width": args.line_width,
            "intersection_crop_width": args.intersection_crop_width,
            "intersection_line_width": args.intersection_line_width,
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

        # Split frames into batches, isolating junction frames into their own
        # single-frame batches so intersection-specific prompting doesn't bleed
        # into normal frames.
        batches = []
        normal_buf = []
        for fr in frames:
            if fr.get("is_junction"):
                if normal_buf:
                    batches.append(normal_buf)
                    normal_buf = []
                batches.append([fr])
            else:
                normal_buf.append(fr)
                if len(normal_buf) >= ws:
                    batches.append(normal_buf)
                    normal_buf = []
        if normal_buf:
            batches.append(normal_buf)

        num_batches = len(batches)
        n_junction_batches = sum(1 for b in batches if len(b) == 1 and b[0].get("is_junction"))
        print(f"\nSegment '{seg_id}': {len(frames)} frames, {num_batches} batch(es) "
              f"({n_junction_batches} junction)")

        for batch_idx, batch in enumerate(batches):
            batch_num += 1

            batch_id = f"{seg_id}_b{batch_idx}"
            frame_ids = [f["frame_id"] for f in batch]

            needs_run, reason = batch_needs_run(batch_id, frame_ids, existing_output)
            if not needs_run:
                skipped += 1
                print(f"  Batch {batch_idx + 1}/{num_batches} ({batch_id}): skipped ({reason})")
                continue

            is_junction_batch = len(batch) == 1 and batch[0].get("is_junction")
            junction_tag = " [JUNCTION]" if is_junction_batch else ""
            print(f"  Batch {batch_idx + 1}/{num_batches} ({batch_id}): running ({reason}){junction_tag}")
            print(f"    Frames: {frame_ids}")

            try:
                batch_results = process_batch(
                    client, batch, orientation_lookup, system_prompt_template,
                    crop_width=args.crop_width,
                    crop_height=args.crop_height,
                    line_width_deg=args.line_width,
                    intersection_crop_width=args.intersection_crop_width,
                    intersection_line_width_deg=args.intersection_line_width,
                    processed_images=processed_images,
                    dry_run=args.dry_run,
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
