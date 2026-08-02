#!/usr/bin/env python3
"""
Pull the live site's project data down to this local dashboard, so they stay in sync.

The cloud instance stores everything the team adds (renders, voiceovers, edits, drafts,
history) on its own /data disk — not in git. This script downloads that data over HTTP
(authenticated) and mirrors it into the local projects/ folder.

One-way: LIVE  →  LOCAL. Your current local projects/ is backed up first.

Usage:
    python3 pull_from_live.py --password rocket
    python3 pull_from_live.py --url https://xumplur-create-7xdlj.sevalla.app --password rocket
    LIVE_PASSWORD=rocket python3 pull_from_live.py
    python3 pull_from_live.py --password rocket --no-history   # skip large _history archives
"""
import argparse
import http.cookiejar
import json
import os
import shutil
import sys
import time
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_URL = "https://xumplur-create-7xdlj.sevalla.app"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("LIVE_URL", DEFAULT_URL))
    ap.add_argument("--password", default=os.environ.get("LIVE_PASSWORD"))
    ap.add_argument("--no-history", action="store_true", help="skip the _history archives (smaller/faster)")
    a = ap.parse_args()
    base = a.url.rstrip("/")
    if not a.password:
        a.password = input("Live site password: ").strip()

    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    # Sevalla is behind Cloudflare, which 403s the default python-urllib UA — look like a browser.
    op.addheaders = [("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")]

    # 1) log in (only needed if the site has a password gate; harmless otherwise)
    try:
        op.open(urllib.request.Request(
            f"{base}/login", data=json.dumps({"password": a.password}).encode(),
            headers={"Content-Type": "application/json"}), timeout=30)
    except Exception as e:
        print(f"  (login step: {e} — continuing; site may be open or use Google auth)")

    # 2) download the data zip
    url = f"{base}/api/export" + ("?history=0" if a.no_history else "")
    print(f"Downloading {url} …")
    tmp = os.path.join(ROOT, ".pull_tmp.zip")
    try:
        with op.open(url, timeout=600) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
    except Exception as e:
        sys.exit(f"ERROR downloading export: {e}\n(Is the URL right and the password correct? "
                 f"Does the live build include /api/export yet?)")
    print(f"  got {os.path.getsize(tmp)//1024} KB")

    # 3) back up the current local data
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup = os.path.join(ROOT, f"projects_backup_{ts}")
    if os.path.isdir(os.path.join(ROOT, "projects")):
        shutil.copytree(os.path.join(ROOT, "projects"), backup)
        print(f"  backed up local projects/ → {os.path.basename(backup)}")
        # keep only the 3 most recent backups
        old = sorted(g for g in __import__("glob").glob(os.path.join(ROOT, "projects_backup_*")))
        for g in old[:-3]:
            shutil.rmtree(g, ignore_errors=True)

    # 4) extract over the local copy
    with zipfile.ZipFile(tmp) as z:
        members = z.namelist()
        # safety: only allow projects/* and projects.json
        for m in members:
            if not (m == "projects.json" or m.startswith("projects/")) or ".." in m:
                sys.exit(f"ERROR: unexpected entry in archive: {m}")
        z.extractall(ROOT)
    os.remove(tmp)
    projs = json.load(open(os.path.join(ROOT, "projects.json"))) if os.path.exists(os.path.join(ROOT, "projects.json")) else []
    print(f"\nDone ✅  Synced {len(projs)} project(s) from {base} to local.")
    print(f"Local backup kept at {os.path.basename(backup) if os.path.isdir(backup) else '(none)'}.")
    print("Refresh the local dashboard to see the pulled data.")


if __name__ == "__main__":
    main()
