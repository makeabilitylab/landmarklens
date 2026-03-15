# VLM Landmark Detection Pipeline

## Problem Context

This project is part of a VR navigation tool that places users in virtual environments built from 360-degree panoramic images of Tokyo streets. The goal is to automatically highlight landmarks and street features that help people with poor sense of direction orient themselves.

We collected eye-tracking data from people with good spatial ability to identify what kinds of objects are most useful for navigation. From that data we derived a ranked list of object categories (famous landmarks, wayfinding signage, transit stations, etc.) and a set of objects to exclude (transient things like cars and pedestrians, generic signage, temporary advertisements).

The VLM pipeline takes sequences of panoramic street images and asks Gemini to identify and locate these navigation-relevant objects, producing bounding boxes, descriptions, and relevance scores.

## Solution Design

### Image Preprocessing

Panoramas are captured by a 360 camera that has an arbitrary forward direction. Before sending images to the VLM, each panorama is **rotated** so the walking direction is centered in the image. This uses the same `rotate_panorama` function from `models/dataloader.py` that the gaze heatmap pipeline uses, ensuring coordinate consistency. For reverse-direction segments, an additional 180-degree offset is applied.

At junctions, an additional rotation is applied to center the **turn direction** in the image. This uses the `turn_angle` field from the route JSON and is only applied when the previous frame (by `frame_order` within the segment) had `is_turn_warning` set to `true`. The rotation is a second `np.roll` applied after the walking-direction rotation.

### Batching and Prompt Templating

The input JSON is organized by segment (e.g., `{"segment1": [frames...], "segment2": [frames...]}`). Each segment is processed independently — batches never span segment boundaries. Within each segment, frames are sorted by `frame_order` and sent to Gemini in consecutive windows of `--window-size` (default 5). The last batch in a segment may be smaller than the window size.

`--max-frames` sets a global cap applied across segments in order. For example, with two segments of 7 and 8 frames and `--max-frames 10`, segment 1 gets all 7 and segment 2 gets 3.

The system prompt uses a `$NUM_IMAGES` placeholder that is substituted per batch with the actual image count, so the same prompt works for any batch size without manual editing.

### Structured Output

The Gemini response is constrained to a JSON schema via `response_mime_type` and `response_schema`. The schema is built dynamically to match the number of images in each batch (e.g., a batch of 3 produces a schema with keys `image1`, `image2`, `image3`). Each image key maps to an array of detections:

```json
{
  "bbox": [y_min, x_min, y_max, x_max],
  "description": "...",
  "landmark_score": 0.0
}
```

Bounding boxes use Gemini's normalized 0-1000 coordinate system.

### Detection Rules (enforced via prompt)

- **Cross-frame deduplication**: The same real-world object is reported in at most 2 images across a batch, preferring its earliest appearance.
- **Per-frame cap**: At most 2 objects per image.
- **Directional bias**: Objects near the center (walking direction) are prioritized.

## Directory Structure

```
vlm/
├── README.md
├── run_gemini.py                  # Main detection script
├── dedup_detections.py            # Post-processing deduplication
├── view_detections.py             # Browser-based detection viewer
└── prompt_engineering/
    ├── create_prompt_prompt.md     # Original spec used to draft the prompts
    └── system_prompt1.md           # System prompt template
```

## Usage

### Prerequisites

- Google Cloud project with Vertex AI enabled
- `GOOGLE_CLOUD_PROJECT` environment variable set (or pass `--project`), can also be set via `.env` file
- Python packages: `google-genai`, `numpy`, `Pillow`, `python-dotenv`

### Input Format

A JSON object mapping segment IDs to lists of frame objects:

```json
{
  "segment1": [
    {
      "frame_id": "GS010388_0005",
      "sequence_id": "GS010388_rev",
      "direction": "reverse",
      "frame_order": 120,
      "pano_path": "/absolute/path/to/panorama.jpg"
    }
  ],
  "segment2": [...]
}
```

Each frame must have at least `frame_id`, `frame_order`, `direction`, and `pano_path`. Other fields are preserved but not used by the script.

### Running

```bash
python vlm/run_gemini.py \
    --input frames.json \
    --output results.json \
    --system-prompt vlm/prompt_engineering/system_prompt1.md
```

### Options

| Flag              | Default        | Description                                      |
|-------------------|----------------|--------------------------------------------------|
| `--input`         | *(required)*   | Path to input JSON                               |
| `--output`        | *(required)*   | Path to output JSON                              |
| `--system-prompt` | *(required)*   | Path to system prompt `.md` template             |
| `--window-size`   | `5`            | Number of images per API call                    |
| `--max-frames`    | all            | Truncate input to first N frames (for testing)   |
| `--crop-width`    | none (full)    | Fraction of panorama width to keep, centered (e.g. `0.5` for middle 50%). Output bboxes are remapped to full panorama coordinates. |
| `--save-samples`  | `0`            | Save N random processed (post-rotation) images for testing |
| `--save-samples-dir` | `<output_dir>/sample_images` | Directory to save sample images |
| `--project`       | env var        | Google Cloud project ID                          |
| `--location`      | `global`       | Google Cloud region                              |

### Resumability and Error Handling

The script is designed to be safely re-runnable. Progress is saved after every batch, so if the process is interrupted or an API call fails, you can simply re-run the same command and it will pick up where it left off:

- **Completed batches** are skipped on re-run.
- **Errored batches** (e.g. API failures) are recorded with `"status": "error"` and automatically retried on the next run.
- **Changed batches** (e.g. if the input frames for a batch changed) are detected and re-run.

The console output indicates what happens to each batch:

```
  Batch 1/3 (seg1_b0): skipped (already completed)
  Batch 2/3 (seg1_b1): running (previous error: 429 Resource has been exhausted)
  Batch 3/3 (seg1_b2): running (new)
```

### Output Format

The output JSON is structured by batch, with metadata:

```json
{
  "metadata": {
    "window_size": 5,
    "crop_width": null
  },
  "batches": {
    "segment1_b0": {
      "segment_id": "segment1",
      "batch_index": 0,
      "frame_ids": ["GS010388_0005", "GS010388_0003"],
      "status": "completed",
      "results": {
        "GS010388_0005": [
          {
            "bbox": [120, 340, 450, 620],
            "description": "Large red torii gate at the entrance to a shrine.",
            "landmark_score": 0.92
          }
        ],
        "GS010388_0003": []
      }
    }
  }
}
```

Each batch entry contains its status (`"completed"` or `"error"`), the list of frame IDs it covers, and its results. Errored batches include an `"error"` field with the error message.

## Deduplication

`dedup_detections.py` is a post-processing step that removes duplicate detections of the same real-world object across consecutive frames. The Gemini prompt limits duplicates within a batch, but the same object can still appear in adjacent batches. This script uses DINOv3 visual embeddings to find and merge those cross-batch duplicates.

### How It Works

1. **Load & Flatten** — Reads the route JSON and detections JSON, producing a flat list of detections sorted by segment and frame order.
2. **Crop** — For each detection, crops the bounding box region from the rotated panorama (with a configurable buffer multiplier), handling 360° wrapping at image edges.
3. **Embed** — Passes all crops through DINOv3 (`facebook/dinov3-vitb16-pretrain-lvd1689m`) to get L2-normalized CLS token embeddings.
4. **Compare** — Within each segment, compares detections in nearby frames (sliding window) via cosine similarity.
5. **Merge** — Uses union-find to group transitively matched detections, then keeps only the earliest `--max-duplicates` per group.
6. **Reconstruct** — Writes a new JSON in the same format as the input detections, with duplicates removed.

### Dedup Usage

```bash
python vlm/dedup_detections.py \
    --input vlm/kinshicho_c/kinshicho_c_route.json \
    --detections vlm/output/kinshicho_c.json \
    --output vlm/output/kinshicho_c_deduped.json \
    --similarity-threshold 0.75 \
    --max-duplicates 2 \
    --window-size 5 \
    --batch-size 32 \
    --device mps
```

### Dedup Options

| Flag                     | Default      | Description                                              |
|--------------------------|--------------|----------------------------------------------------------|
| `--input`                | *(required)* | Route JSON (has `pano_path`, `frame_order`, `direction`) |
| `--detections`           | *(required)* | Detections JSON from `run_gemini.py`                     |
| `--output`               | *(required)* | Output path for deduplicated JSON                        |
| `--similarity-threshold` | `0.75`       | Cosine similarity threshold for matching                 |
| `--max-duplicates`       | `2`          | Max detections to keep per matched set                   |
| `--window-size`          | `5`          | Frame window for comparison (±half frames)               |
| `--batch-size`           | `32`         | Batch size for DINOv3 inference                          |
| `--device`               | auto         | `mps`/`cuda`/`cpu` (auto-detects MPS on macOS)           |
| `--buffer`               | `1.5`        | Crop buffer multiplier in each direction                 |

### Dedup Prerequisites

- Python packages: `torch`, `transformers`, `numpy`, `Pillow`

### Dedup Output

The output JSON has the same structure as the input detections JSON (same `metadata` and `batches` format), with duplicate detections filtered out. The deduplicated output can be used directly with `view_detections.py` for inspection.

The script prints a summary on completion:

```
=== Deduplication Summary ===
Total detections (before): 47
Total detections (after):  31
Matched sets found:        8
Detections removed:        16
Output written to: vlm/output/kinshicho_c_deduped.json
```

## Detection Viewer

`view_detections.py` launches a local browser-based viewer for inspecting detections overlaid on the panoramic images.

### Viewer Usage

```bash
python vlm/view_detections.py \
    --input route.json \
    --detections output.json \
    --image-dir /path/to/panoramas
```

| Flag           | Default  | Description                                  |
|----------------|----------|----------------------------------------------|
| `--input`      | *(required)* | Route JSON (for frame ordering)          |
| `--detections` | *(required)* | Output JSON from `run_gemini.py`         |
| `--image-dir`  | *(required)* | Directory containing panoramic images    |
| `--port`       | `8765`   | Port for the local HTTP server               |

### Viewer Features

- Navigate frames with left/right arrow keys or on-screen buttons
- Bounding boxes are drawn on the rotated panorama with color-coded labels
- Hover over a bounding box to see its description and score in a tooltip
- Info panel below the image shows all detections for the current frame
- Only frames with detections are shown (empty frames are filtered out)
- Images are rotated on-the-fly using the same `rotate_panorama` function, so bboxes align correctly
