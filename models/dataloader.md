# Gaze Heatmap Dataloader

PyTorch dataloader for feeding panoramic street-view images and their corresponding gaze heatmaps into ML models.

## Data Layout

The dataloader expects two directory trees:

```
study-data/route-heatmaps/combined/npy/
  {group}/{region}/{route_id}/{sequence}/{frame}.npy

data/frames/
  {sequence}/{frame}.jpg
```

- **group**: `good` or `poor` (sense of direction)
- **region**: `ginza`, `kinshicho`, `koenji`, `nezu`
- **route_id**: e.g. `ginza_c`
- **sequence/frame**: e.g. `GS010323/GS010323_0121`

Heatmaps are float32 numpy arrays (480x960), normalized to [0, 1]. Source panoramas are 3840x1920 JPGs, resized to match heatmap resolution by default.

The dataloader also reads orientation data from `web/data.json`, which provides per-frame `yaw` and `pitch` values (the default viewing direction used during gaze data collection).

## Orientation Correction

Panorama images are stored in their raw equirectangular orientation. During gaze data collection, each frame was displayed with a specific yaw as the default viewing direction. The VR app rotates the panorama sphere by `-yaw` around Y (`ApplySphereOrientation`), and `EyeGazeLogger` records gaze UV in world-space. The dataloader automatically rotates panorama images horizontally to match the heatmap coordinate system.

- **Yaw** (horizontal, `-180` to `180`): The VR sphere is rotated by yaw, so `hitU` is in world-space and does not directly correspond to a pixel column in the raw image. The dataloader corrects this by rolling the panorama horizontally by `-yaw/360 * W` pixels, which puts the panorama into the same coordinate space as the heatmap. For `_rev` sequences, `export_route_vr.py` adds 180° to yaw (flipping the view); `generate_route_heatmaps.py` accounts for this with a `rev_offset` of 0.5 in the gaze UV formula.
- **Pitch** (`-15` to `15`): **Not applied.** The VR app only rotates the sphere by yaw, not pitch — pitch/roll are baked heading adjustments stored as metadata but do not affect the sphere orientation. This means `hitV` is already in texture-space (maps directly to pixel rows in the raw image). Rolling the panorama by pitch would misalign it with the heatmap.

### Why yaw "cancels out"

`generate_route_heatmaps.py` intentionally omits the per-frame yaw from the gaze UV formula:

```
u = (u_raw + rev_offset + SPHERE_UV_OFFSET) % 1.0     # no yaw term
```

Compare with `generate_heatmaps.py` which includes it:

```
u = (u_raw + vr_yaw/360 + SPHERE_UV_OFFSET) % 1.0     # yaw included
```

The difference is that `generate_heatmaps.py` composites onto the **raw** panorama (so gaze must be in texture-space), while `generate_route_heatmaps.py` produces `.npy` heatmaps consumed by this dataloader, which rolls the panorama by `-yaw`. Since both the gaze UV and the panorama are shifted by the same yaw offset, the yaw terms cancel — only the `rev_offset` and `SPHERE_UV_OFFSET` remain.

When `generate_route_heatmaps.py` saves composite frames (`--save-frames`), it also rolls the panorama by yaw before overlaying the heatmap, maintaining the same alignment.

If `web/data.json` is missing, a warning is issued and no rotation is applied.

## Quick Start

```python
from models.dataloader import get_dataloaders

(train_loader, val_loader, test_loader), split_info = get_dataloaders()

for batch in train_loader:
    images = batch["image"]      # (B, 3, 480, 960)
    heatmaps = batch["heatmap"]  # (B, 1, 480, 960)
    groups = batch["group"]      # list of str
    regions = batch["region"]    # list of str
    # ...
```

## Train/Val/Test Splitting

The `atomicity` parameter controls what unit is kept together during splitting, to prevent data leakage:

| Atomicity | Split unit | Description |
|-----------|-----------|-------------|
| `"route"` (default) | `(group, route_id)` | All frames from a route stay in the same split. 68 route keys are shuffled and divided. |
| `"sequence"` | `(group, sequence)` | All frames from a sequence (e.g. `GS010331`) stay together. Finer than route (a route contains multiple sequences), coarser than none. |
| `"region"` | region name | Entire regions are assigned to splits via greedy bin-packing. Only 4 regions, so splits will be coarse. |
| `"none"` | individual frame | Frames are shuffled independently. Fastest to balance, but frames from the same route may appear in multiple splits. |

## API Reference

### `get_dataloaders(...)`

Factory function returning `((train_loader, val_loader, test_loader), split_info)`.

```python
(train_loader, val_loader, test_loader), split_info = get_dataloaders(
    npy_root="study-data/route-heatmaps/combined/npy",
    panorama_root="data/frames",
    ratios=(0.7, 0.15, 0.15),   # train/val/test proportions
    atomicity="route",           # "route", "sequence", "region", or "none"
    seed=42,
    batch_size=8,
    num_workers=4,
    transform=None,              # custom image transform (overrides default resize)
    target_size=(480, 960),      # (H, W) output resolution
    groups=None,                 # "good", "poor", or None for both
    routes=None,                 # route_id(s) to include, or None for all
)
```

#### `groups` parameter

Controls which participant groups are included in the data:

| Value | Effect |
|-------|--------|
| `None` (default) | Include both `"good"` and `"poor"` groups |
| `"good"` | Include only good sense-of-direction participants |
| `"poor"` | Include only poor sense-of-direction participants |
| `{"good", "poor"}` | Explicit set, equivalent to `None` |

```python
# Good group only
(train_loader, val_loader, test_loader), split_info = get_dataloaders(groups="good")

# Poor group only
(train_loader, val_loader, test_loader), split_info = get_dataloaders(groups="poor")
```

#### `routes` parameter

Controls which routes are included in the data:

| Value | Effect |
|-------|--------|
| `None` (default) | Include all routes |
| `"ginza_c"` | Include only the `ginza_c` route |
| `{"ginza_c", "nezu_a"}` | Include only the specified routes |

```python
# Single route
(train_loader, val_loader, test_loader), split_info = get_dataloaders(routes="ginza_c")

# Multiple routes
(train_loader, val_loader, test_loader), split_info = get_dataloaders(routes={"ginza_c", "nezu_a"})
```

`split_info` is a dict describing how data was divided:

```python
# atomicity="route"
{
    "atomicity": "route",
    "train": {"routes": [("good", "ginza_c"), ...], "n_samples": 420},
    "val":   {"routes": [...], "n_samples": 90},
    "test":  {"routes": [...], "n_samples": 90},
}

# atomicity="sequence"
{
    "atomicity": "sequence",
    "train": {"sequences": [("good", "GS010323"), ...], "n_samples": 420},
    "val":   {"sequences": [...], "n_samples": 90},
    "test":  {"sequences": [...], "n_samples": 90},
}

# atomicity="region"
{
    "atomicity": "region",
    "train": {"regions": ["ginza", "koenji"], "n_samples": 420},
    ...
}

# atomicity="none"
{
    "atomicity": "none",
    "train": {"n_samples": 420},
    ...
}
```

### `GazeHeatmapDataset`

The underlying `torch.utils.data.Dataset`. Each item is a dict:

| Key | Type | Shape/Value |
|-----|------|-------------|
| `image` | `torch.FloatTensor` | `(3, H, W)`, values in [0, 1] |
| `heatmap` | `torch.FloatTensor` | `(1, H, W)`, values in [0, 1] |
| `group` | `str` | `"good"` or `"poor"` |
| `region` | `str` | e.g. `"ginza"` |
| `route_id` | `str` | e.g. `"ginza_c"` |
| `frame` | `str` | e.g. `"GS010323_0121"` |

### `split_samples(samples, ratios, atomicity, seed)`

Standalone split function if you need direct access to sample lists without creating DataLoaders. Returns `(train, val, test, split_info)`.

```python
from models.dataloader import _discover_samples, split_samples

samples = _discover_samples("study-data/route-heatmaps/combined/npy", "data/frames", groups="good")
train, val, test, split_info = split_samples(samples, ratios=(0.8, 0.1, 0.1), atomicity="route")
```

## Custom Transforms

Pass a `transform` callable to override the default image preprocessing. When provided, it receives a `PIL.Image` and should return a `(3, H, W)` tensor. The orientation rotation is applied **after** the custom transform, so the transform operates on the raw (unrotated) image. The heatmap is always loaded from the `.npy` file and resized to `target_size` independently.

```python
from torchvision import transforms

t = transforms.Compose([
    transforms.Resize((480, 960)),
    transforms.ColorJitter(brightness=0.2),
    transforms.ToTensor(),
])

(train_loader, val_loader, test_loader), split_info = get_dataloaders(transform=t)
```
