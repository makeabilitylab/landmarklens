#!/usr/bin/env python3
"""
Generate gaze heatmaps organized by region / route_id / participant.

Reads all gaze CSVs from a directory. Produces heatmaps in two modes:

  Per-participant (default):
    <output>/<group>/<region>/<route_id>/<participant>/<frame>.{jpg,npy}

  Combined (--combined):
    <output>/<group>/<region>/<route_id>/<frame>.{jpg,npy}

Group (good/poor) is extracted from CSV filenames (e.g. p01-good.csv -> good).

Save modes (can be combined):
  --save-frames   Save composite JPGs (heatmap overlaid on panorama)
  --save-npy      Save raw heatmap arrays as .npy

Use --max-save N to limit output to N route_ids (for testing).

Usage:
    # Save npy only, per-participant, all routes
    python script/generate_route_heatmaps.py study-data/eye-gaze-data/clean/

    # Save both frames and npy, combined, limited to 2 routes
    python script/generate_route_heatmaps.py study-data/eye-gaze-data/clean/ \\
        --combined --save-frames --save-npy --max-save 2

    # Dry run to preview what would be generated
    python script/generate_route_heatmaps.py study-data/eye-gaze-data/clean/ --dry-run
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

# Output base directories
OUTPUT_BASE = BASE_DIR / "study-data" / "route-heatmaps"
# Per-participant
PERPART_FRAMES_DIR = OUTPUT_BASE / "per-participant" / "frames"
PERPART_NPY_DIR = OUTPUT_BASE / "per-participant" / "npy"
# Combined
COMBINED_FRAMES_DIR = OUTPUT_BASE / "combined" / "frames"
COMBINED_NPY_DIR = OUTPUT_BASE / "combined" / "npy"

MIN_GAZE_POINTS = 3
HEATMAP_SCALE = 0.25
SPHERE_UV_OFFSET = 0.25

EXCLUDED_ROUTES = {"freenav", "practice"}


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
        print(f"  Warning: {DATA_JSON} not found - skipping yaw correction")
        return {}
    with open(DATA_JSON) as f:
        data = json.load(f)
    lookup: dict[str, float] = {}
    for frame in data.get("frames", []):
        filename = frame.get("filename", "")
        stem = Path(filename).stem
        lookup[stem] = frame.get("yaw", 0.0)
    return lookup


def get_group(participant: str) -> str:
    """Extract group from participant name: 'p01-good' -> 'good'."""
    parts = participant.split("-", 1)
    return parts[1] if len(parts) == 2 else "unknown"


def load_gaze_rows(csv_path: Path, yaw_lookup: dict[str, float]):
    """Read a gaze CSV and yield (group, route_id, participant, web_path, u, v) tuples."""
    participant = csv_path.stem
    group = get_group(participant)

    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("isTracked") not in ("1", "1.0"):
                continue
            photo_path = row.get("photoPath", "").strip()
            if not photo_path:
                continue
            route_id = row.get("route_id", "").strip()
            if not route_id or route_id in EXCLUDED_ROUTES:
                continue
            # Exclude routes without underscore (no region)
            if "_" not in route_id:
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

            web_path = vr_path_to_web_path(photo_path)
            yield group, route_id, participant, web_path, u, v


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


# ── Compositing ───────────────────────────────────────────────────────

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

GLOBAL_OPACITY = 0.70


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


# ── Worker functions ──────────────────────────────────────────────────

def _save_frame(
    web_path: str,
    uv_points: list[tuple[float, float]],
    frames_dir: str,
    out_path: str,
) -> tuple[bool, str]:
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
    out_file = Path(out_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_file), result, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return True, f"{web_path} ({len(uv_points)} pts)"


def _save_npy(
    web_path: str,
    uv_points: list[tuple[float, float]],
    out_path: str,
    width: int = 1920,
    height: int = 960,
) -> tuple[bool, str]:
    sigma = width * 0.015
    heatmap = generate_heatmap(uv_points, width, height, sigma)
    out_file = Path(out_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    np.save(str(out_file), heatmap)
    return True, f"{web_path} ({len(uv_points)} pts)"


# ── Data loading ──────────────────────────────────────────────────────

def load_all_gaze(csv_dir: Path, yaw_lookup: dict[str, float]):
    """Load all CSVs and organize gaze data.

    Returns:
        by_group: {group: {route_id: {participant: {web_path: [(u,v), ...]}}}}
    """
    by_group: dict[str, dict[str, dict[str, dict[str, list[tuple[float, float]]]]]] = {}

    csv_files = sorted(csv_dir.glob("*.csv"))
    if not csv_files:
        print(f"  Error: no CSV files found in {csv_dir}")
        sys.exit(1)

    print(f"  Found {len(csv_files)} CSV files")
    for csv_path in csv_files:
        print(f"    Loading {csv_path.name}...")
        for group, route_id, participant, web_path, u, v in load_gaze_rows(csv_path, yaw_lookup):
            if group not in by_group:
                by_group[group] = {}
            if route_id not in by_group[group]:
                by_group[group][route_id] = {}
            if participant not in by_group[group][route_id]:
                by_group[group][route_id][participant] = defaultdict(list)
            by_group[group][route_id][participant][web_path].append((u, v))

    return by_group


def get_region(route_id: str) -> str:
    return route_id.rsplit("_", 1)[0]


# ── Processing ────────────────────────────────────────────────────────

def _collect_route_ids(by_group: dict, max_save: int | None) -> list[str]:
    """Get deduplicated, sorted route_ids across all groups, with optional limit."""
    all_routes = set()
    for group_data in by_group.values():
        all_routes.update(group_data.keys())
    route_ids = sorted(all_routes)
    if max_save is not None:
        route_ids = route_ids[:max_save]
    return route_ids


def process_per_participant(
    by_group: dict,
    save_frames: bool,
    save_npy: bool,
    max_save: int | None,
    dry_run: bool,
):
    route_ids = _collect_route_ids(by_group, max_save)

    total_jobs = 0
    jobs = []

    for group in sorted(by_group):
        by_route = by_group[group]
        for route_id in route_ids:
            if route_id not in by_route:
                continue
            region = get_region(route_id)
            participants = sorted(by_route[route_id].keys())
            for participant in participants:
                frames = by_route[route_id][participant]
                eligible = {k: v for k, v in frames.items() if len(v) >= MIN_GAZE_POINTS}
                for web_path, uv_points in eligible.items():
                    frame_out = None
                    npy_out = None
                    if save_frames:
                        frame_out = str(PERPART_FRAMES_DIR / group / region / route_id / participant / f"{web_path}.jpg")
                    if save_npy:
                        npy_out = str(PERPART_NPY_DIR / group / region / route_id / participant / f"{web_path}.npy")
                    jobs.append((web_path, uv_points, frame_out, npy_out))
                    total_jobs += 1

    print(f"\n  Per-participant: {len(route_ids)} routes, {total_jobs} frame jobs")
    if dry_run:
        _print_route_summary(by_group, route_ids, combined=False)
        return

    _run_jobs(jobs, save_frames, save_npy)


def process_combined(
    by_group: dict,
    save_frames: bool,
    save_npy: bool,
    max_save: int | None,
    dry_run: bool,
):
    route_ids = _collect_route_ids(by_group, max_save)

    jobs = []

    for group in sorted(by_group):
        by_route = by_group[group]
        for route_id in route_ids:
            if route_id not in by_route:
                continue
            region = get_region(route_id)
            # Merge all participants' gaze within this group for each frame
            merged: dict[str, list[tuple[float, float]]] = defaultdict(list)
            for participant, frames in by_route[route_id].items():
                for web_path, pts in frames.items():
                    merged[web_path].extend(pts)

            eligible = {k: v for k, v in merged.items() if len(v) >= MIN_GAZE_POINTS}
            for web_path, uv_points in eligible.items():
                frame_out = None
                npy_out = None
                if save_frames:
                    frame_out = str(COMBINED_FRAMES_DIR / group / region / route_id / f"{web_path}.jpg")
                if save_npy:
                    npy_out = str(COMBINED_NPY_DIR / group / region / route_id / f"{web_path}.npy")
                jobs.append((web_path, uv_points, frame_out, npy_out))

    print(f"\n  Combined: {len(route_ids)} routes, {len(jobs)} frame jobs")
    if dry_run:
        _print_route_summary(by_group, route_ids, combined=True)
        return

    _run_jobs(jobs, save_frames, save_npy)


def _print_route_summary(by_group, route_ids, combined):
    for group in sorted(by_group):
        by_route = by_group[group]
        print(f"\n  [{group}]")
        for route_id in route_ids:
            if route_id not in by_route:
                continue
            region = get_region(route_id)
            participants = sorted(by_route[route_id].keys())
            if combined:
                merged_frames = set()
                total_pts = 0
                for p in participants:
                    for wp, pts in by_route[route_id][p].items():
                        if len(pts) >= MIN_GAZE_POINTS:
                            merged_frames.add(wp)
                        total_pts += len(pts)
                print(f"    {region}/{route_id}: {len(merged_frames)} frames, "
                      f"{len(participants)} participants, {total_pts} points")
            else:
                for p in participants:
                    eligible = sum(1 for pts in by_route[route_id][p].values()
                                   if len(pts) >= MIN_GAZE_POINTS)
                    total_pts = sum(len(pts) for pts in by_route[route_id][p].values())
                    print(f"    {region}/{route_id}/{p}: {eligible} frames, {total_pts} points")


def _run_jobs(jobs, save_frames, save_npy):
    start = time.time()
    workers = min(os.cpu_count() or 1, len(jobs))
    print(f"  Workers: {workers}")

    done_count = 0
    total = len(jobs)
    errors = 0

    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for web_path, uv_points, frame_out, npy_out in jobs:
            if save_npy and npy_out:
                fut = pool.submit(_save_npy, web_path, uv_points, npy_out)
                futures[fut] = ("npy", web_path)
            if save_frames and frame_out:
                fut = pool.submit(_save_frame, web_path, uv_points, str(FRAMES_DIR), frame_out)
                futures[fut] = ("frame", web_path)

        total_futures = len(futures)
        for fut in as_completed(futures):
            kind, web_path = futures[fut]
            done_count += 1
            ok, msg = fut.result()
            if not ok:
                errors += 1
            if not ok or done_count % 100 == 0 or done_count == total_futures:
                elapsed = time.time() - start
                rate = done_count / elapsed if elapsed > 0 else 0
                print(f"  [{done_count}/{total_futures}] {rate:.1f}/s | {msg}")

    elapsed = time.time() - start
    print(f"\n  Done: {done_count - errors} saved, {errors} errors, {elapsed:.1f}s")


# ── Main ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Generate gaze heatmaps organized by region/route/participant."
    )
    parser.add_argument(
        "csv_dir", type=str,
        help="Directory containing gaze CSV files",
    )
    parser.add_argument(
        "--save-frames", action="store_true",
        help="Save composite JPGs (heatmap overlaid on panorama)",
    )
    parser.add_argument(
        "--save-npy", action="store_true",
        help="Save raw heatmap arrays as .npy files",
    )
    parser.add_argument(
        "--combined", action="store_true",
        help="Merge gaze across participants per route (saves to combined/ dir)",
    )
    parser.add_argument(
        "--route", type=str, action="append", default=None,
        help="Only process this route_id (can be repeated, e.g. --route ginza_c --route nezu_c)",
    )
    parser.add_argument(
        "--max-save", type=int, default=None,
        help="Limit output to N route_ids (for testing)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview what would be generated without writing files",
    )
    args = parser.parse_args()

    if not args.save_frames and not args.save_npy:
        args.save_npy = True
        print("  (No save mode specified, defaulting to --save-npy)")

    csv_dir = Path(args.csv_dir)
    if not csv_dir.is_absolute():
        csv_dir = Path.cwd() / csv_dir
    if not csv_dir.is_dir():
        print(f"Error: {csv_dir} is not a directory")
        sys.exit(1)

    yaw_lookup = load_yaw_lookup()
    print(f"  Yaw lookup: {len(yaw_lookup)} frames from data.json")

    by_group = load_all_gaze(csv_dir, yaw_lookup)

    if args.route:
        all_known = set()
        for group_data in by_group.values():
            all_known.update(group_data.keys())
        missing = [r for r in args.route if r not in all_known]
        if missing:
            print(f"  Warning: route(s) not found in data: {', '.join(missing)}")
        by_group = {
            g: {r: v for r, v in routes.items() if r in args.route}
            for g, routes in by_group.items()
        }
        by_group = {g: routes for g, routes in by_group.items() if routes}
        if not by_group:
            print("  Error: no matching routes found")
            sys.exit(1)

    all_routes = set()
    for group_data in by_group.values():
        all_routes.update(group_data.keys())
    regions = sorted(set(get_region(r) for r in all_routes))

    print(f"\n  Groups:  {', '.join(sorted(by_group.keys()))}")
    print(f"  Regions: {', '.join(regions)}")
    print(f"  Routes:  {len(all_routes)}")
    for group in sorted(by_group):
        by_route = by_group[group]
        for route_id in sorted(by_route):
            n_parts = len(by_route[route_id])
            n_frames = sum(len(frames) for frames in by_route[route_id].values())
            print(f"    [{group}] {get_region(route_id)}/{route_id}: {n_parts} participants, {n_frames} frames")

    if args.combined:
        process_combined(by_group, args.save_frames, args.save_npy, args.max_save, args.dry_run)
    else:
        process_per_participant(by_group, args.save_frames, args.save_npy, args.max_save, args.dry_run)


if __name__ == "__main__":
    main()
