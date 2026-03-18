"""
Second-pass validation for VLM detection pipeline.

Crops each detection from its source panorama and sends it to Gemini
for validation, adding ``is_valid`` and ``reason_is_valid`` fields.

Usage:
    python vlm/validate_detections.py \
        --input vlm/ningyocho_r1/ningyocho_r1_route.json \
        --detections vlm/output/ningyocho_r1_route_deduped.json \
        --output vlm/output/ningyocho_r1_route_validated.json \
        --system-prompt vlm/prompt_engineering/validation/system_prompt1.md \
        --buffer 1.5
"""

import argparse
import copy
import io
import json
import os
import random
import warnings
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import numpy as np
from PIL import Image
from google import genai
from google.genai import types

from dedup_detections import Detection, crop_detection_buffered, load_and_flatten
from geo_utils import load_orientation_lookup, rotate_panorama


# ---------------------------------------------------------------------------
# Gemini helpers
# ---------------------------------------------------------------------------

def build_validation_schema():
    """Build the Gemini response schema for a single detection validation."""
    return types.Schema(
        type="OBJECT",
        properties={
            "is_valid": types.Schema(type="BOOLEAN"),
            "reason_is_valid": types.Schema(type="STRING"),
        },
        required=["is_valid", "reason_is_valid"],
    )


def substitute_prompt(template: str, detection: Detection) -> str:
    """Replace $DESCRIPTION and $CATEGORY placeholders in the prompt template."""
    return (
        template
        .replace("$DESCRIPTION", detection.description)
        .replace("$CATEGORY", detection.category_label)
    )


def draw_bbox_overlay(crop_img: Image.Image, buffer: float) -> Image.Image:
    """Draw a translucent bounding box on the crop showing the original detection region.

    The crop is centered on the bbox and expanded by ``buffer`` in each direction,
    so the original bbox occupies the center 1/buffer fraction of the crop.
    """
    from PIL import ImageDraw

    crop_img = crop_img.copy()
    w, h = crop_img.size

    # Original bbox region within the buffered crop
    margin_x = (buffer - 1) / (2 * buffer)
    margin_y = (buffer - 1) / (2 * buffer)
    x0 = int(w * margin_x)
    y0 = int(h * margin_y)
    x1 = int(w * (1 - margin_x))
    y1 = int(h * (1 - margin_y))

    # Draw translucent fill
    overlay = Image.new("RGBA", crop_img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rectangle([x0, y0, x1, y1], fill=None, outline=(0, 255, 0, 200), width=4)

    # Composite onto the crop
    if crop_img.mode != "RGBA":
        crop_img = crop_img.convert("RGBA")
    crop_img = Image.alpha_composite(crop_img, overlay)
    return crop_img.convert("RGB")


def crop_to_jpeg_bytes(crop_img: Image.Image) -> bytes:
    """Encode a PIL Image as JPEG bytes."""
    buf = io.BytesIO()
    crop_img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Resumability helpers
# ---------------------------------------------------------------------------

def load_existing_output(output_path: str, detections_path: str) -> dict:
    """Load existing validated output, or deep-copy the detections JSON as a starting point."""
    if os.path.exists(output_path):
        with open(output_path, "r") as f:
            return json.load(f)
    # First run: start from a copy of the input detections
    with open(detections_path, "r") as f:
        return copy.deepcopy(json.load(f))


def build_validated_set(output_data: dict) -> set[tuple[str, str, int]]:
    """Return the set of (batch_id, frame_id, det_index) already validated in output."""
    validated = set()
    for batch_id, batch in output_data.get("batches", {}).items():
        if batch.get("status") != "completed":
            continue
        for frame_id, dets in batch.get("results", {}).items():
            for det_idx, det in enumerate(dets):
                if "is_valid" in det:
                    validated.add((batch_id, frame_id, det_idx))
    return validated


def save_output(output_path: str, output_data: dict):
    """Write output data to disk."""
    with open(output_path, "w") as f:
        json.dump(output_data, f, indent=2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Second-pass validation of VLM detections using Gemini"
    )
    parser.add_argument("--input", required=True,
                        help="Route JSON (has pano_path, frame_order, direction)")
    parser.add_argument("--detections", required=True,
                        help="Detections JSON (typically deduped output)")
    parser.add_argument("--output", required=True,
                        help="Output path for validated JSON")
    parser.add_argument("--system-prompt", required=True,
                        help="Path to system prompt .md template "
                             "(supports $DESCRIPTION and $CATEGORY placeholders)")
    parser.add_argument("--buffer", type=float, default=1.5,
                        help="Crop buffer multiplier (default: 1.5)")
    parser.add_argument("--max-frames", type=int, default=None,
                        help="Max number of frames to validate (all detections in each frame are processed)")
    parser.add_argument("--save-samples", type=int, default=0,
                        help="Save N random crop images (as sent to the VLM) for inspection")
    parser.add_argument("--save-samples-dir", default=None,
                        help="Directory to save sample images (default: <output_dir>/sample_crops)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip Gemini API calls; mark all detections as valid")
    parser.add_argument("--project", default=None,
                        help="Google Cloud project ID (or set GOOGLE_CLOUD_PROJECT)")
    parser.add_argument("--location", default="global",
                        help="Google Cloud region")
    args = parser.parse_args()

    # Load & flatten detections (reuses dedup_detections.py logic)
    print("[1/4] Loading and flattening detections...")
    detections = load_and_flatten(args.input, args.detections)
    print(f"  Total detections: {len(detections)}")

    if len(detections) == 0:
        print("No detections found. Nothing to validate.")
        return

    # Load orientation lookup for panorama rotation
    orientation_lookup = load_orientation_lookup()

    # Load prompt template
    with open(args.system_prompt, "r") as f:
        prompt_template = f.read()

    # Initialize Gemini client
    if args.dry_run:
        client = None
        print("DRY RUN: skipping Gemini API calls")
    else:
        client = genai.Client(
            vertexai=True,
            project=args.project or os.getenv("GOOGLE_CLOUD_PROJECT"),
            location=args.location,
        )

    validation_schema = build_validation_schema()

    # Load or initialize output (deep-copy of input detections)
    output_data = load_existing_output(args.output, args.detections)
    already_validated = build_validated_set(output_data)
    print(f"  Already validated: {len(already_validated)}")

    # Ensure output directory exists
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)

    # Group detections by frame_id to avoid reloading panoramas
    by_frame: dict[str, list[Detection]] = defaultdict(list)
    for det in detections:
        by_frame[det.frame_id].append(det)

    # Process each frame group
    max_frames = args.max_frames
    print(f"\n[2/4] Cropping and validating detections...")
    if max_frames is not None:
        print(f"  Capped at {max_frames} frames")
    n_validated = 0
    n_skipped = 0
    n_errors = 0
    n_valid = 0
    n_invalid = 0
    processed_crops = [] if args.save_samples > 0 else None

    frame_ids_sorted = sorted(by_frame.keys(),
                              key=lambda fid: by_frame[fid][0].frame_order)
    total_frames = len(frame_ids_sorted)

    for frame_num, frame_id in enumerate(frame_ids_sorted, 1):
        frame_dets = by_frame[frame_id]
        det0 = frame_dets[0]

        # Load and rotate panorama by yaw only (bboxes are stored in yaw-only space)
        img = np.array(Image.open(det0.pano_path).convert("RGB"))
        is_reverse = det0.direction == "reverse"
        img = rotate_panorama(img, frame_id, orientation_lookup, is_reverse)

        for det in frame_dets:
            key = (det.batch_id, det.frame_id, det.det_index)

            # Skip if already validated
            if key in already_validated:
                n_skipped += 1
                continue

            # Crop detection with buffer, then overlay the original bbox
            crop_img = crop_detection_buffered(img, det.bbox, buffer=args.buffer)
            crop_with_bbox = draw_bbox_overlay(crop_img, args.buffer)
            if processed_crops is not None:
                processed_crops.append((f"{det.frame_id}_det{det.det_index}", crop_with_bbox))
            jpeg_bytes = crop_to_jpeg_bytes(crop_with_bbox)

            # Build per-detection prompt
            system_prompt = substitute_prompt(prompt_template, det)

            if args.dry_run:
                is_valid = True
                reason = "dry run"
            else:
                response = None
                try:
                    response = client.models.generate_content(
                        model="gemini-3.1-pro-preview",
                        contents=[types.Part.from_bytes(
                            data=jpeg_bytes, mime_type="image/jpeg"
                        )],
                        config=types.GenerateContentConfig(
                            system_instruction=system_prompt,
                            temperature=0.0,
                            max_output_tokens=1024,
                            response_mime_type="application/json",
                            response_schema=validation_schema,
                        ),
                    )
                    result = json.loads(response.text)
                    is_valid = result["is_valid"]
                    reason = result["reason_is_valid"]
                except Exception as e:
                    print(f"    ERROR ({det.frame_id} det {det.det_index}): {e}")
                    is_valid = None
                    # Store raw API response so failures are inspectable in the output JSON
                    raw_text = getattr(response, 'text', None) if response is not None else None
                    reason = raw_text if raw_text is not None else str(e)
                    n_errors += 1

            # Write validation fields into the output data
            batch_results = output_data["batches"][det.batch_id]["results"]
            det_dict = batch_results[det.frame_id][det.det_index]
            det_dict["is_valid"] = is_valid
            det_dict["reason_is_valid"] = reason

            n_validated += 1
            if is_valid is True:
                n_valid += 1
            elif is_valid is False:
                n_invalid += 1

        # Save after each frame group
        save_output(args.output, output_data)
        print(f"  Frame {frame_num}/{total_frames} ({frame_id}): "
              f"{len(frame_dets)} detection(s)")

        if max_frames is not None and frame_num >= max_frames:
            print(f"  Reached --max-frames cap ({max_frames}), stopping.")
            break

    # Save random sample crops for inspection
    if processed_crops:
        n_samples = min(args.save_samples, len(processed_crops))
        samples = random.sample(processed_crops, n_samples)
        sample_dir = (Path(args.save_samples_dir) if args.save_samples_dir
                      else Path(args.output).parent / "sample_crops")
        sample_dir.mkdir(parents=True, exist_ok=True)
        for name, pil_img in samples:
            pil_img.save(sample_dir / f"{name}.jpg", format="JPEG", quality=85)
        print(f"\nSaved {n_samples} sample crop(s) to {sample_dir}")

    # Summary
    print(f"\n{'=' * 30}")
    print(f"=== Validation Summary ===")
    print(f"{'=' * 30}")
    print(f"Total detections:  {len(detections)}")
    print(f"Validated (new):   {n_validated}")
    print(f"Skipped (already): {n_skipped}")
    print(f"Valid:             {n_valid}")
    print(f"Invalid:           {n_invalid}")
    print(f"Errors:            {n_errors}")
    print(f"Output written to: {args.output}")


if __name__ == "__main__":
    main()
