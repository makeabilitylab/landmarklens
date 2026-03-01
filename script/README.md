## Prerequisites: Start Valhalla (local routing engine)
Several scripts (`snap_gps.py`, `redistribute_gps.py`, `route_frames.py`) require a local Valhalla instance for map-matching and routing.

**First-time setup:**
```bash
# Download Kanto region OSM data
mkdir -p data/valhalla_data/custom_files
wget https://download.geofabrik.de/asia/japan/kanto-latest.osm.pbf \
     -O data/valhalla_data/custom_files/kanto.osm.pbf

# Start Valhalla in Docker (first run builds routing tiles — takes a while)
docker run --name valhalla -p 8002:8002 \
     -v $PWD/data/valhalla_data/custom_files:/custom_files \
     ghcr.io/gis-ops/docker-valhalla/valhalla:latest
```

**Subsequent runs:**
```bash
docker start valhalla
```

Verify it's running: `curl http://localhost:8002/status`

## How to clean GPS for each neighborhood
### Step 1: Snap GPS to the nearest OSM geometry
`python script/snap_gps.py --neighborhood nezu --search-radius 20 --gps-accuracy 3 --turn-penalty 500`

### Step 2: Export for cleaning in QGIS:
`python script/export_gps.py --neighborhood nezu`

### Step 3: Import the corrected GPS data into the database:
`python script/import_gps.py data/QGIS-corrected/nezu.geojson --delete-missing`

### Step 4: Redistribute GPS points to the nearest OSM geometry:
`python script/redistribute_gps.py --neighborhood nezu`

### For routes with extreme outliers, use:
`python script/redistribute_gps.py --source GS010355 --no-auto-via`

### For routes with keyframes, use:
`python script/redistribute_gps.py --source GS010355 --from-frame GS010355_0000 --to-frame GS010355_0072 --start 35.720034,139.763173 --end 35.721234,139.762251 --no-auto-via && python script/redistribute_gps.py --source GS010355 --from-frame GS010355_0072 --to-frame GS010355_0106 --start 35.721234,139.762251 --end 35.721832,139.762265 --no-auto-via && python script/redistribute_gps.py --source GS010355 --from-frame GS010355_0106 --to-frame GS010355_0169 --start 35.721832,139.762265 --end 35.722896,139.762572 --no-auto-via && python script/redistribute_gps.py --source GS010355 --from-frame GS010355_0169 --to-frame GS010355_0199 --start 35.722896,139.762572 --end 35.723458,139.762735 --no-auto-via && python script/redistribute_gps.py --source GS010355 --from-frame GS010355_0199 --to-frame GS010355_0272 --start 35.723458,139.762735 --end 35.724746,139.763115 --no-auto-via`

## How to split a sequence (same source, direction change)
When a single video source turns mid-route (e.g. GS010366 turns between 0084 and 0086), add a split so the route is exported as two sequences with a junction:

`python script/split_sequence.py --source GS010366 --after-frame 0084`

This records "after frame 0084 start a new sequence". `export_route_vr.py` will then split that source into two segments (e.g. `GS010366_fwd` and `GS010366_fwd_2`) with a junction between them. To remove: `--remove`. To list splits: `--list`.

## How to adjust camera heading (yaw/pitch/roll)
### Step 1: Open the viewer, press E to enter edit mode
Adjust yaw/pitch/roll on a frame and save — it propagates forward to all following frames in the same video source until the next keyframe.

### Step 2: Export keyframes from the browser
Click "Export Keyframes" in the edit panel. This downloads a JSON file.

### Step 3: Import into data.json
`python script/import_heading_adjustments.py data/heading-adjustments/heading-adjustments-2026-02-24T04-14-42.json `

Preview first with `--dry-run`, or filter by neighborhood with `--neighborhood ginza`.

### To clear all heading adjustments:
`python script/import_heading_adjustments.py --clear`