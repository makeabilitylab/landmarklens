# Geodata

Structured spatial database linking panorama frames, walking routes, and participant data (todo).

## Updates

After updating `data.json` re-run:

```
python script/build_geodata.py
```

---

## Tables

### 1. `panos.geojson` — Panorama Frames (Points)

One feature per unique panorama frame. Only includes frames that appear in at least one route (currently 2,390 of 3,120 total frames in `data.json`).

**Geometry:** `Point` (lon, lat — snapped/corrected coordinates)

| Property | Type | Description |
|---|---|---|
| `frame_id` | string | Unique frame identifier, e.g. `GS010317_0004`. Derived from the GoPro filename. |
| `source` | string | GoPro video file the frame was extracted from, e.g. `GS010317`. |
| `neighborhood` | string | One of: `ginza`, `koenji`, `nezu`, `kinshicho`. |
| `lat` | float | Latitude (snapped to road network via Valhalla, or manually corrected in QGIS). This is the coordinate used in the VR app. |
| `lon` | float | Longitude (snapped/corrected). |
| `lat_original` | float | Raw latitude from GoPro GPS. |
| `lon_original` | float | Raw longitude from GoPro GPS. |
| `alt` | float | Altitude from GoPro GPS (meters, not corrected). |
| `heading` | float | Original camera heading from GoPro metadata (degrees). |
| `yaw` | float \| null | Manual yaw correction applied in the web viewer (degrees). Null if no correction. |
| `pitch` | float \| null | Manual pitch correction (degrees). |
| `roll` | float \| null | Manual roll correction (degrees). |
| `distance_to_next` | float | Distance to the next frame in the original capture sequence (meters). |

**Primary key:** `frame_id`

---

### 2. `routes.geojson` — Walking Routes (LineStrings)

One feature per route. Each route is a curated walking path through a subset of panorama frames, potentially traversing GoPro sequences in reverse.

**Geometry:** `LineString` (ordered coordinates of all frames in walking order)

#### Route-level properties

| Property | Type | Description |
|---|---|---|
| `route_id` | string | Unique route identifier, e.g. `ginza_c`, `koenji_u5`, `practice`. Convention: `{neighborhood}_c` = common route (same for all participants), `{neighborhood}_u{N}` = unique route for participant N. |
| `neighborhood` | string | One of: `ginza`, `koenji`, `nezu`, `kinshicho`. |
| `total_frames` | int | Number of frames in this route. |
| `total_sequences` | int | Number of street segments the route passes through. |
| `total_junctions` | int | Number of turns/junctions in this route. |
| `total_distance_m` | float | Total walking distance (meters). |
| `frames` | array | Ordered list of frame entries (see below). |

#### Frame entries (inside `frames` array)

Each element in the `frames` array describes one frame's role within this route. Links to `panos.geojson` via `frame_id`.

| Field | Type | Description |
|---|---|---|
| `frame_id` | string | Links to `panos.geojson`. A frame can appear in multiple routes. |
| `sequence_id` | string | The directed sequence this frame belongs to, e.g. `GS010317_fwd` or `GS010324_rev`. Format: `{source}_{direction}`. |
| `direction` | string | `forward` or `reverse` — whether this sequence traverses the original GoPro capture order or reversed. |
| `segment_number` | int | Which segment of the route (1-indexed). A new segment starts after each junction. |
| `frame_order` | int | Global position of this frame in the route's walking order (0-indexed). |
| `is_junction` | bool | `true` if this frame is at a turn point (either the last frame before a turn or the first frame after). |
| `turn_direction` | string \| null | `left` or `right` at junction frames; `null` otherwise. |
| `turn_angle` | float \| null | Turn angle in degrees at junction frames. Negative = left, positive = right. |
| `is_turn_warning` | bool | `true` if this frame is 1–2 steps before an upcoming turn. In the VR app, participants see "left/right turn ahead" at these frames. |
| `turn_warning_direction` | string \| null | The upcoming turn direction (`left` / `right`) for warning frames. |
| `turn_warning_angle` | float \| null | The upcoming turn angle for warning frames. |

---

### 3. Participants (planned)

A future table linking participants to the routes they walked and the order they experienced them. Will connect to `routes.geojson` via `route_id`. Source data is in `study-materials/ParticipantSets/P{N}/Routes/route_config.json`.

---

## Relationships

```
panos.geojson              routes.geojson             participants (future)
┌──────────────┐           ┌──────────────┐           ┌──────────────┐
│ frame_id (PK)│◄──────────│ frames[].    │           │ participant  │
│ source       │           │   frame_id   │           │   _id (PK)   │
│ neighborhood │           │              │           │              │
│ lat, lon     │           │ route_id (PK)│◄──────────│ routes[]     │
│ ...          │           │ neighborhood │           │   .route_id  │
└──────────────┘           │ frames[]     │           │   .order     │
                           │ ...          │           └──────────────┘
                           └──────────────┘
```

- A **frame** can appear in multiple **routes** (many-to-many via `frames[].frame_id`).
- A **participant** walks multiple **routes** in a specific order (many-to-many via `route_config.json`).
- Common routes (`_c`) are shared across all participants. Unique routes (`_u{N}`) belong to one participant.

---

## Route naming convention

| Pattern | Example | Meaning |
|---|---|---|
| `{neighborhood}_c` | `ginza_c` | **Common** route — identical for all participants. |
| `{neighborhood}_u{N}` | `ginza_u5` | **Unique** route — specific to participant P{N}. |
| `practice` | `practice` | Practice route for familiarization (in Nezu). |

## Notes

- **Coordinates:** `lat`/`lon` in panos are road-snapped (Valhalla) or manually corrected (QGIS). The `lat_original`/`lon_original` fields preserve the raw GoPro GPS for reference.
- **Turn warnings:** Marked 2 frames before each junction exit frame, matching the VR app behavior where participants see "left/right turn ahead".
