#!/usr/bin/env python3
"""
Import adjusted GPS coordinates from a QGIS-edited GeoJSON back into data.json.

Reads a GeoJSON file (exported from QGIS after manual adjustments) and writes
the adjusted coordinates as lat_snapped / lon_snapped in web/data.json,
preserving the original lat / lon. Frames are matched by 'filename' (with
'frame_index' as fallback).

Usage:
    # Import from an edited GeoJSON
    python script/import_gps.py QGIS/frames_export_edited.geojson

    # Dry-run to preview changes without writing
    python script/import_gps.py QGIS/frames_export_edited.geojson --dry-run

    # Also recalculate distance_to_next after updating
    python script/import_gps.py QGIS/frames_export_edited.geojson --recalc-distance
"""

import sys
import json
import math
import argparse
from pathlib import Path
from typing import Dict, Tuple


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in metres between two GPS points."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def load_geojson(path: Path) -> Dict:
    if not path.exists():
        print(f"Error: {path} not found")
        sys.exit(1)
    with open(path, "r") as f:
        return json.load(f)


def load_data(path: Path) -> Dict:
    if not path.exists():
        print(f"Error: {path} not found")
        sys.exit(1)
    with open(path, "r") as f:
        return json.load(f)


def build_lookup(data: Dict) -> Dict[str, int]:
    """Map filename → list index for fast lookup."""
    return {f["filename"]: i for i, f in enumerate(data["frames"])}


def apply_edits(
    data: Dict,
    geojson: Dict,
    dry_run: bool = False,
) -> Tuple[int, int, int]:
    """Apply coordinate changes from GeoJSON features to data frames.

    Returns (matched, moved, skipped) counts.
    """
    lookup = build_lookup(data)
    frames = data["frames"]

    matched = 0
    moved = 0
    skipped = 0

    for feature in geojson.get("features", []):
        props = feature.get("properties", {})
        geom = feature.get("geometry")
        if not geom:
            skipped += 1
            continue
        coords = geom.get("coordinates")
        if not coords or len(coords) < 2:
            skipped += 1
            continue

        new_lon, new_lat = coords[0], coords[1]
        filename = props.get("filename")
        frame_index = props.get("frame_index")

        if filename and filename in lookup:
            idx = lookup[filename]
        elif frame_index is not None and 0 <= frame_index < len(frames):
            idx = frame_index
            if frames[idx]["filename"] != filename:
                print(f"  Warning: frame_index {frame_index} filename mismatch "
                      f"({frames[idx]['filename']} vs {filename}), using filename lookup")
                if filename in lookup:
                    idx = lookup[filename]
                else:
                    skipped += 1
                    continue
        else:
            skipped += 1
            continue

        matched += 1
        frame = frames[idx]
        orig_lat, orig_lon = frame["lat"], frame["lon"]
        dist = haversine(orig_lat, orig_lon, new_lat, new_lon)

        if dist < 0.01:  # less than 1 cm — unchanged
            continue

        moved += 1
        if dry_run:
            print(f"  [dry-run] {filename}: "
                  f"({orig_lat:.7f}, {orig_lon:.7f}) → ({new_lat:.7f}, {new_lon:.7f})  "
                  f"Δ {dist:.2f} m")
        else:
            frame["lat_snapped"] = round(new_lat, 7)
            frame["lon_snapped"] = round(new_lon, 7)

    return matched, moved, skipped


def _effective_coords(frame: Dict) -> Tuple[float, float]:
    """Return snapped coordinates if available, otherwise original."""
    lat = frame.get("lat_snapped", frame["lat"])
    lon = frame.get("lon_snapped", frame["lon"])
    return lat, lon


def recalculate_distances(data: Dict):
    """Recalculate distance_to_next using snapped coords when available."""
    frames = data["frames"]
    for i, frame in enumerate(frames):
        if i + 1 < len(frames) and frames[i + 1]["neighborhood"] == frame["neighborhood"]:
            lat1, lon1 = _effective_coords(frame)
            lat2, lon2 = _effective_coords(frames[i + 1])
            d = haversine(lat1, lon1, lat2, lon2)
            frame["distance_to_next"] = round(d, 2)
        else:
            frame["distance_to_next"] = 0

    total = sum(f["distance_to_next"] for f in frames)
    data["total_distance_m"] = round(total, 1)


def main():
    parser = argparse.ArgumentParser(
        description="Import adjusted GPS coordinates from QGIS-edited GeoJSON into data.json"
    )
    parser.add_argument(
        "geojson",
        type=str,
        help="Path to the edited GeoJSON file",
    )
    parser.add_argument(
        "--data",
        type=str,
        default=None,
        help="Path to data.json (default: web/data.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing to data.json",
    )
    parser.add_argument(
        "--recalc-distance",
        action="store_true",
        help="Recalculate distance_to_next for all frames after import",
    )
    parser.add_argument(
        "--delete-missing",
        action="store_true",
        help="Remove frames from data.json that are not in the GeoJSON "
             "(only applies to neighborhoods/sources present in the GeoJSON)",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    data_path = Path(args.data) if args.data else repo_root / "web" / "data.json"
    geojson_path = Path(args.geojson)
    if not geojson_path.is_absolute():
        geojson_path = repo_root / geojson_path

    print("=" * 60)
    print("Import Adjusted GPS → data.json")
    print("=" * 60)
    print(f"  GeoJSON:   {geojson_path}")
    print(f"  data.json: {data_path}")
    if args.dry_run:
        print(f"  Mode:      DRY RUN (no changes written)")
    if args.recalc_distance:
        print(f"  Recalc:    distance_to_next will be recalculated")
    if args.delete_missing:
        print(f"  Delete:    frames missing from GeoJSON will be removed")
    print("=" * 60)

    geojson = load_geojson(geojson_path)
    data = load_data(data_path)

    feature_count = len(geojson.get("features", []))
    frame_count = len(data.get("frames", []))
    print(f"\n  GeoJSON features: {feature_count}")
    print(f"  data.json frames: {frame_count}")

    # Handle deletions
    deleted = 0
    if args.delete_missing:
        geo_filenames = {
            feat["properties"]["filename"]
            for feat in geojson.get("features", [])
            if "filename" in feat.get("properties", {})
            and feat.get("geometry") is not None
        }
        geo_neighborhoods = {
            feat["properties"].get("neighborhood")
            for feat in geojson.get("features", [])
        }
        geo_sources = {
            feat["properties"].get("source")
            for feat in geojson.get("features", [])
        }

        before = len(data["frames"])
        kept = []
        for f in data["frames"]:
            in_scope = f.get("neighborhood") in geo_neighborhoods or f.get("source") in geo_sources
            if in_scope and f["filename"] not in geo_filenames:
                deleted += 1
                if args.dry_run:
                    print(f"  [dry-run] delete: {f['filename']}")
            else:
                kept.append(f)

        if not args.dry_run:
            data["frames"] = kept
            data["frame_count"] = len(kept)

        print(f"\n  Deleted:   {deleted} frames")

    matched, moved, skipped = apply_edits(data, geojson, dry_run=args.dry_run)

    print(f"  Matched:   {matched}")
    print(f"  Moved:     {moved}")
    print(f"  Skipped:   {skipped}")

    changes = moved + deleted
    if changes == 0:
        print("\nNo changes — nothing to write.")
        return

    if args.recalc_distance:
        print("\nRecalculating distance_to_next...")
        recalculate_distances(data)
        print(f"  New total distance: {data['total_distance_m']:.1f} m")

    if args.dry_run:
        print(f"\nDry run complete. Re-run without --dry-run to apply changes.")
    else:
        # Re-index frames sequentially
        if deleted > 0:
            for i, f in enumerate(data["frames"]):
                f["index"] = i

        with open(data_path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"\n✓ Updated data.json: {moved} moved, {deleted} deleted")


if __name__ == "__main__":
    main()
