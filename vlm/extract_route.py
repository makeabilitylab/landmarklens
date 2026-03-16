"""
Extract a route from geodata/routes.geojson into the VLM pipeline's input format.

Usage:
    python vlm/extract_route.py ningyocho_r1
    python vlm/extract_route.py ningyocho_r1 --output vlm/ningyocho_r1/route.json
    python vlm/extract_route.py ningyocho_r1 --frames-dir data/frames

Reads route and frame data from geodata/routes.geojson and produces a JSON file
with {segment1: [frame dicts], segment2: [...], ...} that run_gemini.py expects.
"""

import argparse
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ROUTES_GEOJSON = BASE_DIR / "geodata" / "routes.geojson"
DEFAULT_FRAMES_DIR = BASE_DIR / "data" / "frames"


def extract_route(route_id: str, frames_dir: Path) -> dict:
    """Extract a route from routes.geojson into VLM segment format."""
    with open(ROUTES_GEOJSON) as f:
        data = json.load(f)

    route_feature = None
    for feat in data["features"]:
        if feat["properties"]["route_id"] == route_id:
            route_feature = feat
            break

    if route_feature is None:
        available = [f["properties"]["route_id"] for f in data["features"]]
        raise ValueError(f"Route '{route_id}' not found. Available: {available}")

    frames = route_feature["properties"]["frames"]

    # Group by segment_number
    segments: dict[str, list] = {}
    for frame in frames:
        seg_key = f"segment{frame['segment_number']}"
        frame_id = frame["frame_id"]
        source = frame_id.rsplit("_", 1)[0]
        pano_path = str(frames_dir / source / f"{frame_id}.jpg")

        if not os.path.exists(pano_path):
            print(f"  Warning: missing {pano_path}")

        entry = {
            "frame_id": frame_id,
            "sequence_id": frame.get("sequence_id", f"{source}_fwd"),
            "direction": frame.get("direction", "forward"),
            "segment_number": frame["segment_number"],
            "frame_order": frame["frame_order"],
            "is_junction": frame.get("is_junction", False),
            "turn_direction": frame.get("turn_direction"),
            "turn_angle": frame.get("turn_angle"),
            "is_turn_warning": frame.get("is_turn_warning", False),
            "turn_warning_direction": frame.get("turn_warning_direction"),
            "turn_warning_angle": frame.get("turn_warning_angle"),
            "pano_path": pano_path,
        }
        segments.setdefault(seg_key, []).append(entry)

    # Sort frames within each segment by frame_order
    for seg_key in segments:
        segments[seg_key].sort(key=lambda f: f["frame_order"])

    return segments


def main():
    parser = argparse.ArgumentParser(description="Extract route from routes.geojson for VLM pipeline")
    parser.add_argument("route_id", help="Route ID to extract (e.g. ningyocho_r1)")
    parser.add_argument("--output", "-o", default=None,
                        help="Output JSON path (default: vlm/<route_id>/<route_id>_route.json)")
    parser.add_argument("--frames-dir", default=str(DEFAULT_FRAMES_DIR),
                        help=f"Directory containing panorama frames (default: {DEFAULT_FRAMES_DIR})")
    args = parser.parse_args()

    frames_dir = Path(args.frames_dir).resolve()

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = Path(__file__).resolve().parent / args.route_id / f"{args.route_id}_route.json"

    segments = extract_route(args.route_id, frames_dir)

    total_frames = sum(len(v) for v in segments.values())
    print(f"Route: {args.route_id}")
    print(f"Segments: {len(segments)}, Total frames: {total_frames}")
    for seg_key, frames in sorted(segments.items()):
        print(f"  {seg_key}: {len(frames)} frames ({frames[0]['sequence_id']})")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(segments, f, indent=2)

    print(f"\nWritten to {output_path}")


if __name__ == "__main__":
    main()
