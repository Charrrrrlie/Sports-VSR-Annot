import json
import os
import signal
import threading
from collections import OrderedDict
from datetime import datetime
from functools import wraps
from pathlib import Path

import cv2
from flask import (Flask, Response, abort, jsonify, redirect, request,
                   send_from_directory, session, url_for)

BASE_DIR = Path(__file__).resolve().parent
VIDEO_DIR = BASE_DIR / "videos"
ANNO_DIR = BASE_DIR / "annotations"
STATIC_DIR = BASE_DIR / "static"
CONFIG_PATH = BASE_DIR / "config.json"
PERSONS_PATH = BASE_DIR / "persons.json"

VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".webm"}

VIDEO_DIR.mkdir(exist_ok=True)
ANNO_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)


with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = json.load(f)


app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
app.secret_key = CONFIG.get("secret_key", "dev-secret")


class VideoHandle:
    """Holds one cv2.VideoCapture + a lock; OpenCV is not thread-safe."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.cap = cv2.VideoCapture(str(path))
        if not self.cap.isOpened():
            raise RuntimeError(f"cannot open video {path}")
        self.frame_count = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = self.cap.get(cv2.CAP_PROP_FPS)
        self.fps = float(fps) if fps and fps > 0 else 0.0
        self.width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    def read_frame(self, idx: int):
        if idx < 0 or idx >= self.frame_count:
            return None
        with self.lock:
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


VIDEOS: dict[str, VideoHandle] = {}


def scan_videos():
    VIDEOS.clear()
    for p in sorted(VIDEO_DIR.rglob("*")):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            rel = p.relative_to(VIDEO_DIR).as_posix()
            try:
                VIDEOS[rel] = VideoHandle(p)
            except Exception as e:
                print(f"[scan] skip {rel}: {e}")
    print(f"[scan] loaded {len(VIDEOS)} videos: {list(VIDEOS)}")


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


def load_persons():
    with open(PERSONS_PATH, "r", encoding="utf-8") as f:
        return json.load(f).get("person_ids", [])


def anno_path(video_name: str) -> Path:
    p = (ANNO_DIR / f"{video_name}.json").resolve()
    anno_root = ANNO_DIR.resolve()
    if anno_root not in p.parents and p != anno_root:
        raise ValueError(f"unsafe anno path: {video_name}")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def empty_anno(video_name: str) -> dict:
    h = VIDEOS[video_name]
    return {
        "video": video_name,
        "fps": h.fps,
        "frame_count": h.frame_count,
        "keyframes": [],
    }


def load_anno(video_name: str) -> dict:
    p = anno_path(video_name)
    if not p.exists():
        return empty_anno(video_name)
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("video", video_name)
    data.setdefault("fps", VIDEOS[video_name].fps)
    data.setdefault("frame_count", VIDEOS[video_name].frame_count)
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
    return jsonify([
        {
            "name": name,
            "dir": dir_of(name),
            "basename": name.rsplit("/", 1)[-1],
            "frame_count": h.frame_count,
            "fps": h.fps,
            "width": h.width,
            "height": h.height,
        }
        for name, h in VIDEOS.items()
    ])


@app.route("/api/video/<path:name>/frame/<int:idx>")
@require_auth
def api_frame(name, idx):
    if name not in VIDEOS:
        abort(404)
    key = (name, idx)
    data = FRAME_CACHE.get(key)
    if data is None:
        data = VIDEOS[name].read_frame(idx)
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
    fc = VIDEOS[name].frame_count
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
    print(f"[start] http://{CONFIG['host']}:{CONFIG['port']}")
    app.run(host=CONFIG["host"], port=CONFIG["port"], threaded=True, debug=False)
