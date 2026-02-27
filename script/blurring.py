#!/usr/bin/env python3
"""
Batch Face Blurring Script
===========================
Detects and blurs faces in panorama images using MTCNN.

Usage:
    python script/blurring.py INPUT_DIR OUTPUT_DIR [OPTIONS]

Examples:
    python script/blurring.py data/frames_unblurred data/frames
    python script/blurring.py data/frames_unblurred data/frames --workers 8
    python script/blurring.py data/frames_unblurred data/frames --detect-scale 0.5
    python script/blurring.py data/frames_unblurred data/frames --workers 8 --detect-scale 0.5
"""

import argparse
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

# Global per-worker detector (initialized once per process)
_mtcnn = None
_detect_scale = None


def _init_worker(detect_scale):
    """Called once per worker process to initialize its own MTCNN instance."""
    global _mtcnn, _detect_scale
    import torch
    from facenet_pytorch import MTCNN

    _detect_scale = detect_scale
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    _mtcnn = MTCNN(
        keep_all=True, device=device, min_face_size=20,
        thresholds=[0.5, 0.6, 0.6],
    )


def _process_single(args):
    """Process a single image. Designed to run in a worker process."""
    img_path, out_file, quality = args
    img = cv2.imread(str(img_path), cv2.IMREAD_COLOR)
    if img is None:
        return img_path.name, -1, 0.0

    start = time.time()
    h, w = img.shape[:2]
    img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

    # Detect faces — optionally at lower resolution for speed
    det_img = img_rgb
    scale = _detect_scale
    if scale and scale < 1.0:
        det_img = cv2.resize(img_rgb, (int(w * scale), int(h * scale)))

    all_boxes = []
    boxes, _ = _mtcnn.detect(det_img)
    if boxes is not None:
        for box in boxes:
            x1, y1, x2, y2 = box
            if scale and scale < 1.0:
                x1, y1, x2, y2 = x1 / scale, y1 / scale, x2 / scale, y2 / scale
            all_boxes.append((int(x1), int(y1), int(x2), int(y2)))

    # Apply blur on full-res image
    for (x1, y1, x2, y2) in all_boxes:
        pad_w = int((x2 - x1) * 0.2)
        pad_h = int((y2 - y1) * 0.2)
        x1 = max(0, x1 - pad_w)
        y1 = max(0, y1 - pad_h)
        x2 = min(w, x2 + pad_w)
        y2 = min(h, y2 + pad_h)

        face_region = img[y1:y2, x1:x2]
        if face_region.size > 0:
            blur_amount = max(99, (x2 - x1) // 2 * 2 + 1)
            blurred = cv2.GaussianBlur(face_region, (blur_amount, blur_amount), 30)
            img[y1:y2, x1:x2] = blurred

    Path(out_file).parent.mkdir(parents=True, exist_ok=True)
    ext = Path(img_path).suffix.lower()
    if ext in (".jpg", ".jpeg"):
        cv2.imwrite(str(out_file), img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    elif ext == ".png":
        cv2.imwrite(str(out_file), img, [cv2.IMWRITE_PNG_COMPRESSION, 3])
    elif ext == ".webp":
        cv2.imwrite(str(out_file), img, [cv2.IMWRITE_WEBP_QUALITY, quality])
    else:
        cv2.imwrite(str(out_file), img)

    elapsed = time.time() - start
    return img_path.name, len(all_boxes), elapsed


def find_images(input_dir: str) -> list[Path]:
    images = []
    for f in sorted(Path(input_dir).rglob("*")):
        if f.suffix.lower() in SUPPORTED_EXTENSIONS and f.is_file():
            images.append(f)
    return images


def process_batch(
    input_dir: str,
    output_dir: str,
    quality: int = 95,
    workers: int = 1,
    detect_scale: float = None,
):
    input_path = Path(input_dir)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    images = find_images(input_dir)
    if not images:
        print(f"No supported images found in {input_dir}")
        return

    tasks = []
    for img_path in images:
        relative = img_path.relative_to(input_path)
        out_file = output_path / relative
        tasks.append((img_path, str(out_file), quality))

    print(f"\n{'=' * 60}")
    print(f"  Face Blurring")
    print(f"{'=' * 60}")
    print(f"  Input:    {input_path.resolve()}")
    print(f"  Output:   {output_path.resolve()}")
    print(f"  Images:   {len(images)}")
    print(f"  Workers:  {workers}")
    if detect_scale:
        print(f"  Detect @: {detect_scale:.0%} resolution")
    print(f"{'=' * 60}\n")

    start_all = time.time()
    total_faces = 0
    done = 0

    if workers <= 1:
        _init_worker(detect_scale)
        for i, task in enumerate(tasks, 1):
            name, face_count, elapsed = _process_single(task)
            if face_count < 0:
                print(f"  [{i}/{len(tasks)}] {name} ... SKIPPED")
            else:
                total_faces += face_count
                print(f"  [{i}/{len(tasks)}] {name} ... {face_count} face(s) ({elapsed:.1f}s)")
    else:
        ctx = mp.get_context("spawn")
        with ctx.Pool(workers, initializer=_init_worker, initargs=(detect_scale,)) as pool:
            for name, face_count, elapsed in pool.imap_unordered(_process_single, tasks):
                done += 1
                if face_count < 0:
                    print(f"  [{done}/{len(tasks)}] {name} ... SKIPPED")
                else:
                    total_faces += face_count
                    print(f"  [{done}/{len(tasks)}] {name} ... {face_count} face(s) ({elapsed:.1f}s)")

    total = time.time() - start_all
    print(f"\n  All done! {len(images)} images, {total_faces} faces blurred in {total:.1f}s\n")


def main():
    parser = argparse.ArgumentParser(
        description="Batch face blurring for panorama images using MTCNN.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input_dir", help="Folder with images (recursively scanned)")
    parser.add_argument("output_dir", help="Output folder (subdirectory structure preserved)")
    parser.add_argument(
        "--quality", type=int, default=95,
        help="JPEG/WebP quality 1-100 (default: 95)",
    )
    parser.add_argument(
        "--workers", "-j", type=int, default=None,
        help="Number of parallel workers (default: half your CPU cores)",
    )
    parser.add_argument(
        "--detect-scale", type=float, default=None,
        help="Run detection at this fraction of full resolution (e.g. 0.5 = half size, ~4x faster). "
             "Blur is still applied at full resolution.",
    )
    args = parser.parse_args()

    if not os.path.isdir(args.input_dir):
        print(f"Error: '{args.input_dir}' is not a directory.")
        sys.exit(1)

    workers = args.workers or max(1, (os.cpu_count() or 1) // 2)

    process_batch(
        args.input_dir, args.output_dir,
        quality=args.quality,
        workers=workers,
        detect_scale=args.detect_scale,
    )


if __name__ == "__main__":
    main()
