#!/usr/bin/env python3
"""
Generate gaze heatmap composites overlaid on panorama images.

Reads gaze CSVs exported from the VR app, generates per-frame heatmaps,
and composites them directly onto the panorama JPGs:
  - Non-gazed areas receive a blue tint (darker, cooler)
  - Gazed areas show the original image with JET colormap overlay

Output mirrors the web/frames/ directory structure so the viewer can
swap between original and heatmap versions by changing the base path.

Usage:
    # Generate for one CSV
    python script/generate_heatmaps.py study-data/eye-gaze-data/p1-good.csv

    # Test with a single frame (picks the frame with most gaze data)
    python script/generate_heatmaps.py study-data/eye-gaze-data/p1-good.csv --limit 1

    # Multiple CSVs at once
    python script/generate_heatmaps.py study-data/eye-gaze-data/*.csv

    # Preview stats without writing
    python script/generate_heatmaps.py study-data/eye-gaze-data/p1-good.csv --dry-run
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
OUTPUT_DIR = BASE_DIR / "study-data" / "heatmap-frames"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"

MIN_GAZE_POINTS = 3
HEATMAP_SCALE = 0.25  # compute heatmap at quarter res, upscale for compositing

# Unity's default sphere UV maps the equirect center (u=0.5) to +X,
# which is 90° right of +Z. CalculateEquirectangularUV computes u from
# world-space direction (u=0.5 at -Z), so every recorded hitU is offset
# by 90° from the true image pixel coordinate.
SPHERE_UV_OFFSET = 0.25  # 90° / 360°


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


# ── Yaw lookup ─────────────────────────────────────────────────────────

def load_yaw_lookup() -> dict[str, float]:
    """Build {frame_stem: base_yaw} from web/data.json.

    The VR app rotates the panorama sphere by -yaw around Y
    (via ApplySphereOrientation). EyeGazeLogger records hitU in
    world-space, so we need this yaw to correct back to texture-space.
    For _rev sequences, export_route_vr.py adds 180° to the base yaw.
    """
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


# ── CSV parsing ────────────────────────────────────────────────────────

def load_gaze_data(
    csv_path: Path,
    yaw_lookup: dict[str, float],
) -> dict[str, list[tuple[float, float]]]:
    """Read a gaze CSV and return {web_path: [(u, v), ...]}.

    Corrects hitU for two offsets:
    1. Unity sphere UV convention: the mesh maps u=0.5 to +X, but the
       formula computes u=0.5 at -Z → constant +0.25 shift.
    2. Sphere rotation: ApplySphereOrientation rotates by -yaw around Y,
       but EyeGazeLogger records UV from world-space → +yaw/360 shift.
    Combined: u_pixel = (u_world + yaw/360 + 0.25) % 1.0
    For _rev sequences the VR yaw = (base_yaw + 180) % 360.
    """
    by_frame: dict[str, list[tuple[float, float]]] = defaultdict(list)
    corrected_count = 0

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

            parts = photo_path.split("/")
            if len(parts) == 2:
                seq_id, frame_stem = parts[0], parts[1]
                is_rev = seq_id.endswith("_rev")
                base_yaw = yaw_lookup.get(frame_stem, 0.0)
                vr_yaw = (base_yaw + 180) % 360 if is_rev else base_yaw
                u = (u + vr_yaw / 360.0 + SPHERE_UV_OFFSET) % 1.0
                corrected_count += 1

            web_path = vr_path_to_web_path(photo_path)
            by_frame[web_path].append((u, v))

    if corrected_count:
        print(f"  Yaw-corrected {corrected_count} gaze samples")

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


# ── Compositing ────────────────────────────────────────────────────────

# Color/alpha breakpoints: (t, BGR_color, alpha)
# Based on the user's reference heatmap spec.
HEATMAP_BREAKPOINTS = [
    (0.00,  (120, 50, 20),   0.55),   # Dark blue tint
    (0.01,  (120, 50, 20),   0.55),   # Dark blue tint (end)
    (0.15,  (170, 60, 10),   0.70),   # Deeper blue
    (0.30,  (200, 180, 10),  0.80),   # Blue -> cyan
    (0.50,  (60, 210, 20),   0.85),   # Cyan -> green
    (0.70,  (30, 230, 250),  0.85),   # Green -> yellow
    (0.85,  (15, 100, 250),  0.90),   # Yellow -> orange-red
    (1.00,  (15, 20, 255),   0.90),   # Hot red
]

GLOBAL_OPACITY = 0.70


def _build_luts() -> tuple[np.ndarray, np.ndarray]:
    """Build 256-entry color and alpha lookup tables from breakpoints."""
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


def composite_heatmap_onto_panorama(
    panorama: np.ndarray,
    heatmap: np.ndarray,
    global_opacity: float = GLOBAL_OPACITY,
) -> np.ndarray:
    """Composite a heatmap onto a panorama image.

    Uses a custom colormap with per-intensity alpha, modulated by
    global_opacity (default 0.70). Non-gazed areas get a dark blue
    tint; gazed areas transition through cyan/green/yellow to red.
    """
    h, w = heatmap.shape
    hm_idx = (np.clip(heatmap, 0, 1) * 255).astype(np.uint8)

    overlay = _COLOR_LUT[hm_idx.ravel()].reshape(h, w, 3)
    alpha = _ALPHA_LUT[hm_idx.ravel()].reshape(h, w, 1) * global_opacity

    orig_f = panorama.astype(np.float32)
    result = orig_f * (1.0 - alpha) + overlay * alpha

    return np.clip(result, 0, 255).astype(np.uint8)


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
    yaw_lookup: dict[str, float],
    dry_run: bool,
    limit: int | None,
    sequence: str | None = None,
):
    print(f"\n{'=' * 60}")
    print(f"  Generate Heatmap Composites")
    print(f"{'=' * 60}")
    print(f"  CSV:          {csv_path}")
    print(f"  Participant:  {participant}")
    print(f"  Frames dir:   {FRAMES_DIR}")
    print(f"  Output dir:   {OUTPUT_DIR / participant}")
    if sequence:
        print(f"  Sequence:     {sequence}")
    if limit:
        print(f"  Limit:        {limit} frame(s)")
    if dry_run:
        print(f"  Mode:         DRY RUN")
    print(f"{'=' * 60}")

    gaze_data = load_gaze_data(csv_path, yaw_lookup)
    print(f"\n  Frames with gaze data: {len(gaze_data)}")

    if sequence:
        gaze_data = {k: v for k, v in gaze_data.items() if k.startswith(sequence + "/")}

    skipped = sum(1 for pts in gaze_data.values() if len(pts) < MIN_GAZE_POINTS)
    eligible = {k: v for k, v in gaze_data.items() if len(v) >= MIN_GAZE_POINTS}
    print(f"  Eligible (>={MIN_GAZE_POINTS} points): {len(eligible)}")
    if skipped:
        print(f"  Skipped (<{MIN_GAZE_POINTS} points): {skipped}")

    total_points = sum(len(v) for v in eligible.values())
    print(f"  Total gaze points: {total_points}")

    if not eligible:
        print("\n  No frames to process.")
        return []

    # Sort by gaze point count (most first) so --limit picks the best frames
    sorted_frames = sorted(eligible.items(), key=lambda x: len(x[1]), reverse=True)

    if limit:
        sorted_frames = sorted_frames[:limit]

    out_dir = OUTPUT_DIR / participant
    start = time.time()
    workers = min(os.cpu_count() or 1, len(sorted_frames))
    print(f"  Workers:      {workers}")

    if dry_run:
        print(f"\n  Done (dry run): {len(sorted_frames)} frames eligible")
        return [wp for wp, _ in sorted_frames]

    generated = []
    futures_map = {}

    with ProcessPoolExecutor(max_workers=workers) as pool:
        for web_path, uv_points in sorted_frames:
            fut = pool.submit(
                _process_frame, web_path, uv_points, str(FRAMES_DIR), str(out_dir),
            )
            futures_map[fut] = (web_path, len(uv_points))

        done_count = 0
        for fut in as_completed(futures_map):
            web_path, n_pts = futures_map[fut]
            done_count += 1
            ok, msg = fut.result()
            if ok:
                generated.append(web_path)
            if not ok or done_count % 50 == 0 or done_count == len(futures_map):
                elapsed = time.time() - start
                rate = done_count / elapsed if elapsed > 0 else 0
                print(
                    f"  [{done_count}/{len(futures_map)}] {rate:.1f} frames/s"
                    f"  | {msg}"
                )

    elapsed = time.time() - start
    print(f"\n  Done: {len(generated)} composites in {elapsed:.1f}s")
    if generated:
        print(f"  Output: {out_dir}")

    return generated


def _process_frame(
    web_path: str,
    uv_points: list[tuple[float, float]],
    frames_dir: str,
    out_dir: str,
) -> tuple[bool, str]:
    """Process a single frame (runs in a worker process)."""
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

    out_file = Path(out_dir) / f"{web_path}.jpg"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_file), result, [cv2.IMWRITE_JPEG_QUALITY, 92])

    return True, f"{web_path} ({len(uv_points)} pts, {pano_w}x{pano_h})"


def main():
    parser = argparse.ArgumentParser(
        description="Generate gaze heatmap composites overlaid on panorama images."
    )
    parser.add_argument(
        "csv_files", nargs="+", type=str,
        help="One or more gaze CSV files",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Only process N frames per CSV (sorted by most gaze data, for testing)",
    )
    parser.add_argument(
        "--sequence", type=str, default=None,
        help="Only process frames from this sequence (e.g. GS010317)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview without writing files",
    )
    args = parser.parse_args()

    manifest = load_manifest()
    yaw_lookup = load_yaw_lookup()
    print(f"  Yaw lookup: {len(yaw_lookup)} frames loaded from data.json")

    for csv_file in args.csv_files:
        csv_path = Path(csv_file)
        if not csv_path.is_absolute():
            csv_path = Path.cwd() / csv_path
        if not csv_path.exists():
            print(f"Error: {csv_path} not found")
            sys.exit(1)

        participant = csv_path.stem

        generated = process_one(csv_path, participant, yaw_lookup, args.dry_run, args.limit, args.sequence)

        if not args.dry_run and generated:
            if participant not in manifest["participants"]:
                manifest["participants"].append(participant)
            existing = set(manifest["frames"].get(participant, []))
            existing.update(generated)
            manifest["frames"][participant] = sorted(existing)

    if not args.dry_run:
        save_manifest(manifest)
        print(f"\n  Manifest: {MANIFEST_PATH}")
        for p in manifest["participants"]:
            count = len(manifest["frames"].get(p, []))
            print(f"    {p}: {count} frames")


if __name__ == "__main__":
    main()
