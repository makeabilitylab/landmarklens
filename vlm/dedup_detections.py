"""
Post-processing deduplication for VLM detection pipeline.

Uses DINOv3 visual embeddings to find and remove duplicate detections
of the same real-world object across consecutive frames.

Usage:
    python vlm/dedup_detections.py \
        --input vlm/kinshicho_c/kinshicho_c_route.json \
        --detections vlm/output/kinshicho_c.json \
        --output vlm/output/kinshicho_c_deduped.json \
        --similarity-threshold 0.75 \
        --max-duplicates 2 \
        --window-size 5 \
        --batch-size 32 \
        --device mps
"""

import argparse
import copy
import dataclasses
import json
import warnings
from collections import defaultdict
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image

from geo_utils import load_orientation_lookup, rotate_panorama


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Detection:
    segment_id: str
    batch_id: str
    frame_id: str
    frame_order: int
    direction: str
    pano_path: str
    bbox: list[int]        # [y_min, x_min, y_max, x_max] 0-1000
    det_index: int         # index within frame's detection list
    description: str
    category: int
    category_label: str
    color_prominence: float
    shape_prominence: float
    semantic_familiarity: float
    global_index: int      # for union-find
    turn_angle: float = 0.0


# ---------------------------------------------------------------------------
# Disjoint Set Union (union-find)
# ---------------------------------------------------------------------------

class DSU:
    """Disjoint Set Union with path compression and union by rank."""

    def __init__(self, n: int):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> bool:
        a, b = self.find(a), self.find(b)
        if a == b:
            return False
        if self.rank[a] < self.rank[b]:
            a, b = b, a
        self.parent[b] = a
        if self.rank[a] == self.rank[b]:
            self.rank[a] += 1
        return True

    def groups(self) -> dict[int, list[int]]:
        result: dict[int, list[int]] = defaultdict(list)
        for i in range(len(self.parent)):
            result[self.find(i)].append(i)
        return dict(result)


# ---------------------------------------------------------------------------
# DINOv3 Embedding Model
# ---------------------------------------------------------------------------

class EmbeddingModel:
    """Wraps DINOv3 for CLS-token embedding extraction.

    Adapted from UrbanArt deduplication/embedding_model.py.
    """

    MODEL_NAME = "facebook/dinov3-vitb16-pretrain-lvd1689m"

    def __init__(self, device: str = "cpu"):
        from transformers import AutoImageProcessor, AutoModel

        self.device = torch.device(device)
        self.processor = AutoImageProcessor.from_pretrained(self.MODEL_NAME)
        self.model = AutoModel.from_pretrained(self.MODEL_NAME).to(self.device)
        self.model.eval()

    @torch.inference_mode()
    def embed(self, images: list[Image.Image], batch_size: int = 32) -> np.ndarray:
        """Return (N, D) L2-normalized embeddings for a list of PIL images."""
        all_embeddings = []
        for i in range(0, len(images), batch_size):
            batch = images[i : i + batch_size]
            inputs = self.processor(images=batch, return_tensors="pt")
            inputs = {k: v.to(self.device) for k, v in inputs.items()}
            outputs = self.model(**inputs)
            # CLS token: prefer pooler_output, fall back to first token
            if hasattr(outputs, "pooler_output") and outputs.pooler_output is not None:
                embeddings = outputs.pooler_output
            else:
                embeddings = outputs.last_hidden_state[:, 0]
            embeddings = F.normalize(embeddings, p=2, dim=-1)
            all_embeddings.append(embeddings.cpu().numpy())
        return np.concatenate(all_embeddings, axis=0)


# ---------------------------------------------------------------------------
# Cropping
# ---------------------------------------------------------------------------

def crop_detection_buffered(
    image: np.ndarray,
    bbox: list[int],
    buffer: float = 1.5,
) -> Image.Image:
    """Crop a detection from a panorama with buffer, handling 360° wrapping.

    Args:
        image: (H, W, 3) numpy array of the rotated panorama.
        bbox: [y_min, x_min, y_max, x_max] normalized 0-1000.
        buffer: multiplier to expand crop in each direction.

    Returns:
        Cropped PIL Image.
    """
    H, W = image.shape[:2]

    # Convert from 0-1000 to pixel coordinates
    y_min = bbox[0] / 1000.0 * H
    x_min = bbox[1] / 1000.0 * W
    y_max = bbox[2] / 1000.0 * H
    x_max = bbox[3] / 1000.0 * W

    # Compute center and dimensions
    cy = (y_min + y_max) / 2.0
    cx = (x_min + x_max) / 2.0
    h = y_max - y_min
    w = x_max - x_min

    # Expand by buffer
    new_h = h * buffer
    new_w = w * buffer

    # Compute expanded bounds
    y0 = int(max(0, cy - new_h / 2))
    y1 = int(min(H, cy + new_h / 2))

    x0 = cx - new_w / 2
    x1 = cx + new_w / 2

    # Handle 360° wrapping for x
    x0_int = int(round(x0))
    x1_int = int(round(x1))

    if x0_int < 0 or x1_int > W:
        # Wrapping case: take two slices and concatenate
        x0_wrapped = x0_int % W
        x1_wrapped = x1_int % W
        if x0_int < 0:
            # Left side wraps around
            left_slice = image[y0:y1, x0_wrapped:, :]
            right_slice = image[y0:y1, :x1_wrapped, :]
        else:
            # Right side wraps around
            left_slice = image[y0:y1, x0_int:, :]
            right_slice = image[y0:y1, :x1_wrapped, :]
        crop = np.concatenate([left_slice, right_slice], axis=1)
    else:
        crop = image[y0:y1, x0_int:x1_int, :]

    if crop.size == 0:
        # Fallback: use bbox directly without buffer
        y0 = int(max(0, y_min))
        y1 = int(min(H, y_max))
        x0 = int(max(0, x_min))
        x1 = int(min(W, x_max))
        crop = image[y0:y1, x0:x1, :]

    return Image.fromarray(crop.astype(np.uint8))


# ---------------------------------------------------------------------------
# Phase 1: Load & Flatten
# ---------------------------------------------------------------------------

def load_and_flatten(route_path: str, detections_path: str) -> list[Detection]:
    """Load route + detections JSONs and return a flat list of Detections."""
    with open(route_path) as f:
        route_data = json.load(f)

    with open(detections_path) as f:
        det_data = json.load(f)

    # Build lookup: {frame_id: frame_dict}
    # Pre-sort each segment so we can check previous frame's is_turn_warning
    frame_lookup: dict[str, dict] = {}
    for seg_id, frames in route_data.items():
        sorted_frames = sorted(frames, key=lambda f: f["frame_order"])
        for i, frame in enumerate(sorted_frames):
            prev_warned = (i > 0 and sorted_frames[i - 1].get("is_turn_warning", False))
            frame_lookup[frame["frame_id"]] = {
                **frame, "segment_id": seg_id, "_apply_turn_angle": prev_warned,
            }

    detections: list[Detection] = []
    global_index = 0

    for batch_id, batch in det_data.get("batches", {}).items():
        if batch.get("status") != "completed":
            continue
        seg_id = batch["segment_id"]
        results = batch.get("results", {})

        for frame_id, frame_dets in results.items():
            if frame_id not in frame_lookup:
                warnings.warn(
                    f"frame_id '{frame_id}' in detections not found in route JSON — skipping"
                )
                continue
            frame_info = frame_lookup[frame_id]

            for det_idx, det in enumerate(frame_dets):
                detections.append(Detection(
                    segment_id=seg_id,
                    batch_id=batch_id,
                    frame_id=frame_id,
                    frame_order=frame_info["frame_order"],
                    direction=frame_info["direction"],
                    pano_path=frame_info["pano_path"],
                    bbox=det["bbox"],
                    det_index=det_idx,
                    description=det["description"],
                    category=det["category"],
                    category_label=det["category_label"],
                    color_prominence=det["color_prominence"],
                    shape_prominence=det["shape_prominence"],
                    semantic_familiarity=det["semantic_familiarity"],
                    global_index=global_index,
                    turn_angle=(frame_info.get("turn_angle", 0.0) or 0.0) if frame_info.get("_apply_turn_angle") else 0.0,
                ))
                global_index += 1

    # Sort by segment then frame_order
    detections.sort(key=lambda d: (d.segment_id, d.frame_order))
    # Re-assign global indices after sorting
    for i, det in enumerate(detections):
        det.global_index = i

    return detections


# ---------------------------------------------------------------------------
# Phase 2: Crop Detections
# ---------------------------------------------------------------------------

def crop_all_detections(
    detections: list[Detection],
    orientation_lookup: dict,
    buffer: float = 1.5,
) -> list[Image.Image]:
    """Crop detection regions from panoramas, returning PIL images ordered by global_index."""
    crops: list[Image.Image | None] = [None] * len(detections)

    # Group by frame_id to avoid loading the same panorama multiple times
    by_frame: dict[str, list[Detection]] = defaultdict(list)
    for det in detections:
        by_frame[det.frame_id].append(det)

    for frame_id, frame_dets in by_frame.items():
        det0 = frame_dets[0]
        img = np.array(Image.open(det0.pano_path).convert("RGB"))
        is_reverse = det0.direction == "reverse"
        img = rotate_panorama(img, frame_id, orientation_lookup, is_reverse)
        if det0.turn_angle:
            shift_x = -round(det0.turn_angle / 360.0 * img.shape[1])
            img = np.roll(img, shift_x, axis=1)

        for det in frame_dets:
            crop = crop_detection_buffered(img, det.bbox, buffer=buffer)
            crops[det.global_index] = crop

    return crops


# ---------------------------------------------------------------------------
# Phase 4: Compare Within Sliding Window
# ---------------------------------------------------------------------------

def find_matches(
    detections: list[Detection],
    embeddings: np.ndarray,
    window_size: int,
    similarity_threshold: float,
) -> list[tuple[int, int]]:
    """Find matching detection pairs within a sliding window per segment."""
    matches: list[tuple[int, int]] = []
    window_half = window_size // 2

    # Group detections by segment
    by_segment: dict[str, list[Detection]] = defaultdict(list)
    for det in detections:
        by_segment[det.segment_id].append(det)

    for seg_id, seg_dets in by_segment.items():
        # Build frame_order -> list of detections
        by_order: dict[int, list[Detection]] = defaultdict(list)
        for det in seg_dets:
            by_order[det.frame_order].append(det)

        sorted_orders = sorted(by_order.keys())

        for p_idx, p_order in enumerate(sorted_orders):
            dets_p = by_order[p_order]
            # Window: positions [p - window_half, p + window_half]
            for q_idx in range(p_idx + 1, len(sorted_orders)):
                q_order = sorted_orders[q_idx]
                if q_order > p_order + window_half:
                    break
                dets_q = by_order[q_order]

                for dp in dets_p:
                    for dq in dets_q:
                        # Cosine similarity = dot product (embeddings are L2-normalized)
                        sim = float(np.dot(
                            embeddings[dp.global_index],
                            embeddings[dq.global_index],
                        ))
                        if sim >= similarity_threshold:
                            matches.append((dp.global_index, dq.global_index))

    return matches


# ---------------------------------------------------------------------------
# Phase 5: Union-Find & Select
# ---------------------------------------------------------------------------

def select_detections_to_remove(
    detections: list[Detection],
    matches: list[tuple[int, int]],
    max_duplicates: int,
) -> set[int]:
    """Union matched pairs and mark excess detections for removal."""
    n = len(detections)
    dsu = DSU(n)

    for a, b in matches:
        dsu.union(a, b)

    removed: set[int] = set()
    groups = dsu.groups()

    for root, members in groups.items():
        if len(members) <= max_duplicates:
            continue
        # Sort by frame_order (earliest first)
        members.sort(key=lambda idx: detections[idx].frame_order)
        # Remove everything after max_duplicates
        for idx in members[max_duplicates:]:
            removed.add(idx)

    return removed


# ---------------------------------------------------------------------------
# Phase 6: Reconstruct Output
# ---------------------------------------------------------------------------

def reconstruct_output(
    detections_path: str,
    detections: list[Detection],
    removed: set[int],
) -> dict:
    """Deep-copy original detections JSON and filter out removed detections."""
    with open(detections_path) as f:
        output = json.load(f)

    output = copy.deepcopy(output)

    # Build removal lookup: {(batch_id, frame_id): set of det_index}
    removal_lookup: dict[tuple[str, str], set[int]] = defaultdict(set)
    for det in detections:
        if det.global_index in removed:
            removal_lookup[(det.batch_id, det.frame_id)].add(det.det_index)

    for batch_id, batch in output.get("batches", {}).items():
        if batch.get("status") != "completed":
            continue
        results = batch.get("results", {})
        for frame_id in list(results.keys()):
            key = (batch_id, frame_id)
            if key in removal_lookup:
                indices_to_remove = removal_lookup[key]
                results[frame_id] = [
                    det for i, det in enumerate(results[frame_id])
                    if i not in indices_to_remove
                ]

    return output


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def auto_device() -> str:
    """Auto-detect best available device."""
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def main():
    parser = argparse.ArgumentParser(
        description="Deduplicate VLM detections using DINOv3 visual embeddings"
    )
    parser.add_argument("--input", required=True, help="Route JSON (has pano_path, frame_order, direction)")
    parser.add_argument("--detections", required=True, help="Detections JSON from run_gemini.py")
    parser.add_argument("--output", required=True, help="Output path for deduplicated JSON")
    parser.add_argument("--similarity-threshold", type=float, default=0.75,
                        help="Cosine similarity threshold for matching (default: 0.75)")
    parser.add_argument("--max-duplicates", type=int, default=2,
                        help="Max detections to keep per matched set (default: 2)")
    parser.add_argument("--window-size", type=int, default=5,
                        help="Frame window for comparison (default: 5)")
    parser.add_argument("--batch-size", type=int, default=32,
                        help="Batch size for DINOv3 inference (default: 32)")
    parser.add_argument("--device", default=None, help="Device: mps/cuda/cpu (auto-detects)")
    parser.add_argument("--buffer", type=float, default=1.5,
                        help="Crop buffer multiplier (default: 1.5)")
    args = parser.parse_args()

    device = args.device or auto_device()
    print(f"Using device: {device}")

    # Phase 1: Load & Flatten
    print("\n[Phase 1] Loading and flattening detections...")
    detections = load_and_flatten(args.input, args.detections)
    print(f"  Total detections: {len(detections)}")

    if len(detections) == 0:
        print("No detections found. Nothing to deduplicate.")
        return

    # Phase 2: Crop Detections
    print("\n[Phase 2] Cropping detections from panoramas...")
    orientation_lookup = load_orientation_lookup()
    crops = crop_all_detections(detections, orientation_lookup, buffer=args.buffer)
    print(f"  Cropped {len(crops)} regions")

    # Phase 3: Generate DINOv3 Embeddings
    print("\n[Phase 3] Computing DINOv3 embeddings...")
    model = EmbeddingModel(device=device)
    embeddings = model.embed(crops, batch_size=args.batch_size)
    print(f"  Embedding shape: {embeddings.shape}")

    # Phase 4: Compare Within Sliding Window
    print("\n[Phase 4] Finding matches within sliding window...")
    matches = find_matches(detections, embeddings, args.window_size, args.similarity_threshold)
    print(f"  Match pairs found: {len(matches)}")

    # Phase 5: Union-Find & Select
    print("\n[Phase 5] Selecting detections to remove...")
    removed = select_detections_to_remove(detections, matches, args.max_duplicates)
    print(f"  Detections marked for removal: {len(removed)}")

    # Phase 6: Reconstruct Output
    print("\n[Phase 6] Reconstructing output...")
    output = reconstruct_output(args.detections, detections, removed)

    # Write output
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)

    # Count matched sets
    if matches:
        dsu = DSU(len(detections))
        for a, b in matches:
            dsu.union(a, b)
        matched_sets = sum(1 for members in dsu.groups().values() if len(members) > 1)
    else:
        matched_sets = 0

    # Summary
    after = len(detections) - len(removed)
    print(f"\n{'=' * 30}")
    print(f"=== Deduplication Summary ===")
    print(f"{'=' * 30}")
    print(f"Total detections (before): {len(detections)}")
    print(f"Total detections (after):  {after}")
    print(f"Matched sets found:        {matched_sets}")
    print(f"Detections removed:        {len(removed)}")
    print(f"Output written to: {args.output}")


if __name__ == "__main__":
    main()
