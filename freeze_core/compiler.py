"""Compile ``compute`` steps: one LLM call at freeze time, zero at run time.

The model writes ``compute(scope)``; the result only counts if it reproduces the value the agent
produced in the recording. On a mismatch the model gets the difference and tries again.
"""
from __future__ import annotations

import ast
import json
import posixpath
import re
from typing import Any, Callable, Optional

from . import sandbox
from .analyze import leaves

MAX_ATTEMPTS = 3
_PREVIEW = 3500
_ITEM_PREVIEW = 1200

_SYSTEM = (
    "You turn one value that an AI agent produced during a recorded run into a deterministic "
    "Python function, so the run can be repeated later on fresh data without the AI.\n"
    "Rules:\n"
    "- Define exactly `def compute(scope):` and return the value. Nothing runs at import time.\n"
    "- Derive everything from `scope` so the function stays correct when the data changes. "
    "Never hardcode counts, totals, names or lines that come from tool results.\n"
    "- Allowed imports: " + ", ".join(sorted(sandbox.ALLOWED_MODULES)) + ". From "
    + "; ".join(f"{m} only {', '.join(names)}" for m, names in sorted(sandbox.MODULE_ALLOW.items()))
    + ". No files, network, os, sys, subprocess, eval, exec, open, getattr, "
    "operator.attrgetter/methodcaller or dunder attributes.\n"
    "- read_file results carry `content` with a `N|` line-number prefix on every line; strip it.\n"
    "- terminal results are dicts with `output` (stdout+stderr text) and `exit_code`.\n"
    "- Read the fields the value depends on directly (`row['amount']`), not with `.get()` "
    "defaults or try/except: if such a field is missing later, the function must fail so the "
    "replay stops instead of writing a wrong value. Use a fallback only for fields the data "
    "shown already leaves out in some items.\n"
    "- If the value cannot be derived from the data shown (the agent used information its tool "
    "calls never returned), do not guess: reply with one line `INSUFFICIENT: <what is missing>`.\n"
    "- Otherwise reply with one ```python code block and nothing else."
)


class Insufficient(RuntimeError):
    """The agent's value rests on data its tool calls never returned."""


def _preview(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > limit:
        return text[:limit] + f"... [{len(text) - limit} more chars]"
    return text


def describe_scope(steps: list[dict], scope: dict, upto: str) -> str:
    lines = []
    for step in steps:
        if step["id"] == upto:
            break
        sid = step["id"]
        if step["kind"] == "tool":
            lines.append(f'scope["{sid}"]: result of {step["tool"]}('
                         f'{_preview(step.get("recorded_args", step["args"]), 300)})')
            lines.append("    " + _preview(scope.get(sid), _PREVIEW))
        elif step["kind"] == "foreach":
            items = scope.get(sid) or []
            lines.append(f'scope["{sid}"]: list of {len(items)} results, one {step["tool"]} call per '
                         f'item of {step["over"]} (same order). Showing up to 6:')
            for i, item in enumerate(items[:6]):
                lines.append(f"    [{i}] " + _preview(item, _ITEM_PREVIEW))
        elif step["kind"] == "compute" and sid in scope:
            lines.append(f'scope["{sid}"]: computed value for {step["for"]}: '
                         + _preview(scope[sid], 600))
    return "\n".join(lines)


def _extract_code(text: str) -> str:
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text or "", re.S)
    return (m.group(1) if m else text or "").strip()


def _first_difference(got: Any, want: Any) -> str:
    if isinstance(got, str) and isinstance(want, str):
        for i, (a, b) in enumerate(zip(got, want)):
            if a != b:
                return (f"first difference at char {i}: got {got[max(0, i - 40):i + 40]!r}, "
                        f"expected {want[max(0, i - 40):i + 40]!r}")
        return f"lengths differ: got {len(got)} chars, expected {len(want)}"
    return f"got {_preview(got, 300)}, expected {_preview(want, 300)}"


def matches(got: Any, want: Any) -> bool:
    if isinstance(got, str) and isinstance(want, str):
        return got.strip() == want.strip()
    return got == want


def hardcoded(code: str, scope: dict, task: str) -> list[str]:
    """Literals in the code that are whole values from the tool results (names, paths, totals).

    Code that spells out the data reproduces the recording but goes stale on the next run.
    Fragments ("orders_", ",") and anything the user wrote in the task are fine.
    """
    data: set = set()
    for sid, val in scope.items():
        for _ref, leaf in leaves(val, sid):
            if isinstance(leaf, str):
                if len(leaf) <= 300:
                    data.add(leaf.strip())
                    data.add(posixpath.basename(leaf))
                data.update(ln.split("|", 1)[-1].strip() for ln in leaf.splitlines()[:5000])
            else:
                data.add(leaf)
    # Distinctive values (paths, ids, dates) also count when they sit inside a longer literal.
    distinct = [d for d in data if isinstance(d, str) and 6 <= len(d) <= 120 and " " not in d
                and re.search(r"[\d/.]", d) and d not in task][:5000]
    found = []
    for node in ast.walk(ast.parse(code)):
        if not isinstance(node, ast.Constant) or isinstance(node.value, bool):
            continue
        v = node.value
        if isinstance(v, str) and len(v) >= 5 and v not in task and (
                v in data or any(d in v for d in distinct)):
            found.append(repr(v))
        elif isinstance(v, (int, float)) and abs(v) >= 10 and v in data and str(v) not in task:
            found.append(repr(v))
    return sorted(set(found))


def compile_step(complete: Callable[[list[dict]], str], *, task: str, steps: list[dict],
                 scope: dict, step: dict, partial_walks: Optional[list[dict]] = None) -> dict:
    """Return {"code", "attempts", "reads"}, {"insufficient", "attempts"} or {"error", "attempts"}.

    ``reads`` lists the scope fields the accepted code reads (see ``sandbox.track_reads``), or
    None when tracking could not reproduce the value; replays then skip the field check.
    """
    view = {k: v for k, v in scope.items()}
    context = describe_scope(steps, view, step["id"])
    for walk in partial_walks or []:
        context += (f"\nNote: the agent called {walk['tool']} for only {len(walk['used'])} of the "
                    f"{walk['total']} items of {walk['over']}; it never retrieved data for "
                    + ", ".join(walk["missing"]) + ".")
    want = step["recorded"]
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": (
            f"The user's task was:\n{task}\n\n"
            f"Available data (what the earlier tool calls returned):\n{context}\n\n"
            f"The agent produced this value for {step['for']}:\n```\n{_preview(want, 6000)}\n```\n"
            "Write compute(scope) that produces it from the data.")},
    ]
    last_error: Optional[str] = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        reply = complete(messages)
        m = re.search(r"INSUFFICIENT\s*:\s*(.+)", reply or "")
        if m and "```" not in (reply or ""):
            return {"insufficient": m.group(1).strip()[:400], "attempts": attempt}
        code = _extract_code(reply)
        visible = _visible(scope, steps, step["id"])
        try:
            got = sandbox.run(code, visible)
            baked = hardcoded(code, visible, task)
            if baked:
                last_error = ("the code spells out values from the data (" + ", ".join(baked[:8])
                              + "); compute them from scope instead")
            elif matches(got, want):
                return {"code": code, "attempts": attempt, "reads": _reads(code, visible, got)}
            else:
                last_error = ("the result does not match the recorded value; "
                              + _first_difference(got, want))
        except (sandbox.UnsafeCode, sandbox.ComputeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        messages += [
            {"role": "assistant", "content": reply},
            {"role": "user", "content": f"That attempt failed: {last_error}\nFix it and reply with "
                                        "the full corrected code block."},
        ]
    return {"error": last_error or "no attempt succeeded", "attempts": MAX_ATTEMPTS}


def _reads(code: str, visible: dict, got: Any) -> Optional[list[dict]]:
    """Fields the accepted code reads, or None if the tracked run behaves differently.

    Tracking wraps the data in dict/list/str subclasses; code that reacts to that is rare, and
    then the workflow simply has no field check rather than a wrong one.
    """
    try:
        value, reads = sandbox.track_reads(code, visible)
    except (sandbox.UnsafeCode, sandbox.ComputeError):
        return None
    return reads if value == got else None


def _visible(scope: dict, steps: list[dict], upto: str) -> dict:
    """Only what exists before the step runs, so code cannot lean on later results."""
    seen = {}
    for step in steps:
        if step["id"] == upto:
            break
        if step["id"] in scope:
            seen[step["id"]] = scope[step["id"]]
    return seen
