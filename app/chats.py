from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from functools import lru_cache
from pathlib import Path

from .config import CHAT_ID_RE, CHATS_ROOT, PROJECTS_ROOT
from .jsonio import load_json

JSONL_HISTORY_MAX_BYTES = 8_000_000
JSONL_PREVIEW_MAX_BYTES = 400_000


def workspace_hash(workspace: str) -> str:
    return hashlib.md5(workspace.encode()).hexdigest()


def extract_message_text(content) -> str:
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        text = "".join(parts)
    else:
        return ""
    match = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, re.S)
    if match:
        return match.group(1).strip()
    return text.strip()


def chat_store_path(workspace: str, chat_id: str) -> Path:
    return CHATS_ROOT / workspace_hash(workspace) / chat_id / "store.db"


def workspace_project_slug(workspace: str) -> str:
    path = Path(workspace)
    try:
        path = path.resolve()
    except OSError:
        pass
    raw = str(path).replace(":", "").replace("\\", "/").replace("//", "/")
    parts = [part for part in raw.split("/") if part]
    return "-".join(part.lower() for part in parts)


@lru_cache(maxsize=16)
def ide_transcripts_dir(workspace: str) -> Path | None:
    root = PROJECTS_ROOT
    if not root.is_dir():
        return None
    slug = workspace_project_slug(workspace)
    direct = root / slug / "agent-transcripts"
    if direct.is_dir():
        return direct
    tail_parts = [part for part in Path(workspace).parts if part not in (".", "/")]
    tail = "-".join(p.lower().rstrip(":\\") for p in tail_parts[-2:]) if len(tail_parts) >= 2 else slug
    matches: list[Path] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.is_dir():
            continue
        tdir = entry / "agent-transcripts"
        if not tdir.is_dir():
            continue
        name = entry.name.lower()
        if name == slug or (tail and name.endswith(tail)):
            matches.append(tdir)
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def ide_jsonl_path(workspace: str, chat_id: str) -> Path | None:
    base = ide_transcripts_dir(workspace)
    if not base:
        return None
    path = base / chat_id / f"{chat_id}.jsonl"
    return path if path.is_file() else None


def chat_source(workspace: str, chat_id: str) -> str | None:
    if not chat_id:
        return None
    if chat_store_path(workspace, chat_id).exists():
        return "cli"
    if ide_jsonl_path(workspace, chat_id):
        return "ide"
    return None


def chat_exists(workspace: str, chat_id: str) -> bool:
    return chat_source(workspace, chat_id) is not None


def _message_content(obj: dict):
    message = obj.get("message")
    if isinstance(message, dict) and "content" in message:
        return message.get("content")
    return obj.get("content")


def _read_tail_bytes(path: Path, max_bytes: int) -> tuple[bytes, bool]:
    data = path.read_bytes()
    if len(data) <= max_bytes:
        return data, False
    data = data[-max_bytes:]
    newline = data.find(b"\n")
    if newline >= 0:
        data = data[newline + 1 :]
    return data, True


def _jsonl_objects(path: Path, max_bytes: int) -> tuple[list[dict], bool]:
    data, truncated = _read_tail_bytes(path, max_bytes)
    records: list[dict] = []
    for line in data.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records, truncated


def _jsonl_to_messages(records: list[dict]) -> list[dict]:
    messages: list[dict] = []
    for obj in records:
        role = obj.get("role")
        if role not in ("user", "assistant"):
            continue
        content = _message_content(obj)
        if role == "user" and is_system_user_blob(content):
            continue
        text = extract_message_text(content)
        if not text:
            continue
        _keep_deduped(messages, {"role": role, "text": text, "rowid": 0})
    for i, msg in enumerate(messages, 1):
        msg["rowid"] = i
    return messages


def dedupe_messages(messages: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    for msg in messages:
        if deduped and deduped[-1]["role"] == msg["role"] and deduped[-1]["text"] == msg["text"]:
            continue
        deduped.append(msg)
    return deduped


def is_system_user_blob(content) -> bool:
    raw = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    if "<user_query>" in raw:
        return False
    return any(
        tag in raw
        for tag in (
            "<user_info>",
            "<agent_skills>",
            "<rules>",
            "<timestamp>",
            "<dynamic_tools>",
            "<agent_transcripts>",
            "<open_and_recently_viewed_files>",
            "<git_status>",
            "<always_applied_workspace_rules>",
        )
    )


def _blob_message(rowid: int, data) -> dict | None:
    try:
        obj = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    role = obj.get("role")
    if role not in ("user", "assistant"):
        return None
    if role == "user" and is_system_user_blob(obj.get("content")):
        return None
    text = extract_message_text(obj.get("content"))
    if not text:
        return None
    return {"role": role, "text": text, "rowid": rowid}


def _keep_deduped(messages: list[dict], msg: dict) -> None:
    if messages and messages[-1]["role"] == msg["role"] and messages[-1]["text"] == msg["text"]:
        return
    messages.append(msg)


def _history_payload(messages: list[dict], has_older: bool) -> dict:
    return {
        "messages": messages,
        "total": len(messages),
        "truncated": has_older,
        "has_older": has_older,
        "oldest_rowid": messages[0]["rowid"] if messages else None,
        "latest_rowid": messages[-1]["rowid"] if messages else None,
    }


def _empty_history() -> dict:
    return _history_payload([], False)


def _slice_messages(
    messages: list[dict],
    limit: int | None,
    after_rowid: int | None,
    before_rowid: int | None,
    file_truncated: bool,
) -> dict:
    if after_rowid is not None:
        sliced = [m for m in messages if m["rowid"] > after_rowid]
        if limit is not None:
            sliced = sliced[:limit]
        return _history_payload(sliced, False)
    if before_rowid is not None:
        older = [m for m in messages if m["rowid"] < before_rowid]
        take = limit if limit is not None else 40
        has_older = file_truncated or len(older) > take
        return _history_payload(older[-take:], has_older)
    take = limit if limit is not None else len(messages)
    has_older = file_truncated or len(messages) > take
    return _history_payload(messages[-take:], has_older)


def _load_jsonl_history(
    path: Path,
    limit: int | None,
    after_rowid: int | None,
    before_rowid: int | None,
) -> dict:
    records, truncated = _jsonl_objects(path, JSONL_HISTORY_MAX_BYTES)
    messages = _jsonl_to_messages(records)
    return _slice_messages(messages, limit, after_rowid, before_rowid, truncated)


def load_history(
    workspace: str,
    chat_id: str,
    limit: int | None = 40,
    after_rowid: int | None = None,
    before_rowid: int | None = None,
) -> dict:
    db_path = chat_store_path(workspace, chat_id)
    empty = _empty_history()
    if not db_path.exists():
        jsonl = ide_jsonl_path(workspace, chat_id)
        if not jsonl:
            return empty
        if limit is not None and limit <= 0:
            limit = None
        return _load_jsonl_history(path=jsonl, limit=limit, after_rowid=after_rowid, before_rowid=before_rowid)
    if limit is not None and limit <= 0:
        limit = None
    messages: list[dict] = []
    total = 0
    has_older = False
    con = sqlite3.connect(db_path)
    try:
        if after_rowid is not None:
            rows = con.execute(
                "SELECT rowid, data FROM blobs WHERE rowid > ? ORDER BY rowid",
                (after_rowid,),
            )
            cap = 200 if limit is None else max(limit, 200)
            for rowid, data in rows:
                msg = _blob_message(rowid, data)
                if not msg:
                    continue
                _keep_deduped(messages, msg)
                if len(messages) >= cap:
                    break
            total = len(messages)
        elif before_rowid is not None:
            newest_first: list[dict] = []
            rows = con.execute(
                "SELECT rowid, data FROM blobs WHERE rowid < ? ORDER BY rowid DESC",
                (before_rowid,),
            )
            take = limit if limit is not None else 40
            for rowid, data in rows:
                msg = _blob_message(rowid, data)
                if not msg:
                    continue
                if newest_first and newest_first[-1]["role"] == msg["role"] and newest_first[-1]["text"] == msg["text"]:
                    continue
                newest_first.append(msg)
                if len(newest_first) > take:
                    has_older = True
                    newest_first.pop()
                    break
            messages = list(reversed(newest_first))
            total = len(messages)
        else:
            newest_first: list[dict] = []
            take = limit if limit is not None else 10**9
            for rowid, data in con.execute("SELECT rowid, data FROM blobs ORDER BY rowid DESC"):
                msg = _blob_message(rowid, data)
                if not msg:
                    continue
                if newest_first and newest_first[-1]["role"] == msg["role"] and newest_first[-1]["text"] == msg["text"]:
                    continue
                if len(newest_first) < take:
                    newest_first.append(msg)
                    total += 1
                else:
                    has_older = True
                    break
            messages = list(reversed(newest_first))
    finally:
        con.close()
    return _history_payload(messages, has_older)


def _clip_preview(text: str) -> str:
    preview = text.replace("\n", " ")
    return preview[:56] + ("…" if len(preview) > 56 else "")


def _jsonl_preview(path: Path) -> str:
    records, _truncated = _jsonl_objects(path, JSONL_PREVIEW_MAX_BYTES)
    for obj in reversed(records):
        if obj.get("role") != "user":
            continue
        content = _message_content(obj)
        if is_system_user_blob(content):
            continue
        text = extract_message_text(content)
        if text:
            return _clip_preview(text)
    return "空会话"


def session_preview(workspace: str, chat_id: str) -> str:
    db_path = chat_store_path(workspace, chat_id)
    if db_path.exists():
        con = sqlite3.connect(db_path)
        try:
            rows = con.execute("SELECT data FROM blobs ORDER BY rowid DESC LIMIT 80").fetchall()
            for (data,) in rows:
                try:
                    obj = json.loads(data)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if obj.get("role") != "user" or is_system_user_blob(obj.get("content")):
                    continue
                text = extract_message_text(obj.get("content"))
                if not text:
                    continue
                return _clip_preview(text)
        finally:
            con.close()
        jsonl = ide_jsonl_path(workspace, chat_id)
        if jsonl:
            return _jsonl_preview(jsonl)
        return "空会话"
    jsonl = ide_jsonl_path(workspace, chat_id)
    if jsonl:
        return _jsonl_preview(jsonl)
    return "空会话"


def sessions_etag(sessions: list[dict]) -> str:
    raw = "|".join(
        f"{s['id']}:{s.get('updated_at') or 0}:{s.get('source') or ''}" for s in sessions
    )
    return hashlib.sha1(raw.encode()).hexdigest()


def parse_known(raw: str) -> dict[str, int]:
    known: dict[str, int] = {}
    if not raw:
        return known
    for part in raw.split(","):
        chat_id, sep, ts = part.partition(":")
        chat_id = chat_id.strip()
        if not sep or not CHAT_ID_RE.match(chat_id):
            continue
        try:
            known[chat_id] = int(float(ts))
        except ValueError:
            continue
    return known


def _list_cli_index(workspace: str) -> list[dict]:
    base = CHATS_ROOT / workspace_hash(workspace)
    if not base.exists():
        return []
    index: list[dict] = []
    for entry in base.iterdir():
        if not entry.is_dir() or not CHAT_ID_RE.match(entry.name):
            continue
        meta = load_json(entry / "meta.json")
        if not meta.get("hasConversation", False):
            continue
        updated = meta.get("updatedAtMs")
        try:
            updated_i = int(updated) if updated is not None else 0
        except (TypeError, ValueError):
            updated_i = 0
        index.append({"id": entry.name, "updated_at": updated_i, "source": "cli"})
    return index


def _list_ide_index(workspace: str) -> list[dict]:
    base = ide_transcripts_dir(workspace)
    if not base:
        return []
    index: list[dict] = []
    try:
        entries = list(base.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.is_dir() or not CHAT_ID_RE.match(entry.name):
            continue
        jsonl = entry / f"{entry.name}.jsonl"
        if not jsonl.is_file():
            continue
        try:
            updated_i = int(jsonl.stat().st_mtime * 1000)
        except OSError:
            continue
        index.append({"id": entry.name, "updated_at": updated_i, "source": "ide"})
    return index


def list_sessions(
    workspace: str, limit: int = 30, known: dict[str, int] | None = None
) -> list[dict]:
    by_id: dict[str, dict] = {}
    for item in _list_cli_index(workspace):
        by_id[item["id"]] = item
    for item in _list_ide_index(workspace):
        chat_id = item["id"]
        if chat_id in by_id:
            by_id[chat_id]["updated_at"] = max(
                by_id[chat_id].get("updated_at") or 0, item.get("updated_at") or 0
            )
        else:
            by_id[chat_id] = item
    index = sorted(by_id.values(), key=lambda item: item.get("updated_at") or 0, reverse=True)
    page = index[:limit]
    known = known or {}
    out: list[dict] = []
    for item in page:
        chat_id = item["id"]
        ts = item.get("updated_at") or 0
        row = {"id": chat_id, "updated_at": ts, "source": item.get("source") or "cli"}
        if known.get(chat_id) == ts:
            row["unchanged"] = True
        else:
            row["preview"] = session_preview(workspace, chat_id)
        out.append(row)
    return out
