"""
Orientation utilities for VLM panorama processing.

Reads per-frame yaw/pitch from geodata/panos.geojson and provides
panorama rotation so the walking direction is centered in the image.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Dict, Optional, Tuple, Union

import numpy as np

BASE_DIR = Path(__file__).resolve().parent.parent
PANOS_GEOJSON = BASE_DIR / "geodata" / "panos.geojson"


def load_orientation_lookup(path: Optional[Union[str, Path]] = None) -> Dict[str, Tuple[float, float]]:
    """Build {frame_id: (yaw, pitch)} from geodata/panos.geojson.

    Frames without yaw/pitch corrections get (0.0, 0.0).
    """
    path = Path(path) if path else PANOS_GEOJSON
    if not path.exists():
        warnings.warn(f"{path} not found — panoramas will not be orientation-corrected")
        return {}

    with open(path) as f:
        data = json.load(f)

    lookup: dict[str, tuple[float, float]] = {}
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        frame_id = props.get("frame_id", "")
        yaw = props.get("yaw") or 0.0
        pitch = props.get("pitch") or 0.0
        lookup[frame_id] = (float(yaw), float(pitch))

    return lookup


def rotate_panorama(
    image: np.ndarray,
    frame_id: str,
    orientation_lookup: dict,
    is_reverse: bool = False,
) -> np.ndarray:
    """Horizontally roll a panorama so the walking direction is centered.

    Args:
        image: numpy array with shape (H, W) or (H, W, C).
        frame_id: frame identifier for orientation lookup.
        orientation_lookup: {frame_id: (yaw, pitch)} dict.
        is_reverse: whether this frame belongs to a reverse segment
            (adds 180° to yaw).

    Returns:
        Rolled copy of image (or the original if yaw == 0).
    """
    yaw, _pitch = orientation_lookup.get(frame_id, (0.0, 0.0))
    if is_reverse:
        yaw = (yaw + 180.0) % 360.0
    if yaw == 0.0:
        return image
    width = image.shape[1]
    shift_x = -round(yaw / 360.0 * width)
    return np.roll(image, shift_x, axis=1)
