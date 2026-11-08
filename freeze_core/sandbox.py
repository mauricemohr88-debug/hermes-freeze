"""Run a compiled ``compute(scope)`` function: pure data in, pure data out.

The code was written by a model, so it is checked twice. A static pass refuses imports outside a
small allowlist, dunder access and the builtins that reach files, the process or the interpreter.
Then it runs in a separate ``python -I`` process with a timeout and a reduced set of builtins,
fed the scope as JSON on stdin. It never sees the plugin's memory, the agent or the tool registry.
This is defence in depth for code the user reviewed at freeze time, not a hard sandbox.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from typing import Any

ALLOWED_MODULES = frozenset({
    "re", "json", "math", "statistics", "datetime", "collections", "itertools", "functools",
    "csv", "io", "string", "decimal", "fractions", "posixpath", "textwrap", "operator",
})
_BANNED_NAMES = frozenset({
    "open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars", "getattr",
    "setattr", "delattr", "input", "breakpoint", "exit", "quit", "help", "memoryview",
    "classmethod", "staticmethod", "super", "type", "object",
})
TIMEOUT_S = 10


class UnsafeCode(ValueError):
    pass


class ComputeError(RuntimeError):
    pass


def check(code: str) -> None:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise UnsafeCode(f"syntax error: {exc}") from None
    has_compute = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in ALLOWED_MODULES:
                    raise UnsafeCode(f"import of {alias.name!r} is not allowed")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in ALLOWED_MODULES or node.level:
                raise UnsafeCode(f"import from {node.module!r} is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise UnsafeCode(f"access to {node.attr!r} is not allowed")
        elif isinstance(node, ast.Name) and (node.id in _BANNED_NAMES or node.id.startswith("__")):
            raise UnsafeCode(f"use of {node.id!r} is not allowed")
        elif isinstance(node, (ast.Global, ast.Nonlocal, ast.AsyncFunctionDef, ast.Await)):
            raise UnsafeCode(f"{type(node).__name__} is not allowed")
        elif isinstance(node, ast.FunctionDef) and node.name == "compute":
            has_compute = True
    if not has_compute:
        raise UnsafeCode("code must define compute(scope)")


_RUNNER = r"""
import builtins, io, json, sys
ALLOWED = set(json.loads(sys.argv[1]))
SAFE = ["abs","all","any","bool","chr","dict","divmod","enumerate","filter","float","format",
        "frozenset","hash","int","isinstance","iter","len","list","map","max","min","next","ord",
        "pow","range","repr","reversed","round","set","slice","sorted","str","sum","tuple","zip",
        "Exception","ValueError","KeyError","IndexError","TypeError","ZeroDivisionError",
        "StopIteration","True","False","None","print"]
real_import = builtins.__import__
def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level or name.split(".")[0] not in ALLOWED:
        raise ImportError("import of %r is not allowed" % name)
    return real_import(name, globals, locals, fromlist, level)
payload = json.loads(sys.stdin.read())
safe = {n: getattr(builtins, n) for n in SAFE if hasattr(builtins, n)}
safe["__import__"] = guarded_import
env = {"__builtins__": safe, "__name__": "compiled"}
real_stdout, sys.stdout = sys.stdout, io.StringIO()  # stray print() must not corrupt the reply
try:
    exec(compile(payload["code"], "<compiled>", "exec"), env)
    value = env["compute"](payload["scope"])
    out = {"ok": True, "value": value}
except BaseException as exc:
    out = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
sys.stdout = real_stdout
try:
    sys.stdout.write(json.dumps(out))
except (TypeError, ValueError) as exc:
    sys.stdout.write(json.dumps({"ok": False, "error": "result is not JSON data: %s" % exc}))
"""


def run(code: str, scope: dict) -> Any:
    check(code)
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", _RUNNER, json.dumps(sorted(ALLOWED_MODULES))],
            input=json.dumps({"code": code, "scope": scope}, default=str),
            capture_output=True, text=True, timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise ComputeError(f"compute step exceeded {TIMEOUT_S}s") from None
    if proc.returncode != 0 or not proc.stdout:
        raise ComputeError(f"compute process failed: {proc.stderr.strip()[-500:]}")
    out = json.loads(proc.stdout)
    if not out["ok"]:
        raise ComputeError(out["error"])
    return out["value"]
