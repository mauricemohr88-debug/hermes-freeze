"""References into earlier step results and the ``{{ref}}`` templates that use them.

A ref is ``<step>`` followed by ``.key`` / ``[index]`` parts, e.g. ``s1.files[0]`` or
``item.path``, optionally wrapped as ``basename(<ref>)`` or ``str(<ref>)``. A template is any JSON value whose
strings may contain ``{{ref}}``; a string that is exactly one ``{{ref}}`` renders to the raw
value so ints and lists keep their type. A string that must stay literal although it contains
``{{`` is stored as ``{"$lit": "..."}``.
"""
from __future__ import annotations

import json
import posixpath
import re
from typing import Any, Callable, Optional

TEMPLATE_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
_PART_RE = re.compile(r"\.([A-Za-z_][\w-]*)|\[(\d+)\]")
_HEAD_RE = re.compile(r"^([A-Za-z_]\w*)")


class RefError(LookupError):
    """A ref did not resolve: the data no longer has the shape the workflow was frozen on."""


def parse_ref(ref: str) -> tuple[Optional[str], str, list]:
    ref = ref.strip()
    fn = None
    m = re.fullmatch(r"(basename|str)\((.+)\)", ref)
    if m:
        fn, ref = m.group(1), m.group(2).strip()
    head = _HEAD_RE.match(ref)
    if not head:
        raise RefError(f"bad ref {ref!r}")
    parts: list = []
    pos = head.end()
    while pos < len(ref):
        pm = _PART_RE.match(ref, pos)
        if not pm:
            raise RefError(f"bad ref {ref!r} at {ref[pos:]!r}")
        parts.append(pm.group(1) if pm.group(1) is not None else int(pm.group(2)))
        pos = pm.end()
    return fn, head.group(1), parts


def as_data(value: Any) -> Any:
    """Tool results are JSON strings; parse them so refs can walk into them."""
    if isinstance(value, str):
        s = value.strip()
        if s[:1] in "{[":
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return value
    return value


def resolve(scope: dict, ref: str) -> Any:
    fn, head, parts = parse_ref(ref)
    if head not in scope:
        raise RefError(f"{ref}: no step {head!r}")
    val = scope[head]
    walked = head
    for part in parts:
        val = as_data(val)
        try:
            val = val[part]
        except (KeyError, IndexError, TypeError):
            raise RefError(f"{ref}: {walked} has no {part!r}") from None
        walked += f"[{part}]" if isinstance(part, int) else f".{part}"
    if fn == "basename":
        if not isinstance(val, str):
            raise RefError(f"{ref}: basename of a non-string")
        val = posixpath.basename(val)
    elif fn == "str":
        if isinstance(val, (dict, list)):
            raise RefError(f"{ref}: str of a {type(val).__name__}")
        val = str(val)
    return val


def render(template: Any, scope: dict, check: Optional[Callable[[str, Any], None]] = None) -> Any:
    """Fill a template from scope. ``check(ref, value)`` may raise to veto a substituted value."""
    if isinstance(template, dict):
        if set(template) == {"$lit"}:
            return template["$lit"]
        return {k: render(v, scope, check) for k, v in template.items()}
    if isinstance(template, list):
        return [render(v, scope, check) for v in template]
    if not isinstance(template, str):
        return template
    whole = TEMPLATE_RE.fullmatch(template)
    if whole:
        val = resolve(scope, whole.group(1))
        if check:
            check(whole.group(1), val)
        return val

    def sub(m: re.Match) -> str:
        val = resolve(scope, m.group(1))
        if isinstance(val, (dict, list)):
            raise RefError(f"{m.group(1)}: cannot splice a {type(val).__name__} into text")
        val = str(val)
        if check:
            check(m.group(1), val)
        return val

    return TEMPLATE_RE.sub(sub, template)


def refs_in(template: Any) -> list[str]:
    if isinstance(template, dict):
        if set(template) == {"$lit"}:
            return []
        return [r for v in template.values() for r in refs_in(v)]
    if isinstance(template, list):
        return [r for v in template for r in refs_in(v)]
    if isinstance(template, str):
        return [m.group(1) for m in TEMPLATE_RE.finditer(template)]
    return []


def map_refs(template: Any, fn: Callable[[str], str]) -> Any:
    """Rewrite every ref inside a template (used when steps are merged into a loop)."""
    if isinstance(template, dict):
        if set(template) == {"$lit"}:
            return template
        return {k: map_refs(v, fn) for k, v in template.items()}
    if isinstance(template, list):
        return [map_refs(v, fn) for v in template]
    if isinstance(template, str):
        return TEMPLATE_RE.sub(lambda m: "{{" + fn(m.group(1)) + "}}", template)
    return template


def literal(value: Any) -> Any:
    """Wrap constants so text that happens to contain ``{{`` is never read as a template."""
    if isinstance(value, str) and "{{" in value:
        return {"$lit": value}
    if isinstance(value, dict):
        return {k: literal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [literal(v) for v in value]
    return value
