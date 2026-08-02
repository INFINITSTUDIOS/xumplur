# Deploying the PLÜR Dashboard to Kinsta Application Hosting (team subdomain)

This app is a **Python server that needs ffmpeg and persistent storage**.

> ### 🚨 Two corrections to generic Kinsta/Python guides — required for THIS app
> Most online Kinsta Python tutorials use the **Buildpacks** path with a **gunicorn** start command.
> **Both are wrong for this app:**
> 1. **Use the Dockerfile build path, NOT Buildpacks/"Standard".** Buildpacks install Python packages
>    only — they do **not** include **ffmpeg**, which this app needs for thumbnails, filmstrips, and film
>    render. Our `Dockerfile` installs ffmpeg. Choose "deploy with a Dockerfile."
> 2. **Leave the start command BLANK — do NOT enter gunicorn.** This is a plain stdlib HTTP server, not a
>    WSGI/Flask/Django app; `gunicorn ...wsgi:application` would crash. The Docker image's `CMD`
>    (`python3 app.py`) starts the server and binds the `PORT` Kinsta injects on `0.0.0.0`.
>
> (Application Hosting is the same platform Kinsta now also brands as **Sevalla**. If your MyKinsta has no
> account-level **Applications** section, use sevalla.com instead — the steps below are identical.)

Your WordPress site stays on Kinsta untouched; this dashboard is a separate **Application**, and the
subdomain (e.g. `dashboard.xumplur.com`) is pointed at it via DNS.

## ⚠️ Prerequisites (read first)
1. **Account-level Applications access** (MyKinsta → 🏠 → Applications, or sevalla.com). A **paid pod** is
   required to attach a custom domain later.
2. **A Git repository** — done: **INFINITSTUDIOS/xumplur** (private) on branch `main`.
3. **Persistent disk** mounted at **`/data`**. Without it, every generated video/voiceover, edit, draft and
   trim **resets on each redeploy** (the container filesystem is ephemeral). The app reads `DATA_DIR=/data`
   and seeds it on first boot. (A disk pins the app to a single instance — fine here.)
4. **Funded Cloud API wallet** — the ⚡ "Generate all pending" button uses the Higgsfield **Cloud API**
   (platform.higgsfield.ai), a separate credit wallet from your Team Plan. It must have credits.

## Git (done)
```bash
# already created & pushed as a PRIVATE repo:
#   https://github.com/INFINITSTUDIOS/xumplur   (branch: main)
```
`.gitignore`/`.dockerignore` exclude `.env`, `.venv`, history, and exports — the secret key is never committed.

## Deploy (MyKinsta → 🏠 account level → Applications → Add service → Application)
1. Connect GitHub → pick **INFINITSTUDIOS/xumplur**, branch `main`.
2. **Basic settings:** name it (e.g. `xumplur-dashboard`); **Data center = Phoenix (US)** (matches your WP + users).
3. **Build environment: Dockerfile** (⚠️ not Standard/Buildpacks). **Start command: leave BLANK** (⚠️ not gunicorn).
4. **Environment variables** (next section).
5. Pick a **paid pod** (needed for the custom domain), then **Create application**.
6. After it deploys: **Disks / Persistent storage → add a disk**, process = web, **mount path `/data`**, a few GB.

## Environment variables / secrets
| Var | Value | Purpose |
|-----|-------|---------|
| `HOST` | `0.0.0.0` | already set in the Dockerfile |
| `PORT` | *(leave unset)* | Kinsta injects it; the app reads it |
| `DATA_DIR` | `/data` | persistent disk mount (already in Dockerfile; must match the disk's mount path) |
| `HF_KEY` | `your-key:your-secret` | Higgsfield Cloud API credential (mark as secret) |
| `GOOGLE_CLIENT_ID` | from Google Cloud (see below) | enables Google sign-in |
| `GOOGLE_CLIENT_SECRET` | from Google Cloud (mark as secret) | enables Google sign-in |
| `GOOGLE_ALLOWED_DOMAINS` | `xumplur.com,infinitstudios.com,illusiaagency.com` | only these Workspace domains may sign in |
| `SESSION_SECRET` | a 64-char random hex (mark as secret) | signs login cookies; set once and keep stable |
| `DASH_PASSWORD` | *(optional)* | Simple shared-password gate (form login). Overrides the hash in `auth.json`. Ignored once Google sign-in is configured. |

### Simple password gate (no Kinsta env var needed)
When Google sign-in is **not** configured, the app shows a styled **login page** and requires a shared
password before anything loads (the API is gated too, so nobody can generate via the browser or the API
without it). The password is stored as a **sha256 hash in `auth.json`** (committed to the repo), so you set
or change it by editing that file and pushing — no Kinsta env var required. To change it:
```bash
python3 -c "import hashlib,json; json.dump({'password_sha256':hashlib.sha256(b'YOUR_NEW_PW').hexdigest()}, open('auth.json','w'), indent=2)"
git commit -am "update dashboard password" && git push   # Kinsta redeploys
```
(Setting `DASH_PASSWORD` in the env overrides the file, if you ever prefer that.)

## Google sign-in setup (per-user login + authorship)
When `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET` are set, the app requires Google sign-in and only lets
in accounts on the domains in `GOOGLE_ALLOWED_DOMAINS`. Each signed-in user's name is stamped on whatever
they generate/upload (shown as an author badge). Create the OAuth client in **Google Cloud Console**:
1. **APIs & Services → OAuth consent screen** → User type **Internal** (if your org owns the domain) → fill app name + support email.
2. **APIs & Services → Credentials → Create credentials → OAuth client ID → Web application**.
3. **Authorized redirect URIs** — add one per hostname the app answers on:
   - `https://xumplur-create-7xdlj.sevalla.app/auth/callback`
   - (later, once the custom domain is live) `https://<your-subdomain>/auth/callback`
4. Copy the **Client ID** and **Client secret** into the env vars above. Redeploy.

No JavaScript-origin entry is needed (this is the server-side authorization-code flow). The app derives its
redirect URL from the request host, so each registered redirect URI just needs to match a hostname it serves.

## Domain (the subdomain of your site)
Requires a **paid pod**.
1. **Application → Domains → Add domain** → enter your subdomain (e.g. `dashboard.xumplur.com`).
2. Add the DNS records Kinsta/Sevalla shows, wherever your DNS is managed:
   - a **TXT** ownership record (`_cf-custom-hostname`) and a **TXT** SSL-validation record (`_acme-challenge`), then
   - the record(s) it provides to point the subdomain at the app (Cloudflare-for-SaaS setups use **A records**).
   These add only the subdomain's own records — they don't touch the WordPress site's records.
3. HTTPS/SSL is **automatic and free** once DNS resolves; keep the `_acme-challenge` record so it auto-renews.

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
