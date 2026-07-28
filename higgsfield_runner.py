#!/usr/bin/env python3
"""
Standalone queue engine — talks to Higgsfield Cloud directly (no Claude needed).

Auth: set HF_KEY in the environment or plur-dashboard/.env
    HF_KEY="your-api-key:your-api-secret"
Get a key at https://cloud.higgsfield.ai/api-keys

Commands (run with the project venv so the SDK imports):
    ./.venv/bin/python higgsfield_runner.py selftest
    ./.venv/bin/python higgsfield_runner.py run <project-id>
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

try:
    from env_loader import load_env
    load_env()
except Exception:
    pass

import apply_result as ar  # noqa: E402  (reuse the finalizer)

DATA_ROOT = os.environ.get("DATA_DIR") or ROOT   # persistent volume in cloud; ROOT locally
CONFIG = os.path.join(DATA_ROOT, "config.json")
PROJECTS_DIR = os.path.join(DATA_ROOT, "projects")

_runlog = os.path.join(ROOT, "run.log")   # set per-project in run()
_pbase = ROOT                              # project base dir, set in run()


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(_runlog, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_config():
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def default_pid():
    pj = os.path.join(DATA_ROOT, "projects.json")
    ps = json.load(open(pj, encoding="utf-8")) if os.path.exists(pj) else []
    return ps[0]["id"] if ps else None


def get_client():
    try:
        from higgsfield_client import SyncClient
    except ImportError:
        raise SystemExit("higgsfield-client not installed. Run: ./.venv/bin/pip install higgsfield-client")
    try:
        return SyncClient()
    except Exception as e:
        raise SystemExit(f"Cannot init Higgsfield client: {e}\nSet HF_KEY (see file header).")


def result_url(result, key):
    if isinstance(result, dict):
        for k in (key, "video", "audio", "images", "image"):
            n = result.get(k)
            if isinstance(n, dict) and n.get("url"):
                return n["url"]
            if isinstance(n, list) and n and isinstance(n[0], dict) and n[0].get("url"):
                return n[0]["url"]
    found = []

    def walk(o):
        if isinstance(o, str) and o.startswith("http"):
            found.append(o)
        elif isinstance(o, dict):
            [walk(v) for v in o.values()]
        elif isinstance(o, list):
            [walk(v) for v in o]
    walk(result)
    return found[0] if found else None


def read_queue(pid):
    qp = os.path.join(PROJECTS_DIR, pid, "queue.jsonl")
    if not os.path.exists(qp):
        return []
    out = []
    for line in open(qp, encoding="utf-8"):
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _probe(client, model_id, args, label):
    log(f"Probing {label}: POST https://platform.higgsfield.ai/{model_id}")
    log(f"  body: {json.dumps(args)}")
    try:
        rc = client.submit(application=model_id, arguments=args)
        log(f"  → ACCEPTED (request_id={getattr(rc, 'request_id', '?')}). Path + args valid.")
        try:
            rc.cancel()
        except Exception:
            pass
    except Exception as e:
        log(f"  → FAILED: {type(e).__name__}: {e}")


def selftest():
    cfg = load_config()
    client = get_client()
    v, a = cfg["video"], cfg["audio"]
    log("Credentials loaded OK. Base URL: https://platform.higgsfield.ai")
    log("")
    _probe(client, v["image_to_video_model_id"], {v["prompt_arg"]: "probe", **v["params"]},
           "image-to-video")
    if not v["text_to_video_model_id"].startswith("CONFIRM"):
        _probe(client, v["text_to_video_model_id"], {v["prompt_arg"]: "probe", **v["params"]},
               "text-to-video")
    else:
        log("text-to-video model_id not set.")
    if not a["model_id"].startswith("CONFIRM"):
        _probe(client, a["model_id"],
               {a["text_arg"]: "probe", "voice_id": a["voice_id"], "voice_type": a["voice_type"]},
               "text-to-speech")
    else:
        log("audio model_id not set — get it from cloud.higgsfield.ai/explore.")
    log("")
    log("ACCEPTED → valid · 404 → wrong path · 422 → path OK, arg missing · 401/403 → key issue")


def _apply(pid, argv_extra):
    sys.argv = ["apply_result", "--project", pid] + argv_extra
    ar.main()


def generate_visual(client, cfg, entry, pid):
    v = cfg["video"]
    scene = entry["scene"]
    prompt = entry.get("new_prompt") or ""
    soul_id = (entry.get("new_soul_id") or "").strip()
    send_prompt = prompt
    if soul_id and "<<<" not in send_prompt:
        send_prompt = f"{prompt} {v.get('soul_ref_template', '<<<{id}>>>').format(id=soul_id)}".strip()
        log(f"  referencing character/Element ID {soul_id} in prompt")
    if v.get("audio_directive") and v["audio_directive"] not in send_prompt:
        send_prompt = f"{send_prompt} {v['audio_directive']}".strip()
    params = dict(v["params"])
    if entry.get("duration"):
        params["duration"] = int(entry["duration"])   # per-shot length chosen on re-submit
    args = {v["prompt_arg"]: send_prompt, **params}
    app = v["text_to_video_model_id"]
    refs = entry.get("new_ref_images") or ([entry["new_ref_image"]] if entry.get("new_ref_image") else [])
    urls = []
    for r in refs:
        path = r if os.path.isabs(r) else os.path.join(_pbase, r)
        log(f"  uploading reference image {r} …")
        urls.append(client.upload_file(path))
    if urls:
        app = v["image_to_video_model_id"]
        images_arg = v.get("images_arg")
        if len(urls) > 1 and images_arg:
            args[images_arg] = [{"type": "image_url", "image_url": u} for u in urls]
        else:
            args[v["image_arg"]] = urls[0]
        log(f"  {len(urls)} reference image(s) attached")
    log(f"  submitting VIDEO to '{app}' …")
    result = client.subscribe(application=app, arguments=args)
    url = result_url(result, v.get("output_key", "video"))
    if not url:
        raise RuntimeError(f"No video URL in result: {json.dumps(result)[:300]}")
    log(f"  result video → {url[:70]}…")
    extra = ["--scene", str(scene), "--kind", "visual", "--url", url, "--queue-id", entry["id"]]
    if prompt:
        extra += ["--prompt", prompt]
    if refs:
        extra += ["--ref", refs[0]]
    if soul_id:
        extra += ["--soul", soul_id]
    if entry.get("duration"):
        extra += ["--duration", str(entry["duration"])]
    if entry.get("new_scene"):
        extra += ["--new-scene", "--title", entry.get("title", f"Shot {scene}")]
    _apply(pid, extra)


def generate_vo(client, cfg, entry, pid):
    a = cfg["audio"]
    scene = entry["scene"]
    text = entry.get("new_vo_text") or ""
    voice = entry.get("voice_id") or a["voice_id"]      # per-shot voice overrides the default
    args = {a["text_arg"]: text, "voice_id": voice, "voice_type": a["voice_type"]}
    log(f"  submitting VOICEOVER (voice {voice[:8]}…) to '{a['model_id']}' …")
    result = client.subscribe(application=a["model_id"], arguments=args)
    url = result_url(result, a.get("output_key", "audio"))
    if not url:
        raise RuntimeError(f"No audio URL in result: {json.dumps(result)[:300]}")
    log(f"  result audio → {url[:70]}…")
    extra = ["--scene", str(scene), "--kind", "vo", "--url", url, "--vo-text", text, "--voice", voice]
    if entry.get("id"):
        extra += ["--queue-id", entry["id"]]
    _apply(pid, extra)


def run(pid):
    global _runlog, _pbase
    _pbase = os.path.join(PROJECTS_DIR, pid)
    _runlog = os.path.join(_pbase, "run.log")
    open(_runlog, "w").close()
    cfg = load_config()
    client = get_client()
    items = read_queue(pid)
    if not items:
        log("Queue empty — nothing to do.")
        return
    log(f"[{pid}] Processing {len(items)} queued item(s)…")
    ok = 0
    for e in items:
        try:
            log(f"SHOT {e['scene']} · {e['kind']} · {e['id']}")
            if e["kind"] == "visual":
                generate_visual(client, cfg, e, pid)
                # a new clip may carry a voiceover line to generate too
                if e.get("new_scene") and (e.get("new_vo_text") or "").strip():
                    if not a_model_placeholder(cfg):
                        generate_vo(client, cfg, {"scene": e["scene"], "new_vo_text": e["new_vo_text"]}, pid)
                    else:
                        log("  (voiceover skipped — cloud API has no TTS; regenerate VO via Claude/MCP)")
                ok += 1
            elif e["kind"] == "vo" and a_model_placeholder(cfg):
                log("  voiceover skipped — cloud REST API has no text-to-speech model.")
                log("  → Regenerate this voiceover via Claude ('run the queue') which uses the Higgsfield MCP.")
                # leave the item queued so Claude can pick it up
            else:
                generate_vo(client, cfg, e, pid)
                ok += 1
        except Exception as ex:
            log(f"  FAILED: {type(ex).__name__}: {ex}")
    log(f"Done. {ok}/{len(items)} succeeded. Refresh the dashboard.")


def a_model_placeholder(cfg):
    return cfg["audio"]["model_id"].startswith("CONFIRM")


def check_credits():
    """Probe the Cloud API to report whether the API wallet is funded. Prints one JSON line."""
    cfg = load_config()
    try:
        client = get_client()
    except SystemExit as e:
        print(json.dumps({"api": "no_key", "detail": str(e)[:120]})); return
    v = cfg["video"]
    try:
        rc = client.submit(application=v["text_to_video_model_id"],
                           arguments={v["prompt_arg"]: "probe", **v["params"]})
        try:
            rc.cancel()   # cancel while queued — no credits consumed
        except Exception:
            pass
        print(json.dumps({"api": "funded"}))
    except Exception as e:
        msg = str(e)
        low = msg.lower()
        if "not_enough_credits" in low:
            print(json.dumps({"api": "empty"}))
        elif "credential" in low or "401" in msg or "403" in msg:
            print(json.dumps({"api": "auth_error", "detail": msg[:120]}))
        else:
            print(json.dumps({"api": "error", "detail": msg[:120]}))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "checkcredits":
        check_credits()
    elif cmd == "selftest":
        open(_runlog, "w").close()
        selftest()
    elif cmd == "run":
        pid = sys.argv[2] if len(sys.argv) > 2 else default_pid()
        if not pid:
            raise SystemExit("no project id")
        run(pid)
    else:
        print("usage: higgsfield_runner.py [selftest | run <project-id>]")
