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

## How to adjust camera heading (yaw/pitch/roll)
### Step 1: Open the viewer, press E to enter edit mode
Adjust yaw/pitch/roll on a frame and save — it propagates forward to all following frames in the same video source until the next keyframe.

### Step 2: Export keyframes from the browser
Click "Export Keyframes" in the edit panel. This downloads a JSON file.

### Step 3: Import into data.json
`python script/import_heading_adjustments.py heading-adjustments-2026-02-03.json`

Preview first with `--dry-run`, or filter by neighborhood with `--neighborhood ginza`.

### To clear all heading adjustments:
`python script/import_heading_adjustments.py --clear`