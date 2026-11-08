"""Replay a frozen workflow through Hermes' own tools, without the model.

The run stops at the first sign that the world no longer matches the recording ("drift"): a tool
error, a non-zero exit where the recording had zero, a ref that no longer resolves, a field the
compiled code reads that is missing or changed type, a compute step that fails, or a value that is
unsafe to splice. Steps after the drift never run, so a stale
workflow cannot write half a result.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import refs as R
from . import sandbox
from .analyze import SAFE_TOKEN

READ_ONLY_TOOLS = frozenset({"read_file", "search_files", "web_search", "web_extract"})
WRITE_PATH_TOOLS = frozenset({"write_file", "patch"})


class Drift(RuntimeError):
    def __init__(self, step_id: str, reason: str):
        super().__init__(f"{step_id}: {reason}")
        self.step_id = step_id
        self.reason = reason


class _Skip(Exception):
    """Dry run only: the step needs a result from a step the dry run did not execute."""


def _command_check(ref: str, value: Any) -> None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return
    if not isinstance(value, str) or not SAFE_TOKEN.match(value):
        raise R.RefError(f"{ref} = {str(value)[:80]!r} is not a plain token; refusing to put it "
                         "into a shell command")


def _tool_failed(result: Any) -> Optional[str]:
    if isinstance(result, dict):
        err = result.get("error")
        if err:
            return str(err)[:300]
        if result.get("success") is False:
            return str(result.get("message") or "tool reported success=false")[:300]
    return None


def _inside(path: str, root: str) -> bool:
    p = Path(path)
    if not p.is_absolute():
        p = Path(root) / p
    try:
        p.resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hashes(entry: Any) -> set[str]:
    """A written-map entry: one hash, or several while a write is in flight (old and new)."""
    if isinstance(entry, str):
        return {entry}
    if isinstance(entry, list):
        return {h for h in entry if isinstance(h, str)}
    return set()


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object" if isinstance(value, dict) else "null"


def _fmt_path(path: list) -> str:
    out = ""
    for part in path:
        if isinstance(part, str):
            out += f".{part}" if out else part
        elif part == sandbox.LIST:
            out += "[]"
        elif part == sandbox.ANY:
            out += "[*]"
    return out


def _read_problem(scope: dict, read: dict, parsed: dict, optional: set[str]) -> Optional[str]:
    """Why a field the compiled code read at freeze time is no longer usable, or None.

    A key under a list counts as present if any item still has it, so optional per-item fields
    do not stop a run; a renamed field is missing from every item and does. Nulls and empty
    lists are left to the code itself.
    """
    path = read.get("path") or []
    nodes: list = [scope]
    for i, part in enumerate(path):
        nodes = [n for n in nodes if n is not None]
        if not nodes:
            return None
        if isinstance(part, str):
            nxt = [n[part] for n in nodes if isinstance(n, dict) and part in n]
            if not nxt:
                if json.dumps(path[:i + 1]) in optional:
                    return None  # already missing somewhere when frozen
                return f"{_fmt_path(path[:i + 1])} is missing"
        elif part == sandbox.LIST:
            if any(not isinstance(n, list) for n in nodes):
                return f"{_fmt_path(path[:i])} is no longer a list"
            nxt = [v for n in nodes for v in n]
        elif part == sandbox.ANY:
            if any(not isinstance(n, dict) for n in nodes):
                return f"{_fmt_path(path[:i])} is no longer an object"
            nxt = [v for n in nodes for v in n.values()]
        elif part == sandbox.JSON:
            nxt = []
            for n in nodes:
                if not isinstance(n, str):
                    nxt.append(n)
                    continue
                if id(n) not in parsed:
                    try:
                        parsed[id(n)] = json.loads(n)
                    except ValueError:
                        return f"{_fmt_path(path[:i])} is no longer JSON"
                nxt.append(parsed[id(n)])
        else:
            return None
        nodes = nxt
    was = set(read.get("types") or ()) - {"null"}
    now = {_kind(n) for n in nodes if n is not None}
    if was and now - was:
        return f"{_fmt_path(path)} is now {'/'.join(sorted(now))} (was {'/'.join(sorted(was))})"
    return None


def _short(args: dict) -> str:
    return ", ".join(f"{k}={str(v)[:60]!r}" for k, v in args.items())[:200]


class _Runner:
    def __init__(self, wf: dict, dispatch: Callable[[str, dict], str], dry_run: bool,
                 log: Callable[[str], None], written: dict[str, Any],
                 persist: Optional[Callable[[dict], None]]):
        self.wf, self.dispatch, self.dry_run, self.log = wf, dispatch, dry_run, log
        self.persist = persist
        self.scope: dict[str, Any] = {}
        self.cwd = wf.get("cwd") or os.getcwd()
        self.tool_calls = 0
        self.skipped_any = False
        self.written = dict(written)
        # Fields the compiled code read at freeze time, grouped by the tool step they come from,
        # so each step's result is checked as soon as it arrives, before anything later runs.
        roots = {s["id"] for s in wf["steps"] if s["kind"] in ("tool", "foreach")}
        reads: dict[str, dict[str, dict]] = {}
        for s in wf["steps"]:
            for read in s.get("reads") or ():
                path = read.get("path") or []
                if path and path[0] in roots:
                    reads.setdefault(path[0], {})[json.dumps(path)] = read
        self.reads = {sid: sorted(r.values(), key=lambda x: len(x["path"]))
                      for sid, r in reads.items()}
        self.optional = {key for r in reads.values() for key, read in r.items()
                         if read.get("optional")}

    def _abs(self, path: str) -> str:
        p = Path(path).expanduser()
        return str((p if p.is_absolute() else Path(self.cwd) / p).resolve())

    def _guard_overwrite(self, step: dict, args: dict) -> None:
        """Overwrite an existing file only if it still holds what freeze wrote there.

        "What freeze wrote" is the content the recording wrote, the last replay's, a write that
        was in flight when an earlier run died, or exactly the content this step is about to
        write (rewriting it loses nothing). Anything else means a person or another program
        changed the file, so the run stops instead of clobbering it. When the file is ours, it is read in full through Hermes' read_file, so
        Hermes' own stale-write guard sees the content this run replaces.
        """
        path = self._abs(args["path"])
        if not os.path.isfile(path):
            return
        try:
            current = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise Drift(step["id"], f"cannot check {path} before overwriting it: {exc}") from None
        recorded = (step.get("recorded_args") or {}).get("content")
        ours = _hashes(self.written.get(path))
        for content in (recorded, args.get("content")):
            if isinstance(content, str):
                ours.add(_sha(content))
        if _sha(current) not in ours:
            raise Drift(step["id"], f"{path} changed since freeze last wrote it; not overwriting it")
        if self.dry_run:
            return
        offset = 1
        while True:
            page = R.as_data(self.dispatch("read_file", {"path": path, "offset": offset}))
            self.tool_calls += 1
            failed = _tool_failed(page)
            if failed:
                raise Drift(step["id"], f"read_file before overwriting {path} failed: {failed}")
            nxt = page.get("next_offset") if isinstance(page, dict) else None
            if not isinstance(nxt, int) or nxt <= offset:
                return
            offset = nxt

    def _mark_pending(self, step: dict, args: dict) -> None:
        """Save the new content's hash next to the old one before the write happens.

        The map is otherwise saved only when the run ends. If the process dies between the
        write and that save, the next run would not recognise its own output and would stop.
        """
        if self.persist is None or not isinstance(args.get("path"), str) \
                or not isinstance(args.get("content"), str):
            return
        path = self._abs(args["path"])
        self.written[path] = sorted(_hashes(self.written.get(path)) | {_sha(args["content"])})
        try:
            self.persist(dict(self.written))
        except OSError as exc:
            raise Drift(step["id"], f"cannot save replay state before writing {path}: {exc}") \
                from None

    def _check_reads(self, sid: str) -> None:
        parsed: dict = {}
        for read in self.reads.get(sid, ()):
            problem = _read_problem(self.scope, read, parsed, self.optional)
            if problem:
                raise Drift(sid, f"{problem}; the frozen workflow reads it, so the data no "
                                 "longer has the shape it was frozen on")

    def render(self, step: dict, extra: Optional[dict] = None) -> dict:
        check = _command_check if step["tool"] == "terminal" else None
        try:
            return R.render(step["args"], {**self.scope, **(extra or {})}, check)
        except R.RefError as exc:
            if self.dry_run and self.skipped_any:
                raise _Skip() from None
            raise Drift(step["id"], str(exc)) from None

    def call(self, step: dict, args: dict) -> Any:
        tool = step["tool"]
        if tool in WRITE_PATH_TOOLS and isinstance(args.get("path"), str):
            recorded = (step.get("recorded_args") or {}).get("path")
            if args["path"] != recorded and not _inside(args["path"], self.cwd):
                raise Drift(step["id"], f"{tool} path {args['path']!r} is outside {self.cwd}")
        if tool == "write_file" and isinstance(args.get("path"), str):
            self._guard_overwrite(step, args)
        if self.dry_run and tool not in READ_ONLY_TOOLS:
            self.log(f"    would run {tool}({_short(args)})")
            self.skipped_any = True
            return None
        if tool == "write_file":
            self._mark_pending(step, args)
        result = R.as_data(self.dispatch(tool, args))
        self.tool_calls += 1
        failed = _tool_failed(result)
        if failed:
            raise Drift(step["id"], f"{tool} failed: {failed}")
        rec_exit = step.get("exit_code")
        if isinstance(result, dict) and rec_exit is not None and "exit_code" in result:
            if (rec_exit == 0) != (result["exit_code"] == 0):
                raise Drift(step["id"], f"{tool} exit code {result['exit_code']} "
                                        f"(recording: {rec_exit})")
        if tool == "write_file" and isinstance(args.get("path"), str) \
                and isinstance(args.get("content"), str):
            self.written[self._abs(args["path"])] = _sha(args["content"])
        return result

    def step(self, step: dict) -> None:
        sid, kind = step["id"], step["kind"]
        if kind == "tool":
            self.log(f"• {sid} {step['tool']}")
            self.scope[sid] = self.call(step, self.render(step))
            self._check_reads(sid)
        elif kind == "foreach":
            try:
                items = R.resolve(self.scope, step["over"])
            except R.RefError as exc:
                if self.dry_run and self.skipped_any:
                    raise _Skip() from None
                raise Drift(sid, str(exc)) from None
            if not isinstance(items, list):
                raise Drift(sid, f"{step['over']} is no longer a list")
            if not items and step.get("recorded_count"):
                # An empty input usually means a moved folder or a failed search, not "no work";
                # stopping here keeps the last good output instead of overwriting it with nothing.
                raise Drift(sid, f"{step['over']} is empty now (recording had "
                                 f"{step['recorded_count']} items)")
            self.log(f"• {sid} {step['tool']} × {len(items)} "
                     f"(recording had {step.get('recorded_count')})")
            self.scope[sid] = [self.call(step, self.render(step, {"item": it})) for it in items]
            self._check_reads(sid)
        elif kind == "compute":
            if self.dry_run and self.skipped_any:
                raise _Skip()
            self.log(f"• {sid} compute {step['for']}")
            try:
                self.scope[sid] = sandbox.run(step["code"], dict(self.scope))
            except (sandbox.UnsafeCode, sandbox.ComputeError) as exc:
                raise Drift(sid, f"compute failed: {exc}") from None
        elif kind == "const":
            self.scope[sid] = step["value"]
        else:
            raise Drift(sid, f"unknown step kind {kind!r}")


def run(wf: dict, dispatch: Callable[[str, dict], str], *, dry_run: bool = False,
        log: Callable[[str], None] = print, written: Optional[dict[str, Any]] = None,
        persist: Optional[Callable[[dict], None]] = None) -> dict:
    """Execute ``wf``. ``dispatch(tool, args)`` returns the tool's JSON string result.

    ``written`` maps files earlier replays wrote to their content hash; the report's
    ``written`` is the updated map, including writes made before a drift. ``persist(map)``,
    if given, is called before each file write so an interrupted run leaves a usable map.
    """
    runner = _Runner(wf, dispatch, dry_run, log, written or {}, None if dry_run else persist)
    started = time.time()
    report: dict[str, Any] = {"workflow": wf.get("name"), "ok": False, "steps": 0,
                              "llm_calls": 0, "dry_run": dry_run}
    try:
        for step in wf["steps"]:
            report["steps"] += 1
            try:
                runner.step(step)
            except _Skip:
                runner.log(f"• {step['id']} {step.get('tool', step['kind'])} "
                           "(needs a step the dry run did not execute)")
                runner.scope[step["id"]] = None
        report["ok"] = True
        report["answer"] = runner.scope.get("answer")
    except Drift as d:
        report["drift"] = {"step": d.step_id, "reason": d.reason}
    report["tool_calls"] = runner.tool_calls
    report["seconds"] = round(time.time() - started, 2)
    report["written"] = runner.written
    return report
