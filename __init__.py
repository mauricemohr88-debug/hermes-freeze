"""freeze: let Hermes do a task once, then replay it without the model.

Hooks record each turn's tool calls locally. ``/freeze <name>`` turns the last turn into a
workflow: data flow between tool calls becomes bindings, list walks become loops, and values the
model worked out in its head are compiled once into checked Python. ``hermes freeze run <name>``
replays it through Hermes' own tools with zero model calls and stops on drift.
"""
from __future__ import annotations

import logging
import shlex
import subprocess

from .freeze_core import cli, freezer, store

logger = logging.getLogger(__name__)

# Hermes before 0.21.4 calls slash handlers without session_id. A CLI process hosts one session,
# so the session that last recorded something in this process is the caller's.
_last_session = {"id": ""}

_USAGE = (
    "Usage: /freeze <name> [--turns N]   freeze the last task (or last N turns) of this chat\n"
    "       /freeze list | show <name> | run <name> | delete <name>"
)


def _cwd() -> str:
    try:
        from agent.runtime_cwd import resolve_agent_cwd
        return str(resolve_agent_cwd())
    except Exception as exc:  # cwd is metadata; a missing value must not lose the recording
        logger.warning("freeze: could not resolve the session folder: %s", exc)
        return ""


def _usage_tokens(usage) -> tuple[int, int]:
    if usage is None:
        return 0, 0
    get = usage.get if isinstance(usage, dict) else (lambda k: getattr(usage, k, None))
    # prompt_tokens includes cache reads/writes; input_tokens alone undercounts cached turns.
    inp = get("prompt_tokens") or get("input_tokens") or 0
    out = get("output_tokens") or get("completion_tokens") or 0
    return int(inp or 0), int(out or 0)


def register(ctx) -> None:
    def on_turn_start(session_id=None, turn_id=None, user_message=None, **_kw):
        if not session_id or not turn_id:
            return None
        _last_session["id"] = session_id
        try:
            store.record_turn_start(session_id=session_id, turn_id=turn_id,
                                    user_message=user_message if isinstance(user_message, str)
                                    else str(user_message or ""))
        except Exception as exc:  # recording must never break the user's turn
            logger.warning("freeze: could not record turn start: %s", exc)
        return None

    def on_tool(tool_name=None, args=None, result=None, session_id=None, turn_id=None,
                tool_call_id=None, duration_ms=0, status=None, **_kw):
        if not session_id or not tool_name:
            return
        _last_session["id"] = session_id
        try:
            store.record_call(session_id=session_id, turn_id=turn_id or "", tool_call_id=tool_call_id or "",
                              tool_name=tool_name, args=args or {}, result=result, status=status or "",
                              duration_ms=duration_ms or 0, cwd=_cwd())
        except Exception as exc:
            logger.warning("freeze: could not record %s call: %s", tool_name, exc)

    def on_api(session_id=None, turn_id=None, usage=None, **_kw):
        if not session_id or not turn_id:
            return
        try:
            inp, out = _usage_tokens(usage)
            store.record_usage(session_id=session_id, turn_id=turn_id, input_tokens=inp,
                               output_tokens=out)
        except Exception as exc:
            logger.warning("freeze: could not record usage: %s", exc)

    def on_turn_end(session_id=None, turn_id=None, assistant_response=None, **_kw):
        if not session_id or not turn_id:
            return
        try:
            store.record_turn_end(session_id=session_id, turn_id=turn_id,
                                  answer=assistant_response if isinstance(assistant_response, str) else "")
        except Exception as exc:
            logger.warning("freeze: could not record turn end: %s", exc)

    ctx.register_hook("pre_llm_call", on_turn_start)
    ctx.register_hook("post_tool_call", on_tool)
    ctx.register_hook("post_api_request", on_api)
    ctx.register_hook("post_llm_call", on_turn_end)

    def complete(messages):
        result = ctx.llm.complete(messages=messages, max_tokens=2500, temperature=0,
                                  purpose="freeze.compile")
        return result.text

    def slash(raw_args: str, session_id: str = "", **_kw) -> str:
        try:
            parts = shlex.split(raw_args or "")
        except ValueError as exc:
            return f"freeze: {exc}\n{_USAGE}"
        if not parts or parts[0] in ("help", "-h", "--help"):
            return _USAGE
        sub = parts[0]
        try:
            if sub == "list":
                wfs = store.list_workflows()
                return "\n".join(f"{w['name']}: {len(w.get('steps', []))} steps" for w in wfs) or \
                    "No frozen workflows yet."
            if sub == "show" and len(parts) == 2:
                wf = store.load_workflow(parts[1])
                return f"{wf['name']}: {wf.get('task')}\n{freezer.describe(wf, code=True)}"
            if sub == "delete" and len(parts) == 2:
                return f"Deleted {parts[1]}." if store.delete_workflow(parts[1]) else "Not found."
            if sub == "run" and len(parts) == 2:
                # Replay in its own process: it changes into the workflow's folder, which must
                # never happen inside the gateway that hosts every session.
                proc = subprocess.run(cli._hermes_argv() + ["freeze", "run", store.check_name(parts[1])],
                                      stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                      timeout=600)
                return (proc.stdout + proc.stderr).strip()[-3500:]
            if sub in freezer.RESERVED:
                return _USAGE
            turns = 1
            if len(parts) == 3 and parts[1] == "--turns" and parts[2].isdigit():
                turns = max(1, min(int(parts[2]), 20))
            elif len(parts) != 1:
                return _USAGE
            wf = freezer.freeze(sub, session_id or _last_session["id"], complete, turns=turns)
            return freezer.summary(wf)
        except (freezer.FreezeError, ValueError, FileNotFoundError) as exc:
            return f"freeze: {exc}"
        except subprocess.TimeoutExpired:
            return "freeze: the replay took longer than 10 minutes and was stopped."

    ctx.register_command("freeze", slash, description="Freeze the last task into a replay that needs no model",
                         args_hint="<name>")
    ctx.register_cli_command("freeze", "Run, list and schedule frozen workflows", cli.setup,
                             cli.make_handler(ctx.dispatch_tool))

    try:
        store.prune()
    except Exception as exc:
        logger.warning("freeze: could not prune old recordings: %s", exc)
