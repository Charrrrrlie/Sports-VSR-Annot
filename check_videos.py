import argparse
from pathlib import Path

import cv2

VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".webm"}


def can_open(path: Path) -> bool:
    cap = cv2.VideoCapture(str(path))
    ok = cap.isOpened()
    cap.release()
    return ok


def iter_videos(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            yield p


def main():
    parser = argparse.ArgumentParser(description="Check videos that cannot be opened")
    parser.add_argument("--root", default="videos", help="videos root directory")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not root.exists():
        raise SystemExit(f"root not found: {root}")

    total = 0
    bad = 0
    for p in iter_videos(root):
        total += 1
        if not can_open(p):
            bad += 1
            rel = p.relative_to(root)
            print(f"[bad] {rel}")

    print(f"checked {total} videos, bad: {bad}")
    raise SystemExit(1 if bad > 0 else 0)


if __name__ == "__main__":
    main()
