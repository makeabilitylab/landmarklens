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