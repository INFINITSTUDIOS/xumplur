#!/usr/bin/env python3
"""
Finalize a Higgsfield regeneration back into a project's dashboard data.

Downloads the new asset, swaps it in (timestamped backup kept in
assets/_history/), refreshes the thumbnail for videos, updates scenes.json,
and moves the queue item to processed.jsonl. Works per-project.

Examples
  python3 apply_result.py --project plur-5mapb --scene 3 --kind visual \
      --url https://.../new.mp4 --queue-id ab12 --prompt "..."
  python3 apply_result.py --project plur-5mapb --scene 8 --kind visual \
      --url https://.../new.mp4 --new-scene --title "Outro tag" --prompt "..."
"""
import argparse
import json
import os
import shutil
import subprocess
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = os.environ.get("DATA_DIR") or ROOT   # persistent volume in cloud; ROOT locally


def default_pid():
    pj = os.path.join(DATA_ROOT, "projects.json")
    ps = json.load(open(pj, encoding="utf-8")) if os.path.exists(pj) else []
    return ps[0]["id"] if ps else None


class Paths:
    def __init__(self, pid):
        self.base = os.path.join(DATA_ROOT, "projects", pid)
        self.scenes = os.path.join(self.base, "scenes.json")
        self.queue = os.path.join(self.base, "queue.jsonl")
        self.processed = os.path.join(self.base, "processed.jsonl")
        self.hist = os.path.join(self.base, "assets", "_history")
        os.makedirs(self.hist, exist_ok=True)


def load_scenes(P):
    return json.load(open(P.scenes, encoding="utf-8")) if os.path.exists(P.scenes) else []


def save_scenes(P, scenes):
    with open(P.scenes, "w", encoding="utf-8") as f:
        json.dump(scenes, f, indent=2, ensure_ascii=False)
        f.write("\n")


def backup(P, path):
    if os.path.exists(path):
        ts = time.strftime("%Y%m%d_%H%M%S")
        base = os.path.basename(path)
        shutil.copy2(path, os.path.join(P.hist, f"{ts}_{base}"))
        print(f"  backed up old → assets/_history/{ts}_{base}")


def download(P, url, dst):
    print(f"  downloading {url[:70]}…")
    req = urllib.request.Request(url, headers={"User-Agent": "plur-dashboard"})
    with urllib.request.urlopen(req) as r, open(dst, "wb") as f:
        shutil.copyfileobj(r, f)
    print(f"  saved → {os.path.relpath(dst, P.base)} ({os.path.getsize(dst)//1024} KB)")


def rethumb(video_path, thumb_path):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "1.5", "-i", video_path,
                    "-frames:v", "1", "-vf", "scale=360:-1", thumb_path], check=False)
    print("  refreshed thumbnail")


def mark_processed(P, queue_id, extra):
    if not queue_id or not os.path.exists(P.queue):
        return
    kept, done = [], None
    for line in open(P.queue, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        e = json.loads(line)
        (kept.append(e) if e.get("id") != queue_id else None)
        if e.get("id") == queue_id:
            done = e
    with open(P.queue, "w", encoding="utf-8") as f:
        for e in kept:
            f.write(json.dumps(e) + "\n")
    if done:
        done.update(extra)
        done["status"] = "done"
        done["processed"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(P.processed, "a", encoding="utf-8") as f:
            f.write(json.dumps(done) + "\n")
        print(f"  queue item {queue_id} → processed.jsonl")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=default_pid())
    ap.add_argument("--scene", type=int, required=True)
    ap.add_argument("--kind", choices=["visual", "vo"], required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--queue-id")
    ap.add_argument("--prompt")
    ap.add_argument("--ref")
    ap.add_argument("--soul")
    ap.add_argument("--voice")
    ap.add_argument("--vo-text")
    ap.add_argument("--new-scene", action="store_true")
    ap.add_argument("--title")
    ap.add_argument("--duration", type=int)
    ap.add_argument("--author")
    a = ap.parse_args()
    if not a.project:
        raise SystemExit("no project (pass --project or create one in projects.json)")

    P = Paths(a.project)
    scenes = load_scenes(P)
    scene = next((s for s in scenes if s["id"] == a.scene), None)

    if not scene:
        if not (a.new_scene or a.title):
            raise SystemExit(f"scene {a.scene} not found (pass --new-scene to create it)")
        scene = {"id": a.scene, "title": a.title or f"Shot {a.scene}", "type": "motion-graphic",
                 "duration": 8, "video": f"assets/videos/scene{a.scene}.mp4",
                 "thumb": f"assets/thumbs/scene{a.scene}.jpg", "ref_image": None,
                 "storyboard": "", "camera": "", "visual_prompt": "",
                 "vo_text": "", "vo_audio": f"assets/audio/scene{a.scene}.wav",
                 "soul_id": None}
        scenes.append(scene)
        scenes.sort(key=lambda s: s["id"])
        print(f"  created new scene {a.scene} — {scene['title']}")

    print(f"Applying {a.kind} result to [{a.project}] SHOT {a.scene} — {scene['title']}")

    if a.kind == "visual":
        vid = os.path.join(P.base, f"assets/videos/scene{a.scene}.mp4")
        thumb = os.path.join(P.base, f"assets/thumbs/scene{a.scene}.jpg")
        os.makedirs(os.path.dirname(vid), exist_ok=True)
        os.makedirs(os.path.dirname(thumb), exist_ok=True)
        backup(P, vid)
        download(P, a.url, vid)
        rethumb(vid, thumb)
        scene["video_rev"] = int(time.time())   # marks the video as freshly generated ("NEW")
        if a.author:
            scene["video_author"] = a.author
        if a.duration:
            scene["duration"] = a.duration
        if a.prompt:
            scene["visual_prompt"] = a.prompt
        if a.soul is not None:
            scene["soul_id"] = a.soul or None
        if a.ref:
            src = a.ref if os.path.isabs(a.ref) else os.path.join(P.base, a.ref)
            ext = os.path.splitext(src)[1] or ".jpg"
            dst = os.path.join(P.base, f"assets/refs/scene{a.scene}{ext}")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            backup(P, dst)
            shutil.copy2(src, dst)
            scene["ref_image"] = f"assets/refs/scene{a.scene}{ext}"
            scene["type"] = "product"
    else:
        aud = os.path.join(P.base, f"assets/audio/scene{a.scene}.wav")
        os.makedirs(os.path.dirname(aud), exist_ok=True)
        backup(P, aud)
        download(P, a.url, aud)
        scene["vo_rev"] = int(time.time())      # marks the voiceover as freshly generated ("NEW")
        if a.author:
            scene["vo_author"] = a.author
        if a.vo_text:
            scene["vo_text"] = a.vo_text
        if a.voice:
            scene["voice_id"] = a.voice

    save_scenes(P, scenes)
    print("  scenes.json updated")
    mark_processed(P, a.queue_id, {"result_url": a.url})
    print("Done. Refresh the dashboard to see it.\n")


if __name__ == "__main__":
    main()
