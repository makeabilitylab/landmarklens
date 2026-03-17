"""
Post-processing step that shrinks oversized bounding boxes to a maximum size,
repositioning them to the most visually striking sub-region.

Runs after validate_detections.py. Shrinks vertically toward the horizon
(y=500 in normalized space) and horizontally toward the region with the
highest combined color variance + edge density.

Usage:
python vlm/shrink_bboxes.py \
    --input vlm/ningyocho_r1/ningyocho_r1_route.json \
    --detections vlm/output/ningyocho_r1_route_secondpass.json \
    --output vlm/output/ningyocho_r1_route_shrunk.json \
    --max-height 300 --max-width 200
"""

import argparse
import copy
import json
import os
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from dedup_detections import Detection, load_and_flatten
from geo_utils import load_orientation_lookup, rotate_panorama


# ---------------------------------------------------------------------------
# Image region extraction
# ---------------------------------------------------------------------------

def extract_region(image: np.ndarray, bbox: list[int]) -> np.ndarray:
    """Extract a sub-image for a bbox, handling 360° panorama wrapping.

    Args:
        image: (H, W, 3) numpy array of the rotated panorama.
        bbox: [y_min, x_min, y_max, x_max] normalized 0-1000.

    Returns:
        (h, w, 3) numpy array of the extracted region.
    """
    H, W = image.shape[:2]

    y0 = int(max(0, bbox[0] / 1000.0 * H))
    y1 = int(min(H, bbox[2] / 1000.0 * H))
    x0_raw = bbox[1] / 1000.0 * W
    x1_raw = bbox[3] / 1000.0 * W

    x0 = int(round(x0_raw))
    x1 = int(round(x1_raw))

    if x0 < 0 or x1 > W:
        # 360° wrapping
        x0_w = x0 % W
        x1_w = x1 % W
        if x0 < 0:
            left = image[y0:y1, x0_w:, :]
            right = image[y0:y1, :x1_w, :]
        else:
            left = image[y0:y1, x0:, :]
            right = image[y0:y1, :x1_w, :]
        region = np.concatenate([left, right], axis=1)
    else:
        region = image[y0:y1, x0:x1, :]

    return region


# ---------------------------------------------------------------------------
# Scoring functions
# ---------------------------------------------------------------------------

def compute_color_variance(region: np.ndarray) -> float:
    """Standard deviation of all RGB pixel values in the region."""
    if region.size == 0:
        return 0.0
    return float(np.std(region.astype(np.float32)))


def compute_color_contrast(region: np.ndarray, context: np.ndarray) -> float:
    """Euclidean distance between the mean RGB of the region and its context.

    Higher values mean the region's colors stand out more from surroundings.
    """
    if region.size == 0 or context.size == 0:
        return 0.0
    region_mean = region.astype(np.float32).reshape(-1, 3).mean(axis=0)
    context_mean = context.astype(np.float32).reshape(-1, 3).mean(axis=0)
    return float(np.linalg.norm(region_mean - context_mean))


def compute_edge_density(region: np.ndarray) -> float:
    """Sum of gradient magnitudes on the grayscale version of the region."""
    if region.size == 0:
        return 0.0
    gray = (0.299 * region[:, :, 0]
            + 0.587 * region[:, :, 1]
            + 0.114 * region[:, :, 2]).astype(np.float32)
    gy, gx = np.gradient(gray)
    magnitude = np.sqrt(gx ** 2 + gy ** 2)
    return float(np.sum(magnitude))


# ---------------------------------------------------------------------------
# Bbox shrinking
# ---------------------------------------------------------------------------

def shrink_bbox_vertical(bbox: list[int], max_height: int) -> list[int]:
    """Shrink bbox height toward the horizon (y=500), clamped to original bounds.

    Returns a new bbox with the same x coordinates but adjusted y coordinates.
    """
    y_min, x_min, y_max, x_max = bbox
    current_height = y_max - y_min

    if current_height <= max_height:
        return list(bbox)

    center_y = (y_min + y_max) / 2.0
    shrink_fraction = 1.0 - max_height / current_height
    new_center_y = center_y + (500 - center_y) * shrink_fraction

    new_y_min = new_center_y - max_height / 2.0
    new_y_max = new_center_y + max_height / 2.0

    # Clamp within original bbox bounds
    if new_y_min < y_min:
        new_y_min = y_min
        new_y_max = y_min + max_height
    if new_y_max > y_max:
        new_y_max = y_max
        new_y_min = y_max - max_height

    # Clamp to 0-1000
    new_y_min = max(0, min(1000, new_y_min))
    new_y_max = max(0, min(1000, new_y_max))

    return [int(round(new_y_min)), x_min, int(round(new_y_max)), x_max]


def shrink_bbox_horizontal(
    bbox: list[int],
    max_width: int,
    image: np.ndarray,
    color_weight: float,
    edge_weight: float,
    stride: int,
) -> tuple[list[int], dict | None]:
    """Shrink bbox width by sliding to the most visually striking position.

    Returns (new_bbox, score_info). score_info is None if no shrink was needed,
    otherwise a dict with candidates, color_norm, edge_norm, combined, best_idx.
    """
    y_min, x_min, y_max, x_max = bbox
    current_width = x_max - x_min

    if current_width <= max_width:
        return list(bbox), None

    # Generate candidate x positions
    candidates = list(range(x_min, x_max - max_width + 1, stride))
    # Always include the last valid position
    last_pos = x_max - max_width
    if not candidates or candidates[-1] != last_pos:
        candidates.append(last_pos)

    if len(candidates) == 0:
        return list(bbox), None

    # Extract the full original bbox as context for color contrast
    full_region = extract_region(image, bbox)

    # Score each candidate
    color_scores = []
    edge_scores = []
    for cx_min in candidates:
        candidate_bbox = [y_min, cx_min, y_max, cx_min + max_width]
        region = extract_region(image, candidate_bbox)
        color_scores.append(compute_color_contrast(region, full_region))
        edge_scores.append(compute_edge_density(region))

    color_scores = np.array(color_scores, dtype=np.float64)
    edge_scores = np.array(edge_scores, dtype=np.float64)

    # Normalize to 0-1
    def normalize(arr):
        rng = arr.max() - arr.min()
        if rng < 1e-8:
            return np.zeros_like(arr)
        return (arr - arr.min()) / rng

    color_norm = normalize(color_scores)
    edge_norm = normalize(edge_scores)

    combined = color_weight * color_norm + edge_weight * edge_norm
    best_idx = int(np.argmax(combined))
    best_x_min = candidates[best_idx]

    score_info = {
        "candidates": candidates,
        "color_norm": color_norm,
        "edge_norm": edge_norm,
        "combined": combined,
        "best_idx": best_idx,
    }

    return [y_min, best_x_min, y_max, best_x_min + max_width], score_info


def shrink_single_bbox(
    bbox: list[int],
    image: np.ndarray,
    max_height: int,
    max_width: int,
    color_weight: float,
    edge_weight: float,
    stride: int,
) -> tuple[list[int], bool, dict | None]:
    """Shrink a single bbox if it exceeds max dimensions.

    Returns (new_bbox, was_shrunk, score_info).
    score_info is the horizontal scoring data (None if width was not shrunk).
    """
    height = bbox[2] - bbox[0]
    width = bbox[3] - bbox[1]
    needs_shrink = height > max_height or width > max_width

    if not needs_shrink:
        return list(bbox), False, None

    new_bbox = list(bbox)
    score_info = None
    if height > max_height:
        new_bbox = shrink_bbox_vertical(new_bbox, max_height)
    if width > max_width:
        new_bbox, score_info = shrink_bbox_horizontal(
            new_bbox, max_width, image, color_weight, edge_weight, stride
        )

    return new_bbox, True, score_info


# ---------------------------------------------------------------------------
# Debug image generation
# ---------------------------------------------------------------------------

def save_debug_image(
    image: np.ndarray,
    original_bbox: list[int],
    shrunk_bbox: list[int],
    score_info: dict | None,
    max_width: int,
    output_path: Path,
):
    """Save a debug image showing the original bbox, shrunk bbox, center baseline, and score heatmap.

    The image shows the original bbox region (with padding) annotated with:
    - White dashed outline: original bbox
    - Red outline: center crop (naive baseline)
    - Green outline: chosen shrunk bbox
    - Score heatmap bar along the bottom showing combined score at each candidate x position
    """
    H, W = image.shape[:2]
    oy_min, ox_min, oy_max, ox_max = original_bbox

    # Add padding around original bbox for context (20% of original size)
    pad_y = int((oy_max - oy_min) * 0.2)
    pad_x = int((ox_max - ox_min) * 0.2)
    view_y_min = max(0, int(oy_min / 1000.0 * H) - pad_y)
    view_y_max = min(H, int(oy_max / 1000.0 * H) + pad_y)
    view_x_min = max(0, int(ox_min / 1000.0 * W) - pad_x)
    view_x_max = min(W, int(ox_max / 1000.0 * W) + pad_x)

    crop = image[view_y_min:view_y_max, view_x_min:view_x_max, :].copy()
    heatmap_height = 40

    # Build canvas: crop image + heatmap bar below
    canvas_h = crop.shape[0] + heatmap_height
    canvas_w = crop.shape[1]
    canvas = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
    canvas[:crop.shape[0], :, :] = crop

    # Draw heatmap bar if we have scoring data
    if score_info is not None:
        candidates = score_info["candidates"]
        combined = score_info["combined"]
        # Map candidate x positions to pixel positions within the crop
        for i, (cx_min, score) in enumerate(zip(candidates, combined)):
            px_start = int(cx_min / 1000.0 * W) - view_x_min
            px_end = int((cx_min + max_width) / 1000.0 * W) - view_x_min
            # Center of this candidate in the heatmap
            bar_x = int((px_start + px_end) / 2)
            bar_x = max(0, min(canvas_w - 1, bar_x))
            # Color: red (low) -> yellow (mid) -> green (high)
            r = int(255 * (1.0 - score))
            g = int(255 * score)
            b = 0
            # Draw a vertical bar in the heatmap region
            half_w = max(1, (px_end - px_start) // 2)
            x0 = max(0, bar_x - half_w)
            x1 = min(canvas_w, bar_x + half_w)
            canvas[crop.shape[0]:, x0:x1, :] = [r, g, b]

    pil_img = Image.fromarray(canvas)
    draw = ImageDraw.Draw(pil_img)

    def bbox_to_crop_coords(bbox):
        """Convert 0-1000 bbox to pixel coords relative to the crop view."""
        y0 = int(bbox[0] / 1000.0 * H) - view_y_min
        x0 = int(bbox[1] / 1000.0 * W) - view_x_min
        y1 = int(bbox[2] / 1000.0 * H) - view_y_min
        x1 = int(bbox[3] / 1000.0 * W) - view_x_min
        return x0, y0, x1, y1

    # Original bbox — white dashed (draw as dotted by drawing segments)
    ox0, oy0, ox1, oy1 = bbox_to_crop_coords(original_bbox)
    for dash_start in range(0, max(ox1 - ox0, oy1 - oy0), 8):
        # Top edge
        draw.line([(ox0 + dash_start, oy0), (min(ox0 + dash_start + 4, ox1), oy0)],
                  fill=(255, 255, 255), width=2)
        # Bottom edge
        draw.line([(ox0 + dash_start, oy1), (min(ox0 + dash_start + 4, ox1), oy1)],
                  fill=(255, 255, 255), width=2)
        # Left edge
        draw.line([(ox0, oy0 + dash_start), (ox0, min(oy0 + dash_start + 4, oy1))],
                  fill=(255, 255, 255), width=2)
        # Right edge
        draw.line([(ox1, oy0 + dash_start), (ox1, min(oy0 + dash_start + 4, oy1))],
                  fill=(255, 255, 255), width=2)

    # Center crop baseline — red
    sy_min, _, sy_max, _ = shrunk_bbox
    center_x = (ox_min + ox_max) / 2.0
    center_bbox = [sy_min, int(center_x - max_width / 2), sy_max, int(center_x + max_width / 2)]
    cx0, cy0, cx1, cy1 = bbox_to_crop_coords(center_bbox)
    draw.rectangle([cx0, cy0, cx1, cy1], outline=(255, 0, 0), width=2)

    # Chosen shrunk bbox — green
    sx0, sy0, sx1, sy1 = bbox_to_crop_coords(shrunk_bbox)
    draw.rectangle([sx0, sy0, sx1, sy1], outline=(0, 255, 0), width=3)

    # Legend
    legend_y = 4
    draw.rectangle([4, legend_y, 16, legend_y + 10], outline=(255, 255, 255), width=1)
    draw.text((20, legend_y - 2), "original", fill=(255, 255, 255))
    draw.rectangle([4, legend_y + 16, 16, legend_y + 26], outline=(255, 0, 0), width=1)
    draw.text((20, legend_y + 14), "center (baseline)", fill=(255, 0, 0))
    draw.rectangle([4, legend_y + 32, 16, legend_y + 42], outline=(0, 255, 0), width=1)
    draw.text((20, legend_y + 30), "chosen", fill=(0, 255, 0))

    pil_img.save(output_path, format="JPEG", quality=90)


# ---------------------------------------------------------------------------
# Resumability helpers
# ---------------------------------------------------------------------------

def load_existing_output(output_path: str, detections_path: str) -> dict:
    """Load existing output, or deep-copy the detections JSON as starting point."""
    if os.path.exists(output_path):
        with open(output_path, "r") as f:
            return json.load(f)
    with open(detections_path, "r") as f:
        return copy.deepcopy(json.load(f))


def build_processed_set(output_data: dict) -> set[tuple[str, str, int]]:
    """Return set of (batch_id, frame_id, det_index) already processed."""
    processed = set()
    for batch_id, batch in output_data.get("batches", {}).items():
        if batch.get("status") != "completed":
            continue
        for frame_id, dets in batch.get("results", {}).items():
            for det_idx, det in enumerate(dets):
                if "bbox_shrunk" in det:
                    processed.add((batch_id, frame_id, det_idx))
    return processed


def save_output(output_path: str, output_data: dict):
    """Write output data to disk."""
    with open(output_path, "w") as f:
        json.dump(output_data, f, indent=2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Shrink oversized bounding boxes to a maximum size, "
                    "repositioning to the most visually striking sub-region"
    )
    parser.add_argument("--input", required=True,
                        help="Route JSON (has pano_path, frame_order, direction)")
    parser.add_argument("--detections", required=True,
                        help="Detections JSON (typically validated output)")
    parser.add_argument("--output", required=True,
                        help="Output path for shrunk JSON")
    parser.add_argument("--max-height", type=int, default=300,
                        help="Max bbox height in 0-1000 space (default: 300)")
    parser.add_argument("--max-width", type=int, default=200,
                        help="Max bbox width in 0-1000 space (default: 200)")
    parser.add_argument("--color-weight", type=float, default=0.7,
                        help="Weight for color contrast score (default: 0.7)")
    parser.add_argument("--edge-weight", type=float, default=0.3,
                        help="Weight for edge density score (default: 0.3)")
    parser.add_argument("--stride", type=int, default=3,
                        help="Sliding window step in 0-1000 space (default: 5)")
    parser.add_argument("--save-debug", default=None,
                        help="Directory to save debug images for shrunk detections. "
                             "Each image shows the original bbox (white dashed), "
                             "center baseline (red), chosen bbox (green), and "
                             "a score heatmap bar.")
    args = parser.parse_args()

    # Load & flatten detections
    print("[1/3] Loading and flattening detections...")
    detections = load_and_flatten(args.input, args.detections)
    print(f"  Total detections: {len(detections)}")

    if len(detections) == 0:
        print("No detections found. Nothing to process.")
        return

    # Load orientation lookup for panorama rotation
    orientation_lookup = load_orientation_lookup()

    # Load or initialize output
    output_data = load_existing_output(args.output, args.detections)
    already_processed = build_processed_set(output_data)
    print(f"  Already processed: {len(already_processed)}")

    # Ensure output directory exists
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)

    # Group detections by frame_id
    by_frame: dict[str, list[Detection]] = defaultdict(list)
    for det in detections:
        by_frame[det.frame_id].append(det)

    frame_ids_sorted = sorted(by_frame.keys(),
                              key=lambda fid: by_frame[fid][0].frame_order)
    total_frames = len(frame_ids_sorted)

    # Set up debug output directory
    debug_dir = None
    if args.save_debug:
        debug_dir = Path(args.save_debug)
        debug_dir.mkdir(parents=True, exist_ok=True)
        print(f"  Debug images will be saved to: {debug_dir}")

    print(f"\n[2/3] Processing bounding boxes...")
    n_shrunk = 0
    n_unchanged = 0
    n_skipped = 0

    for frame_num, frame_id in enumerate(frame_ids_sorted, 1):
        frame_dets = by_frame[frame_id]
        det0 = frame_dets[0]

        # Check if all detections in this frame are already processed
        all_done = all(
            (det.batch_id, det.frame_id, det.det_index) in already_processed
            for det in frame_dets
        )
        if all_done:
            n_skipped += len(frame_dets)
            continue

        # Load and rotate panorama (once per frame)
        img = np.array(Image.open(det0.pano_path).convert("RGB"))
        is_reverse = det0.direction == "reverse"
        img = rotate_panorama(img, frame_id, orientation_lookup, is_reverse)
        if det0.turn_angle:
            shift_x = -round(det0.turn_angle / 360.0 * img.shape[1])
            img = np.roll(img, shift_x, axis=1)

        for det in frame_dets:
            key = (det.batch_id, det.frame_id, det.det_index)
            if key in already_processed:
                n_skipped += 1
                continue

            new_bbox, was_shrunk, score_info = shrink_single_bbox(
                det.bbox, img,
                args.max_height, args.max_width,
                args.color_weight, args.edge_weight, args.stride,
            )

            # Update output data in place
            det_dict = output_data["batches"][det.batch_id]["results"][det.frame_id][det.det_index]
            det_dict["bbox_original"] = det_dict["bbox"]
            det_dict["bbox"] = new_bbox
            det_dict["bbox_shrunk"] = was_shrunk

            if was_shrunk:
                n_shrunk += 1
                if debug_dir is not None:
                    debug_path = debug_dir / f"{det.frame_id}_det{det.det_index}.jpg"
                    save_debug_image(
                        img, det.bbox, new_bbox, score_info,
                        args.max_width, debug_path,
                    )
            else:
                n_unchanged += 1

        # Save after each frame (resumability)
        save_output(args.output, output_data)
        print(f"  Frame {frame_num}/{total_frames} ({frame_id}): "
              f"{len(frame_dets)} detection(s)")

    # Summary
    print(f"\n{'=' * 30}")
    print(f"=== Bbox Shrink Summary ===")
    print(f"{'=' * 30}")
    print(f"Total detections:  {n_shrunk + n_unchanged + n_skipped}")
    print(f"Shrunk:            {n_shrunk}")
    print(f"Unchanged:         {n_unchanged}")
    print(f"Skipped (resumed): {n_skipped}")
    if debug_dir is not None:
        print(f"Debug images:      {n_shrunk} saved to {debug_dir}")
    print(f"Output written to: {args.output}")


if __name__ == "__main__":
    main()
