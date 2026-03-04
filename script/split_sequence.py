#!/usr/bin/env python3
"""
Add or remove sequence splits so a single video source is treated as multiple
sequences (e.g. when the path turns mid-source).

Splits are stored in web/data.json under the top-level key "sequence_splits":
  { "GS010366": ["0084"], ... }
meaning: after frame GS010366_0084, a new sequence starts (0086 is the first
frame of the next segment). export_route_vr.py uses this to split segments.

Turn angle overrides are stored under "split_turn_overrides":
  { "GS010354_0051": -90 }
meaning: at the split after GS010354_0051, force a -90° (left) turn instead
of computing from GPS. Both the web viewer and VR export use this.

Usage:
  # Split GS010366 after frame 0084
  python script/split_sequence.py --source GS010366 --after-frame 0084

  # Split with a forced turn angle (negative = left, positive = right)
  python script/split_sequence.py --source GS010354 --after-frame 0051 --turn-angle -90

  # Update just the turn angle on an existing split
  python script/split_sequence.py --source GS010354 --after-frame 0051 --turn-angle -90

  # Remove that split
  python script/split_sequence.py --source GS010366 --after-frame 0084 --remove

  # List all splits (optionally for one source)
  python script/split_sequence.py --list
  python script/split_sequence.py --list --source GS010366

  # Dry run (don't write data.json)
  python script/split_sequence.py --source GS010366 --after-frame 0084 --dry-run
"""

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_JSON = BASE_DIR / "web" / "data.json"


def normalize_frame_num(s: str) -> str:
    """Normalize to 4-digit frame number (e.g. 84 -> 0084)."""
    return str(int(s)).zfill(4)


def override_key(source: str, frame_num: str) -> str:
    return f"{source}_{frame_num}"


def get_splits(data: dict) -> dict:
    return data.setdefault("sequence_splits", {})


def get_overrides(data: dict) -> dict:
    return data.setdefault("split_turn_overrides", {})


def main():
    parser = argparse.ArgumentParser(
        description="Add or remove sequence splits (split one source into two segments)."
    )
    parser.add_argument("--data", type=str, default=str(DATA_JSON),
                        help="Path to web/data.json")
    parser.add_argument("--source", type=str,
                        help="Video source id (e.g. GS010366)")
    parser.add_argument("--after-frame", type=str, metavar="FRAME",
                        help="Frame number after which to start a new sequence (e.g. 0084 or 84)")
    parser.add_argument("--turn-angle", type=float, metavar="DEGREES",
                        help="Force turn angle at this split (negative = left, positive = right)")
    parser.add_argument("--remove", action="store_true",
                        help="Remove this split instead of adding")
    parser.add_argument("--list", action="store_true",
                        help="List sequence splits and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't write data.json")
    args = parser.parse_args()

    path = Path(args.data)
    if not path.exists():
        print(f"Error: {path} not found", file=sys.stderr)
        sys.exit(1)

    with open(path) as f:
        data = json.load(f)

    splits = get_splits(data)
    overrides = get_overrides(data)

    if args.list:
        if args.source:
            if args.source not in splits:
                print(f"No splits for source {args.source}")
            else:
                for fn in splits[args.source]:
                    key = override_key(args.source, fn)
                    angle = overrides.get(key)
                    suffix = f"  (turn: {angle}°)" if angle is not None else ""
                    print(f"  {args.source}: after {fn}{suffix}")
        else:
            if not splits:
                print("No sequence splits defined.")
            else:
                for src, frame_nums in sorted(splits.items()):
                    for fn in frame_nums:
                        key = override_key(src, fn)
                        angle = overrides.get(key)
                        suffix = f"  (turn: {angle}°)" if angle is not None else ""
                        print(f"  {src}: after {fn}{suffix}")
        return

    if not args.source or not args.after_frame:
        print("Error: --source and --after-frame required (unless using --list)", file=sys.stderr)
        sys.exit(1)

    frame_num = normalize_frame_num(args.after_frame)
    source_splits = splits.setdefault(args.source, [])
    key = override_key(args.source, frame_num)

    if args.remove:
        if frame_num not in source_splits:
            print(f"Split after {args.source}_{frame_num} not found; nothing to remove.")
            return
        source_splits.remove(frame_num)
        if not source_splits:
            del splits[args.source]
        if key in overrides:
            del overrides[key]
        if not overrides:
            del data["split_turn_overrides"]
        print(f"Removed split: {args.source} after {frame_num}")
    else:
        if frame_num in source_splits:
            if args.turn_angle is not None:
                overrides[key] = args.turn_angle
                print(f"Updated turn angle: {args.source} after {frame_num} → {args.turn_angle}°")
            else:
                print(f"Split after {args.source}_{frame_num} already exists; no change.")
                return
        else:
            source_splits.append(frame_num)
            source_splits.sort()
            print(f"Added split: {args.source} after {frame_num} (next frame starts new sequence)")
            if args.turn_angle is not None:
                overrides[key] = args.turn_angle
                print(f"  Turn angle override: {args.turn_angle}°")

    if args.dry_run:
        print("(dry run; data.json not written)")
        return

    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Updated {path}")


if __name__ == "__main__":
    main()
