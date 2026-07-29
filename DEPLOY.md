# Deploying the PLÜR Dashboard to Sevalla (team subdomain)

This app is a **Python server that needs ffmpeg and persistent storage**.

> **Where does this run?** Kinsta moved its Application Hosting into a separate product, **Sevalla**
> (sevalla.com). That's why MyKinsta has no "Applications" tab anymore. Your WordPress site stays on
> Kinsta/MyKinsta untouched; this dashboard runs on **Sevalla**, a separate signup (log in with
> Google / GitHub / email). The two are fully independent — WordPress never runs this code — and the
> subdomain (e.g. `dashboard.yoursite.com`) is pointed at the Sevalla app via DNS.

## ⚠️ Prerequisites (read first)
1. **A Sevalla account** — sign up at sevalla.com. Pay-as-you-go; there's a usage-based free trial credit.
   **Attaching a custom domain requires a paid pod tier** (the free/Hobby pod can't add custom domains).
2. **A Git repository.** Sevalla deploys from GitHub / GitLab / Bitbucket, or a Dockerfile/Docker image.
   Already done: **INFINITSTUDIOS/xumplur** (private) on branch `main`.
3. **Persistent disk** — add a Sevalla disk mounted at **`/data`**. Without it, every generated
   video/voiceover, edit, draft and trim **resets on each redeploy** (the container filesystem is ephemeral).
   The app reads `DATA_DIR=/data` and seeds it on first boot. (Disks pin the app to a single instance — fine here.)
4. **Funded Cloud API wallet** — the ⚡ "Generate all pending" button uses the Higgsfield **Cloud API**
   (platform.higgsfield.ai), a separate credit wallet from your Team Plan. It must have credits.

## Git (done)
The repo is already pushed:
```bash
# already created & pushed as a PRIVATE repo:
#   https://github.com/INFINITSTUDIOS/xumplur   (branch: main)
```
`.gitignore`/`.dockerignore` exclude `.env`, `.venv`, history, and exports — the secret key is never committed.

## Deploy on Sevalla
1. **sevalla.com → Applications → Add application** → connect GitHub → pick **INFINITSTUDIOS/xumplur**, branch `main`.
2. Build: choose **Dockerfile** (Sevalla builds it — ffmpeg + SDK included). No build/start command needed
   (`CMD` runs `python3 app.py`).
3. Pick a paid **pod** size (needed for the custom domain later; the smallest is fine to start).
4. **Applications → your app → Disks → Create disk**: Process = web, **Path = `/data`**, Size = a few GB.
5. Set the environment variables (next section), then **Deploy**.

## Environment variables / secrets (Applications → your app → Environment variables)
| Var | Value | Purpose |
|-----|-------|---------|
| `HOST` | `0.0.0.0` | already set in the Dockerfile |
| `PORT` | *(leave unset)* | Sevalla injects it; the app reads it |
| `DATA_DIR` | `/data` | persistent disk mount (already in Dockerfile; must match the disk's Path) |
| `HF_KEY` | `your-key:your-secret` | Higgsfield Cloud API credential (mark as secret) |
| `DASH_USER` | e.g. `team` | login username for the gate |
| `DASH_PASSWORD` | a shared password (mark as secret) | **enables the login gate** — without it the site is open to anyone with the URL |

## Domain (the subdomain of your WordPress site)
Requires a **paid pod** (Hobby pods can't add custom domains).
1. **Applications → your app → Domains → Add custom domain** → enter your subdomain (e.g. `dashboard.yoursite.com`).
2. Sevalla shows DNS records to add wherever your DNS is managed (likely the same place your WordPress DNS lives):
   - a **TXT** ownership record (`_cf-custom-hostname`) and a **TXT** SSL-validation record (`_acme-challenge`), then
   - the **A record** value(s) Sevalla provides to point the subdomain at the app.
   (Note: Sevalla points via **A records**, not a CNAME.) These only add the subdomain's own records — they
   do not touch the WordPress site's records.
3. HTTPS/SSL is **automatic and free** once DNS resolves; keep the `_acme-challenge` record in place so it auto-renews.

## What works in the cloud vs. not
- ✅ **Video** generation via the ⚡ button — works once `HF_KEY` is set and the Cloud API wallet is funded.
- ✅ Trims, drafts, downloads, film render, per-shot editing — all work (ffmpeg is in the image).
- ⚠️ **Voiceover generation cannot run in the cloud** — confirmed: the Cloud REST API exposes no plain
  text-to-speech model (only `higgsfield-ai/speak`, a talking-avatar that needs an image), and the MCP
  that makes your "Sterling" voice is behind Cloudflare bot protection + Claude-only OAuth, so a headless
  server can't call it. VO credits also live on your funded **team** wallet, not the API wallet.

### The voiceover workflow (local → cloud)
Because TTS can't run server-side, voiceovers are produced locally and uploaded:
1. On the **local** dashboard, generate the voiceover as usual (via Claude + MCP, Sterling voice, team credits).
2. Download that shot's audio (the ⬇ Audio button), or grab it from `projects/<id>/assets/audio/`.
3. On the **cloud** dashboard, open the shot → **⬆ Upload VO audio** → pick the mp3/wav.
   It swaps into the scene (old one archived to `_history`), shows the ✦ NEW badge, and is included in
   film renders — exactly like a generated one. The voice picker/VO text on the shot are attached too.

This keeps the exact Sterling voice while letting the team work in the cloud. If you later want the team to
regenerate VO directly from the cloud UI, that requires wiring in a third-party TTS provider (ElevenLabs/
OpenAI) — a different voice — which we chose not to do.

## Rotate the key
`HF_KEY` has appeared in chat/.env during development — rotate it at cloud.higgsfield.ai/api-keys and set
the new value as the Kinsta secret. Never commit `.env` (it's gitignored).
