#!/usr/bin/env python3
"""
PLÜR — Scene Dashboard (local, multi-project)

Run:  python3 app.py   → http://localhost:8756

Each "project" is a folder under projects/<id>/ with its own scenes.json,
assets/, uploads/, and queue.jsonl. Shared at the root: config.json,
catalog.json, characters.json, and the engine scripts.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))

try:
    from env_loader import load_env
    load_env()
except Exception:
    pass

PORT = int(os.environ.get("PORT", 8756))  # honor launcher-assigned port; fall back to 8756

# DATA_DIR points project data at a persistent volume in the cloud; defaults to ROOT locally.
DATA_ROOT = os.environ.get("DATA_DIR") or ROOT
PROJECTS_DIR = os.path.join(DATA_ROOT, "projects")
PROJECTS_JSON = os.path.join(DATA_ROOT, "projects.json")
CONFIG = os.path.join(DATA_ROOT, "config.json")
CATALOG = os.path.join(DATA_ROOT, "catalog.json")
RUNNER = os.path.join(ROOT, "higgsfield_runner.py")

# runner interpreter: local venv if present, else the current interpreter (container has the SDK system-wide)
VENV_PY = os.path.join(ROOT, ".venv", "bin", "python")
if not os.path.isfile(VENV_PY):
    VENV_PY = sys.executable


def _seed_data_dir():
    """On first cloud boot, copy the bundled project data + config into the persistent DATA_DIR."""
    if DATA_ROOT == ROOT:
        return
    os.makedirs(DATA_ROOT, exist_ok=True)
    if not os.path.exists(PROJECTS_JSON) and os.path.exists(os.path.join(ROOT, "projects.json")):
        shutil.copy2(os.path.join(ROOT, "projects.json"), PROJECTS_JSON)
    if not os.path.isdir(PROJECTS_DIR) and os.path.isdir(os.path.join(ROOT, "projects")):
        shutil.copytree(os.path.join(ROOT, "projects"), PROJECTS_DIR)
    for f in ("config.json", "catalog.json"):
        if not os.path.exists(os.path.join(DATA_ROOT, f)) and os.path.exists(os.path.join(ROOT, f)):
            shutil.copy2(os.path.join(ROOT, f), os.path.join(DATA_ROOT, f))


_seed_data_dir()

RUN = {}  # project_id -> Popen

CTYPES = {
    ".html": "text/html; charset=utf-8", ".js": "application/javascript",
    ".css": "text/css", ".json": "application/json",
    ".mp4": "video/mp4", ".wav": "audio/wav", ".mp3": "audio/mpeg",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp",
}


def load_json(path, default=None):
    if not os.path.exists(path) and default is not None:
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def has_credentials():
    return bool(os.environ.get("HF_KEY") or
                (os.environ.get("HF_API_KEY") and os.environ.get("HF_API_SECRET")))


def projects():
    return load_json(PROJECTS_JSON, [])


def valid_pid(pid):
    return bool(pid) and any(p["id"] == pid for p in projects())


def default_pid():
    ps = projects()
    return ps[0]["id"] if ps else None


def pdir(pid):
    return os.path.join(PROJECTS_DIR, pid)


def scenes_path(pid):
    return os.path.join(pdir(pid), "scenes.json")


def queue_path(pid):
    return os.path.join(pdir(pid), "queue.jsonl")


def uploads_dir(pid):
    d = os.path.join(pdir(pid), "uploads")
    os.makedirs(d, exist_ok=True)
    return d


def runlog_path(pid):
    return os.path.join(pdir(pid), "run.log")


def saved_path(pid):
    return os.path.join(pdir(pid), "saved.json")


def voice_name(vid):
    if not vid:
        return "voice"
    try:
        for v in load_json(CATALOG).get("voices", []):
            if v.get("voice_id") == vid:
                return v.get("name", "voice")
    except Exception:
        pass
    return "voice"


def slug_name(n):
    return re.sub(r"[^A-Za-z0-9]+", "-", n or "").strip("-") or "voice"


def _archive(pid, live_path):
    """Move a live asset to _history before it's overwritten."""
    if not os.path.isfile(live_path):
        return
    hist = os.path.join(pdir(pid), "assets", "_history")
    os.makedirs(hist, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    shutil.move(live_path, os.path.join(hist, f"{ts}_{os.path.basename(live_path)}"))


def _rethumb(video, thumb):
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "1.5", "-i", video,
                        "-frames:v", "1", "-vf", "scale=360:-1", thumb], check=False, timeout=60)
    except Exception:
        pass


def read_queue(pid):
    qp = queue_path(pid)
    if not os.path.exists(qp):
        return []
    out = []
    with open(qp, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or ("project-" + uuid.uuid4().hex[:6])


def safe_path(rel):
    rel = rel.lstrip("/")
    # project assets live under DATA_ROOT (persistent volume); app files under ROOT
    root = DATA_ROOT if rel.startswith("projects/") else ROOT
    full = os.path.normpath(os.path.join(root, rel))
    if not (full.startswith(ROOT) or full.startswith(DATA_ROOT)):
        return None
    return full


def qs_get(path, key):
    if "?" not in path:
        return None
    from urllib.parse import parse_qs
    return (parse_qs(path.split("?", 1)[1]).get(key) or [None])[0]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _pid(self):
        pid = qs_get(self.path, "project") or default_pid()
        return pid if valid_pid(pid) else default_pid()

    def _check_auth(self):
        """Optional HTTP Basic gate: active only when DASH_PASSWORD is set (for cloud/team use)."""
        pw = os.environ.get("DASH_PASSWORD")
        if not pw:
            return True
        user = os.environ.get("DASH_USER", "team")
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                u, p = base64.b64decode(hdr[6:]).decode("utf-8").split(":", 1)
                if u == user and p == pw:
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="PLUR Dashboard"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        if not self._check_auth():
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(ROOT, "dashboard.html"), "rb") as f:
                return self._send(200, f.read(), CTYPES[".html"])
        if path == "/api/projects":
            return self._send(200, projects())
        if path == "/api/scenes":
            pid = self._pid()
            return self._send(200, load_json(scenes_path(pid), []))
        if path == "/api/queue":
            return self._send(200, read_queue(self._pid()))
        if path == "/api/saved":
            return self._send(200, load_json(saved_path(self._pid()), []))
        if path == "/api/settings":
            cfg = load_json(CONFIG)
            return self._send(200, {
                "catalog": load_json(CATALOG),
                "current": {
                    "image_to_video_model_id": cfg["video"]["image_to_video_model_id"],
                    "text_to_video_model_id": cfg["video"]["text_to_video_model_id"],
                    "voice_id": cfg["audio"]["voice_id"],
                },
            })
        if path == "/api/api-credit-status":
            if not os.path.isfile(VENV_PY):
                return self._send(200, {"api": "no_sdk"})
            if not has_credentials():
                return self._send(200, {"api": "no_key"})
            try:
                r = subprocess.run([VENV_PY, RUNNER, "checkcredits"], cwd=ROOT,
                                   env=os.environ.copy(), capture_output=True, text=True, timeout=45)
                line = (r.stdout.strip().splitlines() or ["{}"])[-1]
                return self._send(200, json.loads(line))
            except Exception as e:
                return self._send(200, {"api": "error", "detail": str(e)[:120]})
        if path == "/api/config-status":
            pid = self._pid()
            proc = RUN.get(pid)
            return self._send(200, {
                "has_credentials": has_credentials(),
                "sdk_ready": os.path.isfile(VENV_PY),
                "running": bool(proc and proc.poll() is None),
            })
        if path == "/api/filmstrip":
            return self.handle_filmstrip()
        if path == "/api/render-film":
            return self.handle_render_film()
        if path == "/api/run-log":
            pid = self._pid()
            proc = RUN.get(pid)
            txt = ""
            rp = runlog_path(pid)
            if os.path.exists(rp):
                with open(rp, encoding="utf-8") as f:
                    txt = f.read()
            return self._send(200, {"running": bool(proc and proc.poll() is None), "log": txt})
        fp = safe_path(path)
        if fp and os.path.isfile(fp):
            return self.serve_file(fp)
        return self._send(404, {"error": "not found"})

    def serve_file(self, fp):
        """Serve a static file with HTTP Range support (required for video seeking)."""
        ext = os.path.splitext(fp)[1].lower()
        ctype = CTYPES.get(ext, "application/octet-stream")
        size = os.path.getsize(fp)
        rng = self.headers.get("Range")
        try:
            if rng and rng.startswith("bytes="):
                s, e = rng[6:].split("-", 1)
                start = int(s) if s else 0
                end = int(e) if e else size - 1
                start = max(0, start)
                end = min(end, size - 1)
                if start > end:
                    start, end = 0, size - 1
                with open(fp, "rb") as f:
                    f.seek(start)
                    chunk = f.read(end - start + 1)
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(len(chunk)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(chunk)
            else:
                with open(fp, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser closed the connection mid-stream (normal during seeking)

    def do_POST(self):
        if not self._check_auth():
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid json"})

        route = self.path.split("?", 1)[0]
        if route == "/api/resubmit":
            return self.handle_resubmit(data)
        if route == "/api/upload-vo":
            return self.handle_upload_vo(data)
        if route == "/api/add-clip":
            return self.handle_add_clip(data)
        if route == "/api/delete":
            return self.handle_delete(data)
        if route == "/api/run-queue":
            return self.handle_run_queue(data)
        if route == "/api/set-config":
            return self.handle_set_config(data)
        if route == "/api/new-project":
            return self.handle_new_project(data)
        if route == "/api/set-trim":
            return self.handle_set_trim(data)
        if route == "/api/delete-scene":
            return self.handle_delete_scene(data)
        if route == "/api/save-version":
            return self.handle_save_version(data)
        if route == "/api/restore-version":
            return self.handle_restore_version(data)
        if route == "/api/delete-version":
            return self.handle_delete_version(data)
        return self._send(404, {"error": "unknown endpoint"})

    def handle_save_version(self, data):
        pid = self._proj_from(data)
        if not pid:
            return self._send(400, {"error": "unknown project"})
        try:
            sid = int(data.get("scene"))
        except (TypeError, ValueError):
            return self._send(400, {"error": "scene required"})
        kind = data.get("kind")
        if kind not in ("video", "audio"):
            return self._send(400, {"error": "kind must be video|audio"})
        scenes = load_json(scenes_path(pid), [])
        scene = next((s for s in scenes if s["id"] == sid), None)
        if not scene:
            return self._send(404, {"error": "scene not found"})
        base = pdir(pid)
        rel = scene.get("video") if kind == "video" else scene.get("vo_audio")
        src = os.path.join(base, rel) if rel else None
        if not src or not os.path.isfile(src):
            return self._send(400, {"error": f"no {kind} to save for this shot"})
        sd = os.path.join(base, "saved")
        os.makedirs(sd, exist_ok=True)
        ext = os.path.splitext(src)[1]
        ts = time.strftime("%Y%m%d_%H%M%S")
        if kind == "audio":
            default_vid = load_json(CONFIG)["audio"].get("voice_id")
            vname = voice_name(scene.get("voice_id") or default_vid)
            fname = f"{slug_name(vname)}_scene{sid}_audio_{ts}{ext}"
            default_label = f"{vname} · {scene.get('title', 'Shot '+str(sid))}"
        else:
            fname = f"scene{sid}_{kind}_{ts}{ext}"
            default_label = f"{scene.get('title', 'Shot '+str(sid))} · {kind}"
        shutil.copy2(src, os.path.join(sd, fname))
        entry = {"id": uuid.uuid4().hex[:8], "scene": sid, "kind": kind,
                 "label": (data.get("label") or "").strip() or default_label,
                 "vo_text": scene.get("vo_text", "") if kind == "audio" else "",
                 "voice_id": scene.get("voice_id") if kind == "audio" else None,
                 "file": f"saved/{fname}", "created": time.strftime("%Y-%m-%d %H:%M")}
        lst = load_json(saved_path(pid), [])
        lst.insert(0, entry)
        save_json(saved_path(pid), lst)
        return self._send(200, {"ok": True, "entry": entry})

    def handle_restore_version(self, data):
        pid = self._proj_from(data)
        if not pid:
            return self._send(400, {"error": "unknown project"})
        lst = load_json(saved_path(pid), [])
        e = next((x for x in lst if x["id"] == data.get("id")), None)
        if not e:
            return self._send(404, {"error": "saved clip not found"})
        base = pdir(pid)
        src = os.path.join(base, e["file"])
        if not os.path.isfile(src):
            return self._send(404, {"error": "saved file missing"})
        scenes = load_json(scenes_path(pid), [])
        # restore to the target scene (default: origin scene, override with data['scene'])
        tid = int(data.get("scene") or e["scene"])
        scene = next((s for s in scenes if s["id"] == tid), None)
        if not scene:
            return self._send(404, {"error": f"target shot {tid} not found"})
        if e["kind"] == "video":
            dst = os.path.join(base, scene["video"])
            _archive(pid, dst)
            shutil.copy2(src, dst)
            _rethumb(dst, os.path.join(base, scene["thumb"]))
            scene["video_rev"] = int(time.time())
        else:
            dst = os.path.join(base, scene["vo_audio"])
            _archive(pid, dst)
            shutil.copy2(src, dst)
            scene["vo_rev"] = int(time.time())
            if e.get("vo_text"):
                scene["vo_text"] = e["vo_text"]
            if e.get("voice_id"):
                scene["voice_id"] = e["voice_id"]
        save_json(scenes_path(pid), scenes)
        return self._send(200, {"ok": True, "restored_to": tid})

    def handle_delete_version(self, data):
        pid = self._proj_from(data)
        if not pid:
            return self._send(400, {"error": "unknown project"})
        lst = load_json(saved_path(pid), [])
        e = next((x for x in lst if x["id"] == data.get("id")), None)
        if e:
            fp = os.path.join(pdir(pid), e["file"])
            if os.path.isfile(fp):
                try:
                    os.remove(fp)
                except OSError:
                    pass
        save_json(saved_path(pid), [x for x in lst if x["id"] != data.get("id")])
        return self._send(200, {"ok": True})

    def handle_delete_scene(self, data):
        pid = self._proj_from(data)
        if not pid:
            return self._send(400, {"error": "unknown project"})
        try:
            sid = int(data.get("scene"))
        except (TypeError, ValueError):
            return self._send(400, {"error": "scene required"})
        scenes = load_json(scenes_path(pid), [])
        scene = next((s for s in scenes if s["id"] == sid), None)
        if not scene:
            return self._send(404, {"error": "scene not found"})
        base = pdir(pid)
        hist = os.path.join(base, "assets", "_history")
        os.makedirs(hist, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        for rel in (scene.get("video"), scene.get("vo_audio"), scene.get("thumb"), scene.get("ref_image")):
            if not rel:
                continue
            fp = os.path.join(base, rel)
            if os.path.isfile(fp):
                shutil.move(fp, os.path.join(hist, f"deleted_{ts}_scene{sid}_{os.path.basename(fp)}"))
        scenes = [s for s in scenes if s["id"] != sid]
        save_json(scenes_path(pid), scenes)
        return self._send(200, {"ok": True})

    def _ffprobe(self, path, entries):
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", entries,
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, timeout=20)
            return out.stdout.strip()
        except Exception:
            return ""

    def handle_render_film(self):
        """Assemble all clips into one MP4, honoring per-scene trim in/out, VO offset, and volumes."""
        pid = self._pid()
        scenes = load_json(scenes_path(pid), [])
        if not scenes:
            return self._send(400, {"error": "no clips to render"})

        def fnum(v, d):
            try:
                return float(v)
            except (TypeError, ValueError):
                return d
        vv = fnum(qs_get(self.path, "vidvol"), 0.25)   # video-audio volume
        av = fnum(qs_get(self.path, "vovol"), 1.0)     # voiceover volume
        base = pdir(pid)
        exports = os.path.join(base, "exports")
        segdir = os.path.join(exports, "_seg")
        shutil.rmtree(segdir, ignore_errors=True)
        os.makedirs(segdir, exist_ok=True)

        seg_files = []
        for idx, s in enumerate(scenes):
            video = os.path.join(base, s.get("video", ""))
            if not os.path.isfile(video):
                continue
            start = fnum(s.get("trim_start"), 0.0)
            end = s.get("trim_end")
            dur = fnum(self._ffprobe(video, "format=duration"), 0.0)
            L = (fnum(end, 0.0) - start) if end is not None else max(0.1, dur - start)
            if L <= 0:
                L = max(0.1, dur or 1.0)
            vo_rel = s.get("vo_audio")
            vo = os.path.join(base, vo_rel) if vo_rel else None
            vo_exists = bool(vo and os.path.isfile(vo))
            voff = fnum(s.get("vo_offset"), 0.0)
            has_va = "audio" in self._ffprobe(video, "stream=codec_type").split()

            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(start)]
            if end is not None:
                cmd += ["-to", str(end)]
            cmd += ["-i", video,
                    "-f", "lavfi", "-t", f"{L:.3f}", "-i",
                    "anullsrc=channel_layout=stereo:sample_rate=48000"]
            if vo_exists:
                cmd += ["-i", vo]
            fc = ("[0:v]scale=720:1280:force_original_aspect_ratio=decrease,"
                  "pad=720:1280:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v];")
            mix = ["[1:a]"]  # silent base (length L) keeps segment audio = video length
            if has_va:
                fc += f"[0:a]volume={vv}[va];"
                mix.append("[va]")
            if vo_exists:
                ms = int(voff * 1000)
                fc += f"[2:a]adelay={ms}|{ms},volume={av}[vo];"
                mix.append("[vo]")
            fc += "".join(mix) + f"amix=inputs={len(mix)}:duration=first:dropout_transition=0:normalize=0[a]"
            seg = os.path.join(segdir, f"seg{idx:03d}.mp4")
            cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]",
                    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", "30",
                    "-c:a", "aac", "-ar", "48000", "-ac", "2", seg]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
            if r.returncode != 0:
                return self._send(500, {"error": f"shot {s.get('id')} render failed: {r.stderr[-300:]}"})
            seg_files.append(seg)

        if not seg_files:
            return self._send(400, {"error": "no renderable clips"})
        listf = os.path.join(segdir, "list.txt")
        with open(listf, "w", encoding="utf-8") as f:
            for sg in seg_files:
                f.write(f"file '{os.path.basename(sg)}'\n")
        film = os.path.join(exports, "film.mp4")
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                            "-i", listf, "-c", "copy", film],
                           capture_output=True, text=True, cwd=segdir, timeout=240)
        if r.returncode != 0:
            r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                                "-i", listf, "-c:v", "libx264", "-preset", "veryfast",
                                "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", film],
                               capture_output=True, text=True, cwd=segdir, timeout=360)
            if r.returncode != 0:
                return self._send(500, {"error": f"concat failed: {r.stderr[-300:]}"})
        return self._send(200, {"url": f"projects/{pid}/exports/film.mp4", "scenes": len(seg_files)})

    def handle_filmstrip(self):
        pid = self._pid()
        sid = qs_get(self.path, "scene")
        if not pid or not sid:
            return self._send(400, {"error": "project and scene required"})
        base = pdir(pid)
        video = os.path.join(base, "assets", "videos", f"scene{sid}.mp4")
        if not os.path.isfile(video):
            return self._send(404, {"error": "video not found"})
        rel = f"assets/thumbs/scene{sid}_strip.jpg"
        strip = os.path.join(base, rel)
        # regenerate if missing or older than the video
        if (not os.path.isfile(strip)) or os.path.getmtime(strip) < os.path.getmtime(video):
            cols = 12
            # duration via ffprobe (fallback 8s)
            dur = 8.0
            try:
                out = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", video],
                    capture_output=True, text=True, timeout=20)
                dur = max(0.5, float(out.stdout.strip()))
            except Exception:
                pass
            fps = cols / dur
            os.makedirs(os.path.dirname(strip), exist_ok=True)
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-i", video,
                     "-vf", f"fps={fps:.4f},scale=140:-1,tile={cols}x1", "-frames:v", "1", strip],
                    check=True, timeout=60)
            except Exception as e:
                return self._send(500, {"error": f"filmstrip failed: {e}"})
        return self._send(200, {"url": f"projects/{pid}/{rel}", "cols": 12})

    def handle_set_trim(self, data):
        pid = self._proj_from(data)
        if not pid:
            return self._send(400, {"error": "unknown project"})
        try:
            sid = int(data.get("scene"))
        except (TypeError, ValueError):
            return self._send(400, {"error": "scene required"})
        scenes = load_json(scenes_path(pid), [])
        scene = next((s for s in scenes if s["id"] == sid), None)
        if not scene:
            return self._send(404, {"error": "scene not found"})

        def num(v):
            try:
                return max(0.0, float(v))
            except (TypeError, ValueError):
                return None
        start = num(data.get("start")) or 0.0
        end = num(data.get("end"))
        if end is not None and end <= start:
            end = None  # invalid range → treat as full clip
        scene["trim_start"] = round(start, 2)
        scene["trim_end"] = round(end, 2) if end is not None else None
        if "vo_offset" in data:
            off = num(data.get("vo_offset")) or 0.0
            scene["vo_offset"] = round(off, 2)
        save_json(scenes_path(pid), scenes)
        return self._send(200, {"ok": True, "trim_start": scene["trim_start"],
                                "trim_end": scene["trim_end"], "vo_offset": scene.get("vo_offset", 0.0)})

    def _proj_from(self, data):
        pid = data.get("project") or default_pid()
        return pid if valid_pid(pid) else None

    def _save_ref_images(self, pid, scene, rid, data):
        imgs = data.get("ref_images_data")
        if not imgs and data.get("ref_image_data"):
            imgs = [data.get("ref_image_data")]
        saved = []
        for i, img_data in enumerate(imgs or []):
            m = re.match(r"data:(image/[\w.+-]+);base64,(.*)$", img_data or "", re.S)
            if not m:
                continue
            ext = {"image/jpeg": ".jpg", "image/png": ".png",
                   "image/webp": ".webp", "image/gif": ".gif"}.get(m.group(1), ".img")
            fname = f"scene{scene}_{rid}_{i}{ext}"
            with open(os.path.join(uploads_dir(pid), fname), "wb") as f:
                f.write(base64.b64decode(m.group(2)))
            saved.append(f"uploads/{fname}")
        return saved

    def handle_resubmit(self, data):
        pid = self._proj_from(data)
        if not pid:
            return self._send(400, {"error": "unknown project"})
        kind = data.get("kind")
        scene = data.get("scene")
        if kind not in ("visual", "vo") or not scene:
            return self._send(400, {"error": "need kind (visual|vo) and scene"})
        rid = uuid.uuid4().hex[:8]
        entry = {"id": rid, "scene": int(scene), "kind": kind, "status": "queued",
                 "created": time.strftime("%Y-%m-%d %H:%M:%S")}
        if kind == "visual":
            entry["new_prompt"] = (data.get("prompt") or "").strip()
            entry["new_soul_id"] = (data.get("soul_id") or "").strip()
            try:
                if data.get("duration"):
                    entry["duration"] = int(data["duration"])
            except (TypeError, ValueError):
                pass
            saved = self._save_ref_images(pid, int(scene), rid, data)
            if saved:
                entry["new_ref_images"] = saved
                entry["new_ref_image"] = saved[0]
                entry["new_ref_names"] = data.get("ref_images_names") or []
        else:
            entry["new_vo_text"] = (data.get("vo_text") or "").strip()
            if data.get("voice_id"):
                entry["voice_id"] = data.get("voice_id")
        with open(queue_path(pid), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        return self._send(200, {"ok": True, "entry": entry})

    def handle_upload_vo(self, data):
        """Attach a finished voiceover audio file directly to a scene.

        This is the local→cloud bridge: voiceovers are generated on the local
        dashboard (Sterling voice via MCP), then the resulting mp3/wav is uploaded
        here so it lands in a cloud scene. No TTS runs in the cloud.
        """
        pid = self._proj_from(data)
        if not pid:
            return self._send(400, {"error": "unknown project"})
        try:
            sid = int(data.get("scene"))
        except (TypeError, ValueError):
            return self._send(400, {"error": "scene required"})
        m = re.match(r"data:(audio/[\w.+-]+);base64,(.*)$", data.get("audio_data") or "", re.S)
        if not m:
            return self._send(400, {"error": "audio_data must be a base64 audio data URI"})
        ext = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav",
               "audio/x-wav": ".wav", "audio/wave": ".wav", "audio/mp4": ".m4a",
               "audio/x-m4a": ".m4a", "audio/aac": ".aac", "audio/ogg": ".ogg",
               "audio/webm": ".webm", "audio/flac": ".flac"}.get(m.group(1), ".mp3")
        scenes = load_json(scenes_path(pid), [])
        scene = next((s for s in scenes if s["id"] == sid), None)
        if not scene:
            return self._send(404, {"error": "scene not found"})
        try:
            raw = base64.b64decode(m.group(2))
        except Exception:
            return self._send(400, {"error": "could not decode audio data"})
        if not raw:
            return self._send(400, {"error": "empty audio file"})
        base = pdir(pid)
        old_rel = scene.get("vo_audio")
        if old_rel:
            _archive(pid, os.path.join(base, old_rel))
        rel = f"assets/audio/scene{sid}{ext}"
        dst = os.path.join(base, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(raw)
        scene["vo_audio"] = rel
        scene["vo_rev"] = int(time.time())   # NEW badge
        if (data.get("vo_text") or "").strip():
            scene["vo_text"] = data["vo_text"].strip()
        if data.get("voice_id"):
            scene["voice_id"] = data["voice_id"]
        save_json(scenes_path(pid), scenes)
        return self._send(200, {"ok": True, "vo_audio": rel, "bytes": len(raw)})

    def handle_add_clip(self, data):
        pid = self._proj_from(data)
        if not pid:
            return self._send(400, {"error": "unknown project"})
        prompt = (data.get("prompt") or "").strip()
        if not prompt:
            return self._send(400, {"error": "a visual prompt is required for a new clip"})
        scenes = load_json(scenes_path(pid), [])
        new_id = (max([s["id"] for s in scenes], default=0) + 1)
        rid = uuid.uuid4().hex[:8]
        entry = {"id": rid, "scene": new_id, "kind": "visual", "new_scene": True,
                 "title": (data.get("title") or f"Shot {new_id}").strip(),
                 "new_prompt": prompt, "new_soul_id": (data.get("soul_id") or "").strip(),
                 "new_vo_text": (data.get("vo_text") or "").strip(),
                 "status": "queued", "created": time.strftime("%Y-%m-%d %H:%M:%S")}
        saved = self._save_ref_images(pid, new_id, rid, data)
        if saved:
            entry["new_ref_images"] = saved
            entry["new_ref_image"] = saved[0]
        with open(queue_path(pid), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        return self._send(200, {"ok": True, "entry": entry, "new_scene_id": new_id})

    def handle_delete(self, data):
        pid = self._proj_from(data)
        if not pid:
            return self._send(400, {"error": "unknown project"})
        rid = data.get("id")
        items = [e for e in read_queue(pid) if e.get("id") != rid]
        with open(queue_path(pid), "w", encoding="utf-8") as f:
            for e in items:
                f.write(json.dumps(e) + "\n")
        return self._send(200, {"ok": True})

    def handle_run_queue(self, data):
        pid = self._proj_from(data)
        if not pid:
            return self._send(400, {"error": "unknown project"})
        proc = RUN.get(pid)
        if proc and proc.poll() is None:
            return self._send(409, {"error": "a run is already in progress for this project"})
        if not os.path.isfile(VENV_PY):
            return self._send(400, {"error": "SDK venv missing"})
        if not has_credentials():
            return self._send(400, {"error": "No credentials — set HF_KEY in .env and restart."})
        RUN[pid] = subprocess.Popen([VENV_PY, RUNNER, "run", pid], cwd=ROOT, env=os.environ.copy())
        return self._send(200, {"ok": True, "started": True})

    def handle_set_config(self, data):
        cfg = load_json(CONFIG)
        changed = []
        vkey = data.get("video_key")
        if vkey:
            m = next((v for v in load_json(CATALOG)["video_models"] if v["key"] == vkey), None)
            if not m:
                return self._send(400, {"error": f"unknown video_key {vkey}"})
            cfg["video"]["image_to_video_model_id"] = m["image_to_video_model_id"]
            cfg["video"]["text_to_video_model_id"] = m["text_to_video_model_id"]
            changed.append(f"video → {m['label']}")
        if data.get("voice_id"):
            cfg["audio"]["voice_id"] = data["voice_id"]
            changed.append("voice updated")
        if not changed:
            return self._send(400, {"error": "nothing to change"})
        save_json(CONFIG, cfg)
        return self._send(200, {"ok": True, "changed": changed})

    def handle_new_project(self, data):
        name = (data.get("name") or "").strip()
        if not name:
            return self._send(400, {"error": "project name required"})
        ps = projects()
        pid = slugify(name)
        if any(p["id"] == pid for p in ps):
            pid = pid + "-" + uuid.uuid4().hex[:4]
        for sub in ("assets/videos", "assets/audio", "assets/thumbs", "assets/refs",
                    "assets/_history", "uploads"):
            os.makedirs(os.path.join(pdir(pid), sub), exist_ok=True)
        save_json(scenes_path(pid), [])
        open(queue_path(pid), "w").close()
        ps.append({"id": pid, "name": name})
        save_json(PROJECTS_JSON, ps)
        return self._send(200, {"ok": True, "id": pid, "name": name})


if __name__ == "__main__":
    HOST = os.environ.get("HOST", "127.0.0.1")   # cloud sets HOST=0.0.0.0
    where = "http://localhost:%d" % PORT if HOST == "127.0.0.1" else "%s:%d" % (HOST, PORT)
    print(f"\n  PLÜR dashboard running →  {where}\n  (Ctrl+C to stop)\n")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
