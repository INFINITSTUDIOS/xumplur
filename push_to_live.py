#!/usr/bin/env python3
"""
Push local project data UP to the live site, so a project made/generated locally
appears on the cloud dashboard the team uses.

One-way: LOCAL  →  LIVE. The live import is additive — it adds or overwrites the
project(s) you push and never deletes anything already on the cloud.

Usage:
    python3 push_to_live.py --project xum-a534 --password rocket
    python3 push_to_live.py --all --password rocket
    python3 push_to_live.py --project xum-a534 --no-history --password rocket
    LIVE_PASSWORD=rocket python3 push_to_live.py --project xum-a534
"""
import argparse
import http.cookiejar
import io
import json
import os
import sys
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_URL = "https://xumplur-create-7xdlj.sevalla.app"


def main():
    try:
        sys.path.insert(0, ROOT)
        from env_loader import load_env
        load_env()
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("LIVE_URL", DEFAULT_URL))
    ap.add_argument("--password", default=os.environ.get("LIVE_PASSWORD"))
    ap.add_argument("--project", help="project id to push (e.g. xum-a534)")
    ap.add_argument("--all", action="store_true", help="push every local project")
    ap.add_argument("--no-history", action="store_true", help="skip the _history archives (smaller/faster)")
    a = ap.parse_args()
    base = a.url.rstrip("/")
    if not a.project and not a.all:
        sys.exit("Pass --project <id> or --all")
    if not a.password:
        a.password = input("Live site password: ").strip()

    index = json.load(open(os.path.join(ROOT, "projects.json"))) if os.path.exists(os.path.join(ROOT, "projects.json")) else []
    if a.all:
        pids = [p["id"] for p in index]
    else:
        pids = [a.project]
    # index entries for the projects being pushed (so the cloud registers name + id)
    push_index = [p for p in index if p.get("id") in pids] or [{"id": pid, "name": pid} for pid in pids]

    # build the zip in memory: projects.json (subset) + projects/<pid>/**
    buf = io.BytesIO()
    total_files = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("projects.json", json.dumps(push_index, indent=2, ensure_ascii=False))
        for pid in pids:
            base_dir = os.path.join(ROOT, "projects", pid)
            if not os.path.isdir(base_dir):
                sys.exit(f"ERROR: local project not found: projects/{pid}")
            for root, _dirs, files in os.walk(base_dir):
                if a.no_history and os.sep + "_history" in root:
                    continue
                for fn in files:
                    full = os.path.join(root, fn)
                    z.write(full, os.path.relpath(full, ROOT))   # arcname = projects/<pid>/...
                    total_files += 1
    payload = buf.getvalue()
    print(f"Pushing {pids} — {total_files} file(s), {len(payload)//1024} KB → {base}/api/import")

    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")]
    # 1) log in for the session cookie (harmless if the site is open)
    try:
        op.open(urllib.request.Request(
            f"{base}/login", data=json.dumps({"password": a.password}).encode(),
            headers={"Content-Type": "application/json"}), timeout=30)
    except Exception as e:
        print(f"  (login step: {e} — continuing)")
    # 2) POST the zip to the import endpoint
    try:
        req = urllib.request.Request(f"{base}/api/import", data=payload,
                                     headers={"Content-Type": "application/zip"}, method="POST")
        with op.open(req, timeout=900) as r:
            out = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        sys.exit(f"ERROR {e.code} from /api/import: {body}\n"
                 f"(If this is 404, the live build doesn't include /api/import yet — deploy first.)")
    except Exception as e:
        sys.exit(f"ERROR pushing: {e}")
    print(f"\nDone ✅  {out}")
    print("Open the live dashboard to see the pushed project(s).")


if __name__ == "__main__":
    main()
