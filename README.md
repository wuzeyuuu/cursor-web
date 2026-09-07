# Cursor Web

Browser UI for the **Cursor Agent CLI** running on this machine. Send prompts from a phone or another computer on the same network; the agent still edits files locally.

This is a stdlib-only Python app (no pip packages). Python 3.10+.

## What it does

- Password-protected web UI (cookie session)
- Streams `agent -p --output-format stream-json` over SSE
- Resumes Cursor chats (`~/.cursor/chats`)
- Modes: agent / ask / plan; model + fast variant

The web chat is **not** the Cursor IDE sidebar. It is a separate CLI session against the workspace you set.

## Layout

```
server.py              entry point
app/
  config.py            paths, env, agent binary discovery
  store.py             state.json / secrets.json
  auth.py              password + sessions
  jobs.py              in-flight task status
  chats.py             transcript history from Cursor sqlite
  agent_cli.py         spawn / stream / models
  http_handler.py      HTTP routes
  main.py
templates/             login + chat HTML
```

Runtime files (gitignored): `secrets.json`, `state.json`, `.env`.

## Install Cursor Agent CLI

The web server shells out to `agent`. Install it first.

Windows (PowerShell):

```powershell
irm 'https://cursor.com/install?win32=true' | iex
```

macOS / Linux / WSL:

```bash
curl https://cursor.com/install -fsS | bash
```

If it is not on PATH, set `AGENT_BIN` to the binary. On native Windows the installer usually puts a launcher at `%LOCALAPPDATA%\cursor-agent\agent.cmd`.

Get an API key from [Cursor Dashboard → API Keys](https://cursor.com/dashboard/api). Paste it in the page **Settings**, or set `CURSOR_API_KEY`.

## Run

```bash
cd cursor_web
copy .env.example .env    # Windows: copy  |  Unix: cp .env.example .env
```

Edit `.env`:

- `AGENT_WEB_PASSWORD` — required on first run (hashed into `secrets.json`, not stored in plaintext)
- `AGENT_WORKSPACE` — directory the agent may edit (default: parent repo if this folder is `third_party/cursor_web`)
- `CURSOR_API_KEY` — optional if you paste the key in the UI
- `AGENT_BIN` — optional if `agent` is on PATH
- `AGENT_WEB_HOST` / `AGENT_WEB_PORT` — default `0.0.0.0:8786`

Then:

```bash
python server.py
```

Windows: double-click `start.bat`, or:

```bat
set AGENT_WEB_PASSWORD=your-long-random-password
python server.py
```

Open `http://127.0.0.1:8786/login`.

## Cloudflare Tunnel (public HTTPS URL)

This app must stay on **this machine** (it shells out to local `agent`). Do not deploy it as a Worker. Map the local port with Cloudflare Tunnel instead.

1. Leave `start.bat` / `python server.py` running on port 8786.
2. Run `tunnel.bat` (installs are already done via `cloudflared`).
3. Copy the printed `https://….trycloudflare.com` URL and open `/login` on it.

Quick tunnels (`trycloudflare.com`) are for testing: the URL changes every run, and Cloudflare documents that they **do not support SSE**. Login and the UI should work; live chat streaming may stall until you create a **named tunnel** on your own domain (Zero Trust → Networks → Tunnels, then `cloudflared tunnel run --token …`).

This exposes an agent that can edit files. Keep the web password, prefer Cloudflare Access on a named hostname, and do not share the URL.

If you copied `secrets.json` from another machine, the old password hash still works until you set `AGENT_WEB_PASSWORD` (that overwrites the hash). Foreign Linux workspace paths are remapped to this machine on startup; the old `chat_id` is cleared.

## Security (read this before exposing the port)

- Default bind is `0.0.0.0` so a phone on LAN can connect. Traffic is **plain HTTP**. Do not use public Wi‑Fi. Prefer SSH tunnel or bind `AGENT_WEB_HOST=127.0.0.1`.
- Agent mode can **read/write files and run commands** in the workspace (`--force`). Use ask/plan if you only want Q&A.
- Never commit `secrets.json`, `state.json`, or `.env`. They hold the API key, password hash, and session cookies.
- The API key in a copied `secrets.json` should be treated as exposed if that file ever left your machine. Rotate it in the Cursor dashboard if you are unsure.

## GitHub publish checklist

This directory is meant to be split into its own repo later.

1. Copy `.env.example` → `.env` locally. Do not copy real secrets into the example files.
2. Confirm `.gitignore` ignores `secrets.json`, `state.json`, `.env`.
3. `git status` must not list API keys, password hashes, session tokens, or machine-specific paths (`/mnt/...`, `/workspace/...`).
4. Keep `secrets.example.json` and `state.example.json` empty of credentials.
5. Do not commit Cursor chat databases under `~/.cursor/chats`.

## Environment

| Variable | Meaning |
| --- | --- |
| `AGENT_BIN` | Path to `agent` / `agent.exe` / `agent.cmd` |
| `AGENT_WORKSPACE` | Default workspace |
| `AGENT_WEB_HOST` | Bind address |
| `AGENT_WEB_PORT` | Port (default 8786) |
| `AGENT_WEB_PASSWORD` | Sets/replaces the login password hash |
| `CURSOR_API_KEY` | Agent auth (or paste in UI) |
| `AGENT_MODEL` | Default model id |
| `CURSOR_CHATS_ROOT` | Override chat transcript root |
| `AGENT_WEB_SESSION_DAYS` | Cookie lifetime (default 7) |

## License

Use at your own risk. This wraps Cursor’s CLI; you need a Cursor account and their terms apply.
