"""Local storage: recorded tool calls (SQLite) and frozen workflows (JSON files).

Everything lives under ``<hermes home>/plugin-data/freeze/``. Nothing leaves the machine.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

PLUGIN = "freeze"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
# Tool results beyond this size are stored truncated; a truncated result can still be replayed
# but values past the cut cannot be bound, so they end up as compiled steps instead.
RESULT_CAP = 256_000
RETENTION_DAYS = 14

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    turn_id TEXT,
    tool_call_id TEXT,
    tool_name TEXT NOT NULL,
    args TEXT NOT NULL,
    result TEXT,
    truncated INTEGER NOT NULL DEFAULT 0,
    status TEXT,
    duration_ms INTEGER,
    cwd TEXT,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS calls_session ON calls(session_id, id);
CREATE TABLE IF NOT EXISTS turns (
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    user_message TEXT,
    answer TEXT,
    started REAL,
    ended REAL,
    api_calls INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, turn_id)
);
"""

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_conn_home: Optional[str] = None


def data_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir(PLUGIN)


def _db() -> sqlite3.Connection:
    """One shared connection per Hermes home; hooks fire from several threads, so callers hold _lock."""
    global _conn, _conn_home
    home = str(data_dir())
    if _conn is None or _conn_home != home:
        from plugins.plugin_storage import plugin_db
        _conn = plugin_db(PLUGIN)
        _conn.executescript(_SCHEMA)
        _conn_home = home
    return _conn


# ---------------------------------------------------------------- recording

def record_call(*, session_id: str, turn_id: str, tool_call_id: str, tool_name: str, args: Any,
                result: Any, status: str, duration_ms: int, cwd: str) -> None:
    text = result if isinstance(result, str) else json.dumps(result, default=str)
    truncated = len(text) > RESULT_CAP
    if truncated:
        text = text[:RESULT_CAP]
    with _lock:
        conn = _db()
        conn.execute(
            "INSERT INTO calls (session_id, turn_id, tool_call_id, tool_name, args, result, truncated,"
            " status, duration_ms, cwd, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, turn_id, tool_call_id, tool_name, json.dumps(args, default=str), text,
             int(truncated), status, int(duration_ms or 0), cwd, time.time()),
        )
        conn.commit()


def record_turn_start(*, session_id: str, turn_id: str, user_message: str) -> None:
    with _lock:
        conn = _db()
        conn.execute(
            "INSERT INTO turns (session_id, turn_id, user_message, started) VALUES (?,?,?,?)"
            " ON CONFLICT(session_id, turn_id) DO UPDATE SET user_message=excluded.user_message",
            (session_id, turn_id, user_message, time.time()),
        )
        conn.commit()


def record_turn_end(*, session_id: str, turn_id: str, answer: str) -> None:
    with _lock:
        conn = _db()
        conn.execute("UPDATE turns SET answer=?, ended=? WHERE session_id=? AND turn_id=?",
                     (answer, time.time(), session_id, turn_id))
        conn.commit()


def record_usage(*, session_id: str, turn_id: str, input_tokens: int, output_tokens: int) -> None:
    with _lock:
        conn = _db()
        conn.execute(
            "UPDATE turns SET api_calls=api_calls+1, input_tokens=input_tokens+?,"
            " output_tokens=output_tokens+? WHERE session_id=? AND turn_id=?",
            (int(input_tokens or 0), int(output_tokens or 0), session_id, turn_id),
        )
        conn.commit()


def prune(now: Optional[float] = None) -> int:
    """Drop recordings older than RETENTION_DAYS; frozen workflows are never pruned."""
    cutoff = (now or time.time()) - RETENTION_DAYS * 86400
    with _lock:
        conn = _db()
        n = conn.execute("DELETE FROM calls WHERE ts < ?", (cutoff,)).rowcount
        conn.execute("DELETE FROM turns WHERE COALESCE(ended, started) < ?", (cutoff,))
        conn.commit()
    return n


def load_turns(session_id: str, last: int = 1) -> list[dict]:
    """The session's last ``last`` turns that made tool calls, oldest first, with their calls."""
    with _lock:
        conn = _db()
        conn.row_factory = sqlite3.Row
        try:
            turn_ids = [r["turn_id"] for r in conn.execute(
                "SELECT turn_id, MIN(id) AS first FROM calls WHERE session_id=? GROUP BY turn_id"
                " ORDER BY first DESC LIMIT ?", (session_id, last))]
            turns = []
            for tid in reversed(turn_ids):
                meta = conn.execute("SELECT * FROM turns WHERE session_id=? AND turn_id=?",
                                    (session_id, tid)).fetchone()
                calls = [dict(r) for r in conn.execute(
                    "SELECT * FROM calls WHERE session_id=? AND turn_id IS ? ORDER BY id",
                    (session_id, tid))]
                turns.append({"turn_id": tid, "meta": dict(meta) if meta else {}, "calls": calls})
            return turns
        finally:
            conn.row_factory = None


# ---------------------------------------------------------------- workflows

def check_name(name: str) -> str:
    if not NAME_RE.fullmatch(name or ""):
        raise ValueError(f"invalid workflow name {name!r}: use lowercase letters, digits, '-' or '_'")
    return name


def _wf_dir() -> Path:
    d = data_dir() / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_workflow(wf: dict) -> Path:
    path = _wf_dir() / f"{check_name(wf['name'])}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(wf, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


def load_workflow(name: str) -> dict:
    path = _wf_dir() / f"{check_name(name)}.json"
    if not path.exists():
        raise FileNotFoundError(f"no frozen workflow named {name!r} (see: hermes freeze list)")
    return json.loads(path.read_text(encoding="utf-8"))


def list_workflows() -> list[dict]:
    out = []
    for p in sorted(_wf_dir().glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            out.append({"name": p.stem, "error": f"unreadable: {exc}"})
    return out


def delete_workflow(name: str) -> bool:
    path = _wf_dir() / f"{check_name(name)}.json"
    _written_path(name).unlink(missing_ok=True)
    if path.exists():
        path.unlink()
        return True
    return False


# Files a workflow's replays wrote, as {absolute path: sha256 of the content}. A replay only
# overwrites a file that still holds what freeze itself wrote last, so it never clobbers edits.
# While a write is in flight the value is a list [old, new], so a crash mid-run is recognised.

def _written_path(name: str) -> Path:
    d = data_dir() / "written"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{check_name(name)}.json"


def load_written(name: str) -> dict[str, str | list[str]]:
    path = _written_path(name)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def save_written(name: str, written: dict[str, str | list[str]]) -> None:
    path = _written_path(name)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(written, indent=2), encoding="utf-8")
    tmp.replace(path)


def append_run_log(name: str, entry: dict) -> None:
    log = data_dir() / "runs.jsonl"
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"workflow": name, **entry}, default=str) + "\n")
