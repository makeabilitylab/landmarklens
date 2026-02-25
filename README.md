# tokyo-streetview

Start the viewer: `python -m http.server 3000` (serves `web/`)

## Exporting routes for VR / participant sets

### Prerequisites

Before exporting, make sure GPS and headings are baked into `web/data.json`:

1. **GPS cleaned** — see `script/README.md` for the snap → QGIS → import → redistribute workflow.
2. **Headings baked** — open the viewer in edit mode (press E), adjust yaw/pitch/roll, export keyframes, then import:
   ```
   python script/import_heading_adjustments.py data/heading-adjustments/<file>.json
   ```

### Step 1: Build the route in the viewer

Open the viewer, use the Route Builder to create a route (select frames, set junctions), then click **Export Route**. This saves a route JSON to `data/routes/`.

### Step 2: Export for VR

```
python script/export_route_vr.py \
    --route data/routes/<route_file>.json \
    --name "Route Name" \
    --output StudyData/ParticipantSets/<PID>/Routes/<route_name>
```

This produces a self-contained directory with `route.json` (metadata, nav links, headings) and sequence folders containing the panorama images.

| Flag | Purpose |
|------|---------|
| `--route` | Route JSON from the Route Builder (required) |
| `--output` | Output directory (default: `exports/<route_id>`) |
| `--name` | Human-readable name embedded in the export |
| `--route-index N` | Select which route if the file contains multiple (default: 0) |
| `--dry-run` | Preview without writing anything |
| `--no-copy` | Export `route.json` only, skip copying photos |

### Step 3: Register the route in the participant config

Add the route directory name to `StudyData/ParticipantSets/<PID>/Routes/route_config.json`:

```json
{
  "participant_id": "P00",
  "routes": [
    "practice",
    "ginza_common",
    ...
  ]
}
```

### Step 4: Push to headset

Connect the Galaxy XR headset via ADB, then from `StudyData/`:

```
./push_routes.sh P00          # push routes
./push_routes.sh P00 --clean  # clear device first, then push
./push_routes.sh --list       # list available participant sets
```