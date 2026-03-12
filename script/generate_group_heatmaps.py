#!/usr/bin/env python3
"""
Generate group-aggregated gaze heatmap composites (good vs poor navigators).

Reads the combined clean CSV and generates side-by-side heatmaps for
specified frames, aggregating all participants within each group.

Usage:
    python script/generate_group_heatmaps.py

    # Dry run (preview stats only)
    python script/generate_group_heatmaps.py --dry-run

    # Specific frames only
    python script/generate_group_heatmaps.py --frames GS010396_0072 GS010338_0031
"""

import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import cv2
import numpy as np

BASE_DIR = Path(__file__).resolve().parent.parent
FRAMES_DIR = BASE_DIR / "web" / "frames"
DATA_JSON = BASE_DIR / "web" / "data.json"
CLEAN_CSV = BASE_DIR / "study-data" / "eye-gaze-data" / "clean" / "clean_all.csv"
OUTPUT_DIR = BASE_DIR / "study-data" / "heatmap-frames" / "overall-comparison"

MIN_GAZE_POINTS = 3
HEATMAP_SCALE = 0.25
SPHERE_UV_OFFSET = 0.25
GLOBAL_OPACITY = 0.70

# Frames where GOOD spread >> POOR spread
SPREAD_FRAMES = [
    "GS010396_0072",
    "GS010338_0031",
    "GS010397_0064",
    "GS010317_0028",
    "GS010383_0015",
    "GS010392_0007",
    "GS010338_0025",
    "GS010360_0094",
    "GS010324_0044",
    "GS010396_0074",
]

# Frames where groups LOOK AT DIFFERENT THINGS
CENTER_FRAMES = [
    "GS010319_0053",
    "GS010338_0029",
    "GS010396_0072",
    "GS010397_0033",
    "GS010319_0039",
    "GS010360_0074",
    "GS010324_0044",
    "GS010404_0042",
    "GS010392_0000",
    "GS010323_0085",
]

ALL_TARGET_FRAMES = sorted(set(SPREAD_FRAMES + CENTER_FRAMES))


# ── Reused from generate_heatmaps.py ─────────────────────────────────

def vr_path_to_web_path(photo_path: str) -> str:
    parts = photo_path.strip().split("/")
    if len(parts) != 2:
        return photo_path
    frame_name = parts[1]
    source = frame_name.rsplit("_", 1)[0]
    return f"{source}/{frame_name}"


def load_yaw_lookup() -> dict[str, float]:
    if not DATA_JSON.exists():
        print(f"  Warning: {DATA_JSON} not found – skipping yaw correction")
        return {}
    with open(DATA_JSON) as f:
        data = json.load(f)
    lookup: dict[str, float] = {}
    for frame in data.get("frames", []):
        filename = frame.get("filename", "")
        stem = Path(filename).stem
        lookup[stem] = frame.get("yaw", 0.0)
    return lookup


HEATMAP_BREAKPOINTS = [
    (0.00, (120, 50, 20), 0.55),
    (0.01, (120, 50, 20), 0.55),
    (0.15, (170, 60, 10), 0.70),
    (0.30, (200, 180, 10), 0.80),
    (0.50, (60, 210, 20), 0.85),
    (0.70, (30, 230, 250), 0.85),
    (0.85, (15, 100, 250), 0.90),
    (1.00, (15, 20, 255), 0.90),
]


def _build_luts() -> tuple[np.ndarray, np.ndarray]:
    color_lut = np.zeros((256, 3), dtype=np.float32)
    alpha_lut = np.zeros(256, dtype=np.float32)
    bp = HEATMAP_BREAKPOINTS
    for i in range(256):
        t = i / 255.0
        for j in range(len(bp) - 1):
            t0, c0, a0 = bp[j]
            t1, c1, a1 = bp[j + 1]
            if t <= t1 or j == len(bp) - 2:
                frac = 0.0 if t1 == t0 else min((t - t0) / (t1 - t0), 1.0)
                color_lut[i] = [c0[k] + frac * (c1[k] - c0[k]) for k in range(3)]
                alpha_lut[i] = a0 + frac * (a1 - a0)
                break
    return color_lut, alpha_lut


_COLOR_LUT, _ALPHA_LUT = _build_luts()


def generate_heatmap(
    uv_points: list[tuple[float, float]],
    width: int,
    height: int,
    sigma: float,
) -> np.ndarray:
    us = np.array([p[0] for p in uv_points])
    vs = np.array([p[1] for p in uv_points])

    xs = (us * width).astype(np.int32) % width
    ys = np.clip((vs * height).astype(np.int32), 0, height - 1)

    heatmap = np.zeros((height, width), dtype=np.float32)
    np.add.at(heatmap, (ys, xs), 1.0)

    pad = int(sigma * 3)
    if pad > 0:
        padded = np.pad(heatmap, ((0, 0), (pad, pad)), mode="wrap")
    else:
        padded = heatmap

    ksize = int(sigma * 6) | 1
    blurred = cv2.GaussianBlur(padded, (ksize, ksize), sigma)

    if pad > 0:
        heatmap = blurred[:, pad : pad + width]
    else:
        heatmap = blurred

    peak = heatmap.max()
    if peak > 0:
        heatmap /= peak

    return heatmap


def composite_heatmap_onto_panorama(
    panorama: np.ndarray,
    heatmap: np.ndarray,
    global_opacity: float = GLOBAL_OPACITY,
) -> np.ndarray:
    h, w = heatmap.shape
    hm_idx = (np.clip(heatmap, 0, 1) * 255).astype(np.uint8)
    overlay = _COLOR_LUT[hm_idx.ravel()].reshape(h, w, 3)
    alpha = _ALPHA_LUT[hm_idx.ravel()].reshape(h, w, 1) * global_opacity
    orig_f = panorama.astype(np.float32)
    result = orig_f * (1.0 - alpha) + overlay * alpha
    return np.clip(result, 0, 255).astype(np.uint8)


# ── Group-specific logic ─────────────────────────────────────────────

def load_grouped_gaze_data(
    csv_path: Path,
    yaw_lookup: dict[str, float],
    target_frames: set[str],
) -> dict[str, dict[str, list[tuple[float, float]]]]:
    """Read clean CSV and return {frame_stem: {group: [(u, v), ...]}}.

    Only loads rows for frames in target_frames.
    """
    data: dict[str, dict[str, list[tuple[float, float]]]] = defaultdict(
        lambda: defaultdict(list)
    )

    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("isTracked") != "1.0" and row.get("isTracked") != "1":
                continue

            frame_stem = row.get("frame", "").strip()
            if frame_stem not in target_frames:
                continue

            group = row.get("group", "").strip()
            if group not in ("good", "poor"):
                continue

            photo_path = row.get("photoPath", "").strip()
            if not photo_path:
                continue

            try:
                u = float(row["hitU"])
                v = float(row["hitV"])
            except (ValueError, KeyError):
                continue

            parts = photo_path.split("/")
            if len(parts) == 2:
                seq_id = parts[0]
                is_rev = seq_id.endswith("_rev")
                base_yaw = yaw_lookup.get(frame_stem, 0.0)
                vr_yaw = (base_yaw + 180) % 360 if is_rev else base_yaw
                u = (u + vr_yaw / 360.0 + SPHERE_UV_OFFSET) % 1.0

            data[frame_stem][group].append((u, v))

    return dict(data)


def process_frame(
    frame_stem: str,
    group: str,
    uv_points: list[tuple[float, float]],
    frames_dir: str,
    out_dir: str,
) -> tuple[bool, str]:
    source = frame_stem.rsplit("_", 1)[0]
    web_path = f"{source}/{frame_stem}"
    pano_path = Path(frames_dir) / f"{web_path}.jpg"

    if not pano_path.exists():
        return False, f"not found: {pano_path}"

    panorama = cv2.imread(str(pano_path))
    if panorama is None:
        return False, f"failed to read: {pano_path}"

    pano_h, pano_w = panorama.shape[:2]
    hm_w = int(pano_w * HEATMAP_SCALE)
    hm_h = int(pano_h * HEATMAP_SCALE)
    sigma = hm_w * 0.015

    heatmap_small = generate_heatmap(uv_points, hm_w, hm_h, sigma)
    heatmap = cv2.resize(heatmap_small, (pano_w, pano_h), interpolation=cv2.INTER_LINEAR)
    result = composite_heatmap_onto_panorama(panorama, heatmap)

    out_file = Path(out_dir) / group / f"{web_path}.jpg"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_file), result, [cv2.IMWRITE_JPEG_QUALITY, 92])

    return True, f"{group}/{web_path} ({len(uv_points)} pts, {pano_w}x{pano_h})"


def main():
    parser = argparse.ArgumentParser(
        description="Generate group-aggregated gaze heatmaps (good vs poor)."
    )
    parser.add_argument(
        "--frames", nargs="*", default=None,
        help="Specific frame stems to process (default: all pre-defined frames)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview stats without writing files",
    )
    args = parser.parse_args()

    target_frames = set(args.frames) if args.frames else set(ALL_TARGET_FRAMES)

    print(f"\n{'=' * 60}")
    print(f"  Generate Group Comparison Heatmaps")
    print(f"{'=' * 60}")
    print(f"  CSV:        {CLEAN_CSV}")
    print(f"  Frames dir: {FRAMES_DIR}")
    print(f"  Output dir: {OUTPUT_DIR}")
    print(f"  Targets:    {len(target_frames)} frames")
    if args.dry_run:
        print(f"  Mode:       DRY RUN")
    print(f"{'=' * 60}")

    yaw_lookup = load_yaw_lookup()
    print(f"\n  Yaw lookup: {len(yaw_lookup)} frames from data.json")

    print(f"  Loading gaze data from {CLEAN_CSV.name}...")
    grouped_data = load_grouped_gaze_data(CLEAN_CSV, yaw_lookup, target_frames)
    print(f"  Frames with data: {len(grouped_data)}")

    tasks = []
    for frame_stem in sorted(grouped_data.keys()):
        groups = grouped_data[frame_stem]
        for group in ("good", "poor"):
            pts = groups.get(group, [])
            n = len(pts)
            status = "OK" if n >= MIN_GAZE_POINTS else "SKIP"
            print(f"    {frame_stem}  {group:4s}  {n:5d} pts  [{status}]")
            if n >= MIN_GAZE_POINTS:
                tasks.append((frame_stem, group, pts))

    missing = target_frames - set(grouped_data.keys())
    if missing:
        print(f"\n  Warning: {len(missing)} frames not found in CSV:")
        for m in sorted(missing):
            print(f"    {m}")

    if not tasks:
        print("\n  No frames to process.")
        return

    print(f"\n  Total tasks: {len(tasks)} (frame x group combinations)")

    if args.dry_run:
        print(f"\n  Done (dry run)")
        return

    start = time.time()
    workers = min(os.cpu_count() or 1, len(tasks))
    print(f"  Workers: {workers}")

    generated = []
    futures_map = {}

    with ProcessPoolExecutor(max_workers=workers) as pool:
        for frame_stem, group, uv_points in tasks:
            fut = pool.submit(
                process_frame, frame_stem, group, uv_points,
                str(FRAMES_DIR), str(OUTPUT_DIR),
            )
            futures_map[fut] = (frame_stem, group, len(uv_points))

        done_count = 0
        for fut in as_completed(futures_map):
            frame_stem, group, n_pts = futures_map[fut]
            done_count += 1
            ok, msg = fut.result()
            if ok:
                generated.append(f"{group}/{frame_stem}")
            status = "OK" if ok else "FAIL"
            print(f"  [{done_count}/{len(futures_map)}] [{status}] {msg}")

    elapsed = time.time() - start
    print(f"\n  Done: {len(generated)} composites in {elapsed:.1f}s")
    print(f"  Output: {OUTPUT_DIR}")

    good_count = sum(1 for g in generated if g.startswith("good/"))
    poor_count = sum(1 for g in generated if g.startswith("poor/"))
    print(f"    good: {good_count} frames")
    print(f"    poor: {poor_count} frames")


if __name__ == "__main__":
    main()
