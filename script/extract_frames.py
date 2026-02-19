#!/usr/bin/env python3
"""
Extract frames from GoPro 360 videos and match them with GPS data.

1. Extracts GPS tracks from the original .360 files using exiftool
2. Extracts equirectangular frames from the processed videos using ffmpeg
3. Matches each frame to a GPS position by interpolating the GPS track
4. Filters out stationary frames (e.g., stopped at traffic lights)
5. Thins frames to ~3m spacing along the route
6. Outputs a metadata JSON file for the web viewer

Heading is 0 for all frames — GoPro Direction Lock orients images forward.
"""

import argparse
import subprocess
import json
import math
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
VIDEO_360_DIR = DATA_DIR / "original-files-video"
VIDEO_MP4_DIR = DATA_DIR / "video"
OUTPUT_FRAMES_DIR = DATA_DIR / "frames"
OUTPUT_JSON = BASE_DIR / "web" / "data.json"

FRAME_INTERVAL_SEC = 0.1
MIN_MOVEMENT_M = 1.5
TARGET_SPACING_M = 3.0

def discover_all_videos():
    """Auto-discover all video pairs: .360 in original-files-video + .mp4/.mov in video/."""
    if not VIDEO_360_DIR.exists() or not VIDEO_MP4_DIR.exists():
        return []
    raw_stems = sorted(p.stem for p in VIDEO_360_DIR.iterdir() if p.suffix.lower() == ".360")
    videos = []
    for stem in raw_stems:
        found = find_video_by_stem(stem)
        if found:
            videos.append(found)
    return videos


def parse_args():
    parser = argparse.ArgumentParser(description="Extract frames from GoPro 360 videos and match with GPS.")
    parser.add_argument("--only", metavar="VIDEO", help="Process only this video (filename or stem, e.g. GS010318)")
    return parser.parse_args()


def find_video_by_stem(stem):
    """Find a video file and its .360 in the data dirs. Returns one dict like VIDEO_FILES entries, or None."""
    stem = stem.strip()
    raw_path = VIDEO_360_DIR / f"{stem}.360"
    if not raw_path.exists():
        return None
    # Match any video file whose stem is exactly the stem or starts with stem_
    candidates = []
    for p in VIDEO_MP4_DIR.iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() not in (".mp4", ".mov"):
            continue
        if p.stem == stem or p.stem.startswith(stem + "_"):
            candidates.append(p)
    if not candidates:
        return None
    # Prefer exact stem match, then sort for deterministic choice
    candidates.sort(key=lambda p: (p.stem != stem, p.name))
    return {"mp4": candidates[0].name, "raw": f"{stem}.360"}


def extract_gps_from_360(filepath):
    """Extract GPS data from a .360 file using exiftool."""
    print(f"  Extracting GPS from {filepath.name}...")
    result = subprocess.run(
        ["exiftool", "-ee", "-n", "-p",
         "$GPSLatitude,$GPSLongitude,$GPSAltitude,$GPSSpeed,$GPSDateTime",
         str(filepath)],
        capture_output=True, text=True, timeout=60, check=True,
    )

    points = []
    for line in result.stdout.strip().split("\n"):
        parts = line.strip().split(",")
        if len(parts) < 5:
            continue
        try:
            lat, lon, alt = float(parts[0]), float(parts[1]), float(parts[2])
        except ValueError:
            continue
        if abs(lat) < 1 and abs(lon) < 1:
            continue
        if not (35.0 < lat < 36.0 and 139.0 < lon < 140.5):
            continue
        points.append({"lat": lat, "lon": lon, "alt": alt})

    print(f"    Found {len(points)} valid GPS points")
    return points


def get_video_duration(filepath):
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", str(filepath)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return float(result.stdout.strip())


def extract_frames(mp4_path, output_dir, prefix):
    """Extract frames as highest-quality JPEG."""
    print(f"  Extracting frames from {mp4_path.name}...")
    output_dir.mkdir(parents=True, exist_ok=True)

    duration = get_video_duration(mp4_path)
    print(f"    Video duration: {duration:.2f}s")

    subprocess.run(
        ["ffmpeg", "-y", "-i", str(mp4_path),
         "-vf", f"fps=1/{FRAME_INTERVAL_SEC}",
         "-q:v", "1", "-start_number", "0",
         str(output_dir / f"{prefix}_%04d.jpg")],
        capture_output=True, timeout=600, check=True,
    )

    frames = []
    for i, fpath in enumerate(sorted(output_dir.glob(f"{prefix}_*.jpg"))):
        video_time = i * FRAME_INTERVAL_SEC
        frames.append({
            "filename": fpath.name,
            "video_fraction": min(video_time / duration, 1.0) if duration > 0 else 0,
        })

    print(f"    Extracted {len(frames)} frames")
    return frames


def haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371000
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def interpolate_gps(gps_points, fraction):
    if not gps_points:
        return {"lat": 0, "lon": 0, "alt": 0}
    if fraction <= 0:
        return gps_points[0]
    if fraction >= 1:
        return gps_points[-1]

    idx = fraction * (len(gps_points) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(gps_points) - 1)
    t = idx - lo
    p1, p2 = gps_points[lo], gps_points[hi]

    return {
        "lat": p1["lat"] + t * (p2["lat"] - p1["lat"]),
        "lon": p1["lon"] + t * (p2["lon"] - p1["lon"]),
        "alt": p1["alt"] + t * (p2["alt"] - p1["alt"]),
    }


def filter_stationary(frames, min_dist):
    if not frames:
        return frames
    kept = [frames[0]]
    for f in frames[1:]:
        if haversine_distance(kept[-1]["lat"], kept[-1]["lon"], f["lat"], f["lon"]) >= min_dist:
            kept.append(f)
    print(f"    Removed {len(frames) - len(kept)} stationary frames ({len(kept)} remaining)")
    return kept


def thin_by_distance(frames, spacing):
    if not frames:
        return frames
    kept = [frames[0]]
    acc = 0
    for i in range(1, len(frames)):
        acc += haversine_distance(frames[i-1]["lat"], frames[i-1]["lon"], frames[i]["lat"], frames[i]["lon"])
        if acc >= spacing:
            kept.append(frames[i])
            acc = 0
    print(f"    Thinned to {len(kept)} frames (~{spacing}m spacing, removed {len(frames) - len(kept)})")
    return kept


def cleanup_unused(output_dir, kept_frames):
    kept = {Path(f["filename"]).name for f in kept_frames}
    removed = 0
    for f in output_dir.glob("*.jpg"):
        if f.name not in kept:
            f.unlink()
            removed += 1
    if removed:
        print(f"    Cleaned up {removed} unused frame files")


def main():
    args = parse_args()
    if args.only:
        key = args.only.strip()
        found = find_video_by_stem(key)
        if found:
            videos = [found]
            print(f"  Using: {found['mp4']} + {found['raw']}")
        else:
            if not VIDEO_MP4_DIR.exists():
                print(f"ERROR: Video directory not found: {VIDEO_MP4_DIR}")
            elif not VIDEO_360_DIR.exists():
                print(f"ERROR: .360 directory not found: {VIDEO_360_DIR}")
            else:
                videos_in_dir = list(VIDEO_MP4_DIR.glob("*.mp4")) + list(VIDEO_MP4_DIR.glob("*.mov"))
                print(f"ERROR: No video matching '{key}'.")
                print(f"  Need: {key}.360 in {VIDEO_360_DIR.name}/ and a .mp4/.mov with stem '{key}' or '{key}_...' in {VIDEO_MP4_DIR.name}/")
                if videos_in_dir:
                    print(f"  Found in {VIDEO_MP4_DIR.name}/: {[p.name for p in videos_in_dir[:5]]}" + (" ..." if len(videos_in_dir) > 5 else ""))
            sys.exit(1)
    else:
        videos = discover_all_videos()
        if not videos:
            print("ERROR: No video pairs found. Need .360 files in original-files-video/ and matching .mp4/.mov in video/")
            sys.exit(1)
        print(f"  Auto-discovered {len(videos)} video pairs")

    print("=" * 60)
    print("GoPro 360 Frame Extractor + GPS Matcher")
    print(f"  Spacing: {TARGET_SPACING_M}m | Min movement: {MIN_MOVEMENT_M}m | Format: JPG (q:v 1)")
    print("=" * 60)

    OUTPUT_FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)

    all_frames = []

    for vid in videos:
        mp4_path = VIDEO_MP4_DIR / vid["mp4"]
        raw_path = VIDEO_360_DIR / vid["raw"]
        prefix = Path(vid["raw"]).stem

        print(f"\nProcessing {prefix}...")
        if not mp4_path.exists() or not raw_path.exists():
            print(f"  ERROR: Missing files for {prefix}")
            continue

        gps_points = extract_gps_from_360(raw_path)
        if not gps_points:
            continue

        video_dir = OUTPUT_FRAMES_DIR / prefix
        frames = extract_frames(mp4_path, video_dir, prefix)
        if not frames:
            continue

        # Match GPS
        for f in frames:
            gps = interpolate_gps(gps_points, f["video_fraction"])
            f.update({"lat": round(gps["lat"], 7), "lon": round(gps["lon"], 7),
                       "alt": round(gps["alt"], 2), "source": prefix,
                       "filename": f"{prefix}/{f['filename']}"})

        frames = filter_stationary(frames, MIN_MOVEMENT_M)
        frames = thin_by_distance(frames, TARGET_SPACING_M)
        cleanup_unused(video_dir, frames)
        all_frames.extend(frames)

    # Index + distance
    for i, f in enumerate(all_frames):
        f["index"] = i
        f["heading"] = 0
        if i < len(all_frames) - 1 and f["source"] == all_frames[i+1]["source"]:
            f["distance_to_next"] = round(haversine_distance(
                f["lat"], f["lon"], all_frames[i+1]["lat"], all_frames[i+1]["lon"]), 2)
        else:
            f["distance_to_next"] = 0

    total_dist = sum(f["distance_to_next"] for f in all_frames)

    output = {
        "generated": True,
        "frame_count": len(all_frames),
        "frames_dir": "frames",
        "total_distance_m": round(total_dist, 1),
        "settings": {"min_movement_m": MIN_MOVEMENT_M, "target_spacing_m": TARGET_SPACING_M},
        "frames": [
            {k: f[k] for k in ("index", "filename", "lat", "lon", "alt", "heading", "distance_to_next", "source")}
            for f in all_frames
        ],
    }

    with open(OUTPUT_JSON, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nDone! {len(all_frames)} frames | {total_dist:.0f}m total", end="")
    if len(all_frames) > 1:
        print(f" | ~{total_dist / (len(all_frames) - 1):.1f}m avg spacing")
    print(f"Output: {OUTPUT_JSON}")


if __name__ == "__main__":
    main()