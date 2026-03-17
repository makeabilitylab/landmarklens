# tokyo-streetview

## Getting Started
1. In the route directory, create a `data` folder, drop in the `frames` folder from Google drive (https://drive.google.com/drive/folders/1WOiSnUeZP2YRl7grwVf1YkAWtIF1KkkH?usp=drive_link).
2. In the route directory, create a `study-data` folder, drop in the `eye-gaze-data` and `heatmap-frames` folders from Google drive (https://drive.google.com/drive/folders/1WOiSnUeZP2YRl7grwVf1YkAWtIF1KkkH?usp=drive_link).
3. Start the viewer: `python -m http.server 3000` (serves `web/`)

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

#### Batch export for a participant

Update the route file timestamps below to match the latest exports in `data/routes/`, then run:

```bash
# ── Route file mapping ──────────────────────────────────────
# Update these timestamps when routes are re-exported from the viewer
GINZA_C="route_ginza_c_2026-02-27T15-47-54.json"
GINZA_U1="route_ginza_u1_2026-02-27T15-47-58.json"
GINZA_U2="route_ginza_u2_2026-02-27T15-48-02.json"
GINZA_U3="route_ginza_u3_2026-02-28T06-48-00.json"
GINZA_U4="route_ginza_u4_2026-03-03T13-04-02.json"
GINZA_U5="route_ginza_u5_2026-03-02T00-25-32.json"
GINZA_U6="route_ginza_u6_2026-03-02T00-25-43.json"
GINZA_U7="route_ginza_u7_2026-03-02T08-34-23.json"
GINZA_U8="route_ginza_u8_2026-03-02T16-57-52.json"
GINZA_U9="route_ginza_u9_2026-03-02T15-26-28.json"
GINZA_U10="route_ginza_u10_2026-03-04T02-01-38.json"
KOENJI_C="route_konenji_c_2026-02-27T15-48-23.json"
KOENJI_U1="route_koenji_u1_2026-02-27T15-48-26.json"
KOENJI_U2="route_koenji_u2_2026-02-27T15-48-29.json"
KOENJI_U3="route_koenji_u3_2026-02-28T09-49-54.json"
KOENJI_U4="route_koenji_u4_2026-03-03T13-04-30.json"
KOENJI_U5="route_koenji_u5_2026-03-02T00-49-03.json"
KOENJI_U6="route_koenji_u6_2026-03-02T00-49-08.json"
KOENJI_U7="route_koenji_u7_2026-03-02T08-34-52.json"
KOENJI_U8="route_koenji_u8_2026-03-02T16-58-12.json"
KOENJI_U9="route_koenji_u9_2026-03-03T01-47-30.json"
KOENJI_U10="route_koenji_u10_2026-03-04T02-01-59.json"
NEZU_C="route_nezu_c_2026-02-27T15-48-08.json"
NEZU_U1="route_nezu_u1_2026-02-27T15-48-10.json"
NEZU_U2="route_nezu_u2_2026-02-27T15-48-16.json"
NEZU_U3="route_nezu_u3_2026-02-28T09-48-47.json"
NEZU_U4="route_nezu_u4_2026-03-03T13-04-18.json"
NEZU_U5="route_nezu_u5_2026-03-02T00-38-56.json"
NEZU_U6="route_nezu_u6_2026-03-02T00-38-59.json"
NEZU_U7="route_nezu_u7_2026-03-02T08-34-41.json"
NEZU_U8="route_nezu_u8_2026-03-02T16-58-04.json"
NEZU_U9="route_nezu_u9_2026-03-03T01-58-34.json"
NEZU_U10="route_nezu_u10_2026-03-04T02-01-50.json"
KINSHICHO_C="route_kinshicho_c_2026-02-27T15-48-39.json"
KINSHICHO_U1="route_kinshicho_u1_2026-02-27T15-48-35.json"
KINSHICHO_U2="route_kinshicho_u2_2026-02-27T15-48-42.json"
KINSHICHO_U3="route_kinshicho_u3_2026-02-28T06-17-52.json"
KINSHICHO_U4="route_kinshicho_u4_2026-03-03T13-07-54.json"
KINSHICHO_U5="route_kinshicho_u5_2026-03-02T01-00-57.json"
KINSHICHO_U6="route_kinshicho_u6_2026-03-02T01-01-01.json"
KINSHICHO_U7="route_kinshicho_u7_2026-03-02T08-35-01.json"
KINSHICHO_U8="route_kinshicho_u8_2026-03-02T16-58-22.json"
KINSHICHO_U9="route_kinshicho_u9_2026-03-02T15-27-01.json"
KINSHICHO_U10="route_kinshicho_u10_2026-03-04T02-02-08.json"
PRACTICE="route_practice_route_2026-02-27T15-48-13.json"
R="data/routes"
```

```bash
# ── P01: Practice, Ginza(C), Ginza(U1), Koenji(U1), Koenji(C), Nezu(C), Nezu(U1), Kinshicho(U1), Kinshicho(C)
rm -rf StudyData/ParticipantSets/P01/Routes
O="StudyData/ParticipantSets/P01/Routes"
python script/export_route_vr.py --route "$R/$PRACTICE"      --name "Practice"        --output "$O/practice" && \
python script/export_route_vr.py --route "$R/$GINZA_C"       --name "Ginza (C)"       --output "$O/ginza_c" && \
python script/export_route_vr.py --route "$R/$GINZA_U1"      --name "Ginza (U1)"      --output "$O/ginza_u1" && \
python script/export_route_vr.py --route "$R/$KOENJI_U1"     --name "Koenji (U1)"     --output "$O/koenji_u1" && \
python script/export_route_vr.py --route "$R/$KOENJI_C"      --name "Koenji (C)"      --output "$O/koenji_c" && \
python script/export_route_vr.py --route "$R/$NEZU_C"        --name "Nezu (C)"        --output "$O/nezu_c" && \
python script/export_route_vr.py --route "$R/$NEZU_U1"       --name "Nezu (U1)"       --output "$O/nezu_u1" && \
python script/export_route_vr.py --route "$R/$KINSHICHO_U1"  --name "Kinshicho (U1)"  --output "$O/kinshicho_u1" && \
python script/export_route_vr.py --route "$R/$KINSHICHO_C"   --name "Kinshicho (C)"   --output "$O/kinshicho_c"
```

```bash
# ── P02: Practice, Koenji(U2), Koenji(C), Kinshicho(C), Kinshicho(U2), Ginza(U2), Ginza(C), Nezu(C), Nezu(U2)
rm -rf StudyData/ParticipantSets/P02/Routes
O="StudyData/ParticipantSets/P02/Routes"
python script/export_route_vr.py --route "$R/$PRACTICE"      --name "Practice"        --output "$O/practice" && \
python script/export_route_vr.py --route "$R/$KOENJI_U2"     --name "Koenji (U2)"     --output "$O/koenji_u2" && \
python script/export_route_vr.py --route "$R/$KOENJI_C"      --name "Koenji (C)"      --output "$O/koenji_c" && \
python script/export_route_vr.py --route "$R/$KINSHICHO_C"   --name "Kinshicho (C)"   --output "$O/kinshicho_c" && \
python script/export_route_vr.py --route "$R/$KINSHICHO_U2"  --name "Kinshicho (U2)"  --output "$O/kinshicho_u2" && \
python script/export_route_vr.py --route "$R/$GINZA_U2"      --name "Ginza (U2)"      --output "$O/ginza_u2" && \
python script/export_route_vr.py --route "$R/$GINZA_C"       --name "Ginza (C)"       --output "$O/ginza_c" && \
python script/export_route_vr.py --route "$R/$NEZU_C"        --name "Nezu (C)"        --output "$O/nezu_c" && \
python script/export_route_vr.py --route "$R/$NEZU_U2"       --name "Nezu (U2)"       --output "$O/nezu_u2"
```

```bash
# ── P03: Practice, Nezu(C), Nezu(U3), Ginza(U3), Ginza(C), Kinshicho(C), Kinshicho(U3), Koenji(U3), Koenji(C)
rm -rf StudyData/ParticipantSets/P03/Routes
O="StudyData/ParticipantSets/P03/Routes"
python script/export_route_vr.py --route "$R/$PRACTICE"      --name "Practice"        --output "$O/practice" && \
python script/export_route_vr.py --route "$R/$NEZU_C"        --name "Nezu (C)"        --output "$O/nezu_c" && \
python script/export_route_vr.py --route "$R/$NEZU_U3"       --name "Nezu (U3)"       --output "$O/nezu_u3" && \
python script/export_route_vr.py --route "$R/$GINZA_U3"      --name "Ginza (U3)"      --output "$O/ginza_u3" && \
python script/export_route_vr.py --route "$R/$GINZA_C"       --name "Ginza (C)"       --output "$O/ginza_c" && \
python script/export_route_vr.py --route "$R/$KINSHICHO_C"   --name "Kinshicho (C)"   --output "$O/kinshicho_c" && \
python script/export_route_vr.py --route "$R/$KINSHICHO_U3"  --name "Kinshicho (U3)"  --output "$O/kinshicho_u3" && \
python script/export_route_vr.py --route "$R/$KOENJI_U3"      --name "Koenji (U3)"     --output "$O/koenji_u3" && \
python script/export_route_vr.py --route "$R/$KOENJI_C"      --name "Koenji (C)"      --output "$O/koenji_c"
```

```bash
# ── P04: Practice, Kinshicho(U4), Kinshicho(C), Nezu(C), Nezu(U4), Koenji(U4), Koenji(C), Ginza(C), Ginza(U4)
rm -rf StudyData/ParticipantSets/P04/Routes
O="StudyData/ParticipantSets/P04/Routes"
python script/export_route_vr.py --route "$R/$PRACTICE"       --name "Practice"        --output "$O/practice" && \
python script/export_route_vr.py --route "$R/$KINSHICHO_U4"  --name "Kinshicho (U4)"  --output "$O/kinshicho_u4" && \
python script/export_route_vr.py --route "$R/$KINSHICHO_C"   --name "Kinshicho (C)"   --output "$O/kinshicho_c" && \
python script/export_route_vr.py --route "$R/$NEZU_C"        --name "Nezu (C)"        --output "$O/nezu_c" && \
python script/export_route_vr.py --route "$R/$NEZU_U4"       --name "Nezu (U4)"       --output "$O/nezu_u4" && \
python script/export_route_vr.py --route "$R/$KOENJI_U4"     --name "Koenji (U4)"     --output "$O/koenji_u4" && \
python script/export_route_vr.py --route "$R/$KOENJI_C"      --name "Koenji (C)"      --output "$O/koenji_c" && \
python script/export_route_vr.py --route "$R/$GINZA_C"       --name "Ginza (C)"       --output "$O/ginza_c" && \
python script/export_route_vr.py --route "$R/$GINZA_U4"      --name "Ginza (U4)"       --output "$O/ginza_u4"
```

```bash
# ── P05: Practice, Ginza(U5), Ginza(C), Koenji(C), Koenji(U5), Nezu(U5), Nezu(C), Kinshicho(C), Kinshicho(U5)
rm -rf study-materials/ParticipantSets/P05/Routes
O="study-materials/ParticipantSets/P05/Routes"
python script/export_route_vr.py --route "$R/$PRACTICE"       --name "Practice"        --output "$O/practice" && \
python script/export_route_vr.py --route "$R/$GINZA_U5"      --name "Ginza (U5)"      --output "$O/ginza_u5" && \
python script/export_route_vr.py --route "$R/$GINZA_C"       --name "Ginza (C)"       --output "$O/ginza_c" && \
python script/export_route_vr.py --route "$R/$KOENJI_C"       --name "Koenji (C)"      --output "$O/koenji_c" && \
python script/export_route_vr.py --route "$R/$KOENJI_U5"     --name "Koenji (U5)"     --output "$O/koenji_u5" && \
python script/export_route_vr.py --route "$R/$NEZU_U5"      --name "Nezu (U5)"       --output "$O/nezu_u5" && \
python script/export_route_vr.py --route "$R/$NEZU_C"        --name "Nezu (C)"        --output "$O/nezu_c" && \
python script/export_route_vr.py --route "$R/$KINSHICHO_C"   --name "Kinshicho (C)"   --output "$O/kinshicho_c" && \
python script/export_route_vr.py --route "$R/$KINSHICHO_U5"   --name "Kinshicho (U5)"  --output "$O/kinshicho_u5"
```

```bash
# ── P06: Practice, Koenji(C), Koenji(U6), Kinshicho(U6), Kinshicho(C), Ginza(C), Ginza(U6), Nezu(U6), Nezu(C)
rm -rf study-materials/ParticipantSets/P06/Routes
O="study-materials/ParticipantSets/P06/Routes"
python script/export_route_vr.py --route "$R/$PRACTICE"       --name "Practice"        --output "$O/practice" && \
python script/export_route_vr.py --route "$R/$KOENJI_C"       --name "Koenji (C)"      --output "$O/koenji_c" && \
python script/export_route_vr.py --route "$R/$KOENJI_U6"      --name "Koenji (U6)"     --output "$O/koenji_u6" && \
python script/export_route_vr.py --route "$R/$KINSHICHO_U6"   --name "Kinshicho (U6)"  --output "$O/kinshicho_u6" && \
python script/export_route_vr.py --route "$R/$KINSHICHO_C"   --name "Kinshicho (C)"   --output "$O/kinshicho_c" && \
python script/export_route_vr.py --route "$R/$GINZA_C"        --name "Ginza (C)"       --output "$O/ginza_c" && \
python script/export_route_vr.py --route "$R/$GINZA_U6"      --name "Ginza (U6)"      --output "$O/ginza_u6" && \
python script/export_route_vr.py --route "$R/$NEZU_U6"       --name "Nezu (U6)"       --output "$O/nezu_u6" && \
python script/export_route_vr.py --route "$R/$NEZU_C"         --name "Nezu (C)"        --output "$O/nezu_c"
```

```bash
# ── P07: Practice, Nezu(U7), Nezu(C), Ginza(C), Ginza(U7), Kinshicho(U7), Kinshicho(C), Koenji(C), Koenji(U7)
rm -rf study-materials/ParticipantSets/P07/Routes
O="study-materials/ParticipantSets/P07/Routes"
python script/export_route_vr.py --route "$R/$PRACTICE"       --name "Practice"        --output "$O/practice" && \
python script/export_route_vr.py --route "$R/$NEZU_U7"       --name "Nezu (U7)"       --output "$O/nezu_u7" && \
python script/export_route_vr.py --route "$R/$NEZU_C"         --name "Nezu (C)"        --output "$O/nezu_c" && \
python script/export_route_vr.py --route "$R/$GINZA_C"        --name "Ginza (C)"       --output "$O/ginza_c" && \
python script/export_route_vr.py --route "$R/$GINZA_U7"      --name "Ginza (U7)"      --output "$O/ginza_u7" && \
python script/export_route_vr.py --route "$R/$KINSHICHO_U7"  --name "Kinshicho (U7)"  --output "$O/kinshicho_u7" && \
python script/export_route_vr.py --route "$R/$KINSHICHO_C"   --name "Kinshicho (C)"   --output "$O/kinshicho_c" && \
python script/export_route_vr.py --route "$R/$KOENJI_C"       --name "Koenji (C)"      --output "$O/koenji_c" && \
python script/export_route_vr.py --route "$R/$KOENJI_U7"     --name "Koenji (U7)"     --output "$O/koenji_u7"
```

```bash
# ── P08: Practice, Kinshicho(C), Kinshicho(U8), Nezu(U8), Nezu(C), Koenji(C), Koenji(U8), Ginza(U8), Ginza(C)
rm -rf study-materials/ParticipantSets/P08/Routes
O="study-materials/ParticipantSets/P08/Routes"
python script/export_route_vr.py --route "$R/$PRACTICE"       --name "Practice"        --output "$O/practice" && \
python script/export_route_vr.py --route "$R/$KINSHICHO_C"   --name "Kinshicho (C)"   --output "$O/kinshicho_c" && \
python script/export_route_vr.py --route "$R/$KINSHICHO_U8"  --name "Kinshicho (U8)"  --output "$O/kinshicho_u8" && \
python script/export_route_vr.py --route "$R/$NEZU_U8"       --name "Nezu (U8)"       --output "$O/nezu_u8" && \
python script/export_route_vr.py --route "$R/$NEZU_C"        --name "Nezu (C)"        --output "$O/nezu_c" && \
python script/export_route_vr.py --route "$R/$KOENJI_C"       --name "Koenji (C)"      --output "$O/koenji_c" && \
python script/export_route_vr.py --route "$R/$KOENJI_U8"     --name "Koenji (U8)"     --output "$O/koenji_u8" && \
python script/export_route_vr.py --route "$R/$GINZA_U8"      --name "Ginza (U8)"      --output "$O/ginza_u8" && \
python script/export_route_vr.py --route "$R/$GINZA_C"       --name "Ginza (C)"       --output "$O/ginza_c"
```

```bash
# ── P09: Practice, Ginza(U9), Ginza(C), Koenji(C), Koenji(U9), Nezu(U9), Nezu(C), Kinshicho(C), Kinshicho(U9)
rm -rf study-materials/ParticipantSets/P09/Routes
O="study-materials/ParticipantSets/P09/Routes"
python script/export_route_vr.py --route "$R/$PRACTICE"       --name "Practice"        --output "$O/practice" && \
python script/export_route_vr.py --route "$R/$GINZA_U9"      --name "Ginza (U9)"      --output "$O/ginza_u9" && \
python script/export_route_vr.py --route "$R/$GINZA_C"        --name "Ginza (C)"       --output "$O/ginza_c" && \
python script/export_route_vr.py --route "$R/$KOENJI_C"       --name "Koenji (C)"      --output "$O/koenji_c" && \
python script/export_route_vr.py --route "$R/$KOENJI_U9"     --name "Koenji (U9)"     --output "$O/koenji_u9" && \
python script/export_route_vr.py --route "$R/$NEZU_U9"       --name "Nezu (U9)"       --output "$O/nezu_u9" && \
python script/export_route_vr.py --route "$R/$NEZU_C"         --name "Nezu (C)"        --output "$O/nezu_c" && \
python script/export_route_vr.py --route "$R/$KINSHICHO_C"   --name "Kinshicho (C)"   --output "$O/kinshicho_c" && \
python script/export_route_vr.py --route "$R/$KINSHICHO_U9"  --name "Kinshicho (U9)"  --output "$O/kinshicho_u9"
```


### Step 3: Register the route in the participant config

Add the route directory name to `study-materials/ParticipantSets/<PID>/Routes/route_config.json`:

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

Connect the Galaxy XR headset via ADB, then from `study-materials/`:

```
./push_routes.sh P00          # push routes
./push_routes.sh P00 --clean  # clear device first, then push
./push_routes.sh --list       # list available participant sets
```

To update just the `detections.json` on the headset (without re-pushing images):

```
./push_detections.sh <route_id> <path/to/detections.json>
```

Example:

```bash
./push_detections.sh ningyocho_r1 EvaluationSets/P00/Routes/ningyocho_r1/detections.json
```

## Generating gaze heatmaps

After collecting eye-tracking data from the VR study, you can generate heatmap composites that overlay gaze density onto the original panorama images.

### Prerequisites

- Eye-gaze CSVs in `study-data/eye-gaze-data/` (exported from the VR app's `EyeGazeLogger`)
- Python with `opencv-python` and `numpy` installed
- Original panorama frames in `web/frames/`

### Generate heatmaps

```bash
# All participants at once
python script/generate_heatmaps.py study-data/eye-gaze-data/*.csv

# Single participant
python script/generate_heatmaps.py study-data/eye-gaze-data/p1-good.csv

# Filter to a specific sequence (useful for testing)
python script/generate_heatmaps.py study-data/eye-gaze-data/p1-good.csv --sequence GS010317

# Preview frame counts without writing files
python script/generate_heatmaps.py study-data/eye-gaze-data/*.csv --dry-run
```

Each CSV filename becomes a participant label in the viewer dropdown (e.g., `p1-good.csv` → "p1-good"). Output goes to `study-data/heatmap-frames/<participant>/`, mirroring the `web/frames/` directory structure. A `manifest.json` is written alongside the heatmap folders so the web viewer knows which participants and frames are available.

The script corrects for two coordinate offsets between the VR app's recorded `hitU` and the actual panorama image pixel position:
1. Unity's sphere UV convention (90° offset)
2. Per-frame sphere rotation from heading adjustments (including +180° for `_rev` sequences)

### View in the browser

1. Create a symlink so the web server can access the heatmap frames:
   ```bash
   ln -s ../study-data/heatmap-frames web/heatmap-frames
   ```
2. Start the viewer:
   ```bash
   cd web && python -m http.server 3000
   ```
3. Open `http://localhost:3000/viewer.html`. A dropdown in the header lets you switch between "Original" and any participant's heatmap overlay.