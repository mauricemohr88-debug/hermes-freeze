"""Run a compiled ``compute(scope)`` function: pure data in, pure data out.

The code was written by a model and runs on every replay, unattended ones included, so it is
checked twice. A static pass refuses imports outside a small allowlist, dunder access, attribute
names that lead to the OS, the interpreter or stack frames, and the builtins that reach files,
the process or the interpreter. Then it runs in a separate ``python -I`` process with an empty
environment (no provider keys), a timeout and a reduced set of builtins, fed the scope as JSON on
stdin. Imported modules are wrapped so only listed functions are reachable: ``posixpath.os`` or
``io.open`` fail even if the static pass missed them. It never sees the plugin's memory, the agent
or the tool registry. This is defence in depth, not a hard sandbox.

``track_reads`` runs the code once more at freeze time and notes which fields of the scope it
reads (including JSON parsed from a tool's text output), so replays can stop when one of them
disappears or changes type instead of computing a wrong value from a fallback.
"""
from __future__ import annotations

import ast
import json
import os
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
# Modules whose public API also reaches files or the OS expose only these names. In the others
# every public name is reachable except submodules and the names below.
MODULE_ALLOW = {
    "posixpath": ["basename", "commonpath", "commonprefix", "dirname", "extsep", "isabs", "join",
                  "normpath", "sep", "split", "splitdrive", "splitext"],
    "io": ["BytesIO", "StringIO"],
    "string": ["Template", "ascii_letters", "ascii_lowercase", "ascii_uppercase", "capwords",
               "digits", "hexdigits", "octdigits", "printable", "punctuation", "whitespace"],
}
# attrgetter/methodcaller read attributes by name and update_wrapper/wraps copy them, which
# would get around the static check on attribute names.
MODULE_DENY = {
    "operator": ["attrgetter", "methodcaller"],
    "functools": ["update_wrapper", "wraps"],
}
# Attribute names that lead out of pure data: the OS, the interpreter, or a stack frame whose
# globals hold the runner's own unrestricted builtins.
BANNED_ATTRS = frozenset({
    "os", "sys", "open", "system", "popen", "environ", "builtins", "modules", "subprocess",
    "gi_frame", "gi_code", "cr_frame", "cr_code", "ag_frame", "ag_code", "tb_frame", "tb_next",
    "f_back", "f_globals", "f_locals", "f_builtins", "f_code", "co_code",
})
TIMEOUT_S = 10
# Only what the interpreter needs to start; provider keys and tokens never reach compiled code.
_CHILD_ENV_KEYS = ("PATH", "SYSTEMROOT")
# Path markers in recorded reads: any item of a list, JSON parsed from a string, and any value
# of a dict looked up by a key that comes from the data (a file name, a date) rather than the code.
LIST, JSON, ANY = 0, 1, 2


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
    # Names bound by "import x" / "import x as y", so x.name can be checked per module.
    modules = {(a.asname or a.name): a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
               for a in n.names}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in ALLOWED_MODULES:
                    raise UnsafeCode(f"import of {alias.name!r} is not allowed")
        elif isinstance(node, ast.ImportFrom):
            if node.module not in ALLOWED_MODULES or node.level:
                raise UnsafeCode(f"import from {node.module!r} is not allowed")
            for alias in node.names:
                if not _module_name_ok(node.module, alias.name):
                    raise UnsafeCode(f"{node.module}.{alias.name} is not allowed")
        elif isinstance(node, ast.Attribute) and (node.attr.startswith("_")
                                                   or node.attr in BANNED_ATTRS):
            raise UnsafeCode(f"access to {node.attr!r} is not allowed")
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id in modules and modules[node.value.id] in ALLOWED_MODULES \
                and not _module_name_ok(modules[node.value.id], node.attr):
            raise UnsafeCode(f"{modules[node.value.id]}.{node.attr} is not allowed")
        elif isinstance(node, ast.Name) and (node.id in _BANNED_NAMES or node.id.startswith("__")):
            raise UnsafeCode(f"use of {node.id!r} is not allowed")
        elif isinstance(node, (ast.Global, ast.Nonlocal, ast.AsyncFunctionDef, ast.Await)):
            raise UnsafeCode(f"{type(node).__name__} is not allowed")
        elif isinstance(node, ast.FunctionDef) and node.name == "compute":
            has_compute = True
    if not has_compute:
        raise UnsafeCode("code must define compute(scope)")


def _module_name_ok(module: str, name: str) -> bool:
    if name == "*" or name.startswith("_") or name in BANNED_ATTRS:
        return False
    if module in MODULE_ALLOW:
        return name in MODULE_ALLOW[module]
    return name not in MODULE_DENY.get(module, ())


_RUNNER = r"""
import builtins, io, json, sys, types
POLICY = json.loads(sys.argv[1])
ALLOWED = set(POLICY["modules"])
MODULE_ALLOW = {k: set(v) for k, v in POLICY["allow"].items()}
MODULE_DENY = {k: set(v) for k, v in POLICY["deny"].items()}
BANNED = set(POLICY["banned"])
SAFE = ["abs","all","any","bool","chr","dict","divmod","enumerate","filter","float","format",
        "frozenset","hash","int","isinstance","iter","len","list","map","max","min","next","ord",
        "pow","range","repr","reversed","round","set","slice","sorted","str","sum","tuple","zip",
        "Exception","ValueError","KeyError","IndexError","TypeError","ZeroDivisionError",
        "StopIteration","True","False","None","print"]
real_import = builtins.__import__
def safe_module(mod):
    # The compiled code gets this view, never the module: only listed names, no submodules.
    name = mod.__name__
    def lookup(attr):
        if attr.startswith("_") or attr in BANNED or attr in MODULE_DENY.get(name, ()):
            raise AttributeError("%s.%s is not allowed" % (name, attr))
        if name in MODULE_ALLOW and attr not in MODULE_ALLOW[name]:
            raise AttributeError("%s.%s is not allowed" % (name, attr))
        value = getattr(mod, attr)
        if isinstance(value, types.ModuleType):
            raise AttributeError("%s.%s is not allowed" % (name, attr))
        return value
    view = type(name, (), {"__slots__": (), "__getattr__": lambda self, attr: lookup(attr),
                           "__repr__": lambda self: "<module %s>" % name})
    return view()
def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level or name not in ALLOWED:
        raise ImportError("import of %r is not allowed" % name)
    return safe_module(real_import(name, globals, locals, fromlist, level))
payload = json.loads(sys.stdin.read())
reads = {}
absent = set()
misses = {}
scope = payload["scope"]
if payload.get("track"):
    # Wrap the scope so field reads are noted: at freeze time to learn them, and at replay for
    # steps whose code looks things up by keys that come from the data.
    LIST, JSON, ANY = 0, 1, 2
    FIELDS = set(payload.get("fields") or ())
    def kind(v):
        if v is None: return "null"
        if isinstance(v, bool): return "bool"
        if isinstance(v, (int, float)): return "number"
        if isinstance(v, str): return "string"
        if isinstance(v, list): return "array"
        return "object"
    def wrap(v, path):
        if isinstance(v, dict): return TDict(v, path)
        if isinstance(v, list): return TList(v, path)
        if isinstance(v, str): return TStr(v, path)
        return v
    class TDict(dict):
        def __init__(self, d, path):
            dict.__init__(self, d)
            self._path = path
        def _child(self, key):
            # Only names spelled out in the code are fields; keys taken from the data change
            # from run to run and must not become requirements.
            return self._path + [key if key in FIELDS else ANY]
        def _miss(self, key):
            path = self._child(key)
            absent.add(json.dumps(path))
            if path[-1] == ANY:
                misses.setdefault(json.dumps(path), str(key)[:120])
        def __getitem__(self, key):
            path = self._child(key)
            try:
                v = dict.__getitem__(self, key)
            except KeyError:
                self._miss(key)
                raise
            reads.setdefault(json.dumps(path), set()).add(kind(v))
            return wrap(v, path)
        def get(self, key, default=None):
            # A field read through get() still counts when it was there: a fallback must not
            # hide that it disappeared later. One already missing somewhere is optional.
            if dict.__contains__(self, key):
                return self[key]
            self._miss(key)
            return default
        def __contains__(self, key):
            found = dict.__contains__(self, key)
            if not found:
                self._miss(key)
            return found
    class TList(list):
        def __init__(self, items, path):
            list.__init__(self, items)
            self._path = path
        def __getitem__(self, i):
            v = list.__getitem__(self, i)
            if isinstance(i, slice):
                return TList(v, self._path)
            return wrap(v, self._path + [LIST])
        def __iter__(self):
            for v in list.__iter__(self):
                yield wrap(v, self._path + [LIST])
    class TStr(str):
        def __new__(cls, s, path):
            obj = str.__new__(cls, s)
            obj._path = path
            return obj
    real_loads = json.loads
    def tracked_loads(s, *args, **kwargs):
        v = real_loads(s, *args, **kwargs)
        return wrap(v, s._path + [JSON]) if isinstance(s, TStr) else v
    json.loads = tracked_loads
    scope = TDict(scope, [])
safe = {n: getattr(builtins, n) for n in SAFE if hasattr(builtins, n)}
safe["__import__"] = guarded_import
env = {"__builtins__": safe, "__name__": "compiled"}
real_stdout, sys.stdout = sys.stdout, io.StringIO()  # stray print() must not corrupt the reply
try:
    exec(compile(payload["code"], "<compiled>", "exec"), env)
    value = env["compute"](scope)
    out = {"ok": True, "value": value,
           "reads": [[k, sorted(v), k in absent] for k, v in reads.items()], "misses": misses}
except BaseException as exc:
    out = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
sys.stdout = real_stdout
try:
    sys.stdout.write(json.dumps(out))
except (TypeError, ValueError) as exc:
    sys.stdout.write(json.dumps({"ok": False, "error": "result is not JSON data: %s" % exc}))
"""


def run(code: str, scope: dict) -> Any:
    return _execute(code, scope, track=False)["value"]


def track_reads(code: str, scope: dict) -> tuple[Any, list[dict]]:
    """Run ``code`` and return (value, reads); each read is {"path": [...], "types": [...]}.

    ``optional`` marks a field the data already left out somewhere at freeze time. A path
    starts with a step id, then keys, ``LIST`` for "any item of this list", ``JSON``
    for "the JSON parsed from this string" and ``ANY`` for "any value of this dict".
    """
    out = _execute(code, scope, track=True)
    reads = [{"path": json.loads(path), "types": types, **({"optional": True} if optional else {})}
             for path, types, optional in out["reads"]]
    return out["value"], sorted(reads, key=lambda r: (len(r["path"]), json.dumps(r["path"])))


def run_tracked(code: str, scope: dict) -> tuple[Any, dict[str, str]]:
    """Run ``code`` with tracking and return (value, misses).

    ``misses`` maps each read path ending in ``ANY`` (a lookup by a key from the data) that
    found nothing to the first key it missed, e.g. a file name a count table has no entry for.
    """
    out = _execute(code, scope, track=True)
    return out["value"], out.get("misses") or {}


_POLICY = json.dumps({"modules": sorted(ALLOWED_MODULES), "allow": MODULE_ALLOW,
                      "deny": MODULE_DENY, "banned": sorted(BANNED_ATTRS)})


def _string_literals(code: str) -> set[str]:
    return {n.value for n in ast.walk(ast.parse(code))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def _execute(code: str, scope: dict, *, track: bool) -> dict:
    check(code)
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", _RUNNER, _POLICY],
            input=json.dumps({"code": code, "scope": scope, "track": track,
                              "fields": sorted(_string_literals(code)) if track else []},
                             default=str),
            capture_output=True, text=True, timeout=TIMEOUT_S,
            env={k: os.environ[k] for k in _CHILD_ENV_KEYS if k in os.environ},
        )
    except subprocess.TimeoutExpired:
        raise ComputeError(f"compute step exceeded {TIMEOUT_S}s") from None
    if proc.returncode != 0 or not proc.stdout:
        raise ComputeError(f"compute process failed: {proc.stderr.strip()[-500:]}")
    out = json.loads(proc.stdout)
    if not out["ok"]:
        raise ComputeError(out["error"])
    return out
