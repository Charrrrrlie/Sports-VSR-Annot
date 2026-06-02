import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import oss2

VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".webm"}


def oss_url_for_key(endpoint: str, bucket: str, key: str) -> str:
    endpoint = endpoint.strip()
    if not endpoint.startswith("http"):
        endpoint = "https://" + endpoint
    host = endpoint.split("//", 1)[1].rstrip("/")
    encoded = "/".join(quote(p) for p in key.split("/"))
    return f"{endpoint.split('//', 1)[0]}//{bucket}.{host}/{encoded}"


def iter_local_videos(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            yield p


def count_local_videos(root: Path) -> int:
    return sum(1 for _ in iter_local_videos(root))


def render_progress(done: int, total: int, width: int = 30):
    total = max(total, 1)
    filled = int(width * done / total)
    bar = "=" * filled + "-" * (width - filled)
    sys.stdout.write(f"\r[{bar}] {done}/{total}")
    sys.stdout.flush()


def probe_video_meta(url: str) -> dict | None:
    import cv2

    cap = cv2.VideoCapture(url)
    if not cap.isOpened():
        cap.release()
        return None
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    meta = {
        "frame_count": frame_count,
        "fps": float(fps) if fps and fps > 0 else 0.0,
        "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
    }
    cap.release()
    return meta


def main():
    parser = argparse.ArgumentParser(description="Build OSS index JSON from local videos folder")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--prefix", required=True, help="oss://bucket/prefix without scheme/bucket")
    parser.add_argument("--ak", required=True)
    parser.add_argument("--sk", required=True)
    parser.add_argument("--local-root", default="videos")
    parser.add_argument("--out", default="video_index.json")
    parser.add_argument("--public-read", action="store_true")
    parser.add_argument("--probe-meta", action="store_true")
    args = parser.parse_args()

    auth = oss2.Auth(args.ak, args.sk)
    bucket = oss2.Bucket(auth, args.endpoint, args.bucket)

    prefix = args.prefix.strip("/")
    if prefix:
        prefix = prefix + "/"

    root = Path(args.local_root).resolve()
    if not root.exists():
        raise SystemExit(f"local root not found: {root}")

    items = []
    total = count_local_videos(root)
    done = 0
    render_progress(done, total)
    for p in iter_local_videos(root):
        rel = p.relative_to(root).as_posix()
        key = f"{prefix}{rel}" if prefix else rel
        if args.public_read:
            bucket.put_object_acl(key, oss2.OBJECT_ACL_PUBLIC_READ)
        url = oss_url_for_key(args.endpoint, args.bucket, key)
        item = {
            "name": rel,
            "source": "oss",
            "key": key,
            "url": url,
        }
        if args.probe_meta:
            meta = probe_video_meta(url)
            if meta:
                item.update(meta)
        items.append(item)
        done += 1
        render_progress(done, total)

    sys.stdout.write("\n")

    out_path = Path(args.out).resolve()
    out_path.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(items),
        "items": items,
        "video_source": "oss",
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"saved {len(items)} items to {out_path}")


if __name__ == "__main__":
    main()
