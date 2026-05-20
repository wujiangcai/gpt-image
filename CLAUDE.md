# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Common commands

```bash
python -m venv .venv
source .venv/Scripts/activate  # Git Bash on Windows
pip install -r requirements.txt
cp .env.example .env
python main.py
```

The local app serves `http://127.0.0.1:8000` by default. `main.py` also honors `HOST` and `PORT` environment variables.

Docker deployment runs this app plus a private `chatgpt2api` upstream service:

```bash
docker compose up -d --build
docker compose logs -f image-gen-demo
docker compose logs -f chatgpt2api
docker compose down
```

There is currently no configured test suite, lint command, or package build step in this repository. For a quick syntax check after Python edits, use:

```bash
python -m py_compile main.py
```

## Architecture notes

`main.py` is a single FastAPI application. It serves the user UI from `static/index.html` at `/`, the admin UI from `static/admin.html` at `/admin`, and static assets under `/static`.

The backend has three main responsibilities:

- End-user image proxy: `/api/generate` and `/api/edits` require a bearer user/admin token and forward requests to an upstream image API.
- Admin/account proxy: `/api/accounts*` forwards account-management requests to a configured `chatgpt2api` service using hidden `C2A_BASE`/`C2A_KEY` server-side credentials.
- Local user-key management: `/api/users*` creates, updates, disables, and deletes browser user keys stored in `_auth.json`.

Runtime mode is selected by `MODE`:

- `relay` forwards to an OpenAI-compatible Images API using `IMAGE_API_BASE`, `IMAGE_API_KEY`, and `IMAGE_MODEL`. Admin-saved relay settings in `_auth.json` override the `.env` defaults.
- `chat2api` translates image prompts into chat completions via `CHAT_API_BASE`/`CHAT_API_KEY`, extracts markdown image URLs from the response, downloads them, and returns base64 image data. `/api/edits` is not implemented in this mode.

Authentication state is stored in `_auth.json`. If `ADMIN_TOKEN` is set, startup writes it into `_auth.json`; otherwise the first startup creates an `admin-*` token and prints it in logs. Browser tokens are stored client-side by `static/auth.js` in localStorage and sent as `Authorization: Bearer ...`.

`docker-compose.yml` builds `image-gen-demo`, runs `chatgpt2api` on the Docker network without exposing it to the host, and exposes only this app on host port `8080`. The compose file expects `C2A_KEY` and optionally `ADMIN_TOKEN` from the environment or `.env`.

## Operational notes

Local runtime files may contain secrets or account state: `.env`, `_auth.json`, `c2a-config.json`, and `c2a-data/`. Read only what is needed and do not print or copy secret values into responses, docs, or commits.
