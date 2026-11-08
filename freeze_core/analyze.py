"""Turn a recorded agent turn into a draft workflow.

Every argument leaf of every recorded tool call is classified by where its value came from:

* const     - the user asked for it, or the model chose it and nothing in the data explains it
* bound     - it equals a value in an earlier tool result      -> ``{{s1.files[0]}}``
* template  - earlier result values spliced into fixed text     -> ``cat {{s1.files[0]}}``
* derived   - it carries data that appears nowhere as-is: the model computed it in its head.
              These become ``compute`` steps, compiled to Python later and checked against
              the recorded value.

Consecutive calls of one tool that walk a whole list (``files[0]`` .. ``files[n-1]``) become one
``foreach`` step, so the workflow keeps working when the list grows.
"""
from __future__ import annotations

import json
import posixpath
import re
from typing import Any, Iterable, Optional

from . import refs as R

# Tools that need the model, a human, or change agent state: replaying them makes no sense.
NOT_REPLAYABLE = frozenset({
    "clarify", "todo", "memory", "delegate_task", "session_search", "skill_view", "skills_list",
    "skill_manage", "vision_analyze", "mixture_of_agents", "text_to_speech", "image_generate",
    "cronjob_manage", "send_message", "tool_search", "tool_describe",
})
# Argument leaves that hold code: a model-chosen literal, never data to bind or compute.
CODE_ARGS = {("terminal", "command"), ("execute_code", "code")}
# Values spliced into shell commands must be plain tokens (no spaces, quotes or shell syntax).
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_@%+=:,./~][A-Za-z0-9_@%+=:,./~-]*$")

_TOKEN_RE = re.compile(r"[A-Za-z0-9][\w.-]*")
_MARK = "{}"
_MARK_RE = re.compile("(\\d+)")
_STOPWORDS = frozenset("""
the and for with from this that into each file files total count line lines name none true
false null data json text item items result results value values output input path paths
""".split())


def tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text or "")}


def leaves(obj: Any, ref: str) -> Iterable[tuple[str, Any]]:
    """(ref, scalar) for every scalar inside a parsed result."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and re.fullmatch(r"[A-Za-z_][\w-]*", k):
                yield from leaves(v, f"{ref}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from leaves(v, f"{ref}[{i}]")
    elif isinstance(obj, (str, int, float)) and not isinstance(obj, bool):
        yield ref, obj


def _arg_leaves(obj: Any, path: tuple = ()) -> Iterable[tuple[tuple, Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _arg_leaves(v, path + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _arg_leaves(v, path + (i,))
    else:
        yield path, obj


def _set_path(obj: Any, path: tuple, value: Any) -> Any:
    if not path:
        return value
    obj[path[0]] = _set_path(obj[path[0]], path[1:], value)
    return obj


class Context:
    """What is known before a step runs: the task text and every earlier result value."""

    def __init__(self, task: str):
        self.task = task or ""
        self.task_tokens = tokens(self.task)
        self.exact: dict[Any, str] = {}           # value -> most recent ref
        self.splice: dict[str, str] = {}          # text  -> most recent ref (for templates)
        self.result_tokens: set[str] = set()

    def add_result(self, ref: str, data: Any) -> None:
        for leaf_ref, val in leaves(data, ref):
            key = val if isinstance(val, str) else ("#num", val)
            self.exact[key] = leaf_ref
            if isinstance(val, str):
                if len(val) <= 2000:
                    self.result_tokens |= tokens(val)
                if self._spliceable(val):
                    self.splice[val] = leaf_ref
                if "/" in val and "\n" not in val:
                    base = posixpath.basename(val)
                    if base != val and self._spliceable(base):
                        self.splice.setdefault(base, f"basename({leaf_ref})")
            elif isinstance(val, int) and abs(val) >= 1000 and str(val) not in self.task_tokens:
                self.splice[str(val)] = f"str({leaf_ref})"
        if isinstance(data, str):
            self.result_tokens |= tokens(data[:20000])

    def _spliceable(self, text: str) -> bool:
        # Plain words ("refunded") are too ambiguous to splice; paths, ids and urls are not.
        return (4 <= len(text) <= 500 and "\n" not in text and not text.isalpha()
                and text not in self.task)


def classify_leaf(value: Any, ctx: Context, *, is_code: bool) -> tuple[str, Any]:
    """Return (kind, template) for one argument scalar."""
    if isinstance(value, bool) or value is None:
        return "const", value
    if isinstance(value, (int, float)):
        ref = ctx.exact.get(("#num", value))
        if ref and abs(value) >= 100 and str(value) not in ctx.task_tokens:
            return "bound", "{{" + ref + "}}"
        return "const", value
    if not isinstance(value, str):
        return "const", R.literal(value)
    if len(value) < 3:
        # "5" can be a total the model added up; short values without digits are flags and modes.
        if not is_code and _carries_data(value, ctx):
            return "derived", None
        return "const", R.literal(value)
    if value in ctx.task:
        return "const", R.literal(value)
    ref = ctx.exact.get(value)
    if ref and not is_code:
        return "bound", "{{" + ref + "}}"
    if ref and is_code and SAFE_TOKEN.match(value):
        return "bound", "{{" + ref + "}}"

    # Template: splice earlier values (longest first) and look at the literal text left over.
    rest, used = value, []
    for text in sorted(ctx.splice, key=len, reverse=True):
        if text in rest:
            if is_code and not SAFE_TOKEN.match(text):
                continue
            rest = rest.replace(text, _MARK.format(len(used)))
            used.append(ctx.splice[text])
    leftover = _MARK_RE.sub(" ", rest)
    if not is_code and _carries_data(leftover, ctx):
        return "derived", None
    if not used or "{{" in leftover:
        return "const", R.literal(value)
    return "template", _MARK_RE.sub(lambda m: "{{" + used[int(m.group(1))] + "}}", rest)


def _carries_data(text: str, ctx: Context) -> bool:
    for tok in tokens(text):
        if tok in ctx.task_tokens or tok in _STOPWORDS:
            continue
        if any(c.isdigit() for c in tok):
            return True                      # a number the model worked out
        if len(tok) >= 4 and tok in ctx.result_tokens:
            return True                      # a word copied out of the data
    return False


def build(task: str, calls: list[dict], answer: Optional[str], cwd: str) -> dict:
    """Draft workflow from recorded calls (dicts with tool_name, args, result, status)."""
    ctx = Context(task)
    steps: list[dict] = []
    skipped: list[str] = []
    scope: dict[str, Any] = {}
    n_compute = 0
    for idx, call in enumerate(calls, 1):
        tool = call["tool_name"]
        if tool in NOT_REPLAYABLE:
            skipped.append(f"{tool} (needs the model or a person)")
            continue
        if call.get("status") == "error":
            skipped.append(f"{tool} (failed in the recording)")
            continue
        args = json.loads(call["args"]) if isinstance(call["args"], str) else call["args"]
        sid = f"s{idx}"
        tmpl = json.loads(json.dumps(args))
        kinds: dict[str, str] = {}
        for path, val in _arg_leaves(args):
            is_code = (tool, path[0] if path else "") in CODE_ARGS
            kind, t = classify_leaf(val, ctx, is_code=is_code)
            if kind == "derived":
                n_compute += 1
                cid = f"c{n_compute}"
                steps.append({"id": cid, "kind": "compute", "for": f"{tool}.{_fmt(path)}",
                              "recorded": val, "code": None})
                t = "{{" + cid + "}}"
            tmpl = _set_path(tmpl, path, t)
            kinds[_fmt(path)] = kind
        result = R.as_data(call.get("result"))
        steps.append({"id": sid, "kind": "tool", "tool": tool, "args": tmpl, "kinds": kinds,
                      "recorded_args": args, "shape": _shape(result),
                      "exit_code": result.get("exit_code") if isinstance(result, dict) else None,
                      "flagged": _flagged(result)})
        scope[sid] = result
        ctx.add_result(sid, result)

    steps, scope, partial = merge_loops(steps, scope)
    if answer and answer.strip():
        if _carries_data(answer, ctx) or any(t.isdigit() for t in tokens(answer)):
            steps.append({"id": "answer", "kind": "compute", "for": "final answer",
                          "recorded": answer, "code": None})
        else:
            steps.append({"id": "answer", "kind": "const", "value": answer})
    return {"task": task, "cwd": cwd, "steps": steps, "skipped": skipped,
            "partial_walks": partial, "_scope": scope}


def _fmt(path: tuple) -> str:
    return "".join(f"[{p}]" if isinstance(p, int) else (f".{p}" if i else str(p))
                   for i, p in enumerate(path))


def _shape(result: Any) -> Any:
    if isinstance(result, dict):
        return sorted(k for k in result if k not in ("error", "approval"))
    return type(result).__name__


_FLAGGED_RE = re.compile(r"flagged \(([^)]+)\)")


def _flagged(result: Any) -> Optional[str]:
    """Why Hermes' approval gate flagged this call in the recording, if it did.

    Hermes puts an ``approval`` note on a terminal result whose command needed approval (by the
    user or by smart mode). Replays meet the same gate, so the workflow remembers it.
    """
    note = result.get("approval") if isinstance(result, dict) else None
    if not isinstance(note, str) or not note.strip():
        return None
    m = _FLAGGED_RE.search(note)
    return (m.group(1) if m else note.strip())[:100]


# ---------------------------------------------------------------- loops

_IDX_RE = re.compile(r"^(?P<fn>basename\(|str\()?(?P<list>s\d+(?:\.[A-Za-z_][\w-]*|\[\d+\])*?)"
                     r"\[(?P<i>\d+)\](?P<rest>(?:\.[A-Za-z_][\w-]*|\[\d+\])*)\)?$")


def _loop_key(step: dict, scope: dict) -> Optional[tuple]:
    """(signature, list ref, index) when every indexed ref in the step points into one list."""
    if step["kind"] != "tool":
        return None
    found: set[tuple[str, int]] = set()

    def sig(ref: str) -> str:
        m = _IDX_RE.match(ref)
        if not m:
            return ref
        try:
            lst = R.resolve(scope, m.group("list"))
        except R.RefError:
            return ref
        if not isinstance(lst, list):
            return ref
        found.add((m.group("list"), int(m.group("i"))))
        return f"{m.group('fn') or ''}item{m.group('rest')}{')' if m.group('fn') else ''}"

    signature = json.dumps({"tool": step["tool"], "args": R.map_refs(step["args"], sig)},
                           sort_keys=True)
    if len(found) != 1:
        return None
    (list_ref, i), = found
    return signature, list_ref, i


def merge_loops(steps: list[dict], scope: dict) -> tuple[list[dict], dict, list[dict]]:
    """Merge whole-list walks into foreach steps; also report walks that skipped items."""
    out: list[dict] = []
    partial: list[dict] = []
    alias: dict[str, str] = {}
    i = 0
    while i < len(steps):
        key = _loop_key(steps[i], scope)
        j = i + 1
        if key:
            while j < len(steps):
                k2 = _loop_key(steps[j], scope)
                if not k2 or k2[0] != key[0] or k2[1] != key[1]:
                    break
                j += 1
        group = steps[i:j]
        if key and len(group) >= 2:
            list_ref = key[1]
            idxs = [_loop_key(s, scope)[2] for s in group]
            n = len(R.resolve(scope, list_ref))
            if sorted(idxs) == list(range(n)):
                loop_id = group[0]["id"]
                by_index = {ix: s for ix, s in zip(idxs, group)}
                for ix, s in by_index.items():
                    alias[s["id"]] = f"{loop_id}[{ix}]"
                first = group[0]
                body = R.map_refs(first["args"], lambda r: _to_item(r, list_ref))
                out.append({"id": loop_id, "kind": "foreach", "over": list_ref,
                            "tool": first["tool"], "args": body, "kinds": first["kinds"],
                            "recorded_count": n, "shape": first["shape"],
                            "exit_code": first.get("exit_code"),
                            "flagged": next((s["flagged"] for s in group if s.get("flagged")),
                                            None)})
                results = [scope.pop(by_index[ix]["id"]) for ix in range(n)]
                scope[loop_id] = results
                i = j
                continue
            if len(set(idxs)) == len(idxs):
                items = R.resolve(scope, list_ref)
                missing = [ix for ix in range(n) if ix not in idxs]
                partial.append({"tool": group[0]["tool"], "over": list_ref, "total": n,
                                "used": sorted(idxs), "missing": [str(items[ix])[:120] for ix in missing]})
                out.extend(group)  # keep them positional; never re-read a tail of the same walk
                i = j
                continue
        out.append(steps[i])
        i += 1
    if alias:
        def rewrite(ref: str) -> str:
            fn, head, _ = R.parse_ref(ref)
            if head not in alias:
                return ref
            inner = ref[len(fn) + 1:-1] if fn else ref
            new = alias[head] + inner[len(head):]
            return f"{fn}({new})" if fn else new
        for s in out:
            if s["kind"] in ("tool", "foreach"):
                s["args"] = R.map_refs(s["args"], rewrite)
    return out, scope, partial


def _to_item(ref: str, list_ref: str) -> str:
    m = _IDX_RE.match(ref)
    if not m or m.group("list") != list_ref:
        return ref
    inner = "item" + m.group("rest")
    return f"{m.group('fn')}{inner})" if m.group("fn") else inner
