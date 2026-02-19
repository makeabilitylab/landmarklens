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

def discover_all_videos(folders=None):
    """Auto-discover all video pairs: .360 in original-files-video + .mp4/.mov in video/."""
    if not VIDEO_360_DIR.exists() or not VIDEO_MP4_DIR.exists():
        return []
    if folders:
        raw_files = []
        for folder in folders:
            folder_path = VIDEO_360_DIR / folder
            if folder_path.is_dir():
                raw_files.extend(sorted(folder_path.glob("*.360")))
            else:
                print(f"  WARNING: Folder not found: {folder_path}")
    else:
        raw_files = sorted(VIDEO_360_DIR.rglob("*.360"))
    videos = []
    for raw_path in raw_files:
        found = find_video_by_stem(raw_path.stem)
        if found:
            videos.append(found)
    return videos


def parse_args():
    parser = argparse.ArgumentParser(description="Extract frames from GoPro 360 videos and match with GPS.")
    parser.add_argument("--only", metavar="VIDEO", help="Process only this video (filename or stem, e.g. GS010318)")
    parser.add_argument("--extract-only", action="store_true",
                        help="Only extract frames (ffmpeg). Skip GPS matching and data.json generation.")
    parser.add_argument("--json-only", action="store_true",
                        help="Only rebuild data.json from existing frames on disk. No ffmpeg extraction.")
    parser.add_argument("folders", nargs="*", metavar="FOLDER",
                        help="Process only videos in these subfolders (e.g. kinshicho koenji)")
    return parser.parse_args()


def find_video_by_stem(stem):
    """Find a video file and its .360 in the data dirs. Returns one dict like VIDEO_FILES entries, or None."""
    stem = stem.strip()
    raw_matches = list(VIDEO_360_DIR.rglob(f"{stem}.360"))
    if not raw_matches:
        return None
    raw_path = raw_matches[0]
    candidates = [
        p for p in VIDEO_MP4_DIR.rglob("*")
        if p.is_file() and p.suffix.lower() in (".mp4", ".mov")
        and (p.stem == stem or p.stem.startswith(stem + "_"))
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: (p.stem != stem, p.name))
    return {
        "mp4": str(candidates[0].relative_to(VIDEO_MP4_DIR)),
        "raw": str(raw_path.relative_to(VIDEO_360_DIR)),
    }


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


def scan_existing_frames(video_path, output_dir, prefix):
    """Scan already-extracted frames on disk instead of running ffmpeg."""
    jpgs = sorted(output_dir.glob(f"{prefix}_*.jpg"))
    if not jpgs:
        return []
    duration = get_video_duration(video_path)
    print(f"    Video duration: {duration:.2f}s | Found {len(jpgs)} frames on disk")
    frames = []
    for i, fpath in enumerate(jpgs):
        num = int(fpath.stem.split("_")[-1])
        video_time = num * FRAME_INTERVAL_SEC
        frames.append({
            "filename": fpath.name,
            "video_fraction": min(video_time / duration, 1.0) if duration > 0 else 0,
        })
    return frames


def discover_from_frames():
    """Discover videos based on existing frame directories on disk."""
    if not OUTPUT_FRAMES_DIR.exists():
        return []
    videos = []
    for frame_dir in sorted(OUTPUT_FRAMES_DIR.iterdir()):
        if not frame_dir.is_dir():
            continue
        stem = frame_dir.name
        if not list(frame_dir.glob(f"{stem}_*.jpg")):
            continue
        found = find_video_by_stem(stem)
        if found:
            videos.append(found)
        else:
            print(f"  WARNING: Frames exist for {stem} but no matching .360 + video found, skipping")
    return videos


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


def discover_videos(args):
    """Resolve the list of videos to process based on CLI arguments."""
    if args.only:
        key = args.only.strip()
        found = find_video_by_stem(key)
        if found:
            print(f"  Using: {found['mp4']} + {found['raw']}")
            return [found]
        if not VIDEO_MP4_DIR.exists():
            print(f"ERROR: Video directory not found: {VIDEO_MP4_DIR}")
        elif not VIDEO_360_DIR.exists():
            print(f"ERROR: .360 directory not found: {VIDEO_360_DIR}")
        else:
            print(f"ERROR: No video matching '{key}'.")
        sys.exit(1)

    if args.json_only and not args.folders:
        videos = discover_from_frames()
    else:
        videos = discover_all_videos(args.folders or None)

    if not videos:
        if args.folders:
            print(f"ERROR: No video pairs found in folders: {', '.join(args.folders)}")
        else:
            print("ERROR: No video pairs found.")
        sys.exit(1)

    label = f"folders: {', '.join(args.folders)}" if args.folders else "all"
    print(f"  Auto-discovered {len(videos)} video pairs ({label})")
    return videos


def write_data_json(new_frames, processed_sources):
    """Merge new frames with existing data.json (preserving unprocessed sources), then write."""
    # Load existing data and keep frames for sources we didn't just process
    existing_frames = []
    if OUTPUT_JSON.exists():
        try:
            with open(OUTPUT_JSON) as f:
                existing = json.load(f)
            existing_frames = [
                fr for fr in existing.get("frames", [])
                if fr["source"] not in processed_sources
            ]
            if existing_frames:
                kept_sources = sorted(set(fr["source"] for fr in existing_frames))
                print(f"\n  Keeping {len(existing_frames)} existing frames from: {', '.join(kept_sources)}")
        except (json.JSONDecodeError, KeyError):
            pass

    all_frames = existing_frames + new_frames

    # Re-index and compute distances
    for i, f in enumerate(all_frames):
        f["index"] = i
        f["heading"] = f.get("heading", 0)
        if i < len(all_frames) - 1 and f["source"] == all_frames[i+1]["source"]:
            f["distance_to_next"] = round(haversine_distance(
                f["lat"], f["lon"], all_frames[i+1]["lat"], all_frames[i+1]["lon"]), 2)
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
            {k: f[k] for k in ("index", "filename", "lat", "lon", "alt", "heading", "distance_to_next", "source", "neighborhood") if k in f}
            for f in all_frames
        ],
    }

    with open(OUTPUT_JSON, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nDone! {len(all_frames)} frames | {total_dist:.0f}m total", end="")
    if len(all_frames) > 1:
        print(f" | ~{total_dist / (len(all_frames) - 1):.1f}m avg spacing")
    print(f"Output: {OUTPUT_JSON}")


def main():
    args = parse_args()
    if args.extract_only and args.json_only:
        print("ERROR: --extract-only and --json-only are mutually exclusive.")
        sys.exit(1)

    videos = discover_videos(args)

    mode = "extract-only" if args.extract_only else "json-only" if args.json_only else "full"
    print("=" * 60)
    print(f"GoPro 360 Frame Extractor + GPS Matcher  [{mode}]")
    print(f"  Spacing: {TARGET_SPACING_M}m | Min movement: {MIN_MOVEMENT_M}m | Format: JPG (q:v 1)")
    print("=" * 60)

    OUTPUT_FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    all_frames = []
    processed_sources = set()

    for vid in videos:
        mp4_path = VIDEO_MP4_DIR / vid["mp4"]
        raw_path = VIDEO_360_DIR / vid["raw"]
        prefix = Path(vid["raw"]).stem
        video_dir = OUTPUT_FRAMES_DIR / prefix

        print(f"\nProcessing {prefix}...")
        if not mp4_path.exists():
            print(f"  ERROR: Missing video file: {mp4_path}")
            continue

        # ── Extract-only: just ffmpeg, then move on ──
        if args.extract_only:
            if not raw_path.exists():
                print(f"  WARNING: .360 missing, extracting frames without GPS")
            extract_frames(mp4_path, video_dir, prefix)
            continue

        # ── JSON-only or full: need GPS ──
        if not raw_path.exists():
            print(f"  ERROR: Missing .360 file: {raw_path}")
            continue

        gps_points = extract_gps_from_360(raw_path)
        if not gps_points:
            continue

        if args.json_only:
            frames = scan_existing_frames(mp4_path, video_dir, prefix)
        else:
            frames = extract_frames(mp4_path, video_dir, prefix)

        if not frames:
            continue

        raw_parts = Path(vid["raw"]).parts
        neighborhood = raw_parts[0] if len(raw_parts) > 1 else ""

        for f in frames:
            gps = interpolate_gps(gps_points, f["video_fraction"])
            f.update({"lat": round(gps["lat"], 7), "lon": round(gps["lon"], 7),
                       "alt": round(gps["alt"], 2), "source": prefix,
                       "neighborhood": neighborhood,
                       "filename": f"{prefix}/{f['filename']}"})

        frames = filter_stationary(frames, MIN_MOVEMENT_M)
        frames = thin_by_distance(frames, TARGET_SPACING_M)
        cleanup_unused(video_dir, frames)
        all_frames.extend(frames)
        processed_sources.add(prefix)

    if args.extract_only:
        print(f"\nDone! Frames extracted for {len(videos)} videos.")
        print("Run with --json-only to rebuild data.json from all frames on disk.")
        return

    write_data_json(all_frames, processed_sources)


if __name__ == "__main__":
    main()