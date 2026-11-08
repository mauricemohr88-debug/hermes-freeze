"""/freeze: recorded turn -> analysed, compiled, verified workflow on disk."""
from __future__ import annotations

import os
import time
from typing import Callable

from . import analyze, compiler, store

RESERVED = frozenset({"list", "show", "run", "delete", "schedule", "help"})


class FreezeError(RuntimeError):
    pass


def freeze(name: str, session_id: str, complete: Callable[[list[dict]], str], *,
           turns: int = 1) -> dict:
    store.check_name(name)
    if name in RESERVED:
        raise FreezeError(f"{name!r} is a subcommand; pick another workflow name")
    if not session_id:
        raise FreezeError("this chat has no session yet; run the task first")
    recorded = store.load_turns(session_id, turns)
    calls = [c for t in recorded for c in t["calls"]]
    if not calls:
        raise FreezeError("no tool calls recorded in this session yet. Let Hermes do the task "
                          "once, then run /freeze <name>.")
    task = "\n\n".join((t["meta"].get("user_message") or "").strip() for t in recorded).strip()
    answer = recorded[-1]["meta"].get("answer")
    cwd = next((c["cwd"] for c in calls if c.get("cwd")), os.getcwd())

    draft = analyze.build(task, calls, answer, cwd)
    scope = draft.pop("_scope")
    steps = draft["steps"]
    if not any(s["kind"] in ("tool", "foreach") for s in steps):
        raise FreezeError("nothing in this turn can be replayed without the model "
                          f"(skipped: {', '.join(draft['skipped']) or 'none'})")

    notes = []
    for step in list(steps):
        if step["kind"] != "compute":
            continue
        try:
            res = compiler.compile_step(complete, task=task, steps=steps, scope=scope, step=step,
                                        partial_walks=draft["partial_walks"])
        except Exception as exc:  # provider errors come in many types; all mean "try again later"
            raise FreezeError(f"the model call failed while compiling {step['for']} "
                              f"({type(exc).__name__}: {str(exc)[:200]}). Nothing was saved; "
                              "run /freeze again.") from exc
        if "code" in res:
            step["code"], step["attempts"] = res["code"], res["attempts"]
            step["reads"] = res.get("reads")
            scope[step["id"]] = step["recorded"]
            continue
        problem = (res.get("insufficient") or res.get("error") or "").rstrip(". ")
        if step["id"] == "answer":
            steps.remove(step)
            notes.append(f"the final answer could not be compiled ({problem}); "
                         "runs print a step summary instead")
        elif "insufficient" in res:
            raise FreezeError(
                f"the agent's {step['for']} uses data its tool calls never returned: {problem}. "
                + _partial_hint(draft["partial_walks"])
                + "Freeze will not guess. Ask Hermes to redo the task and check every item, "
                  "then /freeze again. Nothing was saved.")
        else:
            raise FreezeError(f"could not turn {step['for']} into code after {res['attempts']} "
                              f"attempts: {problem}. Nothing was saved.")

    metas = [t["meta"] for t in recorded]
    started = min((m.get("started") or 0) for m in metas) or None
    ended = max((m.get("ended") or 0) for m in metas) or None
    wf = {
        "name": name,
        "format": 1,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": {"session_id": session_id, "turn_ids": [t["turn_id"] for t in recorded]},
        "task": task,
        "cwd": cwd,
        "steps": steps,
        "skipped": draft["skipped"],
        "notes": notes,
        "recorded_run": {
            "seconds": round(ended - started, 1) if started and ended else None,
            "model_calls": sum(m.get("api_calls") or 0 for m in metas),
            "input_tokens": sum(m.get("input_tokens") or 0 for m in metas),
            "output_tokens": sum(m.get("output_tokens") or 0 for m in metas),
            "tool_calls": len(calls),
        },
    }
    store.save_workflow(wf)
    return wf


def _partial_hint(walks: list[dict]) -> str:
    return "".join(f"It ran {w['tool']} on {len(w['used'])} of {w['total']} items of {w['over']} "
                   f"(skipped: {', '.join(w['missing'][:5])}). " for w in walks)


def flagged_steps(wf: dict) -> list[tuple[str, str]]:
    """``(step id, reason)`` for steps whose command Hermes' approval gate flagged."""
    return [(s["id"], s["flagged"]) for s in wf["steps"] if s.get("flagged")]


def approval_note(wf: dict) -> str:
    flagged = flagged_steps(wf)
    if not flagged:
        return ""
    ids = ", ".join(i for i, _ in flagged)
    reasons = "; ".join(sorted({r for _, r in flagged}))
    return (f"Needs approval: {ids} ({reasons}). Hermes asks you when you run it in a terminal; "
            "unattended runs are blocked unless approvals.cron_mode is approve. To get a workflow "
            "that runs unattended, ask Hermes to use its file tools instead of inline scripts, "
            "then /freeze again.")


def describe(wf: dict, *, code: bool = False) -> str:
    lines = []
    for s in wf["steps"]:
        mark = "  [needs approval]" if s.get("flagged") else ""
        if s["kind"] == "tool":
            lines.append(f"  {s['id']:<7} {s['tool']}({_args(s['args'])}){mark}")
        elif s["kind"] == "foreach":
            lines.append(f"  {s['id']:<7} {s['tool']}({_args(s['args'])})  for each item of "
                         f"{s['over']}  [was {s.get('recorded_count')} calls]{mark}")
        elif s["kind"] == "compute":
            tries = s.get("attempts")
            lines.append(f"  {s['id']:<7} compiled code for {s['for']}  "
                         f"[matches the recording{f', attempt {tries}' if tries else ''}]")
            if code and s.get("code"):
                lines += ["          " + ln for ln in s["code"].splitlines()]
        elif s["kind"] == "const":
            lines.append(f"  {s['id']:<7} fixed text for the final answer")
    return "\n".join(lines)


def _args(args: dict) -> str:
    parts = []
    for k, v in args.items():
        text = v if isinstance(v, str) else repr(v)
        text = text.replace("\n", "\\n")
        parts.append(f"{k}={text[:48] + '…' if len(text) > 48 else text}")
    return ", ".join(parts)


def summary(wf: dict) -> str:
    rec = wf.get("recorded_run") or {}
    tokens = (rec.get("input_tokens") or 0) + (rec.get("output_tokens") or 0)
    head = (f'Frozen "{wf["name"]}": {len(wf["steps"])} steps, replays with 0 model calls.')
    out = [head, describe(wf)]
    if wf.get("skipped"):
        out.append("Left out: " + "; ".join(wf["skipped"]))
    for note in wf.get("notes") or []:
        out.append("Note: " + note)
    if rec:
        secs = f"{rec['seconds']} s, " if rec.get("seconds") else ""
        out.append(f"Recorded run: {secs}{rec.get('model_calls', 0)} model calls, "
                   f"{tokens:,} tokens, {rec.get('tool_calls', 0)} tool calls.")
    out.append(f"Run it:       hermes freeze run {wf['name']}")
    note = approval_note(wf)
    if note:
        out.append(note)
    else:
        out.append(f'Schedule it:  hermes freeze schedule {wf["name"]} "0 9 * * *"')
    return "\n".join(out)
