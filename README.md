# PLÜR — Scene Dashboard (multi-project)

A local review + re-generation console for AI video projects. Ships with the
PLÜR "Science of 5-MAPB" film; you can add more projects via tabs.

## Run it

```bash
cd plur-dashboard
python3 app.py
```

Open **http://localhost:8756**. No installs — Python standard library only.

## Highlights

- **Project tabs** across the top — each project is its own set of clips. **＋ New project** scaffolds an empty one.
- **➕ Add clip** — queue a brand-new shot (title + prompt + optional character + voiceover) into the current project.
- **▶ Play all (film)** — plays every clip and its voiceover back-to-back as one continuous film (prev / next / pause).
- **▶ Play shot + voiceover** on each card — preview a single shot with its narration in sync.
- Per-scene edit → **Re-submit** → generate (standalone with your key, or via Claude) → auto-swapped back in, old render archived.

## What's here

```
plur-dashboard/
├── app.py              multi-project local server
├── dashboard.html      the UI (tabs, play-all, add-clip)
├── apply_result.py     finalizer: --project aware; can create new scenes
├── higgsfield_runner.py standalone engine: run <project-id>
├── config.json  catalog.json  characters.json   shared, global
├── projects.json       registry: [{id, name}]
└── projects/
    └── <project-id>/
        ├── scenes.json       source of truth for that project's shots
        ├── queue.jsonl       pending re-submissions (written by the UI)
        ├── processed.jsonl   completed re-submissions
        ├── uploads/          reference images uploaded through the UI
        └── assets/
            ├── videos/  sceneN.mp4     audio/  sceneN.wav
            ├── thumbs/  sceneN.jpg     refs/   reference images
            └── _history/ timestamped backups of every replaced asset (undo)
```

## The loop

1. In the dashboard, edit a **visual prompt** and/or **voiceover line**.
   For a visual you can also **upload a new reference image**.
2. Click **Re-submit visual** or **Re-submit voiceover** → the request is saved to `queue.jsonl`
   (uploaded images land in `uploads/`).
3. Tell Claude **"run the queue"**. Claude:
   - reads `queue.jsonl`,
   - for a new reference image: `media_upload` → PUT bytes → `media_confirm` to get a Higgsfield media_id,
   - regenerates the visual (`kling3_0_turbo`, 9:16, 8s — image-to-video if a ref is present) or the
     voiceover (`seed_audio`, Sterling voice `dc382508-…`),
   - runs `apply_result.py` to download the result, back up the old asset, refresh the thumbnail,
     update `scenes.json`, and move the item to `processed.jsonl`.
4. **Refresh the dashboard** — the new video/voiceover is in place.

`apply_result.py` usage is documented in its header. Every replaced file is backed up to
`assets/_history/`, so any change can be rolled back.

## Standalone mode (no Claude in the loop)

Make the dashboard regenerate on its own, straight through Higgsfield Cloud.

1. Get an API key at **https://cloud.higgsfield.ai/api-keys**.
2. Export it in your terminal (the key stays on your machine — never share it):
   ```bash
   export HF_KEY="your-api-key:your-api-secret"
   # or:  export HF_API_KEY="..."  HF_API_SECRET="..."
   ```
3. Confirm the SDK sees your key and the model slug is right:
   ```bash
   cd plur-dashboard && ./.venv/bin/python higgsfield_runner.py selftest
   ```
   If it reports the app slug/args need changing, edit `config.json` (the
   `video`/`audio` app slugs) and re-run selftest.
4. Start the dashboard **in that same shell** (so the server inherits the key):
   ```bash
   python3 app.py
   ```
5. In the UI: edit prompts / VO, upload references, **Re-submit**, then click
   **⚡ Generate all pending**. The backend uploads references, generates,
   polls, downloads, and swaps the new assets in — a live log streams in the page.
   Refresh happens automatically when the run finishes.

`config.json` holds the cloud application slugs and default params (9:16, 8s,
Sterling voice id). `higgsfield_runner.py run` is what the button triggers; you
can also run it directly from a terminal or a cron job.

## Notes

- A static page can't call Higgsfield directly (that needs Claude's authenticated MCP tools),
  so the dashboard **captures** requests and Claude executes them — no API keys live in the page.
- Sterling voice id: `dc382508-c8bd-443c-8cb2-46e57b8d2e6f`. "PLÜR" is voiced as one word ("plurr").
