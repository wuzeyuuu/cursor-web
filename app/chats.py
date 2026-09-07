from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from .config import CHAT_ID_RE, CHATS_ROOT
from .jsonio import load_json


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
    return any(tag in raw for tag in ("<user_info>", "<agent_skills>", "<rules>", "<timestamp>"))


def load_history(workspace: str, chat_id: str, limit: int | None = 40) -> tuple[list[dict], int]:
    db_path = chat_store_path(workspace, chat_id)
    if not db_path.exists():
        return [], 0
    messages: list[dict] = []
    con = sqlite3.connect(db_path)
    try:
        for (_rowid, data) in con.execute("SELECT rowid, data FROM blobs ORDER BY rowid"):
            try:
                obj = json.loads(data)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            role = obj.get("role")
            if role not in ("user", "assistant"):
                continue
            if role == "user" and is_system_user_blob(obj.get("content")):
                continue
            text = extract_message_text(obj.get("content"))
            if not text:
                continue
            messages.append({"role": role, "text": text})
    finally:
        con.close()
    messages = dedupe_messages(messages)
    total = len(messages)
    if limit is not None and limit >= 0 and total > limit:
        messages = messages[-limit:]
    return messages, total


def session_preview(workspace: str, chat_id: str) -> str:
    db_path = chat_store_path(workspace, chat_id)
    if not db_path.exists():
        return "空会话"
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
            preview = text.replace("\n", " ")
            return preview[:56] + ("…" if len(preview) > 56 else "")
    finally:
        con.close()
    return "空会话"


def list_sessions(workspace: str, limit: int = 15) -> list[dict]:
    base = CHATS_ROOT / workspace_hash(workspace)
    if not base.exists():
        return []
    sessions = []
    for entry in base.iterdir():
        if not entry.is_dir() or not CHAT_ID_RE.match(entry.name):
            continue
        meta = load_json(entry / "meta.json")
        if not meta.get("hasConversation", False):
            continue
        chat_id = entry.name
        sessions.append(
            {
                "id": chat_id,
                "updated_at": meta.get("updatedAtMs"),
                "preview": session_preview(workspace, chat_id),
            }
        )
    sessions.sort(key=lambda item: item.get("updated_at") or 0, reverse=True)
    return sessions[:limit]
