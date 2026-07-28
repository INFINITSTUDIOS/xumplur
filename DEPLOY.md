# Deploying the PLÜR Dashboard to Kinsta (team subdomain)

This app is a **Python server that needs ffmpeg and persistent storage**. That has consequences for Kinsta:

## Your setup
Your main site is WordPress and stays on Kinsta WordPress hosting untouched. This dashboard becomes a
**separate Kinsta Application** and the subdomain (e.g. `dashboard.yoursite.com`) points at it via DNS.
The two are fully independent — WordPress never runs this code.

## ⚠️ Prerequisites (read first)
1. **A Git repository.** Kinsta Application Hosting deploys from GitHub / GitLab / Bitbucket. Push this
   `plur-dashboard/` folder to a **private** repo first (see "Git" below).
2. **Persistent storage volume** — attach a Kinsta persistent disk mounted at **`/data`**.
   Without it, every generated video/voiceover, edit, saved draft and trim **resets on each redeploy**
   (Application Hosting has an ephemeral filesystem). The app reads `DATA_DIR=/data` and seeds it on first boot.
3. **Funded Cloud API wallet** — the ⚡ "Generate all pending" button uses the Higgsfield **Cloud API**
   (platform.higgsfield.ai), a separate credit wallet from your Team Plan. It must have credits.
4. **Single web process** — keep it at 1 instance. Data is flat-file on the volume; multiple replicas
   would risk write conflicts. Fine for a small team.

## Git (do this first)
```bash
cd plur-dashboard
git init && git add -A && git commit -m "PLÜR dashboard"
# create a PRIVATE repo on GitHub, then:
git remote add origin git@github.com:<you>/plur-dashboard.git
git push -u origin main
```
`.gitignore`/`.dockerignore` already exclude `.env`, `.venv`, history, and exports — the secret key is
never committed.

## Deploy on Kinsta
1. MyKinsta → **Applications** → **Add application** → connect the Git repo/branch.
2. Kinsta detects the **Dockerfile** automatically — no build/start command needed (`CMD` runs `python3 app.py`).
3. **Add → Persistent storage**, size a few GB, **mount path `/data`**.
4. Set the environment variables (next section), then **Deploy**.

## Environment variables / secrets (Kinsta → Application → Settings → Environment)
| Var | Value | Purpose |
|-----|-------|---------|
| `HOST` | `0.0.0.0` | already set in the Dockerfile |
| `PORT` | *(leave unset)* | Kinsta injects it; the app reads it |
| `DATA_DIR` | `/data` | persistent volume mount (already in Dockerfile; confirm the volume is mounted there) |
| `HF_KEY` | `your-key:your-secret` | Higgsfield Cloud API credential (secret) |
| `DASH_USER` | e.g. `team` | login username for the gate |
| `DASH_PASSWORD` | a shared password (secret) | **enables the login gate** — without it the site is open to anyone with the URL |

## Domain (the WordPress subdomain)
1. In the Application → **Domains** → add your subdomain (e.g. `dashboard.yoursite.com`).
2. Kinsta shows a **CNAME** target. Add that CNAME record wherever your DNS is managed (likely the same
   place your WordPress DNS lives). This only adds one subdomain record — it does not touch the WordPress
   site's own records.
3. Kinsta provisions HTTPS automatically once DNS resolves.

## What works in the cloud vs. not
- ✅ **Video** generation via the ⚡ button — works once `HF_KEY` is set and the Cloud API wallet is funded.
- ✅ Trims, drafts, downloads, film render, per-shot editing — all work (ffmpeg is in the image).
- ❌ **Voiceover generation does NOT work in the cloud.** The Cloud API has no text-to-speech model, and
  the MCP path (how VO is made today) only exists through Claude locally. So voiceovers must be generated
  from the local dashboard (via Claude) and will sync to the cloud through the shared data volume, OR you
  accept video-only generation in the cloud. There is no cloud TTS to enable this.

## Rotate the key
`HF_KEY` has appeared in chat/.env during development — rotate it at cloud.higgsfield.ai/api-keys and set
the new value as the Kinsta secret. Never commit `.env` (it's gitignored).
