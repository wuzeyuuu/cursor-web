# Cursor Web

[English](README.md) | [中文](README.zh.md)

**Core purpose: write code from your phone, anywhere.**  
The agent runs on the computer at home and edits files on that machine. You can be on the subway, in bed, or outside — open a browser and keep giving it orders.

This is a stdlib-only Python app (no pip packages). Python 3.10+.

## Why this exists (a rant about Cursor)

Cursor sells “AI that writes code” hard, then welds the agent that can actually edit your repo to the desktop IDE. Walk away from that editor window and the capability drops to zero.

Worse: same product, same bill, two conversation stores that do not talk to each other:

- IDE sidebar: `agent-transcripts` / composer
- Agent CLI: `~/.cursor/chats`

Cursor staff have confirmed you **cannot** `--resume` across them. Context you built at the desk is gone the moment you leave. Cloud / mobile sync is a cloud agent, not the local CLI on *this* machine. There is no official way to keep “edit the files on my disk” going from a phone.

So this project is small on purpose: wrap the **local Cursor Agent CLI** in a phone-usable web UI. It is not a new IDE. It is the agent you already pay for, unshackled from sitting in front of a monitor.

The web chat is **not** the Cursor IDE sidebar. It is a CLI session against the workspace you set. That split is Cursor’s, not a bug in this repo.

The session list also **read-only** shows IDE transcripts for the same workspace (`~/.cursor/projects/.../agent-transcripts`) so you can reread desk-side chats on a phone. You still cannot `--resume` an IDE id. Sending from an IDE thread starts a new CLI chat.

## What it does

- Password-protected web UI (cookie session), usable on a phone
- Streams `agent -p --output-format stream-json` over SSE
- Resumes **CLI** chats (`~/.cursor/chats`)
- Read-only listing of matching **IDE** chats; sending starts a new CLI chat
- Modes: agent / ask / plan; model + fast variant
- Bottom session drawer + stop button while a job is running (mobile-first)

## Layout

```
server.py              entry point
app/
  config.py            paths, env, agent binary discovery
  store.py             state.json / secrets.json
  auth.py              password + sessions
  jobs.py              in-flight task status
  chats.py             CLI sqlite + IDE jsonl transcripts
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

This app must stay on **this machine** (it shells out to local `agent`). Do not deploy it as a Worker. The Windows service `cloudflared` already runs named tunnel **cursor** (`27a9add3-53d2-45bf-8cfd-b2ba1a3b6853`). Do not run Quick Tunnels (`trycloudflare.com`); those URLs change and do not support SSE.

Add a public hostname once (Zero Trust → Networks → Tunnels → **cursor** → Public Hostname):

- Subdomain: `cursor`
- Domain: `klinedance.kdns.fr` (also active: `klinedance.dpdns.org`)
- Type: HTTP
- URL: `127.0.0.1:8786`

Then keep `start.bat` running and open `https://cursor.klinedance.kdns.fr/login`. `tunnel.bat` only prints this status; it does not start another tunnel.

This exposes an agent that can edit files. Keep the web password, prefer Cloudflare Access on the hostname, and do not share the URL.

If you copied `secrets.json` from another machine, the old password hash still works until you set `AGENT_WEB_PASSWORD` (that overwrites the hash). Foreign Linux workspace paths are remapped to this machine on startup; the old `chat_id` is cleared.

## Security (read this before exposing the port)

- Default bind is `0.0.0.0` so a phone on LAN can connect. Traffic is **plain HTTP**. Do not use public Wi‑Fi. Prefer SSH tunnel or bind `AGENT_WEB_HOST=127.0.0.1`.
- Agent mode can **read/write files and run commands** in the workspace (`--force`). Use ask/plan if you only want Q&A.
- Never commit `secrets.json`, `state.json`, or `.env`. They hold the API key, password hash, and session cookies.
- The API key in a copied `secrets.json` should be treated as exposed if that file ever left your machine. Rotate it in the Cursor dashboard if you are unsure.

## GitHub publish checklist

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
| `HTTPS_PROXY` / `AGENT_HTTPS_PROXY` | HTTP proxy for Agent CLI (auto-detects Clash/system proxy if unset) |
| `CURSOR_CHATS_ROOT` | Override CLI chat sqlite root |
| `CURSOR_PROJECTS_ROOT` | Override IDE `~/.cursor/projects` root |
| `AGENT_WEB_SESSION_DAYS` | Cookie lifetime (default 7) |

## License

Use at your own risk. This wraps Cursor’s CLI; you need a Cursor account and their terms apply.
