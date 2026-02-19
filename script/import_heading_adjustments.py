#!/usr/bin/env python3
"""
Import heading adjustments from exported browser JSON into data.json.

The browser exports keyframes — only the frames where the user explicitly set
a heading. This script expands keyframes forward within each video source,
writing yaw/pitch/roll to every affected frame in data.json.

Usage:
    python script/import_heading_adjustments.py heading-adjustments-2026-02-03.json

    # Preview without making changes
    python script/import_heading_adjustments.py heading-adjustments.json --dry-run

    # Only import for a specific neighborhood
    python script/import_heading_adjustments.py heading-adjustments.json --neighborhood ginza

    # Clear all heading adjustments from data.json
    python script/import_heading_adjustments.py --clear
"""

import sys
import json
import argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = REPO_ROOT / "web" / "data.json"


def load_data() -> dict:
    with open(DATA_PATH, 'r') as f:
        return json.load(f)


def save_data(data: dict) -> None:
    with open(DATA_PATH, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"  Saved {DATA_PATH}")


def load_adjustments(adjustments_path: Path) -> tuple[dict, bool]:
    """Load adjustments JSON. Returns (adjustments_dict, is_keyframe_mode)."""
    with open(adjustments_path, 'r') as f:
        data = json.load(f)

    is_keyframe = data.get('mode') == 'keyframe'

    if 'adjustments' in data:
        return data['adjustments'], is_keyframe
    return data, False


def import_adjustments(adjustments_path: Path, dry_run: bool = False,
                       neighborhood: str = None) -> None:
    print("=" * 60)
    print("Import Heading Adjustments")
    print("=" * 60)

    data = load_data()
    adjustments, is_keyframe = load_adjustments(adjustments_path)

    frames = data.get('frames', [])
    total = len(frames)

    print(f"  Data file: {DATA_PATH}")
    print(f"  Adjustments file: {adjustments_path}")
    print(f"  Frames in data.json: {total}")
    print(f"  Keyframes to import: {len(adjustments)}")
    print(f"  Mode: {'keyframe (propagating)' if is_keyframe else 'per-frame'}")
    if neighborhood:
        print(f"  Filter: neighborhood={neighborhood}")
    print(f"  {'DRY RUN' if dry_run else 'LIVE'}")
    print("=" * 60)

    if not adjustments:
        print("\nNo adjustments to import.")
        return

    filename_to_idx = {f['filename']: i for i, f in enumerate(frames)}

    # Verify all keyframe filenames exist in data
    not_found = []
    for filename in adjustments:
        if filename not in filename_to_idx:
            not_found.append(filename)

    if not_found:
        print(f"\n  WARNING: {len(not_found)} keyframe(s) not found in data.json:")
        for nf in not_found[:10]:
            print(f"    - {nf}")
        if len(not_found) > 10:
            print(f"    ... and {len(not_found) - 10} more")

    if is_keyframe:
        updated = _expand_keyframes(frames, adjustments, filename_to_idx,
                                    neighborhood, dry_run)
    else:
        updated = _apply_per_frame(frames, adjustments, filename_to_idx,
                                   neighborhood, dry_run)

    print(f"\n  Updated: {updated} frames")

    if not dry_run and updated > 0:
        save_data(data)
        print("  Done!")
    elif dry_run:
        print("  (Dry run — no changes made)")


def _expand_keyframes(frames, adjustments, filename_to_idx,
                      neighborhood, dry_run) -> int:
    """Expand keyframes forward within each source. Returns count of updated frames."""
    # Group frames by source, preserving order
    source_groups = defaultdict(list)
    for i, f in enumerate(frames):
        if neighborhood and f.get('neighborhood') != neighborhood:
            continue
        source_groups[f['source']].append(i)

    updated = 0

    for source, indices in source_groups.items():
        # Find keyframes in this source
        active_adj = None
        source_keyframes = 0

        for idx in indices:
            frame = frames[idx]
            filename = frame['filename']

            if filename in adjustments:
                active_adj = adjustments[filename]
                source_keyframes += 1

            if active_adj is not None:
                yaw = round(active_adj.get('yaw', 0), 2)
                pitch = round(active_adj.get('pitch', 0), 2)
                roll = round(active_adj.get('roll', 0), 2)

                if not dry_run:
                    frame['yaw'] = yaw
                    frame['pitch'] = pitch
                    if roll != 0:
                        frame['roll'] = roll
                    elif 'roll' in frame:
                        del frame['roll']
                updated += 1

        if source_keyframes > 0:
            print(f"\n  Source {source}: {source_keyframes} keyframe(s), "
                  f"{len(indices)} total frames")

    return updated


def _apply_per_frame(frames, adjustments, filename_to_idx,
                     neighborhood, dry_run) -> int:
    """Apply adjustments directly per frame (legacy non-keyframe mode)."""
    updated = 0
    for filename, adj in adjustments.items():
        if filename not in filename_to_idx:
            continue
        idx = filename_to_idx[filename]
        frame = frames[idx]

        if neighborhood and frame.get('neighborhood') != neighborhood:
            continue

        yaw = round(adj.get('yaw', 0), 2)
        pitch = round(adj.get('pitch', 0), 2)
        roll = round(adj.get('roll', 0), 2)

        if not dry_run:
            frame['yaw'] = yaw
            frame['pitch'] = pitch
            if roll != 0:
                frame['roll'] = roll
            elif 'roll' in frame:
                del frame['roll']

        updated += 1
    return updated


def clear_adjustments(dry_run: bool = False, neighborhood: str = None) -> None:
    print("=" * 60)
    print("Clear Heading Adjustments")
    print("=" * 60)

    data = load_data()
    frames = data.get('frames', [])

    print(f"  Frames in data.json: {len(frames)}")
    if neighborhood:
        print(f"  Filter: neighborhood={neighborhood}")
    print(f"  {'DRY RUN' if dry_run else 'LIVE'}")
    print("=" * 60)

    cleared = 0
    for frame in frames:
        if neighborhood and frame.get('neighborhood') != neighborhood:
            continue
        if 'yaw' in frame or 'pitch' in frame or 'roll' in frame:
            if not dry_run:
                frame.pop('yaw', None)
                frame.pop('pitch', None)
                frame.pop('roll', None)
            cleared += 1

    print(f"\n  Cleared adjustments from: {cleared} frames")

    if not dry_run and cleared > 0:
        save_data(data)
        print("  Done!")
    elif dry_run:
        print("  (Dry run — no changes made)")


def main():
    parser = argparse.ArgumentParser(
        description="Import heading adjustments from exported browser JSON into data.json"
    )
    parser.add_argument(
        "adjustments_file",
        nargs="?",
        help="Path to the exported heading adjustments JSON file"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without modifying data.json"
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear all heading adjustments from data.json"
    )
    parser.add_argument(
        "--neighborhood",
        type=str,
        default=None,
        help="Only import/clear for a specific neighborhood (e.g. ginza, nezu)"
    )

    args = parser.parse_args()

    if args.clear:
        clear_adjustments(args.dry_run, args.neighborhood)
    elif args.adjustments_file:
        adjustments_path = Path(args.adjustments_file)
        if not adjustments_path.is_absolute():
            adjustments_path = Path.cwd() / adjustments_path

        if not adjustments_path.exists():
            print(f"Error: File not found: {adjustments_path}")
            sys.exit(1)

        import_adjustments(adjustments_path, args.dry_run, args.neighborhood)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
