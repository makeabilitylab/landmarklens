#!/usr/bin/env python3
"""
Snap frame GPS coordinates to the road network using a local Valhalla instance.

Sends each neighborhood's frame coordinates as a pedestrian trace to Valhalla's
trace_attributes endpoint and writes the snapped positions back to data.json as
lat_snapped / lon_snapped (preserving the original lat / lon).

Prerequisites:
    # Download Kanto region OSM data and start Valhalla in Docker
    mkdir -p data/valhalla_data/custom_files
    wget https://download.geofabrik.de/asia/japan/kanto-latest.osm.pbf \
         -O data/valhalla_data/custom_files/kanto.osm.pbf
    docker run -p 8002:8002 \
         -v $PWD/data/valhalla_data/custom_files:/custom_files \
         ghcr.io/gis-ops/docker-valhalla/valhalla:latest

Usage:
    # Snap all neighborhoods
    python script/snap_gps.py

    # Snap a single neighborhood
    python script/snap_gps.py --neighborhood ginza

    # Tighter matching for dense grids (e.g. Ginza parallel streets)
    python script/snap_gps.py --neighborhood ginza --search-radius 20 --gps-accuracy 3

    # Reduce turn jitter along straight roads
    python script/snap_gps.py --turn-penalty 500

    # Snap a single video source
    python script/snap_gps.py --source GS010317

    # Dry-run (don't write data.json)
    python script/snap_gps.py --dry-run

    # Clear all snapped coordinates
    python script/snap_gps.py --clear
"""

import sys
import json
import math
import argparse
import urllib.request
import urllib.error
from pathlib import Path
from typing import List, Dict, Optional, Tuple


VALHALLA_DEFAULT_URL = "http://localhost:8002"
MAX_POINTS_PER_REQUEST = 2000


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def load_data(path: Path) -> Dict:
    if not path.exists():
        print(f"Error: {path} not found")
        sys.exit(1)
    with open(path, "r") as f:
        return json.load(f)


def group_frames_by_source(frames: List[Dict]) -> Dict[str, List[Dict]]:
    """Group frames by source video so each trace is a contiguous walk."""
    groups: Dict[str, List[Dict]] = {}
    for f in frames:
        groups.setdefault(f["source"], []).append(f)
    for src in groups:
        groups[src].sort(key=lambda f: f["index"])
    return groups


def calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Bearing in degrees (0-360) from point 1 to point 2."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    x = math.sin(dlam) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def call_valhalla(
    url: str,
    points: List[Dict],
    costing: str = "pedestrian",
    search_radius: Optional[float] = None,
    gps_accuracy: Optional[float] = None,
    turn_penalty: Optional[float] = None,
) -> Optional[List[Tuple[float, float]]]:
    """Send a trace_attributes request and return list of (lat, lon) snapped points."""
    shape = []
    for i, p in enumerate(points):
        pt: Dict = {"lat": p["lat"], "lon": p["lon"]}
        # Add bearing hints from consecutive points to help disambiguate parallel roads
        if i < len(points) - 1:
            bearing = calculate_bearing(p["lat"], p["lon"], points[i + 1]["lat"], points[i + 1]["lon"])
            pt["heading"] = round(bearing, 1)
            pt["heading_tolerance"] = 45
        shape.append(pt)

    payload: Dict = {
        "shape": shape,
        "costing": costing,
        "shape_match": "map_snap",
        "filters": {
            "attributes": [
                "matched.point",
                "matched.type",
                "matched.edge_index",
                "matched.distance_from_trace_point",
            ],
            "action": "include",
        },
    }

    trace_options: Dict = {}
    if search_radius is not None:
        trace_options["search_radius"] = search_radius
    if gps_accuracy is not None:
        trace_options["gps_accuracy"] = gps_accuracy
    if turn_penalty is not None:
        trace_options["turn_penalty_factor"] = turn_penalty
    if trace_options:
        payload["trace_options"] = trace_options

    endpoint = f"{url.rstrip('/')}/trace_attributes"
    req_data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=req_data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"    Valhalla HTTP {e.code}: {body[:300]}")
        return None
    except urllib.error.URLError as e:
        print(f"    Valhalla connection error: {e.reason}")
        return None

    matched = result.get("matched_points", [])
    if len(matched) != len(points):
        print(f"    Warning: sent {len(points)} points, got {len(matched)} matched points")

    snapped = []
    for mp in matched:
        lat = mp.get("lat")
        lon = mp.get("lon")
        if lat is not None and lon is not None:
            snapped.append((lat, lon))
        else:
            snapped.append(None)

    return snapped


def chunk_list(lst: List, size: int) -> List[List]:
    """Split a list into chunks with overlap of 1 for continuity."""
    if len(lst) <= size:
        return [lst]
    chunks = []
    i = 0
    while i < len(lst):
        chunks.append(lst[i : i + size])
        i += size - 1  # overlap by 1 for trace continuity
    return chunks


def snap_source(
    frames: List[Dict],
    valhalla_url: str,
    costing: str,
    search_radius: Optional[float] = None,
    gps_accuracy: Optional[float] = None,
    turn_penalty: Optional[float] = None,
) -> Tuple[int, int]:
    """Snap one source's frames. Returns (snapped_count, failed_count)."""
    snapped_count = 0
    failed_count = 0

    chunks = chunk_list(frames, MAX_POINTS_PER_REQUEST)

    for ci, chunk in enumerate(chunks):
        suffix = f" (chunk {ci + 1}/{len(chunks)})" if len(chunks) > 1 else ""
        print(f"    Sending {len(chunk)} points to Valhalla{suffix}...")

        result = call_valhalla(
            valhalla_url, chunk, costing=costing,
            search_radius=search_radius, gps_accuracy=gps_accuracy,
            turn_penalty=turn_penalty,
        )

        if result is None:
            failed_count += len(chunk)
            continue

        for i, frame in enumerate(chunk):
            if i < len(result) and result[i] is not None:
                lat_s, lon_s = result[i]
                frame["lat_snapped"] = round(lat_s, 7)
                frame["lon_snapped"] = round(lon_s, 7)
                snapped_count += 1
            else:
                failed_count += 1

    return snapped_count, failed_count


def clear_snapped(data: Dict) -> int:
    count = 0
    for frame in data["frames"]:
        if "lat_snapped" in frame or "lon_snapped" in frame:
            frame.pop("lat_snapped", None)
            frame.pop("lon_snapped", None)
            count += 1
    return count


def print_stats(data: Dict, frames: List[Dict]):
    """Print snapping statistics: average displacement, max displacement, etc."""
    displacements = []
    for f in frames:
        if "lat_snapped" in f and "lon_snapped" in f:
            d = haversine(f["lat"], f["lon"], f["lat_snapped"], f["lon_snapped"])
            displacements.append(d)

    if not displacements:
        return

    avg = sum(displacements) / len(displacements)
    mx = max(displacements)
    mn = min(displacements)
    over10 = sum(1 for d in displacements if d > 10)

    print(f"\n  Displacement stats ({len(displacements)} frames):")
    print(f"    Average: {avg:.2f} m")
    print(f"    Min:     {mn:.2f} m")
    print(f"    Max:     {mx:.2f} m")
    print(f"    > 10 m:  {over10} frames")


def main():
    parser = argparse.ArgumentParser(
        description="Snap frame GPS to roads via Valhalla map matching"
    )
    parser.add_argument(
        "--neighborhood", type=str,
        help="Filter by neighborhood (e.g. ginza, kinshicho, koenji, nezu)",
    )
    parser.add_argument(
        "--source", type=str,
        help="Filter by video source ID (e.g. GS010317)",
    )
    parser.add_argument(
        "--valhalla-url", type=str, default=VALHALLA_DEFAULT_URL,
        help=f"Valhalla server URL (default: {VALHALLA_DEFAULT_URL})",
    )
    parser.add_argument(
        "--costing", type=str, default="pedestrian",
        choices=["pedestrian", "bicycle", "auto"],
        help="Valhalla costing model (default: pedestrian)",
    )
    parser.add_argument(
        "--search-radius", type=float, default=None,
        help="Search radius in meters for road candidates (default: Valhalla's 50m). "
             "Lower values (e.g. 20) prevent matching to parallel streets in dense grids.",
    )
    parser.add_argument(
        "--gps-accuracy", type=float, default=None,
        help="GPS accuracy in meters (default: Valhalla's 4.07). "
             "Lower values trust the raw GPS more.",
    )
    parser.add_argument(
        "--turn-penalty", type=float, default=None,
        help="Turn penalty factor (default: Valhalla's ~0). "
             "Higher values (e.g. 500) reduce zigzag jitter on straight roads.",
    )
    parser.add_argument(
        "--data", type=str, default=None,
        help="Path to data.json (default: web/data.json)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview without writing to data.json",
    )
    parser.add_argument(
        "--clear", action="store_true",
        help="Remove all lat_snapped/lon_snapped from data.json and exit",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    data_path = Path(args.data) if args.data else repo_root / "web" / "data.json"

    print("=" * 60)
    print("Snap GPS → Valhalla Map Matching")
    print("=" * 60)
    print(f"  data.json:      {data_path}")
    print(f"  Valhalla URL:   {args.valhalla_url}")
    print(f"  Costing:        {args.costing}")
    print(f"  Search radius:  {args.search_radius or 'default (50m)'}")
    print(f"  GPS accuracy:   {args.gps_accuracy or 'default (4.07m)'}")
    print(f"  Turn penalty:   {args.turn_penalty or 'default'}")
    if args.neighborhood:
        print(f"  Neighborhood:   {args.neighborhood}")
    if args.source:
        print(f"  Source:          {args.source}")
    if args.dry_run:
        print(f"  Mode:           DRY RUN")
    if args.clear:
        print(f"  Mode:           CLEAR snapped coordinates")
    print("=" * 60)

    data = load_data(data_path)
    all_frames = data.get("frames", [])
    print(f"\nTotal frames in data.json: {len(all_frames)}")

    if args.clear:
        count = clear_snapped(data)
        if count == 0:
            print("No snapped coordinates to clear.")
            return
        if args.dry_run:
            print(f"[dry-run] Would clear snapped coords from {count} frames.")
        else:
            with open(data_path, "w") as f:
                json.dump(data, f, indent=2)
            print(f"✓ Cleared snapped coordinates from {count} frames.")
        return

    # Filter
    frames = all_frames
    if args.neighborhood:
        frames = [f for f in frames if f.get("neighborhood") == args.neighborhood]
    if args.source:
        frames = [f for f in frames if f.get("source") == args.source]

    print(f"Frames to snap: {len(frames)}")
    if not frames:
        print("No frames found matching criteria.")
        sys.exit(1)

    # Group by source so each trace is one continuous walk
    by_source = group_frames_by_source(frames)
    print(f"Sources: {', '.join(sorted(by_source.keys()))}\n")

    total_snapped = 0
    total_failed = 0

    for source, src_frames in sorted(by_source.items()):
        neighborhood = src_frames[0].get("neighborhood", "?")
        print(f"  [{neighborhood}] {source}: {len(src_frames)} frames")
        snapped, failed = snap_source(
            src_frames, args.valhalla_url, args.costing,
            search_radius=args.search_radius,
            gps_accuracy=args.gps_accuracy,
            turn_penalty=args.turn_penalty,
        )
        total_snapped += snapped
        total_failed += failed
        print(f"    ✓ {snapped} snapped, {failed} failed")

    print(f"\nTotal: {total_snapped} snapped, {total_failed} failed")

    print_stats(data, frames)

    if total_snapped == 0:
        print("\nNo points were snapped — nothing to write.")
        return

    if args.dry_run:
        print(f"\nDry run complete. Re-run without --dry-run to write changes.")
    else:
        with open(data_path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"\n✓ Wrote lat_snapped/lon_snapped to {data_path}")


if __name__ == "__main__":
    main()
