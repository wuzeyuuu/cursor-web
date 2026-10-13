from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "state.json"
SECRETS_PATH = ROOT / "secrets.json"
TEMPLATES_DIR = ROOT / "templates"
AGENT_LOG_PATH = ROOT / "logs" / "agent.log"


def load_dotenv(path: Path | None = None) -> None:
    env_path = path or ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv()

CHATS_ROOT = Path(
    os.environ.get("CURSOR_CHATS_ROOT", Path.home() / ".cursor" / "chats")
)
PROJECTS_ROOT = Path(
    os.environ.get("CURSOR_PROJECTS_ROOT", Path.home() / ".cursor" / "projects")
)

CHAT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)
DEFAULT_MODEL = os.environ.get("AGENT_MODEL", "composer-2.5")
POPULAR_MODELS = [
    "auto",
    "composer-2.5",
    "composer-2.5-fast",
    "gpt-5.3-codex",
    "gpt-5.3-codex-fast",
    "claude-opus-5-thinking-high",
    "claude-sonnet-5-thinking-high",
    "cursor-grok-4.6-high",
    "cursor-grok-4.6-high-fast",
]
MODEL_LINE_RE = re.compile(r"^([^\s]+)\s+-\s+(.+)$")
MODELS_CACHE_TTL = 300
MAX_AGENT_RETRIES = 3

SESSION_COOKIE = "agent_session"
SESSION_TTL = int(os.environ.get("AGENT_WEB_SESSION_DAYS", "7")) * 86400
LOGIN_RATE_WINDOW = 60
LOGIN_RATE_MAX = 5
LOGIN_LOCKOUT_AFTER = 10
LOGIN_LOCKOUT_SECONDS = 900

HOST = os.environ.get("AGENT_WEB_HOST", "0.0.0.0")
PORT = int(os.environ.get("AGENT_WEB_PORT", "8786"))


def infer_default_workspace() -> Path:
    env = os.environ.get("AGENT_WORKSPACE", "").strip()
    if env:
        return Path(env).expanduser()
    if ROOT.parent.name.lower() == "third_party":
        return ROOT.parent.parent
    return Path.cwd()


DEFAULT_WORKSPACE = infer_default_workspace()


def _windows_versioned_agent(localapp: Path) -> list[Path]:
    versions = localapp / "cursor-agent" / "versions"
    if not versions.is_dir():
        return []
    found: list[Path] = []
    for entry in sorted(versions.iterdir(), reverse=True):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        for name in ("cursor-agent.cmd", "agent.cmd", "cursor-agent.exe", "agent.exe"):
            candidate = entry / name
            if candidate.is_file():
                found.append(candidate)
                break
    return found


def _candidate_agent_bins() -> list[Path]:
    names = ["agent.cmd", "agent.exe", "cursor-agent.cmd", "agent"] if os.name == "nt" else ["agent"]
    out: list[Path] = []
    env = os.environ.get("AGENT_BIN", "").strip()
    if env:
        out.append(Path(env).expanduser())
    for name in names:
        found = shutil.which(name)
        if found:
            out.append(Path(found))
    home = Path.home()
    localapp = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
    extras = [
        localapp / "cursor-agent" / "agent.cmd",
        localapp / "cursor-agent" / "cursor-agent.cmd",
        localapp / "cursor-agent" / "agent.exe",
        *_windows_versioned_agent(localapp),
        home / ".local" / "bin" / "agent.exe",
        home / ".local" / "bin" / "agent",
        localapp / "Programs" / "cursor-agent" / "agent.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "cursor"
        / "resources"
        / "app"
        / "bin"
        / "agent.exe",
        Path(r"D:\Program Files\cursor\resources\app\bin\agent.exe"),
    ]
    out.extend(extras)
    return out


def find_agent_bin() -> Path:
    seen: set[str] = set()
    for path in _candidate_agent_bins():
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            return path
    env = os.environ.get("AGENT_BIN", "").strip()
    if env:
        return Path(env).expanduser()
    return Path("agent.exe" if os.name == "nt" else "agent")


AGENT_BIN = find_agent_bin()


def agent_install_hint() -> str:
    if os.name == "nt":
        return (
            "Cursor Agent CLI not found.\n"
            "Install in PowerShell:\n"
            "  irm 'https://cursor.com/install?win32=true' | iex\n"
            "Reopen the terminal, or set AGENT_BIN to the agent.exe path."
        )
    return (
        "Cursor Agent CLI not found.\n"
        "Install:\n"
        "  curl https://cursor.com/install -fsS | bash\n"
        "Then set AGENT_BIN if it is not on PATH."
    )


def workspace_needs_localize(workspace: str) -> bool:
    if not workspace:
        return True
    path = Path(workspace)
    if path.exists():
        return False
    if os.name == "nt" and (workspace.startswith("/") or workspace.startswith("\\mnt")):
        return True
    return False
