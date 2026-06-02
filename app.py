import json
import os
import signal
import threading
import sys
from collections import OrderedDict
from datetime import datetime
from functools import wraps
from pathlib import Path

import cv2
from flask import (Flask, Response, abort, jsonify, redirect, request,
                   send_from_directory, session, url_for)

try:
    import av
except Exception:
    av = None

BASE_DIR = Path(__file__).resolve().parent
VIDEO_DIR = BASE_DIR / "videos"
STATIC_DIR = BASE_DIR / "static"
CONFIG_PATH_DEFAULT = BASE_DIR / "config.json"
PERSONS_PATH = BASE_DIR / "persons.json"
VIDEO_INDEX_PATH_DEFAULT = BASE_DIR / "video_index.json"

VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".webm"}

STATIC_DIR.mkdir(exist_ok=True)


def resolve_path(value: str | None, base: Path) -> Path:
    if not value:
        return base
    p = Path(value)
    return p if p.is_absolute() else base / p


def get_config_path(argv: list[str]) -> str | None:
    if "--config" in argv:
        i = argv.index("--config")
        if i + 1 < len(argv):
            return argv[i + 1]
    if "-c" in argv:
        i = argv.index("-c")
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


CONFIG_PATH = resolve_path(
    os.getenv("CONFIG_PATH") or get_config_path(sys.argv[1:]) or str(CONFIG_PATH_DEFAULT),
    BASE_DIR,
)

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = json.load(f)


def env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return bool(default)
    return str(val).strip().lower() in {"1", "true", "yes", "y", "on"}


GROUP = os.getenv("ANNOT_GROUP") or CONFIG.get("group") or "default"
ANNO_ROOT = os.getenv("ANNO_ROOT") or CONFIG.get("anno_root") or "annotations"
ANNO_DIR = resolve_path(ANNO_ROOT, BASE_DIR) / GROUP
ANNO_DIR.mkdir(parents=True, exist_ok=True)

HOST = os.getenv("HOST") or CONFIG.get("host", "127.0.0.1")
PORT = int(os.getenv("PORT") or CONFIG.get("port", 5000))

VIDEO_SOURCE = (os.getenv("VIDEO_SOURCE") or CONFIG.get("video_source") or "oss").strip().lower()
VIDEO_INDEX_PATH = resolve_path(
    os.getenv("VIDEO_INDEX_PATH") or CONFIG.get("video_index_path") or str(VIDEO_INDEX_PATH_DEFAULT),
    BASE_DIR,
)
REMOTE_FORWARD_MAX = int(os.getenv("REMOTE_FORWARD_MAX") or CONFIG.get("remote_forward_max", 8))


app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
app.secret_key = CONFIG.get("secret_key", "dev-secret")


def is_remote_source(source: str) -> bool:
    return source.startswith("http://") or source.startswith("https://")


class VideoHandle:
    """Holds one cv2.VideoCapture + a lock; OpenCV is not thread-safe."""

    def __init__(self, source: str, meta: dict | None = None):
        self.source = source
        self.lock = threading.Lock()
        self.backend = "av" if is_remote_source(source) else "cv2"
        self.cap = None
        self.container = None
        self.stream = None
        self._av_next_idx = None

        if self.backend == "cv2":
            self.cap = cv2.VideoCapture(source)
            if not self.cap.isOpened():
                raise RuntimeError(f"cannot open video {source}")
            self.frame_count = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = self.cap.get(cv2.CAP_PROP_FPS)
            self.fps = float(fps) if fps and fps > 0 else 0.0
            self.width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            self.height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            return

        if av is None:
            raise RuntimeError("PyAV is required to read remote URLs (pip install av)")

        self.container = av.open(source)
        self.stream = next((s for s in self.container.streams if s.type == "video"), None)
        if not self.stream:
            raise RuntimeError(f"cannot open video stream {source}")
        self.width = int(self.stream.width or 0)
        self.height = int(self.stream.height or 0)
        self.fps = float(self.stream.average_rate) if self.stream.average_rate else 0.0
        if meta:
            self.frame_count = int(meta.get("frame_count") or 0)
            self.fps = float(meta.get("fps") or self.fps or 0.0)
            if meta.get("width"):
                self.width = int(meta.get("width"))
            if meta.get("height"):
                self.height = int(meta.get("height"))
        else:
            self.frame_count = int(self.stream.frames or 0)
            if self.frame_count <= 0 and self.stream.duration and self.fps:
                seconds = float(self.stream.duration * self.stream.time_base)
                self.frame_count = int(seconds * self.fps)

    def release(self):
        with self.lock:
            if self.backend == "cv2" and self.cap:
                self.cap.release()
                self.cap = None
            if self.backend == "av" and self.container:
                try:
                    self.container.close()
                finally:
                    self.container = None
                    self.stream = None
                    self._av_next_idx = None

    def _read_frame_cv2(self, idx: int):
        if idx < 0 or idx >= self.frame_count:
            return None
        if not self.cap:
            self.cap = cv2.VideoCapture(self.source)
            if not self.cap.isOpened():
                return None
        cur = int(self.cap.get(cv2.CAP_PROP_POS_FRAMES))
        if cur != idx:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = self.cap.read()
        if not ok or frame is None:
            return None
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok:
            return None
        return buf.tobytes()

    def _read_frame_av(self, idx: int):
        if self.fps <= 0:
            return None
        if not self.container or not self.stream:
            self.container = av.open(self.source)
            self.stream = next((s for s in self.container.streams if s.type == "video"), None)
            if not self.stream:
                return None

        if self._av_next_idx is not None and idx >= self._av_next_idx:
            gap = idx - self._av_next_idx
            if gap <= REMOTE_FORWARD_MAX:
                for frame in self.container.decode(self.stream):
                    cur_idx = self._av_next_idx
                    self._av_next_idx += 1
                    if cur_idx == idx:
                        img = frame.to_ndarray(format="bgr24")
                        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
                        if not ok:
                            return None
                        return buf.tobytes()

        target_seconds = idx / self.fps
        target_pts = None
        if self.stream.time_base:
            target_pts = int(target_seconds / self.stream.time_base)
            self.container.seek(target_pts, stream=self.stream, any_frame=False, backward=True)
        else:
            self.container.seek(int(target_seconds * 1_000_000), stream=self.stream, any_frame=False, backward=True)

        for frame in self.container.decode(self.stream):
            if target_pts is None or frame.pts is None or frame.pts >= target_pts:
                img = frame.to_ndarray(format="bgr24")
                ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
                if not ok:
                    return None
                self._av_next_idx = idx + 1
                return buf.tobytes()
        return None

    def read_frame(self, idx: int):
        with self.lock:
            if self.backend == "cv2":
                return self._read_frame_cv2(idx)
            return self._read_frame_av(idx)


VIDEOS: dict[str, dict] = {}
VIDEO_META: dict[str, dict] = {}
HANDLE_CACHE: "OrderedDict[str, VideoHandle]" = OrderedDict()
HANDLE_LOCK = threading.Lock()
HANDLE_MAX = int(CONFIG.get("video_handle_lru_size", 4))


def probe_video_meta(source: str) -> dict | None:
    if is_remote_source(source):
        if av is None:
            return None
        try:
            container = av.open(source)
            stream = next((s for s in container.streams if s.type == "video"), None)
            if not stream:
                container.close()
                return None
            fps = float(stream.average_rate) if stream.average_rate else 0.0
            frame_count = int(stream.frames or 0)
            if frame_count <= 0 and stream.duration and fps:
                seconds = float(stream.duration * stream.time_base)
                frame_count = int(seconds * fps)
            meta = {
                "frame_count": frame_count,
                "fps": fps,
                "width": int(stream.width or 0),
                "height": int(stream.height or 0),
            }
            container.close()
            return meta
        except Exception:
            return None

    cap = cv2.VideoCapture(source)
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


def load_video_index(path: Path) -> list[dict]:
    if not path.exists():
        print(f"[scan] video_index not found: {path}")
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[scan] read video_index failed: {e}")
        return []
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        print("[scan] invalid video_index format")
        return []
    return items


def scan_local_videos():
    VIDEO_DIR.mkdir(exist_ok=True)
    for p in sorted(VIDEO_DIR.rglob("*")):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            rel = p.relative_to(VIDEO_DIR).as_posix()
            meta = probe_video_meta(str(p))
            if not meta:
                print(f"[scan] skip {rel}: cannot open")
                continue
            VIDEOS[rel] = {"type": "local", "path": p}
            VIDEO_META[rel] = meta


def scan_index_videos():
    for item in load_video_index(VIDEO_INDEX_PATH):
        name = item.get("name")
        url = item.get("url")
        if not name or not url:
            continue
        VIDEOS.setdefault(name, {"type": "oss", "url": url})
        meta = {k: item.get(k) for k in ("frame_count", "fps", "width", "height")}
        if all(v is not None for v in meta.values()):
            VIDEO_META[name] = meta


def scan_videos():
    VIDEOS.clear()
    VIDEO_META.clear()
    with HANDLE_LOCK:
        for _name, h in HANDLE_CACHE.items():
            h.release()
        HANDLE_CACHE.clear()

    mode = VIDEO_SOURCE
    if mode not in {"local", "oss", "index", "remote"}:
        print(f"[scan] invalid VIDEO_SOURCE={VIDEO_SOURCE}, fallback to local")
        mode = "local"

    if mode == "local":
        scan_local_videos()
    if mode in {"oss", "index", "remote"}:
        if av is None:
            raise RuntimeError("Remote mode requires PyAV. Install 'av' before starting.")
        scan_index_videos()

    print(f"[scan] loaded {len(VIDEOS)} videos (source={mode})")


scan_videos()


class FrameLRU:
    def __init__(self, maxsize: int):
        self.maxsize = maxsize
        self.cache: "OrderedDict[tuple[str, int], bytes]" = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            return None

    def put(self, key, value: bytes):
        with self.lock:
            self.cache[key] = value
            self.cache.move_to_end(key)
            while len(self.cache) > self.maxsize:
                self.cache.popitem(last=False)


FRAME_CACHE = FrameLRU(maxsize=int(CONFIG.get("lru_size", 256)))


def get_video_meta(name: str) -> dict:
    if name in VIDEO_META:
        return VIDEO_META[name]
    src = VIDEOS.get(name)
    if not src:
        raise KeyError(name)
    source = str(src.get("path")) if src.get("type") == "local" else src.get("url")
    meta = probe_video_meta(source)
    if not meta:
        raise RuntimeError(f"cannot open video {name}")
    VIDEO_META[name] = meta
    return meta


def get_video_handle(name: str) -> VideoHandle:
    with HANDLE_LOCK:
        if name in HANDLE_CACHE:
            HANDLE_CACHE.move_to_end(name)
            return HANDLE_CACHE[name]
        src = VIDEOS.get(name)
        if not src:
            raise KeyError(name)
        source = str(src.get("path")) if src.get("type") == "local" else src.get("url")
        h = VideoHandle(source, meta=VIDEO_META.get(name))
        HANDLE_CACHE[name] = h
        HANDLE_CACHE.move_to_end(name)
        while len(HANDLE_CACHE) > HANDLE_MAX:
            _, old = HANDLE_CACHE.popitem(last=False)
            old.release()
        return h


def load_persons():
    with open(PERSONS_PATH, "r", encoding="utf-8") as f:
        return json.load(f).get("person_ids", [])


def list_annotated_videos() -> list[str]:
    annotated: list[str] = []
    for p in ANNO_DIR.rglob("*.json"):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(ANNO_DIR).as_posix()
        except ValueError:
            continue
        video_name = rel[:-5] if rel.endswith(".json") else rel
        if video_name not in VIDEOS:
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("keyframes"):
                annotated.append(video_name)
        except Exception:
            continue
    return annotated


def anno_path(video_name: str) -> Path:
    p = (ANNO_DIR / f"{video_name}.json").resolve()
    anno_root = ANNO_DIR.resolve()
    if anno_root not in p.parents and p != anno_root:
        raise ValueError(f"unsafe anno path: {video_name}")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def empty_anno(video_name: str) -> dict:
    h = get_video_meta(video_name)
    return {
        "video": video_name,
        "fps": h["fps"],
        "frame_count": h["frame_count"],
        "keyframes": [],
    }


def load_anno(video_name: str) -> dict:
    p = anno_path(video_name)
    if not p.exists():
        return empty_anno(video_name)
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    meta = get_video_meta(video_name)
    data.setdefault("video", video_name)
    data.setdefault("fps", meta["fps"])
    data.setdefault("frame_count", meta["frame_count"])
    data.setdefault("keyframes", [])
    return data


def save_anno_atomic(video_name: str, data: dict):
    data["keyframes"] = sorted(data.get("keyframes", []), key=lambda x: x["frame"])
    p = anno_path(video_name)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("auth"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "auth required"}), 401
            return redirect(url_for("login_page"))
        return fn(*args, **kwargs)

    return wrapper


LOGIN_HTML = """<!doctype html>
<meta charset=utf-8>
<title>Login</title>
<style>
  body { font-family: -apple-system, sans-serif; display: flex; height: 100vh;
         align-items: center; justify-content: center; margin: 0;
         background: #f3f4f6; }
  form { background: #fff; padding: 32px 36px; border-radius: 8px;
         box-shadow: 0 4px 16px rgba(0,0,0,0.08); min-width: 280px; }
  h1 { margin: 0 0 18px; font-size: 18px; }
  input[type=password] { width: 100%; padding: 10px; font-size: 14px;
         border: 1px solid #d1d5db; border-radius: 4px; box-sizing: border-box; }
  button { margin-top: 14px; width: 100%; padding: 10px; font-size: 14px;
         background: #2563eb; color: #fff; border: 0; border-radius: 4px;
         cursor: pointer; }
  button:hover { background: #1d4ed8; }
  .err { color: #dc2626; font-size: 13px; margin-top: 10px; }
</style>
<form method=post action="/login">
  <h1>视频标注工具 · 登录</h1>
  <input name=password type=password placeholder="密码" autofocus required>
  <button type=submit>进入</button>
  __ERR__
</form>
"""


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        if request.form.get("password") == CONFIG.get("password"):
            session["auth"] = True
            return redirect(url_for("index"))
        return LOGIN_HTML.replace("__ERR__", '<div class=err>密码错误</div>'), 401
    return LOGIN_HTML.replace("__ERR__", "")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


@app.route("/")
@require_auth
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/api/videos")
@require_auth
def api_videos():
    def dir_of(name: str) -> str:
        return name.rsplit("/", 1)[0] if "/" in name else ""
    items = []
    for name in VIDEOS.keys():
        meta = VIDEO_META.get(name)
        item = {
            "name": name,
            "dir": dir_of(name),
            "basename": name.rsplit("/", 1)[-1],
        }
        if meta:
            item.update(meta)
        items.append(item)
    return jsonify(items)


@app.route("/api/video/<path:name>/meta")
@require_auth
def api_video_meta(name):
    if name not in VIDEOS:
        abort(404)
    try:
        return jsonify(get_video_meta(name))
    except Exception:
        abort(404)


@app.route("/api/video/<path:name>/frame/<int:idx>")
@require_auth
def api_frame(name, idx):
    if name not in VIDEOS:
        abort(404)
    key = (name, idx)
    data = FRAME_CACHE.get(key)
    if data is None:
        data = get_video_handle(name).read_frame(idx)
        if data is None:
            abort(404)
        FRAME_CACHE.put(key, data)
    resp = Response(data, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@app.route("/api/persons")
@require_auth
def api_persons():
    return jsonify({"person_ids": load_persons()})


@app.route("/api/annotated")
@require_auth
def api_annotated():
    return jsonify({"annotated": list_annotated_videos()})


@app.route("/api/annotations/<path:name>", methods=["GET", "POST"])
@require_auth
def api_annotations(name):
    if name not in VIDEOS:
        abort(404)
    if request.method == "GET":
        return jsonify(load_anno(name))
    payload = request.get_json(silent=True) or {}
    keyframes = payload.get("keyframes", [])
    cleaned = []
    seen = set()
    fc = get_video_meta(name)["frame_count"]
    for kf in keyframes:
        try:
            f = int(kf["frame"])
        except (KeyError, TypeError, ValueError):
            continue
        if f < 0 or f >= fc or f in seen:
            continue
        ids = [str(x) for x in kf.get("person_ids", []) if str(x).strip()]
        if not ids:
            continue
        seen.add(f)
        cleaned.append({
            "frame": f,
            "person_ids": ids,
            "updated_at": kf.get("updated_at") or datetime.now().isoformat(timespec="seconds"),
        })
    data = empty_anno(name)
    data["keyframes"] = cleaned
    save_anno_atomic(name, data)
    return jsonify(data)


@app.route("/api/rescan", methods=["POST"])
@require_auth
def api_rescan():
    scan_videos()
    return jsonify({"count": len(VIDEOS), "videos": list(VIDEOS)})


def _force_exit(_sig, _frm):
    # Werkzeug's threaded dev server can hang on Ctrl+C when browsers hold
    # keep-alive connections. Force-exit to release the port reliably.
    print("\n[shutdown]")
    os._exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _force_exit)
    signal.signal(signal.SIGTERM, _force_exit)
    print(f"[start] http://{HOST}:{PORT} (group={GROUP}, anno_dir={ANNO_DIR})")
    app.run(host=HOST, port=PORT, threaded=True, debug=False)
