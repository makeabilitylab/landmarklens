#!/usr/bin/env python3
"""
Export frame GPS coordinates to GeoJSON for manual adjustment in QGIS.

Reads web/data.json and writes a GeoJSON file with one Point feature per frame.
After adjusting points in QGIS, use import_gps.py to write them back.

Usage:
    # Export all frames
    python script/export_gps.py

    # Export a single neighborhood
    python script/export_gps.py --neighborhood ginza

    # Export a single video source
    python script/export_gps.py --source GS010317

    # Custom output path
    python script/export_gps.py --output my_export.geojson
"""

import sys
import json
import argparse
from pathlib import Path
from typing import List, Dict, Optional


def load_data(data_path: Path) -> Dict:
    if not data_path.exists():
        print(f"Error: {data_path} not found")
        sys.exit(1)
    with open(data_path, "r") as f:
        return json.load(f)


def filter_frames(
    frames: List[Dict],
    neighborhood: Optional[str] = None,
    source: Optional[str] = None,
) -> List[Dict]:
    if neighborhood:
        frames = [f for f in frames if f.get("neighborhood") == neighborhood]
    if source:
        frames = [f for f in frames if f.get("source") == source]
    return frames


def create_geojson(frames: List[Dict]) -> Dict:
    features = []
    for frame in frames:
        lat = frame.get("lat")
        lon = frame.get("lon")
        if lat is None or lon is None:
            continue

        feature = {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [lon, lat],
            },
            "properties": {
                "frame_index": frame["index"],
                "filename": frame["filename"],
                "source": frame.get("source"),
                "neighborhood": frame.get("neighborhood"),
                "alt": frame.get("alt"),
                "heading": frame.get("heading"),
                "distance_to_next": frame.get("distance_to_next"),
                "original_lat": lat,
                "original_lon": lon,
            },
        }
        features.append(feature)

    return {
        "type": "FeatureCollection",
        "crs": {
            "type": "name",
            "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"},
        },
        "features": features,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Export frame GPS coordinates to GeoJSON for QGIS editing"
    )
    parser.add_argument(
        "--neighborhood",
        type=str,
        help="Filter by neighborhood (e.g. ginza, kinshicho, koenji, nezu)",
    )
    parser.add_argument(
        "--source",
        type=str,
        help="Filter by video source ID (e.g. GS010317)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output GeoJSON path (default: QGIS/frames_export.geojson)",
    )
    parser.add_argument(
        "--data",
        type=str,
        default=None,
        help="Path to data.json (default: web/data.json)",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    data_path = Path(args.data) if args.data else repo_root / "web" / "data.json"

    if args.output:
        output_path = Path(args.output)
        if not output_path.is_absolute():
            output_path = repo_root / output_path
    else:
        suffix_parts = []
        if args.neighborhood:
            suffix_parts.append(args.neighborhood)
        if args.source:
            suffix_parts.append(args.source)
        suffix = "_" + "_".join(suffix_parts) if suffix_parts else ""
        output_path = repo_root / "data" / "unsnapped-points" / f"frames_export{suffix}.geojson"

    print("=" * 60)
    print("Export Frame GPS → GeoJSON")
    print("=" * 60)
    print(f"  Data file:  {data_path}")
    print(f"  Output:     {output_path}")
    if args.neighborhood:
        print(f"  Neighborhood: {args.neighborhood}")
    if args.source:
        print(f"  Source:       {args.source}")
    print("=" * 60)

    data = load_data(data_path)
    all_frames = data.get("frames", [])
    print(f"\nTotal frames in data.json: {len(all_frames)}")

    neighborhoods = sorted(set(f["neighborhood"] for f in all_frames))
    sources = sorted(set(f["source"] for f in all_frames))
    print(f"  Neighborhoods: {', '.join(neighborhoods)}")
    print(f"  Sources:       {', '.join(sources)}")

    frames = filter_frames(all_frames, neighborhood=args.neighborhood, source=args.source)
    print(f"\nFrames to export: {len(frames)}")

    if not frames:
        print("\nNo frames found matching criteria.")
        sys.exit(1)

    geojson = create_geojson(frames)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(geojson, f, indent=2)

    print(f"\n✓ Exported {len(geojson['features'])} frame locations to {output_path}")
    print(f"\nNext steps:")
    print(f"  1. Open {output_path.name} in QGIS")
    print(f"  2. Load an OSM basemap (QuickMapServices plugin → OSM → OSM Standard)")
    print(f"  3. Move points as needed (Toggle Editing → Vertex Tool)")
    print(f"  4. Save the edited layer (right-click → Export → Save Features As… → GeoJSON)")
    print(f"  5. Run: python script/import_gps.py <edited.geojson>")
    print(f"\n  The 'frame_index' and 'filename' properties are used to match frames back.")


if __name__ == "__main__":
    main()
