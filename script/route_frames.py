#!/usr/bin/env python3
"""
Generate GPS coordinates for video frames using a Valhalla route.

For videos where the embedded GPS data is missing or unusable, this script:
  1. Scans already-extracted frames on disk
  2. Queries Valhalla for a pedestrian route between start/end points
  3. Distributes frames evenly along the route geometry
  4. Filters stationary frames and thins to ~3m spacing
  5. Merges the result into data.json

Prerequisites:
    - Frames must already be extracted:
        python script/extract_frames.py --extract-only --only GS010360

    - Valhalla must be running locally (see snap_gps.py for Docker setup)

Usage:
    # Basic: provide start and end GPS coordinates
    python script/route_frames.py --source GS010360 \
        --start 35.705611,139.651953 --end 35.708519,139.651676

    # With intermediate waypoints to guide the route
    python script/route_frames.py --source GS010360 \
        --start 35.705611,139.651953 --end 35.708519,139.651676 \
        --via 35.707,139.652

    # Preview without writing
    python script/route_frames.py --source GS010360 \
        --start 35.705611,139.651953 --end 35.708519,139.651676 --dry-run
"""

import argparse
import json
import math
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
VIDEO_360_DIR = DATA_DIR / "original-files-video"
VIDEO_MP4_DIR = DATA_DIR / "video"
OUTPUT_FRAMES_DIR = DATA_DIR / "frames"
OUTPUT_JSON = BASE_DIR / "web" / "data.json"

FRAME_INTERVAL_SEC = 0.2
MIN_MOVEMENT_M = 1.5
TARGET_SPACING_M = 3.0
VALHALLA_DEFAULT_URL = "http://localhost:8002"


# ── Geometry helpers ────────────────────────────────────────────────────

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def decode_polyline(encoded: str, precision: int = 6) -> List[Tuple[float, float]]:
    inv = 1.0 / (10 ** precision)
    decoded = []
    previous = [0, 0]
    i = 0
    while i < len(encoded):
        for axis in range(2):
            shift = 0
            result = 0
            while True:
                b = ord(encoded[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            if result & 1:
                result = ~result
            result >>= 1
            previous[axis] += result
        decoded.append((previous[0] * inv, previous[1] * inv))
    return decoded


def cumulative_distances(points: List[Tuple[float, float]]) -> List[float]:
    dists = [0.0]
    for i in range(1, len(points)):
        d = haversine(points[i - 1][0], points[i - 1][1], points[i][0], points[i][1])
        dists.append(dists[-1] + d)
    return dists


def interpolate_along(
    points: List[Tuple[float, float]],
    cum_dists: List[float],
    target_dist: float,
) -> Tuple[float, float]:
    total = cum_dists[-1]
    target_dist = max(0, min(target_dist, total))
    for i in range(1, len(cum_dists)):
        if cum_dists[i] >= target_dist:
            seg_len = cum_dists[i] - cum_dists[i - 1]
            if seg_len < 1e-9:
                return points[i]
            t = (target_dist - cum_dists[i - 1]) / seg_len
            lat = points[i - 1][0] + t * (points[i][0] - points[i - 1][0])
            lon = points[i - 1][1] + t * (points[i][1] - points[i - 1][1])
            return (lat, lon)
    return points[-1]


def distribute_points(
    shape: List[Tuple[float, float]], n: int
) -> List[Tuple[float, float]]:
    if n <= 0:
        return []
    if n == 1:
        return [shape[0]]
    cum = cumulative_distances(shape)
    total = cum[-1]
    spacing = total / (n - 1)
    return [interpolate_along(shape, cum, i * spacing) for i in range(n)]


# ── Valhalla route ──────────────────────────────────────────────────────

def get_route(
    url: str,
    start: Tuple[float, float],
    end: Tuple[float, float],
    via: Optional[List[Tuple[float, float]]] = None,
    costing: str = "pedestrian",
) -> Optional[List[Tuple[float, float]]]:
    locations = [{"lat": start[0], "lon": start[1], "type": "break"}]
    for v in (via or []):
        locations.append({"lat": v[0], "lon": v[1], "type": "via"})
    locations.append({"lat": end[0], "lon": end[1], "type": "break"})

    payload = {
        "locations": locations,
        "costing": costing,
        "directions_options": {"units": "meters"},
    }

    endpoint = f"{url.rstrip('/')}/route"
    req_data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=req_data,
        headers={"Content-Type": "application/json"}, method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"  Valhalla HTTP {e.code}: {body[:300]}")
        return None
    except urllib.error.URLError as e:
        print(f"  Valhalla connection error: {e.reason}")
        return None

    try:
        all_points = []
        for leg in result["trip"]["legs"]:
            decoded = decode_polyline(leg["shape"])
            if all_points:
                decoded = decoded[1:]
            all_points.extend(decoded)

        total = sum(
            haversine(all_points[i][0], all_points[i][1],
                      all_points[i + 1][0], all_points[i + 1][1])
            for i in range(len(all_points) - 1)
        )
        print(f"    Route: {len(all_points)} shape points, {total:.0f} m")
        return all_points
    except (KeyError, IndexError) as e:
        print(f"  Failed to parse route response: {e}")
        return None


# ── Frame scanning ──────────────────────────────────────────────────────

def get_video_duration(filepath: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(filepath)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return float(result.stdout.strip())


def find_video_file(stem: str) -> Optional[Path]:
    candidates = [
        p for p in VIDEO_MP4_DIR.rglob("*")
        if p.is_file() and p.suffix.lower() in (".mp4", ".mov")
        and (p.stem == stem or p.stem.startswith(stem + "_"))
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: (p.stem != stem, p.name))
    return candidates[0]


def scan_frames(stem: str) -> Tuple[List[Dict], float]:
    frame_dir = OUTPUT_FRAMES_DIR / stem
    jpgs = sorted(frame_dir.glob(f"{stem}_*.jpg"))
    if not jpgs:
        print(f"  No frames found in {frame_dir}")
        print(f"  Run: python script/extract_frames.py --extract-only --only {stem}")
        sys.exit(1)

    video_path = find_video_file(stem)
    if not video_path:
        print(f"  No video file found for {stem}")
        sys.exit(1)

    duration = get_video_duration(video_path)
    print(f"    Video: {video_path.name} ({duration:.2f}s)")
    print(f"    Frames on disk: {len(jpgs)}")

    frames = []
    for fpath in jpgs:
        num = int(fpath.stem.split("_")[-1])
        video_time = num * FRAME_INTERVAL_SEC
        frames.append({
            "filename": f"{stem}/{fpath.name}",
            "video_fraction": min(video_time / duration, 1.0) if duration > 0 else 0,
        })
    return frames, duration


# ── Frame filtering ─────────────────────────────────────────────────────

def filter_stationary(frames: List[Dict], min_dist: float) -> List[Dict]:
    if not frames:
        return frames
    kept = [frames[0]]
    for f in frames[1:]:
        if haversine(kept[-1]["lat"], kept[-1]["lon"], f["lat"], f["lon"]) >= min_dist:
            kept.append(f)
    print(f"    Removed {len(frames) - len(kept)} stationary frames ({len(kept)} remaining)")
    return kept


def thin_by_distance(frames: List[Dict], spacing: float) -> List[Dict]:
    if not frames:
        return frames
    kept = [frames[0]]
    acc = 0.0
    for i in range(1, len(frames)):
        acc += haversine(frames[i - 1]["lat"], frames[i - 1]["lon"],
                         frames[i]["lat"], frames[i]["lon"])
        if acc >= spacing:
            kept.append(frames[i])
            acc = 0.0
    print(f"    Thinned to {len(kept)} frames (~{spacing}m spacing)")
    return kept


def cleanup_unused(stem: str, kept_frames: List[Dict]):
    frame_dir = OUTPUT_FRAMES_DIR / stem
    kept = {Path(f["filename"]).name for f in kept_frames}
    removed = 0
    for f in frame_dir.glob("*.jpg"):
        if f.name not in kept:
            f.unlink()
            removed += 1
    if removed:
        print(f"    Cleaned up {removed} unused frame files")


# ── data.json merge ────────────────────────────────────────────────────

def merge_into_data_json(new_frames: List[Dict], source: str):
    existing_frames = []
    if OUTPUT_JSON.exists():
        try:
            with open(OUTPUT_JSON) as f:
                existing = json.load(f)
            existing_frames = [
                fr for fr in existing.get("frames", [])
                if fr["source"] != source
            ]
            if existing_frames:
                kept_sources = sorted(set(fr["source"] for fr in existing_frames))
                print(f"\n  Keeping {len(existing_frames)} existing frames from: {', '.join(kept_sources)}")
        except (json.JSONDecodeError, KeyError):
            pass

    all_frames = existing_frames + new_frames

    for i, f in enumerate(all_frames):
        f["index"] = i
        f["heading"] = f.get("heading", 0)
        if i < len(all_frames) - 1 and f["source"] == all_frames[i + 1]["source"]:
            f["distance_to_next"] = round(haversine(
                f["lat"], f["lon"], all_frames[i + 1]["lat"], all_frames[i + 1]["lon"]), 2)
        else:
            f["distance_to_next"] = 0

    total_dist = sum(f["distance_to_next"] for f in all_frames)
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)

    output = {
        "generated": True,
        "frame_count": len(all_frames),
        "frames_dir": "frames",
        "total_distance_m": round(total_dist, 1),
        "settings": {"min_movement_m": MIN_MOVEMENT_M, "target_spacing_m": TARGET_SPACING_M},
        "frames": [
            {k: f[k] for k in ("index", "filename", "lat", "lon", "alt", "heading",
                                "distance_to_next", "source", "neighborhood",
                                "lat_snapped", "lon_snapped") if k in f}
            for f in all_frames
        ],
    }

    with open(OUTPUT_JSON, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nDone! {len(all_frames)} total frames | {total_dist:.0f}m total distance")
    print(f"  New: {len(new_frames)} frames from {source}")
    print(f"  Output: {OUTPUT_JSON}")


# ── CLI ─────────────────────────────────────────────────────────────────

def parse_latlon(s: str) -> Tuple[float, float]:
    parts = s.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"Expected lat,lon but got: {s}")
    return (float(parts[0].strip()), float(parts[1].strip()))


def main():
    parser = argparse.ArgumentParser(
        description="Generate GPS for video frames via Valhalla route (for GPS-less videos)"
    )
    parser.add_argument("--source", type=str, required=True,
                        help="Video stem (e.g. GS010360)")
    parser.add_argument("--start", type=str, required=True,
                        help="Start GPS as lat,lon")
    parser.add_argument("--end", type=str, required=True,
                        help="End GPS as lat,lon")
    parser.add_argument("--via", type=str, action="append", default=[],
                        help="Intermediate waypoint as lat,lon (repeatable)")
    parser.add_argument("--neighborhood", type=str, default=None,
                        help="Neighborhood name (auto-detected from folder if omitted)")
    parser.add_argument("--valhalla-url", type=str, default=VALHALLA_DEFAULT_URL,
                        help=f"Valhalla server URL (default: {VALHALLA_DEFAULT_URL})")
    parser.add_argument("--costing", type=str, default="pedestrian",
                        choices=["pedestrian", "bicycle", "auto"])
    parser.add_argument("--no-thin", action="store_true",
                        help="Skip stationary/distance filtering (keep all frames)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without writing to data.json")
    args = parser.parse_args()

    start = parse_latlon(args.start)
    end = parse_latlon(args.end)
    via = [parse_latlon(v) for v in args.via]
    stem = args.source.strip()

    # Auto-detect neighborhood from .360 folder structure
    neighborhood = args.neighborhood
    if not neighborhood:
        raw_matches = list(VIDEO_360_DIR.rglob(f"{stem}.360"))
        if raw_matches:
            parts = raw_matches[0].relative_to(VIDEO_360_DIR).parts
            neighborhood = parts[0] if len(parts) > 1 else ""
        else:
            neighborhood = ""

    print("=" * 60)
    print("Route Frames — Valhalla Route → Frame GPS")
    print("=" * 60)
    print(f"  Source:        {stem}")
    print(f"  Neighborhood:  {neighborhood or '(none)'}")
    print(f"  Start:         {start[0]:.6f}, {start[1]:.6f}")
    print(f"  End:           {end[0]:.6f}, {end[1]:.6f}")
    if via:
        print(f"  Via points:    {len(via)}")
    print(f"  Valhalla URL:  {args.valhalla_url}")
    print(f"  Costing:       {args.costing}")
    if args.dry_run:
        print(f"  Mode:          DRY RUN")
    print("=" * 60)

    # 1. Scan extracted frames
    print(f"\n  Scanning frames...")
    frames, duration = scan_frames(stem)

    # 2. Get route from Valhalla
    print(f"\n  Requesting route from Valhalla...")
    shape = get_route(args.valhalla_url, start, end, via=via, costing=args.costing)
    if shape is None:
        print("\n  Failed to get route. Is Valhalla running?")
        print(f"  Try: curl {args.valhalla_url}/status")
        sys.exit(1)

    # 3. Distribute frames along the route
    distributed = distribute_points(shape, len(frames))
    for i, frame in enumerate(frames):
        lat, lon = distributed[i]
        frame.update({
            "lat": round(lat, 7),
            "lon": round(lon, 7),
            "alt": 0.0,
            "source": stem,
            "neighborhood": neighborhood,
        })

    # 4. Filter and thin
    if not args.no_thin:
        print(f"\n  Filtering...")
        frames = filter_stationary(frames, MIN_MOVEMENT_M)
        frames = thin_by_distance(frames, TARGET_SPACING_M)
        if not args.dry_run:
            cleanup_unused(stem, frames)
    else:
        print(f"\n  Skipping filtering (--no-thin)")

    print(f"\n  Final: {len(frames)} frames")

    # 5. Write to data.json
    if args.dry_run:
        print(f"\n  Dry run — not writing data.json")
        print(f"  Re-run without --dry-run to apply.")
    else:
        merge_into_data_json(frames, stem)


if __name__ == "__main__":
    main()
