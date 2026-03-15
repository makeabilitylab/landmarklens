"""Add pano_path field to each frame in a route JSON file.

Usage:
    python vlm/add_pano_paths.py route.json --frames-dir data/frames --output route_with_paths.json
"""

import argparse
import json
from pathlib import Path


def add_pano_paths(route_data, frames_dir):
    """Add pano_path to each frame object based on its frame_id."""
    frames_dir = Path(frames_dir).resolve()

    for segment_key, frames in route_data.items():
        for frame in frames:
            frame_id = frame["frame_id"]
            sequence_id = frame_id.rsplit("_", 1)[0]
            pano_path = frames_dir / sequence_id / f"{frame_id}.jpg"
            frame["pano_path"] = str(pano_path)

    return route_data


def main():
    parser = argparse.ArgumentParser(description="Add pano_path to route JSON frames")
    parser.add_argument("input", help="Input route JSON file")
    parser.add_argument("--frames-dir", default="data/frames",
                        help="Root directory containing panorama frames (default: data/frames)")
    parser.add_argument("--output", "-o", help="Output file (default: overwrite input)")
    args = parser.parse_args()

    with open(args.input) as f:
        route_data = json.load(f)

    add_pano_paths(route_data, args.frames_dir)

    output_path = args.output or args.input
    with open(output_path, "w") as f:
        json.dump(route_data, f, indent=2)

    # Count frames processed
    total = sum(len(frames) for frames in route_data.values())
    print(f"Added pano_path to {total} frames across {len(route_data)} segments -> {output_path}")


if __name__ == "__main__":
    main()
