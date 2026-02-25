#!/usr/bin/env python3
"""
Redistribute frame GPS points evenly along a Valhalla-computed route.

For sources where map-matching produced erratic results, this script:
  1. Takes the start and end points of a source (or custom ones you provide)
  2. Auto-samples intermediate waypoints from existing frame coordinates
     (every N frames) to guide Valhalla along the actual walked path
  3. Asks Valhalla for the pedestrian route through all waypoints
  4. Evenly spaces N points (one per frame) along the route shape
  5. Writes lat_snapped / lon_snapped back to data.json

Usage:
    # Redistribute a single source (auto-samples via points every 10 frames)
    python script/redistribute_gps.py --source GS010320

    # Tighter adherence to the original trace (via point every 5 frames)
    python script/redistribute_gps.py --source GS010320 --via-every 5

    # Disable auto via-points (old behavior: start/end only)
    python script/redistribute_gps.py --source GS010320 --no-auto-via

    # Use custom start/end points (lat,lon)
    python script/redistribute_gps.py --source GS010320 \
        --start 35.6712,139.7650 --end 35.6745,139.7680

    # Add manual intermediate waypoints (disables auto-sampling)
    python script/redistribute_gps.py --source GS010320 \
        --via 35.6725,139.7660 --via 35.6735,139.7670

    # Dry-run
    python script/redistribute_gps.py --source GS010320 --dry-run

    # Partial redistribute: only frames between two keyframes
    # (e.g. fix an intersection point then redistribute from there to end)
    python script/redistribute_gps.py --source GS010355 \
        --from-frame GS010355_0199 --to-frame GS010355_0272 \
        --start 35.7234575,139.7627354 --straight

    # Partial redistribute: fix a middle segment, leave both ends untouched
    python script/redistribute_gps.py --source GS010355 \
        --from-frame GS010355_0050 --to-frame GS010355_0120

    # List all sources with their frame counts and endpoints
    python script/redistribute_gps.py --list
    python script/redistribute_gps.py --list --neighborhood ginza
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
DEFAULT_VIA_EVERY = 10


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def decode_polyline(encoded: str, precision: int = 6) -> List[Tuple[float, float]]:
    """Decode a Valhalla encoded polyline (precision 6) into (lat, lon) pairs."""
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
    """Return cumulative distance along a polyline."""
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
    """Interpolate a point at a given cumulative distance along the polyline."""
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
    shape: List[Tuple[float, float]],
    n: int,
) -> List[Tuple[float, float]]:
    """Distribute n points evenly along a polyline shape."""
    if n <= 0:
        return []
    if n == 1:
        return [shape[0]]

    cum = cumulative_distances(shape)
    total = cum[-1]
    spacing = total / (n - 1)

    result = []
    for i in range(n):
        target = i * spacing
        pt = interpolate_along(shape, cum, target)
        result.append(pt)
    return result


def sample_via_points(
    frames: List[Dict],
    every: int = DEFAULT_VIA_EVERY,
) -> List[Tuple[float, float]]:
    """Sample intermediate frames as via waypoints to guide the Valhalla route.

    Uses snapped coordinates when available, otherwise falls back to original GPS.
    Skips the first and last frame (those become the start/end of the route).
    """
    if len(frames) <= 2 or every < 1:
        return []

    via = []
    for i in range(every, len(frames) - 1, every):
        f = frames[i]
        lat = f.get("lat_snapped", f["lat"])
        lon = f.get("lon_snapped", f["lon"])
        via.append((lat, lon))
    return via


def get_route(
    url: str,
    start: Tuple[float, float],
    end: Tuple[float, float],
    via: Optional[List[Tuple[float, float]]] = None,
    costing: str = "pedestrian",
) -> Optional[List[Tuple[float, float]]]:
    """Get a route from Valhalla and return the decoded shape."""
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
        endpoint,
        data=req_data,
        headers={"Content-Type": "application/json"},
        method="POST",
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
        legs = result["trip"]["legs"]
        all_points = []
        for leg in legs:
            decoded = decode_polyline(leg["shape"])
            if all_points:
                decoded = decoded[1:]
            all_points.extend(decoded)
        return all_points
    except (KeyError, IndexError) as e:
        print(f"  Failed to parse route response: {e}")
        return None


def load_data(path: Path) -> Dict:
    if not path.exists():
        print(f"Error: {path} not found")
        sys.exit(1)
    with open(path, "r") as f:
        return json.load(f)


def parse_latlon(s: str) -> Tuple[float, float]:
    parts = s.split(",")
    if len(parts) != 2:
        raise ValueError(f"Expected lat,lon but got: {s}")
    return (float(parts[0]), float(parts[1]))


def resolve_frame_name(name: str, source: str) -> str:
    """Normalize a frame name to the filename format used in data.json.

    Accepts: 'GS010355_0199', 'GS010355_0199.jpg', or 'GS010355/GS010355_0199.jpg'
    Returns: 'GS010355/GS010355_0199.jpg'
    """
    name = name.strip()
    if "/" in name:
        return name
    if not name.endswith(".jpg"):
        name += ".jpg"
    return f"{source}/{name}"


def list_sources(data: Dict, neighborhood: Optional[str] = None):
    frames = data.get("frames", [])
    if neighborhood:
        frames = [f for f in frames if f.get("neighborhood") == neighborhood]

    by_source: Dict[str, List[Dict]] = {}
    for f in frames:
        by_source.setdefault(f["source"], []).append(f)

    print(f"\n{'Source':<14} {'Hood':<12} {'Frames':>6}  {'Start':>24}  {'End':>24}")
    print("-" * 90)
    for source in sorted(by_source):
        src_frames = sorted(by_source[source], key=lambda f: f["index"])
        hood = src_frames[0].get("neighborhood", "?")
        first = src_frames[0]
        last = src_frames[-1]
        has_snap = any("lat_snapped" in f for f in src_frames)
        marker = " *" if has_snap else ""
        print(
            f"{source:<14} {hood:<12} {len(src_frames):>6}  "
            f"{first['lat']:>11.7f},{first['lon']:>12.7f}  "
            f"{last['lat']:>11.7f},{last['lon']:>12.7f}{marker}"
        )
    print(f"\n  * = has snapped coordinates")
    print(f"  Total: {len(by_source)} sources, {len(frames)} frames")


def main():
    parser = argparse.ArgumentParser(
        description="Redistribute frame points evenly along a Valhalla route"
    )
    parser.add_argument(
        "--source", type=str,
        help="Video source ID to redistribute (e.g. GS010320)",
    )
    parser.add_argument(
        "--start", type=str, default=None,
        help="Custom start point as lat,lon (default: first frame of source)",
    )
    parser.add_argument(
        "--end", type=str, default=None,
        help="Custom end point as lat,lon (default: last frame of source)",
    )
    parser.add_argument(
        "--via", type=str, action="append", default=[],
        help="Intermediate waypoint as lat,lon (can repeat for multiple)",
    )
    parser.add_argument(
        "--valhalla-url", type=str, default=VALHALLA_DEFAULT_URL,
        help=f"Valhalla server URL (default: {VALHALLA_DEFAULT_URL})",
    )
    parser.add_argument(
        "--costing", type=str, default="pedestrian",
        choices=["pedestrian", "bicycle", "auto"],
    )
    parser.add_argument(
        "--data", type=str, default=None,
        help="Path to data.json (default: web/data.json)",
    )
    parser.add_argument(
        "--via-every", type=int, default=DEFAULT_VIA_EVERY,
        help=f"Auto-sample a via waypoint every N frames to guide the route along "
             f"the actual walked path (default: {DEFAULT_VIA_EVERY}). "
             f"Lower values = more waypoints = tighter adherence to the original trace.",
    )
    parser.add_argument(
        "--no-auto-via", action="store_true",
        help="Disable automatic via-point sampling. Only use start/end (and any manual --via).",
    )
    parser.add_argument(
        "--straight", action="store_true",
        help="Linearly interpolate between start/end/via instead of using Valhalla routing. "
             "Use for roads that Valhalla routes incorrectly.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview without writing to data.json",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List all sources with their endpoints and exit",
    )
    parser.add_argument(
        "--neighborhood", type=str, default=None,
        help="Filter by neighborhood (used with --list)",
    )
    parser.add_argument(
        "--from-frame", type=str, default=None,
        help="Start of sub-range: frame name (e.g. GS010355_0199). "
             "Only frames from this one onward are redistributed; earlier frames stay untouched.",
    )
    parser.add_argument(
        "--to-frame", type=str, default=None,
        help="End of sub-range: frame name (e.g. GS010355_0272). "
             "Only frames up to and including this one are redistributed; later frames stay untouched.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    data_path = Path(args.data) if args.data else repo_root / "web" / "data.json"
    data = load_data(data_path)

    if args.list:
        list_sources(data, neighborhood=args.neighborhood)
        return

    if not args.source and not args.neighborhood:
        print("Error: --source or --neighborhood is required (or use --list)")
        sys.exit(1)

    all_frames = data.get("frames", [])

    # Build list of sources to process
    if args.source:
        sources_to_process = [args.source]
    else:
        # Neighborhood mode: find sources where first or last frame has snapped coords
        hood_frames = [f for f in all_frames if f.get("neighborhood") == args.neighborhood]
        by_source: Dict[str, List[Dict]] = {}
        for f in hood_frames:
            by_source.setdefault(f["source"], []).append(f)

        sources_to_process = []
        for source, sframes in sorted(by_source.items()):
            sframes.sort(key=lambda f: f["index"])
            first_snapped = "lat_snapped" in sframes[0]
            last_snapped = "lat_snapped" in sframes[-1]
            if first_snapped or last_snapped:
                sources_to_process.append(source)

        if not sources_to_process:
            print(f"No sources in '{args.neighborhood}' have snapped start/end points.")
            print("Import edited points first (python script/import_gps.py ...)")
            sys.exit(1)

        print("=" * 60)
        print("Redistribute GPS Points Along Route")
        print("=" * 60)
        print(f"  Neighborhood:  {args.neighborhood}")
        print(f"  Sources:       {len(sources_to_process)} with snapped endpoints")
        print(f"  Valhalla URL:  {args.valhalla_url}")
        if args.dry_run:
            print(f"  Mode:          DRY RUN")
        print("=" * 60)

    total_redistributed = 0
    total_failed = 0
    frame_lookup = {f["filename"]: f for f in all_frames}

    for source in sources_to_process:
        src_frames = [f for f in all_frames if f["source"] == source]
        src_frames.sort(key=lambda f: f["index"])

        if not src_frames:
            print(f"\n  {source}: no frames found, skipping")
            total_failed += 1
            continue

        hood = src_frames[0].get("neighborhood", "?")

        # Sub-range slicing: only redistribute a portion of the source
        from_frame = getattr(args, "from_frame", None)
        to_frame = getattr(args, "to_frame", None)
        if from_frame or to_frame:
            filenames = [f["filename"] for f in src_frames]
            if from_frame:
                from_key = resolve_frame_name(from_frame, source)
                if from_key not in filenames:
                    print(f"\n  {source}: --from-frame '{from_frame}' not found, skipping")
                    total_failed += 1
                    continue
                from_idx = filenames.index(from_key)
            else:
                from_idx = 0
            if to_frame:
                to_key = resolve_frame_name(to_frame, source)
                if to_key not in filenames:
                    print(f"\n  {source}: --to-frame '{to_frame}' not found, skipping")
                    total_failed += 1
                    continue
                to_idx = filenames.index(to_key)
            else:
                to_idx = len(src_frames) - 1
            src_frames = src_frames[from_idx : to_idx + 1]

        # Determine start/end — prefer snapped coords (from QGIS import) over originals
        if args.source and args.start:
            start = parse_latlon(args.start)
        else:
            f0 = src_frames[0]
            start = (f0.get("lat_snapped", f0["lat"]), f0.get("lon_snapped", f0["lon"]))

        if args.source and args.end:
            end = parse_latlon(args.end)
        else:
            fN = src_frames[-1]
            end = (fN.get("lat_snapped", fN["lat"]), fN.get("lon_snapped", fN["lon"]))

        if args.source and args.via:
            via_points = [parse_latlon(v) for v in args.via]
            via_label = "manual"
        elif args.no_auto_via:
            via_points = []
            via_label = "disabled"
        else:
            via_points = sample_via_points(src_frames, every=args.via_every)
            via_label = f"auto, every {args.via_every} frames"

        is_partial = from_frame or to_frame

        if len(sources_to_process) == 1:
            print("=" * 60)
            title = "Partial Redistribute GPS Points" if is_partial else "Redistribute GPS Points Along Route"
            print(title)
            print("=" * 60)
            print(f"  Source:        {source} ({hood})")
            print(f"  Frames:        {len(src_frames)}")
            if is_partial:
                print(f"  Range:         {src_frames[0]['filename']} → {src_frames[-1]['filename']}")
            if args.straight:
                print(f"  Mode:          STRAIGHT-LINE interpolation")
            else:
                print(f"  Valhalla URL:  {args.valhalla_url}")
            s_label = "snapped" if "lat_snapped" in src_frames[0] else "original"
            e_label = "snapped" if "lat_snapped" in src_frames[-1] else "original"
            if args.source and args.start:
                s_label = "custom"
            if args.source and args.end:
                e_label = "custom"
            print(f"  Start:         {start[0]:.7f}, {start[1]:.7f} ({s_label})")
            print(f"  End:           {end[0]:.7f}, {end[1]:.7f} ({e_label})")
            print(f"  Via points:    {len(via_points)} ({via_label})")
            if args.dry_run:
                print(f"  Mode:          DRY RUN")
            print("=" * 60)

        n = len(src_frames)
        via_info = f", {len(via_points)} via pts" if via_points else ""
        print(f"\n  [{hood}] {source}: {n} frames{via_info}")

        if args.straight:
            shape = [start] + via_points + [end]
            print(f"    Using straight-line interpolation ({len(shape)} waypoints)")
        else:
            shape = get_route(args.valhalla_url, start, end, via=via_points, costing=args.costing)
            if shape is None:
                print(f"    ✗ Failed to get route")
                total_failed += 1
                continue

        route_dist = sum(
            haversine(shape[i][0], shape[i][1], shape[i + 1][0], shape[i + 1][1])
            for i in range(len(shape) - 1)
        )

        distributed = distribute_points(shape, n)
        spacing = route_dist / (n - 1) if n > 1 else 0

        for i, frame in enumerate(src_frames):
            new_lat, new_lon = distributed[i]
            orig = frame_lookup[frame["filename"]]
            orig["lat_snapped"] = round(new_lat, 7)
            orig["lon_snapped"] = round(new_lon, 7)

        print(f"    ✓ {route_dist:.0f} m route, ~{spacing:.1f} m spacing")
        total_redistributed += n

    # Summary and write
    print(f"\nTotal: {total_redistributed} frames redistributed, {total_failed} sources failed")

    if total_redistributed == 0:
        print("Nothing to write.")
        return

    if args.dry_run:
        print(f"Dry run complete. Re-run without --dry-run to apply.")
    else:
        with open(data_path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"✓ Wrote redistributed lat_snapped/lon_snapped to {data_path}")


if __name__ == "__main__":
    main()
