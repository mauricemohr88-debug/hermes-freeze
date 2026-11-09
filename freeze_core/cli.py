"""``hermes freeze list|show|run|delete|schedule``."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
from typing import Any, Callable

from . import freezer, replay, store

EXIT_DRIFT = 3


def setup(parser) -> None:
    subs = parser.add_subparsers(dest="freeze_command")
    subs.add_parser("list", help="List frozen workflows")
    p = subs.add_parser("show", help="Show a workflow's steps")
    p.add_argument("name")
    p.add_argument("--code", action="store_true", help="Include the compiled code")
    p = subs.add_parser("run", help="Replay a workflow without the model")
    p.add_argument("name")
    p.add_argument("--dry-run", action="store_true",
                   help="Run only read-only tools and print what would be written")
    p.add_argument("--fallback", choices=["none", "agent"], default="none",
                   help="On drift, hand the original task to the agent (costs model calls)")
    p.add_argument("--json", action="store_true", help="Print the run report as JSON")
    p.add_argument("--quiet", action="store_true",
                   help="Print only the result and a one-line footer (used by scheduled runs)")
    p = subs.add_parser("delete", help="Delete a workflow")
    p.add_argument("name")
    p = subs.add_parser("schedule", help="Run a workflow on a cron schedule, without the model")
    p.add_argument("name")
    p.add_argument("schedule", help="e.g. '0 9 * * *' or 'every 2h'")
    p.add_argument("--deliver", default=None, help="Cron delivery target (default: cron's)")
    # Scheduled runs are sold as model-free, so the agent fallback must be asked for by name.
    p.add_argument("--fallback", choices=["none", "agent"], default="none",
                   help="On drift, hand the original task to the agent (costs model calls); "
                        "default: only report the drift")
    # Accepted so commands written for 0.1.4 and earlier still work; it is now the default.
    p.add_argument("--no-fallback", action="store_true", help=argparse.SUPPRESS)


def make_handler(dispatch: Callable[[str, dict], str]) -> Callable[[Any], int]:
    def handler(args) -> int:
        cmd = getattr(args, "freeze_command", None)
        try:
            if cmd == "list":
                return _list()
            if cmd == "show":
                wf = store.load_workflow(args.name)
                print(f"{wf['name']}  (frozen {wf.get('created')}, from session "
                      f"{wf.get('source', {}).get('session_id')})\nTask: {wf.get('task')}\n"
                      f"Folder: {wf.get('cwd')}")
                print(freezer.describe(wf, code=args.code))
                return 0
            if cmd == "run":
                return run(args, dispatch)
            if cmd == "delete":
                ok = store.delete_workflow(args.name)
                print(f"Deleted {args.name}." if ok else f"No workflow named {args.name}.")
                return 0 if ok else 1
            if cmd == "schedule":
                return schedule(args)
        except (ValueError, FileNotFoundError) as exc:
            print(f"freeze: {exc}", file=sys.stderr)
            return 2
        print("usage: hermes freeze {list,show,run,delete,schedule} ...  (in a chat: /freeze <name>)")
        return 0 if cmd is None else 2
    return handler


def _list() -> int:
    wfs = store.list_workflows()
    if not wfs:
        print("No frozen workflows yet. In a chat, let Hermes do a task, then type /freeze <name>.")
        return 0
    for wf in wfs:
        if "error" in wf:
            print(f"{wf['name']:<24} {wf['error']}")
            continue
        task = (wf.get("task") or "").splitlines()[0][:70] if wf.get("task") else ""
        print(f"{wf['name']:<24} {len(wf['steps']):>2} steps  {wf.get('created', '')}  {task}")
    return 0


def run(args, dispatch: Callable[[str, dict], str]) -> int:
    wf = store.load_workflow(args.name)
    _prepare_tools(wf, attended=_attended(args) and not args.dry_run)
    log = (lambda _m: None) if (args.quiet or args.json) else (lambda m: print(m, flush=True))
    report = replay.run(wf, dispatch, dry_run=args.dry_run, log=log,
                        written=store.load_written(wf["name"]),
                        persist=lambda w: store.save_written(wf["name"], w))
    written = report.pop("written")
    if not args.dry_run:
        store.save_written(wf["name"], written)
    store.append_run_log(wf["name"], {"ts": time.time(), **{k: v for k, v in report.items()
                                                           if k != "answer"}})
    rec = wf.get("recorded_run") or {}
    if args.json:
        print(json.dumps({**report, "recorded_run": rec}, ensure_ascii=False, default=str))
    else:
        if report.get("answer"):
            print(report["answer"] if args.quiet else f"\n{_wrap_text(report['answer'])}\n")
        if not args.quiet:
            print(_result_line(report, rec))
        elif report["ok"]:
            print(f"(replayed by freeze: 0 model calls, {report['seconds']} s)")
        else:
            print(_result_line(report, rec))
    if report["ok"]:
        return 0
    if args.fallback == "agent" and not args.dry_run:
        print("Handing the original task to the agent instead.", flush=True)
        return _run_agent(wf)
    return EXIT_DRIFT


def _result_line(report: dict, rec: dict) -> str:
    if report["ok"]:
        tokens = (rec.get("input_tokens") or 0) + (rec.get("output_tokens") or 0)
        was = ""
        if rec.get("seconds") or tokens:
            was = f"  (recorded run: {rec.get('seconds', '?')} s, {rec.get('model_calls', 0)} model calls, {tokens:,} tokens)"
        verb = "Dry run" if report.get("dry_run") else "Replayed"
        return _wrap(f"✓ {verb} {report['steps']} steps, {report['tool_calls']} tool calls, "
                     f"0 model calls, {report['seconds']} s{was}")
    d = report["drift"]
    if "changed since freeze last wrote it" in d["reason"]:
        hint = ("Someone edited this file after freeze wrote it, so freeze left it alone. Move your "
                "changes elsewhere or restore the file, then run again.")
    elif "User denied" in d["reason"]:
        hint = "You denied this command, so the run stopped here; nothing after it ran."
    elif "BLOCKED" in d["reason"]:
        hint = ("Hermes' approval policy stopped this command; nothing after it ran. Run it in a "
                "terminal to approve it yourself; unattended runs follow approvals.cron_mode.")
    else:
        hint = ("The data no longer matches what this workflow was frozen on. Nothing after this "
                "step ran. Re-run the task in a chat and /freeze it again, or use --fallback agent.")
    return _wrap(f"✗ Stopped at {d['step']}: {d['reason']}") + "\n" + _wrap(hint, first="  ")


def _wrap_text(text: str) -> str:
    """Wrap only over-long lines of an answer, keeping its own line breaks and indentation."""
    if not sys.stdout.isatty():
        return text  # pipes and logs get the answer exactly as compiled
    width = max(40, shutil.get_terminal_size((100, 24)).columns - 1)
    out = []
    for line in text.splitlines():
        if len(line) <= width:
            out.append(line)
            continue
        indent = line[:len(line) - len(line.lstrip())]
        out.append(textwrap.fill(line, width=width, subsequent_indent=indent,
                                 break_long_words=False, break_on_hyphens=False))
    return "\n".join(out)


def _wrap(text: str, *, first: str = "") -> str:
    """Wrap at word boundaries to the terminal, so paths and numbers never split mid-word."""
    width = max(40, shutil.get_terminal_size((100, 24)).columns - 1)
    return textwrap.fill(text, width=width, initial_indent=first, subsequent_indent="  ",
                         break_long_words=False, break_on_hyphens=False)


def _prepare_tools(wf: dict, *, attended: bool) -> None:
    # `hermes <plugin>` commands start without the agent, so the tool registry is still empty.
    from tools.registry import discover_builtin_tools, registry
    needed = {s["tool"] for s in wf["steps"] if s["kind"] in ("tool", "foreach")}
    if any(registry.get_entry(t) is None for t in needed):
        discover_builtin_tools()
    missing = sorted(t for t in needed if registry.get_entry(t) is None)
    if missing:
        raise ValueError(f"tools not available outside a chat: {', '.join(missing)}")
    _bind_approvals(attended)
    cwd = wf.get("cwd")
    if cwd:
        if not os.path.isdir(cwd):
            raise ValueError(f"the workflow's folder {cwd} does not exist")
        os.chdir(cwd)
        os.environ["TERMINAL_CWD"] = cwd


def _attended(args) -> bool:
    """A person is at this terminal and can answer Hermes' approval prompt."""
    if getattr(args, "quiet", False) or getattr(args, "json", False):
        return False
    return sys.stdin.isatty() and sys.stdout.isatty()


def _bind_approvals(attended: bool) -> None:
    """Route commands Hermes flags as dangerous to a person, or deny them.

    Attended (a terminal on both ends): mark the process interactive so Hermes asks at this
    terminal, exactly as in a chat. Otherwise (scheduled, piped, ``--quiet``, ``/freeze run``):
    mark it as a cron session, so ``approvals.cron_mode`` (default deny) decides and a blocked
    command stops the run. Without either mark a plain process would auto-approve.
    """
    if attended:
        os.environ.pop("HERMES_CRON_SESSION", None)
        os.environ["HERMES_INTERACTIVE"] = "1"
    else:
        os.environ.pop("HERMES_INTERACTIVE", None)
        os.environ["HERMES_CRON_SESSION"] = "1"


def _hermes_argv() -> list[str]:
    exe = shutil.which("hermes")
    return [exe] if exe else [sys.executable, "-m", "hermes_cli.main"]


def _run_agent(wf: dict) -> int:
    argv = _hermes_argv() + ["chat", "--oneshot", "-Q", "-q", wf["task"]]
    return subprocess.run(argv, cwd=wf.get("cwd") or None).returncode


def _cron_mode() -> str:
    """``approvals.cron_mode`` as Hermes reads it: 'approve' or 'deny' (the default)."""
    from hermes_cli.config import load_config_readonly
    mode = str(((load_config_readonly() or {}).get("approvals") or {}).get("cron_mode", "deny"))
    return "approve" if mode.lower().strip() in {"approve", "off", "allow", "yes"} else "deny"


def schedule(args) -> int:
    wf = store.load_workflow(args.name)
    if freezer.flagged_steps(wf) and _cron_mode() != "approve":
        # The job would stop at the first flagged command on every run.
        print(f"freeze: not scheduling {wf['name']}. {freezer.approval_note(wf)}", file=sys.stderr)
        return 2
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
    scripts = home / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    script = scripts / f"freeze-{wf['name']}.sh"
    run_argv = _hermes_argv() + ["freeze", "run", wf["name"], "--quiet"]
    fallback = args.fallback == "agent" and not args.no_fallback
    if fallback:
        run_argv += ["--fallback", "agent"]
    on_drift = "hands the task to the agent on drift" if fallback else "reports drift and stops"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f"# Written by the freeze plugin: replays the frozen workflow {wf['name']!r} without the model;\n"
        f"# {on_drift}.\n"
        f"export HERMES_HOME={shlex.quote(str(home))}\n"
        f"exec {' '.join(shlex.quote(a) for a in run_argv)}\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    cron = _hermes_argv() + ["cron", "create", args.schedule, "--no-agent", "--script", script.name,
                             "--name", f"freeze: {wf['name']}"]
    if args.deliver:
        cron += ["--deliver", args.deliver]
    proc = subprocess.run(cron, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode == 0:
        print(f"Scheduled {wf['name']} ({args.schedule}) as a no-agent cron job. "
              + ("0 model calls when the replay succeeds; on drift the agent takes over the task "
                 "and that run costs model calls." if fallback else
                 "0 model calls per run; on drift the job reports it and stops."))
    return proc.returncode
