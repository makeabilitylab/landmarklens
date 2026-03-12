#!/usr/bin/env python3
"""
Prepare gaze data for the standalone heatmap viewer.

Reads common-route gaze data, applies yaw correction, and packages
it as compact JSON for client-side heatmap rendering.

Usage:
    python script/prepare_heatmap_viewer.py
"""

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ROUTES_GEOJSON = BASE_DIR / "geodata" / "routes.geojson"
DATA_JSON = BASE_DIR / "web" / "data.json"
GAZE_CSV = (
    BASE_DIR
    / "study-data"
    / "eye-gaze-data"
    / "aggregated"
    / "df_clean_with_junctions.csv"
)
OUTPUT_DIR = BASE_DIR / "heatmap-viewer" / "data"

SPHERE_UV_OFFSET = 0.25  # 90° / 360° (Unity sphere UV convention)


def main():
    print("Preparing heatmap viewer data...\n")

    # ── 1. Yaw lookup from data.json ──────────────────────────────────
    print(f"  Loading {DATA_JSON.name}...")
    with open(DATA_JSON) as f:
        data_json = json.load(f)

    yaw_lookup: dict[str, float] = {}
    frame_heading: dict[str, dict] = {}
    for frame in data_json.get("frames", []):
        filename = frame.get("filename", "")
        stem = Path(filename).stem
        yaw_lookup[stem] = frame.get("yaw") or 0.0
        frame_heading[stem] = {
            "yaw": frame.get("yaw") or 0.0,
            "pitch": frame.get("pitch") or 0.0,
            "roll": frame.get("roll") or 0.0,
        }
    print(f"    {len(yaw_lookup)} frames in yaw lookup")

    # ── 2. Common routes from routes.geojson ──────────────────────────
    print(f"  Loading {ROUTES_GEOJSON.name}...")
    with open(ROUTES_GEOJSON) as f:
        routes_geojson = json.load(f)

    routes: dict[str, dict] = {}
    all_common_frames: set[str] = set()
    frame_to_route: dict[str, str] = {}

    for feature in routes_geojson["features"]:
        props = feature["properties"]
        route_id = props["route_id"]
        if not route_id.endswith("_c"):
            continue

        frames_list = props.get("frames", [])
        sorted_frames = sorted(frames_list, key=lambda f: f["frame_order"])

        route_frames = []
        for f in sorted_frames:
            fid = f["frame_id"]
            source = fid.rsplit("_", 1)[0]
            hi = frame_heading.get(fid, {"yaw": 0, "pitch": 0, "roll": 0})
            base_yaw = hi["yaw"]
            effective_yaw = (
                (base_yaw + 180) % 360
                if f["direction"] == "reverse"
                else base_yaw
            )

            route_frames.append(
                {
                    "id": fid,
                    "source": source,
                    "yaw": round(effective_yaw, 2),
                    "pitch": round(hi["pitch"], 2),
                    "roll": round(hi["roll"], 2),
                    "direction": f["direction"],
                }
            )
            all_common_frames.add(fid)
            frame_to_route[fid] = route_id

        routes[route_id] = {
            "route_id": route_id,
            "neighborhood": props["neighborhood"],
            "total_frames": len(route_frames),
            "frames": route_frames,
        }

    print(f"    {len(routes)} common routes, {len(all_common_frames)} unique frames")
    for rid, r in sorted(routes.items()):
        print(f"      {rid}: {r['total_frames']} frames")

    # ── 3. Gaze data from CSV ─────────────────────────────────────────
    print(f"\n  Loading gaze data from {GAZE_CSV.name}...")
    gaze_by_route: dict[str, dict[str, dict[str, list]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    row_count = 0
    matched_count = 0

    with open(GAZE_CSV, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row_count += 1
            if row_count % 300000 == 0:
                print(f"    ...{row_count:,} rows")

            frame_id = row.get("frame", "").strip()
            if frame_id not in all_common_frames:
                continue

            route_id = frame_to_route.get(frame_id)
            if not route_id:
                continue

            pid = row.get("pid", "").strip()
            if not pid:
                continue

            try:
                u = float(row["hitU"])
                v = float(row["hitV"])
            except (ValueError, KeyError):
                continue

            # Yaw correction (same as generate_heatmaps.py)
            photo_path = row.get("photoPath", "").strip()
            parts = photo_path.split("/")
            if len(parts) == 2:
                seq_id = parts[0]
                is_rev = seq_id.endswith("_rev")
                base_yaw = yaw_lookup.get(frame_id, 0.0)
                vr_yaw = (base_yaw + 180) % 360 if is_rev else base_yaw
                u = (u + vr_yaw / 360.0 + SPHERE_UV_OFFSET) % 1.0

            u_q = round(u * 1000)
            v_q = round(v * 1000)

            gaze_by_route[route_id][pid][frame_id].append((u_q, v_q))
            matched_count += 1

    print(f"    {row_count:,} total rows, {matched_count:,} matched common routes")

    # ── 4. Write output ───────────────────────────────────────────────
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    routes_file = OUTPUT_DIR / "routes.json"
    with open(routes_file, "w") as f:
        json.dump(routes, f)
    print(f"\n  Written: {routes_file.name} ({routes_file.stat().st_size / 1024:.1f} KB)")

    for route_id in sorted(routes.keys()):
        gaze_data: dict[str, dict[str, list[int]]] = {}
        for pid in sorted(gaze_by_route[route_id].keys()):
            gaze_data[pid] = {}
            for frame_id, points in gaze_by_route[route_id][pid].items():
                flat: list[int] = []
                for u, v in points:
                    flat.extend([u, v])
                gaze_data[pid][frame_id] = flat

        gaze_file = OUTPUT_DIR / f"gaze_{route_id}.json"
        with open(gaze_file, "w") as f:
            json.dump(gaze_data, f, separators=(",", ":"))

        total_points = sum(
            len(pts) // 2
            for pid_data in gaze_data.values()
            for pts in pid_data.values()
        )
        size_kb = gaze_file.stat().st_size / 1024
        print(
            f"  Written: {gaze_file.name} "
            f"({size_kb:.1f} KB, {len(gaze_data)} participants, "
            f"{total_points:,} points)"
        )

    print(f"\nDone! Output: {OUTPUT_DIR}")
    print(f"\nTo start the viewer:")
    print(f"  cd {BASE_DIR}")
    print(f"  python -m http.server 3001")
    print(f"  Open http://localhost:3001/heatmap-viewer/")


if __name__ == "__main__":
    main()
