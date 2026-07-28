"""Minimal .env loader (no dependencies).

Reads KEY=VALUE lines from plur-dashboard/.env and puts them in os.environ
(without overriding variables already set in the shell). Lets you keep
HF_KEY in a local file instead of re-exporting it every terminal session.
"""
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(ROOT, ".env")


def load_env():
    if not os.path.exists(ENV_FILE):
        return
    with open(ENV_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:  # shell export wins over .env
                os.environ[key] = val
