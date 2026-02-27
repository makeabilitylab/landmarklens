#!/usr/bin/env python3
"""
GoPro Batch Color Correction Script
====================================
Corrects common GoPro color issues: desaturation, flat contrast, and color cast.

Usage:
    python gopro_color_correct.py INPUT_DIR [OUTPUT_DIR] [--preset PRESET] [OPTIONS]

Examples:
    python gopro_color_correct.py ./photos
    python gopro_color_correct.py ./photos ./corrected --preset vivid
    python gopro_color_correct.py ./photos --saturation 1.4 --contrast 1.2 --vibrance 0.3

Presets:
    natural  - Subtle correction (good starting point)
    vivid    - Punchy, saturated look (similar to Insta360 vivid mode)
    cinematic - Warm tones, lifted shadows, filmic contrast
    underwater - Corrects blue/green cast common in underwater GoPro shots
"""

import argparse
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

# ─── Preset Profiles ────────────────────────────────────────────────────────

PRESETS = {
    "natural": {
        "saturation": 1.25,
        "vibrance": 0.20,
        "contrast": 1.15,
        "brightness": 5,
        "shadow_lift": 10,
        "highlight_recovery": 10,
        "warmth": 3,          # slight warm shift (Kelvin-ish, applied to blue channel)
        "auto_white_balance": True,
        "sharpen": 0.3,
    },
    "vivid": {
        "saturation": 1.45,
        "vibrance": 0.35,
        "contrast": 1.25,
        "brightness": 5,
        "shadow_lift": 15,
        "highlight_recovery": 15,
        "warmth": 5,
        "auto_white_balance": True,
        "sharpen": 0.4,
    },
    "cinematic": {
        "saturation": 1.15,
        "vibrance": 0.15,
        "contrast": 1.20,
        "brightness": 0,
        "shadow_lift": 25,       # lifted blacks for filmic look
        "highlight_recovery": 20,
        "warmth": 8,
        "auto_white_balance": True,
        "sharpen": 0.2,
    },
    "underwater": {
        "saturation": 1.40,
        "vibrance": 0.30,
        "contrast": 1.20,
        "brightness": 10,
        "shadow_lift": 15,
        "highlight_recovery": 10,
        "warmth": 15,            # strong warm shift to counter blue cast
        "auto_white_balance": True,
        "sharpen": 0.5,
    },
}

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp"}

# ─── Core Color Correction Functions ────────────────────────────────────────


def auto_white_balance(img: np.ndarray) -> np.ndarray:
    """Gray-world automatic white balance correction."""
    result = img.astype(np.float32)
    avg_b = np.mean(result[:, :, 0])
    avg_g = np.mean(result[:, :, 1])
    avg_r = np.mean(result[:, :, 2])
    avg_gray = (avg_b + avg_g + avg_r) / 3.0

    result[:, :, 0] = np.clip(result[:, :, 0] * (avg_gray / (avg_b + 1e-6)), 0, 255)
    result[:, :, 1] = np.clip(result[:, :, 1] * (avg_gray / (avg_g + 1e-6)), 0, 255)
    result[:, :, 2] = np.clip(result[:, :, 2] * (avg_gray / (avg_r + 1e-6)), 0, 255)

    return result.astype(np.uint8)


def adjust_saturation(img: np.ndarray, factor: float) -> np.ndarray:
    """Adjust overall saturation. factor > 1 = more saturated."""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).astype(np.float32)
    hsv[:, :, 1] = np.clip(hsv[:, :, 1] * factor, 0, 255)
    return cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)


def adjust_vibrance(img: np.ndarray, amount: float) -> np.ndarray:
    """
    Vibrance: selectively boosts saturation of muted colors more than
    already-saturated colors. amount is 0.0 to 1.0.
    """
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).astype(np.float32)
    saturation = hsv[:, :, 1]

    # Muted pixels get a bigger boost; saturated pixels get less
    # Inverse relationship: lower sat -> higher boost factor
    max_sat = 255.0
    boost = amount * (1.0 - saturation / max_sat)  # 0..amount range
    hsv[:, :, 1] = np.clip(saturation * (1.0 + boost), 0, 255)

    return cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)


def adjust_contrast_brightness(
    img: np.ndarray, contrast: float, brightness: int
) -> np.ndarray:
    """Adjust contrast (multiplicative) and brightness (additive)."""
    result = img.astype(np.float32)
    result = result * contrast + brightness
    return np.clip(result, 0, 255).astype(np.uint8)


def lift_shadows(img: np.ndarray, amount: int) -> np.ndarray:
    """Lift shadow tones without blowing highlights. amount = 0..50."""
    if amount <= 0:
        return img
    # Use a tone curve that lifts blacks
    lut = np.arange(256, dtype=np.float32)
    # Lift formula: stronger effect on dark values, tapering off at midtones
    lut = lut + amount * np.exp(-lut / 60.0)
    lut = np.clip(lut, 0, 255).astype(np.uint8)
    return cv2.LUT(img, lut)


def recover_highlights(img: np.ndarray, amount: int) -> np.ndarray:
    """Pull back blown highlights. amount = 0..50."""
    if amount <= 0:
        return img
    lut = np.arange(256, dtype=np.float32)
    # Compress highlights: stronger effect near 255
    lut = lut - amount * np.exp(-(255 - lut) / 60.0)
    lut = np.clip(lut, 0, 255).astype(np.uint8)
    return cv2.LUT(img, lut)


def adjust_warmth(img: np.ndarray, amount: int) -> np.ndarray:
    """
    Shift color temperature. Positive = warmer (reduce blue, boost red slightly).
    amount roughly -30..+30.
    """
    if amount == 0:
        return img
    result = img.astype(np.float32)
    result[:, :, 0] = np.clip(result[:, :, 0] - amount, 0, 255)  # blue
    result[:, :, 2] = np.clip(result[:, :, 2] + amount * 0.5, 0, 255)  # red
    return result.astype(np.uint8)


def sharpen(img: np.ndarray, amount: float) -> np.ndarray:
    """Light unsharp mask. amount 0.0..1.0."""
    if amount <= 0:
        return img
    blurred = cv2.GaussianBlur(img, (0, 0), sigmaX=2)
    sharpened = cv2.addWeighted(img, 1.0 + amount, blurred, -amount, 0)
    return sharpened


# ─── Pipeline ────────────────────────────────────────────────────────────────


def correct_image(img: np.ndarray, params: dict) -> np.ndarray:
    """Apply the full color correction pipeline to an image."""
    result = img.copy()

    # 1. Auto white balance (first, so other adjustments work on corrected colors)
    if params.get("auto_white_balance", False):
        result = auto_white_balance(result)

    # 2. Warmth shift
    result = adjust_warmth(result, params.get("warmth", 0))

    # 3. Contrast & brightness
    result = adjust_contrast_brightness(
        result, params.get("contrast", 1.0), params.get("brightness", 0)
    )

    # 4. Shadow lift & highlight recovery
    result = lift_shadows(result, params.get("shadow_lift", 0))
    result = recover_highlights(result, params.get("highlight_recovery", 0))

    # 5. Saturation
    result = adjust_saturation(result, params.get("saturation", 1.0))

    # 6. Vibrance (after saturation so it can fine-tune)
    result = adjust_vibrance(result, params.get("vibrance", 0.0))

    # 7. Sharpen (last step)
    result = sharpen(result, params.get("sharpen", 0.0))

    return result


# ─── Batch Processing ───────────────────────────────────────────────────────


def find_images(input_dir: str) -> list[Path]:
    """Recursively find supported image files."""
    images = []
    for f in sorted(Path(input_dir).rglob("*")):
        if f.suffix.lower() in SUPPORTED_EXTENSIONS and f.is_file():
            images.append(f)
    return images


def process_batch(input_dir: str, output_dir: str, params: dict, quality: int = 95):
    """Process all images in input_dir and save to output_dir."""
    input_path = Path(input_dir)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    images = find_images(input_dir)
    if not images:
        print(f"No supported images found in {input_dir}")
        print(f"Supported formats: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
        return

    print(f"\n{'='*60}")
    print(f"  GoPro Color Correction")
    print(f"{'='*60}")
    print(f"  Input:  {input_path.resolve()}")
    print(f"  Output: {output_path.resolve()}")
    print(f"  Images: {len(images)}")
    print(f"  Settings:")
    for k, v in sorted(params.items()):
        print(f"    {k:>22s}: {v}")
    print(f"{'='*60}\n")

    start_all = time.time()

    for i, img_path in enumerate(images, 1):
        # Preserve subdirectory structure
        relative = img_path.relative_to(input_path)
        out_file = output_path / relative
        out_file.parent.mkdir(parents=True, exist_ok=True)

        print(f"  [{i}/{len(images)}] {relative} ... ", end="", flush=True)
        start = time.time()

        img = cv2.imread(str(img_path), cv2.IMREAD_COLOR)
        if img is None:
            print("SKIPPED (could not read)")
            continue

        corrected = correct_image(img, params)

        # Save in same format as input
        ext = img_path.suffix.lower()
        if ext in (".jpg", ".jpeg"):
            cv2.imwrite(str(out_file), corrected, [cv2.IMWRITE_JPEG_QUALITY, quality])
        elif ext == ".png":
            cv2.imwrite(str(out_file), corrected, [cv2.IMWRITE_PNG_COMPRESSION, 3])
        elif ext == ".webp":
            cv2.imwrite(str(out_file), corrected, [cv2.IMWRITE_WEBP_QUALITY, quality])
        else:
            cv2.imwrite(str(out_file), corrected)

        elapsed = time.time() - start
        print(f"done ({elapsed:.1f}s)")

    total = time.time() - start_all
    print(f"\n  All done! {len(images)} images processed in {total:.1f}s\n")


# ─── CLI ─────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Batch color correction for GoPro images.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("input_dir", help="Folder with GoPro images")
    p.add_argument(
        "output_dir",
        nargs="?",
        default=None,
        help="Output folder (default: INPUT_DIR_corrected)",
    )
    p.add_argument(
        "--preset",
        choices=list(PRESETS.keys()),
        default="natural",
        help="Color correction preset (default: natural)",
    )
    p.add_argument("--saturation", type=float, help="Saturation multiplier (e.g. 1.3)")
    p.add_argument("--vibrance", type=float, help="Vibrance amount 0.0-1.0")
    p.add_argument("--contrast", type=float, help="Contrast multiplier (e.g. 1.2)")
    p.add_argument("--brightness", type=int, help="Brightness offset (-50 to 50)")
    p.add_argument("--shadow-lift", type=int, help="Shadow lift amount (0-50)")
    p.add_argument("--highlight-recovery", type=int, help="Highlight recovery (0-50)")
    p.add_argument("--warmth", type=int, help="Warmth shift (-30 to 30)")
    p.add_argument("--sharpen", type=float, help="Sharpen amount 0.0-1.0")
    p.add_argument(
        "--no-awb", action="store_true", help="Disable auto white balance"
    )
    p.add_argument(
        "--quality", type=int, default=95, help="JPEG/WebP quality 1-100 (default: 95)"
    )
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not os.path.isdir(args.input_dir):
        print(f"Error: '{args.input_dir}' is not a directory.")
        sys.exit(1)

    # Start from preset, then override with any CLI args
    params = dict(PRESETS[args.preset])

    if args.saturation is not None:
        params["saturation"] = args.saturation
    if args.vibrance is not None:
        params["vibrance"] = args.vibrance
    if args.contrast is not None:
        params["contrast"] = args.contrast
    if args.brightness is not None:
        params["brightness"] = args.brightness
    if args.shadow_lift is not None:
        params["shadow_lift"] = args.shadow_lift
    if args.highlight_recovery is not None:
        params["highlight_recovery"] = args.highlight_recovery
    if args.warmth is not None:
        params["warmth"] = args.warmth
    if args.sharpen is not None:
        params["sharpen"] = args.sharpen
    if args.no_awb:
        params["auto_white_balance"] = False

    output_dir = args.output_dir or f"{args.input_dir.rstrip('/')}_corrected"

    process_batch(args.input_dir, output_dir, params, quality=args.quality)


if __name__ == "__main__":
    main()
