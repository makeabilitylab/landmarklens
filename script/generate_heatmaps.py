#!/usr/bin/env python3
"""
Generate gaze heatmap overlays from eye-tracking CSV data.

Reads gaze CSVs exported from the VR app, groups samples by photo, and
produces per-frame heatmap PNGs that can be overlaid on panorama frames.

The output PNGs are RGB on a black background, designed for screen-blend
compositing (black = transparent).  A manifest.json is also written so
the web viewer knows which frames have heatmaps.

Usage:
    # Single participant
    python script/generate_heatmaps.py data/eye-gaze-data/pilot_1.csv

    # Explicit participant name
    python script/generate_heatmaps.py data/eye-gaze-data/pilot_1.csv --participant P01

    # Multiple files at once
    python script/generate_heatmaps.py data/eye-gaze-data/pilot_0.csv data/eye-gaze-data/pilot_1.csv

    # Quarter resolution (default), half resolution, or full
    python script/generate_heatmaps.py data/eye-gaze-data/pilot_1.csv --scale 0.5

    # Preview stats without writing
    python script/generate_heatmaps.py data/eye-gaze-data/pilot_1.csv --dry-run
"""

import argparse
import csv
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "web" / "heatmaps"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"

FULL_WIDTH = 7680
FULL_HEIGHT = 3840
MIN_GAZE_POINTS = 3


# ── Photo path conversion ──────────────────────────────────────────────

def vr_path_to_web_path(photo_path: str) -> str:
    """Convert VR export path to web frames-style path.

    'GS010351_fwd/GS010351_0052' -> 'GS010351/GS010351_0052'
    """
    parts = photo_path.strip().split("/")
    if len(parts) != 2:
        return photo_path
    frame_name = parts[1]
    source = frame_name.rsplit("_", 1)[0]
    return f"{source}/{frame_name}"


# ── CSV parsing ────────────────────────────────────────────────────────

def load_gaze_data(csv_path: Path) -> dict[str, list[tuple[float, float]]]:
    """Read a gaze CSV and return {web_path: [(u, v), ...]}."""
    by_frame: dict[str, list[tuple[float, float]]] = defaultdict(list)

    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("isTracked") != "1":
                continue
            photo_path = row.get("photoPath", "").strip()
            if not photo_path:
                continue
            try:
                u = float(row["hitU"])
                v = float(row["hitV"])
            except (ValueError, KeyError):
                continue

            web_path = vr_path_to_web_path(photo_path)
            by_frame[web_path].append((u, v))

    return dict(by_frame)


# ── Heatmap generation ─────────────────────────────────────────────────

def generate_heatmap(
    uv_points: list[tuple[float, float]],
    width: int,
    height: int,
    sigma: float,
) -> np.ndarray:
    """Build a normalised 0-1 heatmap from UV gaze points.

    Handles horizontal wrap-around for equirectangular panoramas.
    """
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


def heatmap_to_rgb(heatmap: np.ndarray, threshold: float = 0.02) -> np.ndarray:
    """Convert a normalised heatmap to an RGB image (black = no data).

    Uses JET colormap. Values below *threshold* are forced to black
    so they contribute nothing under screen-blend compositing.
    """
    mask = heatmap >= threshold
    scaled = np.where(mask, heatmap, 0.0)

    img_8bit = (scaled * 255).astype(np.uint8)
    colored = cv2.applyColorMap(img_8bit, cv2.COLORMAP_JET)

    colored[~mask] = 0

    return colored


# ── Manifest management ────────────────────────────────────────────────

def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        with open(MANIFEST_PATH) as f:
            return json.load(f)
    return {"participants": [], "frames": {}}


def save_manifest(manifest: dict):
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)


# ── Main ───────────────────────────────────────────────────────────────

def process_one(
    csv_path: Path,
    participant: str,
    scale: float,
    dry_run: bool,
):
    print(f"\n{'=' * 60}")
    print(f"  Generate Gaze Heatmaps")
    print(f"{'=' * 60}")
    print(f"  CSV:          {csv_path}")
    print(f"  Participant:  {participant}")
    print(f"  Scale:        {scale:.2f}  ({int(FULL_WIDTH * scale)}×{int(FULL_HEIGHT * scale)})")
    if dry_run:
        print(f"  Mode:         DRY RUN")
    print(f"{'=' * 60}")

    gaze_data = load_gaze_data(csv_path)
    print(f"\n  Frames with gaze data: {len(gaze_data)}")

    skipped = sum(1 for pts in gaze_data.values() if len(pts) < MIN_GAZE_POINTS)
    eligible = {k: v for k, v in gaze_data.items() if len(v) >= MIN_GAZE_POINTS}
    print(f"  Eligible (≥{MIN_GAZE_POINTS} points): {len(eligible)}")
    if skipped:
        print(f"  Skipped (<{MIN_GAZE_POINTS} points): {skipped}")

    total_points = sum(len(v) for v in eligible.values())
    print(f"  Total gaze points: {total_points}")

    if not eligible:
        print("\n  No frames to process.")
        return []

    out_width = int(FULL_WIDTH * scale)
    out_height = int(FULL_HEIGHT * scale)
    sigma = out_width * 0.015

    out_dir = OUTPUT_DIR / participant
    generated = []
    start = time.time()

    for i, (web_path, uv_points) in enumerate(sorted(eligible.items()), 1):
        heatmap = generate_heatmap(uv_points, out_width, out_height, sigma)
        rgb = heatmap_to_rgb(heatmap)

        out_file = out_dir / f"{web_path}.png"

        if not dry_run:
            out_file.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(
                str(out_file), rgb,
                [cv2.IMWRITE_PNG_COMPRESSION, 6],
            )

        generated.append(web_path)

        if i % 50 == 0 or i == len(eligible):
            elapsed = time.time() - start
            rate = i / elapsed if elapsed > 0 else 0
            print(f"  [{i}/{len(eligible)}] {rate:.1f} frames/s")

    elapsed = time.time() - start
    print(f"\n  Done: {len(generated)} heatmaps in {elapsed:.1f}s")
    if not dry_run:
        print(f"  Output: {out_dir}")

    return generated


def main():
    parser = argparse.ArgumentParser(
        description="Generate gaze heatmap overlays from eye-tracking CSVs."
    )
    parser.add_argument(
        "csv_files", nargs="+", type=str,
        help="One or more gaze CSV files",
    )
    parser.add_argument(
        "--participant", type=str, default=None,
        help="Participant ID (default: derived from filename)",
    )
    parser.add_argument(
        "--scale", type=float, default=0.25,
        help="Output scale relative to full 7680×3840 (default: 0.25 → 1920×960)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview without writing files",
    )
    args = parser.parse_args()

    manifest = load_manifest()

    for csv_file in args.csv_files:
        csv_path = Path(csv_file)
        if not csv_path.is_absolute():
            csv_path = Path.cwd() / csv_path
        if not csv_path.exists():
            print(f"Error: {csv_path} not found")
            sys.exit(1)

        if args.participant and len(args.csv_files) == 1:
            participant = args.participant
        else:
            participant = csv_path.stem

        generated = process_one(csv_path, participant, args.scale, args.dry_run)

        if not args.dry_run and generated:
            if participant not in manifest["participants"]:
                manifest["participants"].append(participant)
            manifest["frames"][participant] = sorted(generated)

    if not args.dry_run:
        save_manifest(manifest)
        print(f"\n  Manifest: {MANIFEST_PATH}")
        for p in manifest["participants"]:
            count = len(manifest["frames"].get(p, []))
            print(f"    {p}: {count} frames")


if __name__ == "__main__":
    main()
