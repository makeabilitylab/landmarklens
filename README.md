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

#### Batch export for a participant

Update the route file timestamps below to match the latest exports in `data/routes/`, then run:

```bash
# ── Route file mapping ──────────────────────────────────────
# Update these timestamps when routes are re-exported from the viewer
GINZA_C="route_ginza_c_2026-02-27T15-47-54.json"
GINZA_U1="route_ginza_u1_2026-02-27T15-47-58.json"
GINZA_U2="route_ginza_u2_2026-02-27T15-48-02.json"
GINZA_U3="route_ginza_u3_2026-02-28T06-48-00.json"
KOENJI_C="route_konenji_c_2026-02-27T15-48-23.json"
KOENJI_U1="route_koenji_u1_2026-02-27T15-48-26.json"
KOENJI_U2="route_koenji_u2_2026-02-27T15-48-29.json"
KOENJI_U3="route_koenji_u3_2026-02-28T09-49-54.json"
NEZU_C="route_nezu_c_2026-02-27T15-48-08.json"
NEZU_U1="route_nezu_u1_2026-02-27T15-48-10.json"
NEZU_U2="route_nezu_u2_2026-02-27T15-48-16.json"
NEZU_U3="route_nezu_u3_2026-02-28T09-48-47.json"
KINSHICHO_C="route_kinshicho_c_2026-02-27T15-48-39.json"
KINSHICHO_U1="route_kinshicho_u1_2026-02-27T15-48-35.json"
KINSHICHO_U2="route_kinshicho_u2_2026-02-27T15-48-42.json"
KINSHICHO_U3="route_kinshicho_u3_2026-02-28T06-17-52.json"
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