#!/usr/bin/env python3
"""
PLÜR — Scene Dashboard (local, multi-project)

Run:  python3 app.py   → http://localhost:8756

Each "project" is a folder under projects/<id>/ with its own scenes.json,
assets/, uploads/, and queue.jsonl. Shared at the root: config.json,
catalog.json, characters.json, and the engine scripts.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets as _secrets
import shutil
import subprocess
import sys
import time
import urllib.parse as _urlparse
import urllib.request as _urlreq
import uuid
from http import cookies as _http_cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))

try:
    from env_loader import load_env
    load_env()
except Exception:
    pass

PORT = int(os.environ.get("PORT", 8756))  # honor launcher-assigned port; fall back to 8756

# DATA_DIR points *user data* (projects) at a persistent volume in the cloud; defaults to ROOT locally.
# App config (config.json, catalog.json) always comes from ROOT (the image) so repo changes to the
# model/voice lists take effect on every deploy — they are NOT stored on the persistent volume.
DATA_ROOT = os.environ.get("DATA_DIR") or ROOT
PROJECTS_DIR = os.path.join(DATA_ROOT, "projects")
PROJECTS_JSON = os.path.join(DATA_ROOT, "projects.json")
CONFIG = os.path.join(ROOT, "config.json")
CATALOG = os.path.join(ROOT, "catalog.json")
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
    # config.json / catalog.json intentionally NOT seeded — they are read from ROOT (the image).


_seed_data_dir()

# --- Google sign-in (OpenID Connect) ---------------------------------------
# Active only when GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET are set. Otherwise the
# app falls back to the optional HTTP Basic gate (DASH_PASSWORD), or stays open.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_ALLOWED_DOMAINS = [d.strip().lower() for d in
                          os.environ.get("GOOGLE_ALLOWED_DOMAINS", "").split(",") if d.strip()]
APP_BASE_URL = os.environ.get("APP_BASE_URL", "").rstrip("/")   # optional; else derived from Host header
SESSION_SECRET = (os.environ.get("SESSION_SECRET") or _secrets.token_hex(32)).encode()
AUTH_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
SESSION_MAX_AGE = 30 * 24 * 3600  # 30 days


def _b64u(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64u_dec(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload):
    return hmac.new(SESSION_SECRET, payload, hashlib.sha256).hexdigest()


def make_session_token(user):
    """Stateless signed session: base64(json).hmac — no server-side store needed."""
    payload = _b64u(json.dumps({"n": user.get("name", ""), "e": user.get("email", ""),
                                "p": user.get("picture", ""), "iat": int(time.time())}).encode())
    return payload + "." + _sign(payload.encode())


def read_session_token(tok):
    try:
        payload, sig = tok.split(".", 1)
        if not hmac.compare_digest(sig, _sign(payload.encode())):
            return None
        d = json.loads(_b64u_dec(payload))
        if int(time.time()) - int(d.get("iat", 0)) > SESSION_MAX_AGE:
            return None
        return {"name": d.get("n", ""), "email": d.get("e", ""), "picture": d.get("p", "")}
    except Exception:
        return None


def decode_id_token(id_token):
    """Trust the id_token received directly from Google's token endpoint over TLS
    (obtained with our client_secret) — no local signature verification needed."""
    try:
        _, payload, _ = id_token.split(".")
        return json.loads(_b64u_dec(payload))
    except Exception:
        return {}


def domain_allowed(claims):
    if claims.get("email_verified") is False:
        return False
    email = (claims.get("email") or "").lower()
    hd = (claims.get("hd") or "").lower()
    dom = email.split("@")[-1] if "@" in email else ""
    if not GOOGLE_ALLOWED_DOMAINS:
        return bool(email)   # no allowlist configured → any signed-in Google user
    return (hd in GOOGLE_ALLOWED_DOMAINS) or (dom in GOOGLE_ALLOWED_DOMAINS)


# --- Simple password gate (server-side form login) -------------------------
# Used when Google sign-in is NOT configured. The password comes from env
# DASH_PASSWORD (plaintext) or a sha256 hash in auth.json committed to the repo
# (so it deploys via git push — no Kinsta env var needed). auth.json lives under
# ROOT (the image), never the persistent volume, so a pushed change always applies.
def _load_password_hash():
    env = os.environ.get("DASH_PASSWORD")
    if env:
        return hashlib.sha256(env.encode()).hexdigest()
    try:
        with open(os.path.join(ROOT, "auth.json"), encoding="utf-8") as f:
            return (json.load(f).get("password_sha256") or "").strip() or None
    except Exception:
        return None


PASSWORD_HASH = _load_password_hash()
PASSWORD_ENABLED = bool(PASSWORD_HASH) and not AUTH_ENABLED

LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PLÜR — Sign in</title><style>
:root{color-scheme:dark}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
 background:#0a0510;color:#f5efe6;font-family:-apple-system,Segoe UI,Roboto,sans-serif}
.card{width:min(360px,90vw);background:#160b1e;border:1px solid rgba(255,255,255,.1);
 border-radius:16px;padding:34px 30px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
.brand{font-weight:800;font-size:26px;letter-spacing:.14em;text-transform:uppercase;margin:0 0 4px}
.brand .m{color:#ff2d9b}.brand .c{color:#22d3ee}
p{color:#b8adc4;font-size:13px;margin:0 0 22px}
input{width:100%;box-sizing:border-box;padding:12px 14px;border-radius:10px;
 border:1px solid rgba(255,255,255,.16);background:#0e0715;color:#f5efe6;font-size:15px}
button{width:100%;margin-top:14px;padding:12px;border:0;border-radius:10px;cursor:pointer;
 background:#ff2d9b;color:#1a0010;font-weight:700;font-size:15px}
button:hover{filter:brightness(1.08)}
.err{color:#ff6b8b;font-size:13px;margin-top:12px;min-height:16px}
</style></head><body>
<form class="card" onsubmit="return go(event)">
 <div class="brand">PL<span class="m">Ü</span>R <span class="c">·</span> Dashboard</div>
 <p>Enter the team password to continue.</p>
 <input id="pw" type="password" placeholder="Password" autofocus autocomplete="current-password">
 <button type="submit">Sign in</button>
 <div class="err" id="err"></div>
</form>
<script>
async function go(e){e.preventDefault();
 const pw=document.getElementById('pw').value;
 const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({password:pw})});
 if(r.ok){location.href='/';}
 else{document.getElementById('err').textContent='Incorrect password.';
   document.getElementById('pw').value='';document.getElementById('pw').focus();}
 return false;}
</script></body></html>"""

RUN = {}  # project_id -> Popen

# "Sync Cloud → Claude" button: only meaningful on a LOCAL instance pulling from the live site.
IS_LOCAL = (DATA_ROOT == ROOT)
LIVE_URL = os.environ.get("LIVE_URL", "https://xumplur-create-7xdlj.sevalla.app")
PULL_SCRIPT = os.path.join(ROOT, "pull_from_live.py")

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

    current_user = None

    def _cookies(self):
        c = _http_cookies.SimpleCookie()
        raw = self.headers.get("Cookie")
        if raw:
            try:
                c.load(raw)
            except Exception:
                pass
        return c

    def _read_session(self):
        c = self._cookies()
        if "plur_session" in c:
            return read_session_token(c["plur_session"].value)
        return None

    def _redirect(self, location, cookies=None):
        self.send_response(302)
        self.send_header("Location", location)
        for ck in (cookies or []):
            self.send_header("Set-Cookie", ck)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _base_url(self):
        if APP_BASE_URL:
            return APP_BASE_URL
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "localhost"
        local = host.startswith("localhost") or host.startswith("127.")
        proto = self.headers.get("X-Forwarded-Proto") or ("http" if local else "https")
        return f"{proto}://{host}"

    def _secure_attr(self):
        """Add 'Secure;' to cookies except on localhost (so http dev testing still works)."""
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or ""
        return "" if (host.startswith("localhost") or host.startswith("127.")) else " Secure;"

    def _author(self):
        u = self.current_user or {}
        return u.get("name") or u.get("email") or None

    def _check_auth(self):
        """Google sign-in when configured; else optional HTTP Basic gate (DASH_PASSWORD)."""
        self.current_user = None
        path = self.path.split("?", 1)[0]
        if AUTH_ENABLED:
            if path.startswith("/auth/"):
                return True
            user = self._read_session()
            if user:
                self.current_user = user
                return True
            if path.startswith("/api/"):
                self._send(401, {"error": "login required", "login": "/auth/login"})
            else:
                self._redirect("/auth/login")
            return False
        # fallback: simple server-side password gate (form login + session cookie)
        if PASSWORD_ENABLED:
            if path == "/login":
                return True
            if self._read_session():
                return True
            if path.startswith("/api/"):
                self._send(401, {"error": "login required", "login": "/login"})
            else:
                self._redirect("/login")
            return False
        return True   # no auth configured → open

    def handle_login_submit(self, data):
        pw = data.get("password") or ""
        ok = PASSWORD_HASH and hmac.compare_digest(
            hashlib.sha256(pw.encode()).hexdigest(), PASSWORD_HASH)
        if not ok:
            return self._send(401, {"ok": False, "error": "wrong password"})
        tok = make_session_token({"name": "", "email": ""})
        ck = f"plur_session={tok}; HttpOnly;{self._secure_attr()} SameSite=Lax; Path=/; Max-Age={SESSION_MAX_AGE}"
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", ck)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_auth(self, path):
        if path == "/auth/login":
            state = _secrets.token_urlsafe(24)
            params = {
                "client_id": GOOGLE_CLIENT_ID,
                "redirect_uri": self._base_url() + "/auth/callback",
                "response_type": "code",
                "scope": "openid email profile",
                "state": state,
                "access_type": "online",
                "prompt": "select_account",
            }
            if len(GOOGLE_ALLOWED_DOMAINS) == 1:
                params["hd"] = GOOGLE_ALLOWED_DOMAINS[0]   # workspace hint (only if a single domain)
            url = GOOGLE_AUTH_URL + "?" + _urlparse.urlencode(params)
            ck = f"plur_oauth_state={state}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=600"
            return self._redirect(url, [ck])
        if path == "/auth/callback":
            qs = _urlparse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            code = (qs.get("code") or [""])[0]
            state = (qs.get("state") or [""])[0]
            c = self._cookies()
            want = c["plur_oauth_state"].value if "plur_oauth_state" in c else ""
            if not code or not state or not want or state != want:
                return self._send(400, "Login failed (bad state). <a href='/auth/login'>Try again</a>",
                                  CTYPES[".html"])
            claims = self._exchange_code(code)
            if not claims or not domain_allowed(claims):
                who = (claims or {}).get("email", "(unknown account)")
                doms = ", ".join(GOOGLE_ALLOWED_DOMAINS) or "(any)"
                msg = (f"<h3>Access denied</h3><p><b>{who}</b> is not on an authorized domain "
                       f"({doms}).</p><p><a href='/auth/logout'>Sign in with a different account</a></p>")
                return self._send(403, msg, CTYPES[".html"])
            user = {"name": claims.get("name") or claims.get("email", ""),
                    "email": claims.get("email", ""), "picture": claims.get("picture", "")}
            tok = make_session_token(user)
            cks = [f"plur_session={tok}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age={SESSION_MAX_AGE}",
                   "plur_oauth_state=; Max-Age=0; Path=/"]
            return self._redirect("/", cks)
        if path == "/auth/logout":
            return self._redirect("/auth/login", ["plur_session=; Max-Age=0; Path=/"])
        return self._send(404, {"error": "unknown auth route"})

    def _exchange_code(self, code):
        body = _urlparse.urlencode({
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": self._base_url() + "/auth/callback",
            "grant_type": "authorization_code",
        }).encode()
        try:
            req = _urlreq.Request(GOOGLE_TOKEN_URL, data=body,
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
            with _urlreq.urlopen(req, timeout=20) as r:
                tok = json.loads(r.read().decode())
            return decode_id_token(tok.get("id_token", ""))
        except Exception:
            return {}

    def do_GET(self):
        if not self._check_auth():
            return
        path = self.path.split("?", 1)[0]
        if path.startswith("/auth/"):
            return self.handle_auth(path)
        if path == "/login":
            return self._send(200, LOGIN_PAGE, CTYPES[".html"])
        if path == "/logout":
            return self._redirect("/login", ["plur_session=; Max-Age=0; Path=/"])
        if path == "/api/me":
            return self._send(200, {"user": self.current_user, "auth": AUTH_ENABLED,
                                    "password": PASSWORD_ENABLED, "local": IS_LOCAL,
                                    "live_url": LIVE_URL})
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
        if path == "/api/export":
            return self.handle_export()
        if path == "/api/history":
            return self.handle_history()
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
        if route == "/login":
            return self.handle_login_submit(data)
        if route == "/api/resubmit":
            return self.handle_resubmit(data)
        if route == "/api/upload-vo":
            return self.handle_upload_vo(data)
        if route == "/api/revert-history":
            return self.handle_revert_history(data)
        if route == "/api/sync-from-live":
            return self.handle_sync_from_live(data)
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
                 "author": self._author(),
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

    def handle_sync_from_live(self, data):
        """Local-only: pull the live site's project data down (runs pull_from_live.py).
        The live password is supplied by the browser (stored in its localStorage), or via
        the LIVE_PASSWORD env var — never persisted server-side."""
        if not IS_LOCAL:
            return self._send(400, {"error": "Sync only runs on the local dashboard, not the live site."})
        pw = (data.get("password") or os.environ.get("LIVE_PASSWORD") or "").strip()
        if not pw:
            return self._send(401, {"error": "password required", "need_password": True})
        url = (data.get("url") or LIVE_URL).strip()
        if not os.path.isfile(PULL_SCRIPT):
            return self._send(500, {"error": "pull_from_live.py missing"})
        env = dict(os.environ, LIVE_PASSWORD=pw, LIVE_URL=url)
        try:
            r = subprocess.run([sys.executable, PULL_SCRIPT], cwd=ROOT, env=env,
                               capture_output=True, text=True, timeout=900)
        except Exception as e:
            return self._send(500, {"error": f"sync failed to start: {e}"})
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0:
            bad_pw = "403" in out or "Forbidden" in out or "login" in out.lower()
            return self._send(502, {"error": "sync failed" + (" (check the live password?)" if bad_pw else ""),
                                    "detail": out[-500:], "need_password": bad_pw})
        m = re.search(r"Synced (\d+) project", out)
        return self._send(200, {"ok": True, "projects": int(m.group(1)) if m else None})

    def handle_export(self):
        """Stream a zip of all project data (scenes, assets, saved, history) for syncing
        the live site's data down to a local/other instance. Auth-gated like everything else.
        Add ?history=0 to skip the (large) _history archives."""
        import zipfile
        import tempfile
        include_history = qs_get(self.path, "history") != "0"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
                if os.path.exists(PROJECTS_JSON):
                    z.write(PROJECTS_JSON, "projects.json")
                if os.path.isdir(PROJECTS_DIR):
                    for root, _dirs, files in os.walk(PROJECTS_DIR):
                        if not include_history and os.sep + "_history" in root:
                            continue
                        for fn in files:
                            full = os.path.join(root, fn)
                            z.write(full, os.path.relpath(full, DATA_ROOT))
            tmp.close()
            size = os.path.getsize(tmp.name)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", 'attachment; filename="plur-data.zip"')
            self.end_headers()
            with open(tmp.name, "rb") as f:
                shutil.copyfileobj(f, self.wfile)
        finally:
            try:
                os.remove(tmp.name)
            except OSError:
                pass

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
                 "author": self._author(),
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
        scene["vo_author"] = self._author()
        if (data.get("vo_text") or "").strip():
            scene["vo_text"] = data["vo_text"].strip()
        if data.get("voice_id"):
            scene["voice_id"] = data["voice_id"]
        save_json(scenes_path(pid), scenes)
        return self._send(200, {"ok": True, "vo_audio": rel, "bytes": len(raw)})

    VIDEO_EXTS = {".mp4", ".mov", ".webm"}
    AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac"}

    def handle_history(self):
        """List archived previous renders for a shot (from assets/_history), newest first."""
        pid = self._pid()
        try:
            sid = int(qs_get(self.path, "scene"))
        except (TypeError, ValueError):
            return self._send(400, {"error": "scene required"})
        kind = qs_get(self.path, "kind") or "video"
        exts = self.VIDEO_EXTS if kind == "video" else self.AUDIO_EXTS
        hist = os.path.join(pdir(pid), "assets", "_history")
        out = []
        if os.path.isdir(hist):
            for fn in os.listdir(hist):
                m = re.match(r"^(\d{8}_\d{6})_(.+)$", fn)
                if not m:
                    continue
                ts, base = m.group(1), m.group(2)
                stem, ext = os.path.splitext(base)
                if ext.lower() not in exts or stem != f"scene{sid}":
                    continue
                try:
                    when = time.strftime("%b %-d, %Y · %-I:%M %p", time.strptime(ts, "%Y%m%d_%H%M%S"))
                except ValueError:
                    when = ts
                out.append({"file": fn, "ts": ts, "when": when, "url": f"assets/_history/{fn}"})
        out.sort(key=lambda x: x["ts"], reverse=True)
        return self._send(200, out)

    def handle_revert_history(self, data):
        """Restore a shot's video/voiceover to an archived previous render.
        The current live asset is archived first, so a revert is itself reversible."""
        pid = self._proj_from(data)
        if not pid:
            return self._send(400, {"error": "unknown project"})
        try:
            sid = int(data.get("scene"))
        except (TypeError, ValueError):
            return self._send(400, {"error": "scene required"})
        kind = data.get("kind") or "video"
        fn = os.path.basename(data.get("file") or "")   # basename guards against path traversal
        if not re.match(r"^\d{8}_\d{6}_.+", fn):
            return self._send(400, {"error": "bad history file"})
        base = pdir(pid)
        src = os.path.join(base, "assets", "_history", fn)
        if not os.path.isfile(src):
            return self._send(404, {"error": "history file not found"})
        scenes = load_json(scenes_path(pid), [])
        scene = next((s for s in scenes if s["id"] == sid), None)
        if not scene:
            return self._send(404, {"error": "scene not found"})
        ext = os.path.splitext(fn)[1]
        if kind == "video":
            rel = scene.get("video") or f"assets/videos/scene{sid}.mp4"
            dst = os.path.join(base, rel)
            _archive(pid, dst)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            scene["video"] = rel
            thumb = scene.get("thumb") or f"assets/thumbs/scene{sid}.jpg"
            os.makedirs(os.path.dirname(os.path.join(base, thumb)), exist_ok=True)
            _rethumb(dst, os.path.join(base, thumb))
            scene["thumb"] = thumb
            scene["video_rev"] = int(time.time())
        else:
            if scene.get("vo_audio"):
                _archive(pid, os.path.join(base, scene["vo_audio"]))
            rel = f"assets/audio/scene{sid}{ext}"
            dst = os.path.join(base, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            scene["vo_audio"] = rel
            scene["vo_rev"] = int(time.time())
        save_json(scenes_path(pid), scenes)
        return self._send(200, {"ok": True, "reverted": kind, "from": fn})

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
                 "author": self._author(),
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
            cfg["video"]["via"] = m.get("via", "cloud")
            cfg["video"]["mcp_model_id"] = m.get("mcp_model_id", "")
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
