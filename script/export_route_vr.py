#!/usr/bin/env python3
"""
Export a route as a self-contained package for VR reconstruction.

Reads a route JSON (exported from the Route Builder in the viewer) and
data.json, produces a directory with:
  - route.json    all photo metadata, headings, and navigation links
  - {sequence}/   panorama images organized by sequence

The exported route.json follows the StreetViewData format:
  - GPS coordinates (snapped when available)
  - Baked heading adjustments (yaw/pitch/roll), 180° flip for reversed segments
  - GPS-derived travel bearing
  - Navigation: forward/back (within sequence), left/right (between sequences)

Prerequisites:
    Bake heading adjustments into data.json first:
        python script/import_heading_adjustments.py <keyframes.json>

Usage:
    python script/export_route_vr.py \\
        --route data/routes/routes-ginza-2026-02-20.json \\
        --output exports/ginza-route-1

    # Export a specific route (if the file has multiple)
    python script/export_route_vr.py \\
        --route data/routes/routes-ginza-2026-02-20.json \\
        --route-index 1 --output exports/ginza-route-2

    # Preview without writing
    python script/export_route_vr.py \\
        --route data/routes/routes-ginza-2026-02-20.json --dry-run

    # Export metadata only, skip photo copy
    python script/export_route_vr.py \\
        --route data/routes/routes-ginza-2026-02-20.json \\
        --output exports/ginza --no-copy

    # Point directly into a Unity project
    python script/export_route_vr.py \\
        --route data/routes/routes-ginza-2026-02-20.json \\
        --output ~/Unity/MyProject/Assets/StreamingAssets/routes/ginza/
"""

import argparse
import json
import math
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_JSON = BASE_DIR / "web" / "data.json"
FRAMES_DIR = BASE_DIR / "web" / "frames"


# ── Geometry helpers ────────────────────────────────────────────────────

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlon = math.radians(lon2 - lon1)
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    y = math.sin(dlon) * math.cos(lat2r)
    x = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


# ── Data loading ────────────────────────────────────────────────────────

def load_neighborhood_frames(data_json_path: str, neighborhood_id: str) -> List[Dict]:
    """Load frames for a neighborhood, preferring snapped coords (mirrors viewer.js init)."""
    with open(data_json_path) as f:
        data = json.load(f)

    frames = []
    for frame in data["frames"]:
        if frame.get("neighborhood") != neighborhood_id:
            continue
        f = dict(frame)
        if f.get("lat_snapped") is not None and f.get("lon_snapped") is not None:
            f["lat_original"] = f["lat"]
            f["lon_original"] = f["lon"]
            f["lat"] = f["lat_snapped"]
            f["lon"] = f["lon_snapped"]
        frames.append(f)
    return frames


# ── Heading resolution ──────────────────────────────────────────────────

def resolve_heading(frame: Dict, index: int, frames: List[Dict]) -> Dict:
    """Resolve yaw/pitch/roll for a frame.

    Mirrors viewer.js getFrameHeading():
      1. Frame has baked yaw/pitch/roll -> use them
      2. Walk backwards in same source for nearest keyframe -> inherit
      3. Default (0, 0, 0)
    """
    if frame.get("yaw") is not None or frame.get("pitch") is not None or frame.get("roll") is not None:
        return {
            "yaw": frame.get("yaw", 0),
            "pitch": frame.get("pitch", 0),
            "roll": frame.get("roll", 0),
        }

    source = frame["source"]
    for i in range(index - 1, -1, -1):
        if frames[i]["source"] != source:
            break
        f = frames[i]
        if f.get("yaw") is not None or f.get("pitch") is not None or f.get("roll") is not None:
            return {
                "yaw": f.get("yaw", 0),
                "pitch": f.get("pitch", 0),
                "roll": f.get("roll", 0),
            }

    return {"yaw": 0, "pitch": 0, "roll": 0}


# ── Bearing computation ────────────────────────────────────────────────

def compute_frame_bearings(frames: List[Dict]) -> Dict[int, float]:
    """GPS-derived travel bearing for each frame, grouped by source."""
    by_source: Dict[str, List[int]] = {}
    for i, f in enumerate(frames):
        by_source.setdefault(f["source"], []).append(i)

    bearings: Dict[int, float] = {}
    for indices in by_source.values():
        for j, idx in enumerate(indices):
            if j < len(indices) - 1:
                nxt = indices[j + 1]
                bearings[idx] = calculate_bearing(
                    frames[idx]["lat"], frames[idx]["lon"],
                    frames[nxt]["lat"], frames[nxt]["lon"],
                )
            elif j > 0:
                prv = indices[j - 1]
                bearings[idx] = calculate_bearing(
                    frames[prv]["lat"], frames[prv]["lon"],
                    frames[idx]["lat"], frames[idx]["lon"],
                )
            else:
                bearings[idx] = 0
    return bearings


# ── Route segmentation ─────────────────────────────────────────────────

def split_into_segments(route_indices: List[int], frames: List[Dict]) -> List[List[int]]:
    """Split route into segments where each is a contiguous run of one source."""
    if not route_indices:
        return []
    segments: List[List[int]] = []
    current = [route_indices[0]]
    for idx in route_indices[1:]:
        if frames[idx]["source"] == frames[current[0]]["source"]:
            current.append(idx)
        else:
            segments.append(current)
            current = [idx]
    segments.append(current)
    return segments


def is_reversed(segment_indices: List[int]) -> bool:
    if len(segment_indices) < 2:
        return False
    return segment_indices[1] < segment_indices[0]


def make_sequence_id(source: str, reversed_flag: bool, used_ids: set) -> str:
    direction = "rev" if reversed_flag else "fwd"
    base = f"{source}_{direction}"
    seq_id = base
    counter = 2
    while seq_id in used_ids:
        seq_id = f"{base}_{counter}"
        counter += 1
    used_ids.add(seq_id)
    return seq_id


# ── Junction direction ──────────────────────────────────────────────────

def compute_turn_direction(bearing_travel: float, bearing_to_target: float) -> str:
    """Determine if turning to a target is 'left' or 'right'."""
    diff = (bearing_to_target - bearing_travel + 540) % 360 - 180
    return "right" if diff >= 0 else "left"


# ── Main export ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Export a route as a self-contained VR package"
    )
    parser.add_argument("--route", type=str, required=True,
                        help="Route JSON file (exported from Route Builder)")
    parser.add_argument("--route-index", type=int, default=0,
                        help="Which route in the file (0-based, default: 0)")
    parser.add_argument("--data", type=str, default=str(DATA_JSON),
                        help=f"Path to data.json (default: web/data.json)")
    parser.add_argument("--frames-dir", type=str, default=str(FRAMES_DIR),
                        help=f"Path to frames directory (default: web/frames)")
    parser.add_argument("--output", type=str, default=None,
                        help="Output directory (default: exports/<route_id>)")
    parser.add_argument("--name", type=str, default=None,
                        help="Human-readable route name")
    parser.add_argument("--no-copy", action="store_true",
                        help="Generate route.json only, don't copy photos")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without writing anything")
    args = parser.parse_args()

    # ── Load route ──────────────────────────────────────────────────
    with open(args.route) as f:
        route_file = json.load(f)

    neighborhood_id = route_file["neighborhoodId"]
    routes = route_file.get("routes", [])

    if args.route_index >= len(routes):
        print(f"Error: --route-index {args.route_index} out of range "
              f"(file has {len(routes)} route(s))")
        sys.exit(1)

    route = routes[args.route_index]
    route_id = route.get("id", f"route_{args.route_index}")
    route_junctions = route.get("junctions", [])

    if args.output:
        output_dir = Path(args.output)
    else:
        output_dir = BASE_DIR / "exports" / route_id

    route_name = args.name or f"{neighborhood_id.title()} Route"

    # ── Load frame data ─────────────────────────────────────────────
    frames = load_neighborhood_frames(args.data, neighborhood_id)
    filename_lookup = {f["filename"]: i for i, f in enumerate(frames)}

    # Resolve route frames (filename-based or legacy index-based)
    if "frames" in route:
        route_indices = []
        missing = []
        for fn in route["frames"]:
            idx = filename_lookup.get(fn)
            if idx is not None:
                route_indices.append(idx)
            else:
                missing.append(fn)
        if missing:
            print(f"  Warning: {len(missing)} frame(s) not found in data.json")
            for fn in missing[:5]:
                print(f"    - {fn}")
        if not route_indices:
            print("Error: no valid frames found in route")
            sys.exit(1)
    elif "frameIndices" in route:
        route_indices = route["frameIndices"]
        invalid = [i for i in route_indices if i < 0 or i >= len(frames)]
        if invalid:
            print(f"  Error: {len(invalid)} frame indices out of range: {invalid[:5]}...")
            sys.exit(1)
    else:
        print("Error: route has neither 'frames' nor 'frameIndices'")
        sys.exit(1)

    print("=" * 60)
    print("Export Route for VR")
    print("=" * 60)
    print(f"  Route file:     {args.route}")
    print(f"  Route index:    {args.route_index} ({route_id})")
    print(f"  Route name:     {route_name}")
    print(f"  Neighborhood:   {neighborhood_id}")
    print(f"  Route frames:   {len(route_indices)}")
    print(f"  Output:         {output_dir}")
    if args.no_copy:
        print(f"  Photos:         skip (--no-copy)")
    if args.dry_run:
        print(f"  Mode:           DRY RUN")
    print("=" * 60)

    print(f"\n  {len(frames)} frames for '{neighborhood_id}'")

    bearings = compute_frame_bearings(frames)

    # ── Split into segments ──────────────────────────────────────────
    segments = split_into_segments(route_indices, frames)
    print(f"    {len(segments)} sequence(s) in route")

    # ── Build sequences ──────────────────────────────────────────────
    used_ids: set = set()
    sequences: Dict[str, Dict] = {}
    segment_meta: List[Dict] = []

    for seg_i, seg_indices in enumerate(segments):
        source = frames[seg_indices[0]]["source"]
        rev = is_reversed(seg_indices)
        seq_id = make_sequence_id(source, rev, used_ids)
        direction = "reverse" if rev else "forward"

        print(f"\n  Sequence {seg_i + 1}: {seq_id}")
        print(f"    Source: {source}, {len(seg_indices)} frames, {direction}")

        photos = []
        for pos, frame_idx in enumerate(seg_indices):
            frame = frames[frame_idx]
            heading = resolve_heading(frame, frame_idx, frames)

            yaw = heading["yaw"]
            if rev:
                yaw = (yaw + 180) % 360

            bearing = bearings.get(frame_idx, 0)
            if rev:
                bearing = (bearing + 180) % 360

            stem = Path(frame["filename"]).stem

            fwd_ref = None
            back_ref = None
            if pos < len(seg_indices) - 1:
                next_stem = Path(frames[seg_indices[pos + 1]]["filename"]).stem
                fwd_ref = f"{seq_id}/{next_stem}"
            if pos > 0:
                prev_stem = Path(frames[seg_indices[pos - 1]]["filename"]).stem
                back_ref = f"{seq_id}/{prev_stem}"

            photos.append({
                "filename": stem,
                "latitude": frame["lat"],
                "longitude": frame["lon"],
                "altitude": frame.get("alt", 0),
                "bearing": round(bearing, 1),
                "yaw": round(yaw, 1),
                "pitch": round(heading["pitch"], 1),
                "roll": round(heading["roll"], 1),
                "forward": fwd_ref,
                "back": back_ref,
                "left": None,
                "right": None,
            })

        sequences[seq_id] = {
            "name": f"Segment {seg_i + 1}",
            "source": source,
            "direction": direction,
            "photos": photos,
        }

        segment_meta.append({
            "seq_id": seq_id,
            "seg_indices": seg_indices,
            "reversed": rev,
        })

    # ── Junction links (left/right between sequences) ────────────────
    precomputed = {}
    for junc in route_junctions:
        if "fromFrame" in junc:
            fi = filename_lookup.get(junc["fromFrame"])
            ti = filename_lookup.get(junc["toFrame"])
            if fi is not None and ti is not None:
                precomputed[(fi, ti)] = junc
        elif "fromIndex" in junc:
            precomputed[(junc["fromIndex"], junc["toIndex"])] = junc

    print(f"\n  Computing junction links...")
    if route_junctions:
        print(f"    Using {len(route_junctions)} pre-computed junction(s) from Route Builder")

    junction_summary = []

    for i in range(len(segment_meta) - 1):
        seg_a = segment_meta[i]
        seg_b = segment_meta[i + 1]

        last_idx_a = seg_a["seg_indices"][-1]
        first_idx_b = seg_b["seg_indices"][0]

        frame_a = frames[last_idx_a]
        frame_b = frames[first_idx_b]

        # Use pre-computed junction data if available, otherwise compute
        pre = precomputed.get((last_idx_a, first_idx_b))
        if pre:
            turn_angle = pre["turnAngle"]
            from_bearing = pre["fromBearing"]
            to_bearing = pre["toBearing"]
        else:
            bearing_a = bearings.get(last_idx_a, 0)
            if seg_a["reversed"]:
                bearing_a = (bearing_a + 180) % 360

            bearing_b = bearings.get(first_idx_b, 0)
            if seg_b["reversed"]:
                bearing_b = (bearing_b + 180) % 360

            from_bearing = round(bearing_a, 1)
            to_bearing = round(bearing_b, 1)
            turn_angle = round((to_bearing - from_bearing + 540) % 360 - 180, 1)

        turn_dir = "right" if turn_angle >= 0 else "left"
        opposite_dir = "left" if turn_dir == "right" else "right"

        stem_a = Path(frame_a["filename"]).stem
        stem_b = Path(frame_b["filename"]).stem

        seq_a_photos = sequences[seg_a["seq_id"]]["photos"]
        seq_a_photos[-1][turn_dir] = f"{seg_b['seq_id']}/{stem_b}"
        seq_a_photos[-1]["turnAngle"] = turn_angle

        seq_b_photos = sequences[seg_b["seq_id"]]["photos"]
        seq_b_photos[0][opposite_dir] = f"{seg_a['seq_id']}/{stem_a}"
        seq_b_photos[0]["turnAngle"] = -turn_angle

        dist = haversine(frame_a["lat"], frame_a["lon"],
                         frame_b["lat"], frame_b["lon"])

        junction_summary.append({
            "from_sequence": seg_a["seq_id"],
            "to_sequence": seg_b["seq_id"],
            "from_frame": stem_a,
            "to_frame": stem_b,
            "turn_direction": turn_dir,
            "turn_angle": turn_angle,
            "from_bearing": from_bearing,
            "to_bearing": to_bearing,
            "distance_m": round(dist, 1),
            "source": "route_builder" if pre else "computed",
        })

        src_label = " (from Route Builder)" if pre else ""
        print(f"    Junction {i + 1}: {seg_a['seq_id']} —({turn_dir} {turn_angle:+.1f}°)→ {seg_b['seq_id']}  ({dist:.1f} m){src_label}")

    # ── Compute total distance ───────────────────────────────────────
    total_dist = 0.0
    for seg in segment_meta:
        indices = seg["seg_indices"]
        for j in range(len(indices) - 1):
            f1, f2 = frames[indices[j]], frames[indices[j + 1]]
            total_dist += haversine(f1["lat"], f1["lon"], f2["lat"], f2["lon"])

    # ── Build output JSON ────────────────────────────────────────────
    output_data = {
        "exported": datetime.now(timezone.utc).isoformat(),
        "route_name": route_name,
        "neighborhood": neighborhood_id,
        "route_id": route_id,
        "total_frames": len(route_indices),
        "total_sequences": len(sequences),
        "total_junctions": len(junction_summary),
        "total_distance_m": round(total_dist, 1),
        "junctions": junction_summary,
        "sequences": sequences,
    }

    print(f"\n  Route summary:")
    print(f"    Frames:      {len(route_indices)}")
    print(f"    Sequences:   {len(sequences)}")
    print(f"    Junctions:   {len(junction_summary)}")
    print(f"    Distance:    {total_dist:.0f} m")

    if args.dry_run:
        print(f"\n  Dry run — nothing written.")
        print(f"  Re-run without --dry-run to export.")
        return

    # ── Write route.json ─────────────────────────────────────────────
    output_dir.mkdir(parents=True, exist_ok=True)
    route_json_path = output_dir / "route.json"

    with open(route_json_path, "w") as f:
        json.dump(output_data, f, indent=2)
    print(f"\n  Wrote {route_json_path}")

    # ── Copy photos ──────────────────────────────────────────────────
    if not args.no_copy:
        frames_dir = Path(args.frames_dir)
        copied = 0
        missing = 0

        for seg in segment_meta:
            seq_id = seg["seq_id"]
            photo_dir = output_dir / seq_id
            photo_dir.mkdir(parents=True, exist_ok=True)

            for frame_idx in seg["seg_indices"]:
                frame = frames[frame_idx]
                src_path = frames_dir / frame["filename"]
                stem = Path(frame["filename"]).stem
                dst_path = photo_dir / f"{stem}.jpg"

                if src_path.exists():
                    shutil.copy2(src_path, dst_path)
                    copied += 1
                else:
                    missing += 1
                    if missing <= 3:
                        print(f"    Warning: missing {src_path}")

        if missing > 3:
            print(f"    ... and {missing - 3} more missing files")

        print(f"  Copied {copied} photos" + (f" ({missing} missing)" if missing else ""))

    # ── Summary ──────────────────────────────────────────────────────
    print(f"\n  Done! Output: {output_dir}")
    print(f"    route.json  ({len(route_indices)} frames, {len(sequences)} sequences)")
    if not args.no_copy:
        for seq_id, seq in sequences.items():
            print(f"    {seq_id}/  ({len(seq['photos'])} photos)")


if __name__ == "__main__":
    main()
