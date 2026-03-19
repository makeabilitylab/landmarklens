"""
Export a route + VLM detections as a VR-ready folder for the Unity app.

Reads from geodata/panos.geojson and geodata/routes.geojson to produce the
same folder structure as script/export_route_vr.py:

    <output>/
        route.json          Unity-compatible route with sequences + nav links
        detections.json     VLM bounding-box detections per frame (optional)
        GS010417_fwd/       sequence folders with panorama images
            GS010417_0000.jpg
            ...

Usage:
    # Export route with images
    python vlm/export_for_vr.py ningyocho_r1

    # Include VLM detections
    python vlm/export_for_vr.py ningyocho_r1 \\
        --detections vlm/output/ningyocho_r1_deduped.json

    # Custom output directory
    python vlm/export_for_vr.py ningyocho_r1 \\
        --output study-materials/ParticipantSets/P00/Routes/ningyocho_r1

    # Skip image copy (metadata only)
    python vlm/export_for_vr.py ningyocho_r1 --no-copy
"""

import argparse
import json
import math
import shutil
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ROUTES_GEOJSON = BASE_DIR / "geodata" / "routes.geojson"
PANOS_GEOJSON = BASE_DIR / "geodata" / "panos.geojson"
FRAMES_DIR = BASE_DIR / "data" / "frames"


def haversine(lat1, lon1, lat2, lon2):
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def calculate_bearing(lat1, lon1, lat2, lon2):
    dlon = math.radians(lon2 - lon1)
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    y = math.sin(dlon) * math.cos(lat2r)
    x = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def load_pano_lookup():
    """Build {frame_id: properties} from panos.geojson."""
    with open(PANOS_GEOJSON) as f:
        data = json.load(f)
    return {
        feat["properties"]["frame_id"]: feat["properties"]
        for feat in data["features"]
    }


def load_route(route_id):
    """Load a route from routes.geojson."""
    with open(ROUTES_GEOJSON) as f:
        data = json.load(f)
    for feat in data["features"]:
        if feat["properties"]["route_id"] == route_id:
            return feat["properties"]
    available = [f["properties"]["route_id"] for f in data["features"]]
    raise ValueError(f"Route '{route_id}' not found. Available: {available}")


def load_detections(detections_path, valid_only=False):
    """Flatten VLM detections JSON into {frame_id: [detections]}.

    If valid_only is True, only include detections where is_valid == True
    (for second-pass validated files).
    """
    with open(detections_path) as f:
        data = json.load(f)
    result = {}
    for batch in data.get("batches", {}).values():
        if batch.get("status") != "completed":
            continue
        for frame_id, dets in batch.get("results", {}).items():
            if not dets:
                continue
            dets = [d for d in dets if not d.get("dedup_removed", False)]
            if valid_only:
                dets = [d for d in dets if d.get("is_valid", False)]
            if dets:
                result[frame_id] = dets
    return result


def compute_turn_angle_map(route):
    """Build {frame_id: turn_angle} for junction frames preceded by a turn warning."""
    frames = route["frames"]
    by_seg = {}
    for fr in frames:
        by_seg.setdefault(fr["segment_number"], []).append(fr)
    for seg_num in by_seg:
        by_seg[seg_num].sort(key=lambda f: f["frame_order"])

    ta_map = {}
    for seg_frames in by_seg.values():
        for i, fr in enumerate(seg_frames):
            prev_warned = i > 0 and seg_frames[i - 1].get("is_turn_warning", False)
            if prev_warned:
                ta = fr.get("turn_angle", 0.0) or 0.0
                if ta != 0.0:
                    ta_map[fr["frame_id"]] = ta
    return ta_map


def apply_turn_angle_correction(det_map, ta_map):
    """Shift bbox x-coordinates by turn_angle for junction frames.

    Use this when bboxes were generated in (yaw+turn_angle)-rotated space
    but need to be in yaw-only space for VR display.
    """
    corrected = 0
    for frame_id, angle in ta_map.items():
        if frame_id not in det_map:
            continue
        shift = round(angle / 360.0 * 1000)
        for det in det_map[frame_id]:
            bbox = det["bbox"]
            bbox[1] = (bbox[1] + shift) % 1000
            bbox[3] = (bbox[3] + shift) % 1000
        corrected += 1
    if corrected:
        print(f"Applied turn_angle correction to {corrected} junction frame(s)")
    return det_map


def export_route(route_id, output_dir, frames_dir, detections_path=None,
                 valid_only=False, fix_turn_angle=False, no_copy=False):
    route = load_route(route_id)
    pano_lookup = load_pano_lookup()
    frames = route["frames"]
    neighborhood = route.get("neighborhood", "")

    # Group frames by segment_number
    segments = {}
    for fr in frames:
        seg_num = fr["segment_number"]
        segments.setdefault(seg_num, []).append(fr)
    for seg_num in segments:
        segments[seg_num].sort(key=lambda f: f["frame_order"])

    # Build sequences (matching Unity route.json format)
    sequences = {}
    segment_order = sorted(segments.keys())
    seg_meta = []

    for seg_num in segment_order:
        seg_frames = segments[seg_num]
        first_frame = seg_frames[0]

        source = first_frame["frame_id"].rsplit("_", 1)[0]
        direction = first_frame.get("direction", "forward")
        is_rev = direction == "reverse"
        suffix = "rev" if is_rev else "fwd"
        seq_id = f"{source}_{suffix}"

        # Compute bearing for each frame from GPS
        photos = []
        for i, fr in enumerate(seg_frames):
            fid = fr["frame_id"]
            pano = pano_lookup.get(fid, {})

            lat = pano.get("lat", 0)
            lon = pano.get("lon", 0)

            # Travel bearing from GPS
            if i < len(seg_frames) - 1:
                next_pano = pano_lookup.get(seg_frames[i + 1]["frame_id"], {})
                bearing = calculate_bearing(lat, lon,
                                            next_pano.get("lat", lat),
                                            next_pano.get("lon", lon))
            elif i > 0:
                prev_pano = pano_lookup.get(seg_frames[i - 1]["frame_id"], {})
                bearing = calculate_bearing(prev_pano.get("lat", lat),
                                            prev_pano.get("lon", lon),
                                            lat, lon)
            else:
                bearing = 0

            if is_rev:
                bearing = (bearing + 180) % 360

            yaw = pano.get("yaw") or 0
            pitch = pano.get("pitch") or 0
            roll = pano.get("roll") or 0
            if is_rev:
                yaw = (yaw + 180) % 360

            # Navigation links within segment
            fwd_ref = None
            back_ref = None
            if i < len(seg_frames) - 1:
                next_fid = seg_frames[i + 1]["frame_id"]
                fwd_ref = f"{seq_id}/{next_fid}"
            if i > 0:
                prev_fid = seg_frames[i - 1]["frame_id"]
                back_ref = f"{seq_id}/{prev_fid}"

            photos.append({
                "filename": fid,
                "latitude": lat,
                "longitude": lon,
                "altitude": pano.get("alt", 0),
                "bearing": round(bearing, 1),
                "yaw": round(yaw, 1),
                "pitch": round(pitch, 1),
                "roll": round(roll, 1),
                "forward": fwd_ref,
                "back": back_ref,
                "left": None,
                "right": None,
            })

        sequences[seq_id] = {
            "name": f"Segment {seg_num}",
            "source": source,
            "direction": direction,
            "photos": photos,
        }
        seg_meta.append({
            "seg_num": seg_num,
            "seq_id": seq_id,
            "frames": seg_frames,
        })

    # Wire up junction links (left/right between segments)
    junction_summary = []
    for i in range(len(seg_meta) - 1):
        seg_a = seg_meta[i]
        seg_b = seg_meta[i + 1]

        last_frame_a = seg_a["frames"][-1]
        first_frame_b = seg_b["frames"][0]

        turn_angle = last_frame_a.get("turn_angle") or 0
        turn_dir = "right" if turn_angle >= 0 else "left"
        opposite = "left" if turn_dir == "right" else "right"

        last_fid = last_frame_a["frame_id"]
        first_fid = first_frame_b["frame_id"]

        seq_a_photos = sequences[seg_a["seq_id"]]["photos"]
        seq_a_photos[-1][turn_dir] = f"{seg_b['seq_id']}/{first_fid}"
        seq_a_photos[-1]["turnAngle"] = turn_angle

        seq_b_photos = sequences[seg_b["seq_id"]]["photos"]
        seq_b_photos[0][opposite] = f"{seg_a['seq_id']}/{last_fid}"
        seq_b_photos[0]["turnAngle"] = -turn_angle

        pano_a = pano_lookup.get(last_fid, {})
        pano_b = pano_lookup.get(first_fid, {})
        dist = haversine(
            pano_a.get("lat", 0), pano_a.get("lon", 0),
            pano_b.get("lat", 0), pano_b.get("lon", 0),
        )

        junction_summary.append({
            "from_sequence": seg_a["seq_id"],
            "to_sequence": seg_b["seq_id"],
            "from_frame": last_fid,
            "to_frame": first_fid,
            "turn_direction": turn_dir,
            "turn_angle": turn_angle,
            "distance_m": round(dist, 1),
        })

    # Total distance
    all_frame_ids = [fr["frame_id"] for fr in frames]
    total_dist = 0
    for j in range(len(all_frame_ids) - 1):
        p1 = pano_lookup.get(all_frame_ids[j], {})
        p2 = pano_lookup.get(all_frame_ids[j + 1], {})
        total_dist += haversine(
            p1.get("lat", 0), p1.get("lon", 0),
            p2.get("lat", 0), p2.get("lon", 0),
        )

    # Build output
    output_data = {
        "exported": datetime.now(timezone.utc).isoformat(),
        "route_name": f"{neighborhood.title()} {route_id.split('_')[-1].upper()}",
        "neighborhood": neighborhood,
        "route_id": route_id,
        "total_frames": len(frames),
        "total_sequences": len(sequences),
        "total_junctions": len(junction_summary),
        "total_distance_m": round(total_dist, 1),
        "junctions": junction_summary,
        "sequences": sequences,
    }

    # Write
    output_dir.mkdir(parents=True, exist_ok=True)

    route_json_path = output_dir / "route.json"
    with open(route_json_path, "w") as f:
        json.dump(output_data, f, indent=2)
    print(f"Wrote {route_json_path}")

    # Detections
    if detections_path:
        det_map = load_detections(detections_path, valid_only=valid_only)
        if fix_turn_angle:
            ta_map = compute_turn_angle_map(route)
            det_map = apply_turn_angle_correction(det_map, ta_map)
        det_json_path = output_dir / "detections.json"
        with open(det_json_path, "w") as f:
            json.dump(det_map, f, indent=2)
        det_count = sum(len(v) for v in det_map.values())
        print(f"Wrote {det_json_path} ({det_count} detections across {len(det_map)} frames)")

    # Copy images
    if not no_copy:
        copied = 0
        missing = 0
        for seg in seg_meta:
            seq_id = seg["seq_id"]
            photo_dir = output_dir / seq_id
            photo_dir.mkdir(parents=True, exist_ok=True)

            for fr in seg["frames"]:
                fid = fr["frame_id"]
                source = fid.rsplit("_", 1)[0]
                src_path = frames_dir / source / f"{fid}.jpg"
                dst_path = photo_dir / f"{fid}.jpg"

                if src_path.exists():
                    shutil.copy2(src_path, dst_path)
                    copied += 1
                else:
                    missing += 1
                    if missing <= 3:
                        print(f"  Warning: missing {src_path}")

        print(f"Copied {copied} photos" + (f" ({missing} missing)" if missing else ""))

    # Summary
    print(f"\nRoute: {route_id}")
    print(f"  {len(frames)} frames, {len(sequences)} sequences, {len(junction_summary)} junctions")
    print(f"  Distance: {total_dist:.0f} m")
    print(f"  Output: {output_dir}")
    for seq_id, seq in sequences.items():
        print(f"    {seq_id}/  ({len(seq['photos'])} photos)")
    for junc in junction_summary:
        print(f"    Junction: {junc['from_sequence']} —({junc['turn_direction']} {junc['turn_angle']:+.1f}°)→ {junc['to_sequence']}")


def main():
    parser = argparse.ArgumentParser(description="Export route + detections for VR headset")
    parser.add_argument("route_id", help="Route ID (e.g. ningyocho_r1)")
    parser.add_argument("--output", "-o", default=None,
                        help="Output directory (default: study-materials/ParticipantSets/P00/Routes/<route_id>)")
    parser.add_argument("--detections", default=None,
                        help="VLM detections JSON (from run_gemini.py or dedup)")
    parser.add_argument("--frames-dir", default=str(FRAMES_DIR),
                        help="Panorama frames directory")
    parser.add_argument("--valid-only", action="store_true",
                        help="Only include detections where is_valid == True (second-pass)")
    parser.add_argument("--fix-turn-angle", action="store_true",
                        help="Shift junction bbox x-coords by turn_angle (for old pipeline output)")
    parser.add_argument("--no-copy", action="store_true",
                        help="Skip image copy (metadata only)")
    args = parser.parse_args()

    if args.output:
        output_dir = Path(args.output)
    else:
        output_dir = BASE_DIR / "study-materials" / "EvaluationSets" / "P00" / "Routes" / args.route_id

    export_route(
        args.route_id,
        output_dir,
        Path(args.frames_dir),
        detections_path=args.detections,
        valid_only=args.valid_only,
        fix_turn_angle=args.fix_turn_angle,
        no_copy=args.no_copy,
    )


if __name__ == "__main__":
    main()
