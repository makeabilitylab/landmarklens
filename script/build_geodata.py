#!/usr/bin/env python3
"""
Build GeoJSON geodata from data.json and route.json files.

Generates:
  geodata/panos.geojson   — Point features, one per panorama frame
  geodata/routes.geojson  — LineString features, one per route

Re-run after updating data.json (e.g. after QGIS import + redistribute)
to keep geodata in sync.

Usage:
  python script/build_geodata.py
"""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_JSON = REPO_ROOT / "web" / "data.json"
ROUTES_DIR = REPO_ROOT / "study-materials" / "ParticipantSets"
SKIP_PARTICIPANTS = {"P00"}
OUTPUT_DIR = REPO_ROOT / "geodata"

TURN_WARNING_STEPS = 2


def frame_id_from_filename(filename):
    """'GS010317/GS010317_0004.jpg' -> 'GS010317_0004'"""
    return Path(filename).stem


def build_panos_lookup(data):
    """Build frame_id -> snapped coordinate lookup from data.json."""
    lookup = {}
    for frame in data["frames"]:
        fid = frame_id_from_filename(frame["filename"])
        lookup[fid] = {
            "lat": frame.get("lat_snapped") or frame["lat"],
            "lon": frame.get("lon_snapped") or frame["lon"],
        }
    return lookup


def build_panos_geojson(data, used_frame_ids):
    """Build Panos GeoJSON (Point features) from data.json.

    Only includes frames that appear in at least one route.
    """
    features = []
    for frame in data["frames"]:
        fid = frame_id_from_filename(frame["filename"])
        if fid not in used_frame_ids:
            continue
        lat = frame.get("lat_snapped") or frame["lat"]
        lon = frame.get("lon_snapped") or frame["lon"]

        props = {
            "frame_id": fid,
            "source": frame["source"],
            "neighborhood": frame["neighborhood"],
            "lat": lat,
            "lon": lon,
            "lat_original": frame["lat"],
            "lon_original": frame["lon"],
            "alt": frame.get("alt"),
            "heading": frame.get("heading"),
            "yaw": frame.get("yaw"),
            "pitch": frame.get("pitch"),
            "roll": frame.get("roll"),
            "distance_to_next": frame.get("distance_to_next"),
        }

        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": props,
            }
        )

    return {"type": "FeatureCollection", "features": features}


def collect_unique_routes():
    """Scan all route.json files, deduplicate by folder name (e.g. ginza_c).

    Skips participants in SKIP_PARTICIPANTS (e.g. P00 pilot data).
    """
    routes = {}
    for participant_dir in sorted(ROUTES_DIR.iterdir()):
        if not participant_dir.is_dir():
            continue
        if participant_dir.name in SKIP_PARTICIPANTS:
            continue
        routes_dir = participant_dir / "Routes"
        if not routes_dir.exists():
            continue
        for route_dir in sorted(routes_dir.iterdir()):
            route_file = route_dir / "route.json"
            if not route_file.exists():
                continue
            route_folder = route_dir.name
            if route_folder not in routes:
                with open(route_file) as f:
                    routes[route_folder] = json.load(f)
    return routes


def build_routes_geojson(route_dict, panos_lookup):
    """Build Routes GeoJSON (LineString features) from route.json files.

    Coordinates are sourced from data.json (via panos_lookup) so that
    re-running after a GPS update produces correct geometry.
    """
    features = []

    for route_id, rdata in sorted(route_dict.items()):
        junction_exit_frames = {}
        for junc in rdata.get("junctions", []):
            junction_exit_frames[junc["from_frame"]] = junc

        junction_entry_frames = {}
        for junc in rdata.get("junctions", []):
            junction_entry_frames[junc["to_frame"]] = junc

        ordered_frames = []
        coordinates = []
        seg_num = 0

        for seq_id, seq in rdata["sequences"].items():
            seg_num += 1
            for photo in seq["photos"]:
                fid = photo["filename"]

                if fid in panos_lookup:
                    lat = panos_lookup[fid]["lat"]
                    lon = panos_lookup[fid]["lon"]
                else:
                    lat = photo["latitude"]
                    lon = photo["longitude"]

                is_exit = fid in junction_exit_frames
                is_entry = fid in junction_entry_frames

                turn_dir = None
                turn_ang = None
                if is_exit:
                    turn_dir = junction_exit_frames[fid]["turn_direction"]
                    turn_ang = junction_exit_frames[fid]["turn_angle"]
                elif is_entry:
                    turn_dir = junction_entry_frames[fid]["turn_direction"]
                    turn_ang = junction_entry_frames[fid]["turn_angle"]

                ordered_frames.append(
                    {
                        "frame_id": fid,
                        "sequence_id": seq_id,
                        "direction": seq["direction"],
                        "segment_number": seg_num,
                        "frame_order": len(ordered_frames),
                        "is_junction": is_exit or is_entry,
                        "turn_direction": turn_dir,
                        "turn_angle": turn_ang,
                        "is_turn_warning": False,
                        "turn_warning_direction": None,
                        "turn_warning_angle": None,
                    }
                )
                coordinates.append([lon, lat])

        # Mark turn-warning frames: N steps before each junction exit frame
        for i, fr in enumerate(ordered_frames):
            if fr["frame_id"] in junction_exit_frames:
                junc = junction_exit_frames[fr["frame_id"]]
                for offset in range(1, TURN_WARNING_STEPS + 1):
                    wi = i - offset
                    if wi >= 0 and not ordered_frames[wi]["is_junction"]:
                        ordered_frames[wi]["is_turn_warning"] = True
                        ordered_frames[wi]["turn_warning_direction"] = junc[
                            "turn_direction"
                        ]
                        ordered_frames[wi]["turn_warning_angle"] = junc["turn_angle"]

        props = {
            "route_id": route_id,
            "neighborhood": rdata.get("neighborhood"),
            "total_frames": rdata.get("total_frames"),
            "total_sequences": rdata.get("total_sequences"),
            "total_junctions": rdata.get("total_junctions"),
            "total_distance_m": rdata.get("total_distance_m"),
            "frames": ordered_frames,
        }

        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coordinates},
                "properties": props,
            }
        )

    return {"type": "FeatureCollection", "features": features}


def main():
    print("Loading data.json...")
    with open(DATA_JSON) as f:
        data = json.load(f)

    panos_lookup = build_panos_lookup(data)

    print("Collecting route files...")
    route_dict = collect_unique_routes()
    print(f"Building Routes ({len(route_dict)} unique routes)...")
    routes = build_routes_geojson(route_dict, panos_lookup)

    used_frame_ids = {
        fr["frame_id"]
        for feat in routes["features"]
        for fr in feat["properties"]["frames"]
    }

    print(f"Building Panos ({len(used_frame_ids)} frames used in routes, "
          f"{len(data['frames'])} total in data.json)...")
    panos = build_panos_geojson(data, used_frame_ids)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    panos_path = OUTPUT_DIR / "panos.geojson"
    with open(panos_path, "w") as f:
        json.dump(panos, f)
    print(f"  -> {panos_path.relative_to(REPO_ROOT)} ({len(panos['features'])} features)")

    routes_path = OUTPUT_DIR / "routes.geojson"
    with open(routes_path, "w") as f:
        json.dump(routes, f)
    print(f"  -> {routes_path.relative_to(REPO_ROOT)} ({len(routes['features'])} features)")

    # Summary
    total_route_frames = sum(
        len(f["properties"]["frames"]) for f in routes["features"]
    )
    junction_count = sum(
        1
        for f in routes["features"]
        for fr in f["properties"]["frames"]
        if fr["is_junction"]
    )
    warning_count = sum(
        1
        for f in routes["features"]
        for fr in f["properties"]["frames"]
        if fr["is_turn_warning"]
    )
    print(f"\nSummary:")
    print(f"  Panos:          {len(panos['features'])} unique frames")
    print(f"  Routes:         {len(routes['features'])} routes")
    print(f"  Route-frames:   {total_route_frames} frame appearances across routes")
    print(f"  Junctions:      {junction_count} junction frames")
    print(f"  Turn warnings:  {warning_count} warning frames")


if __name__ == "__main__":
    main()
