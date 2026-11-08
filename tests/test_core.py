"""Unit tests for the freeze core: run with ``python -m unittest discover -s tests``."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freeze_core import analyze, compiler, freezer, refs, replay, sandbox  # noqa: E402

TASK = ("In ./data there are daily order CSV files. Count the rows with status 'refunded' in "
        "every file and write ./refunds.md with one line per file: '<filename>: <count>'.")


def csv_rows(rows):
    lines = ["order_id,amount,status"] + rows
    return "\n".join(f"{i}|{ln}" for i, ln in enumerate(lines, 1))


DATA = {
    "./data/orders_1.csv": ["1,10,paid", "2,5,refunded"],
    "./data/orders_2.csv": ["3,7,refunded", "4,9,refunded", "5,1,paid"],
    "./data/orders_3.csv": ["6,3,paid"],
}


def expected_report(data):
    return "\n".join(f"{os.path.basename(p)}: {sum(r.endswith(',refunded') for r in rows)}"
                     for p, rows in data.items())


def recorded_calls(data):
    files = list(data)
    calls = [{"tool_name": "search_files", "status": "ok",
              "args": json.dumps({"pattern": "*.csv", "target": "files", "path": "./data"}),
              "result": json.dumps({"total_count": len(files), "files": files})}]
    for p in files:
        calls.append({"tool_name": "read_file", "status": "ok", "args": json.dumps({"path": p}),
                      "result": json.dumps({"content": csv_rows(data[p])})})
    calls.append({"tool_name": "todo", "status": "ok", "args": "{}", "result": "{}"})
    calls.append({"tool_name": "write_file", "status": "ok",
                  "args": json.dumps({"path": "./refunds.md", "content": expected_report(data)}),
                  "result": json.dumps({"bytes_written": 40})})
    return calls


GOOD_CODE = '''
import posixpath
def compute(scope):
    out = []
    for path, res in zip(scope["s1"]["files"], scope["s2"]):
        rows = [ln.split("|", 1)[1] for ln in res["content"].splitlines()[1:]]
        n = sum(1 for r in rows if r.split(",")[2] == "refunded")
        out.append(f"{posixpath.basename(path)}: {n}")
    return "\\n".join(out)
'''
WRONG_CODE = GOOD_CODE.replace('"refunded"', '"paid"')


class FakeTools:
    """Stands in for Hermes' tool registry on a fresh data set."""

    def __init__(self, data):
        self.data, self.written, self.calls = data, {}, []

    def __call__(self, tool, args):
        self.calls.append((tool, args))
        if tool == "search_files":
            return json.dumps({"total_count": len(self.data), "files": list(self.data)})
        if tool == "read_file":
            if args["path"] not in self.data:
                return json.dumps({"error": f"File not found: {args['path']}"})
            return json.dumps({"content": csv_rows(self.data[args["path"]])})
        if tool == "write_file":
            self.written[args["path"]] = args["content"]
            return json.dumps({"bytes_written": len(args["content"])})
        if tool == "terminal":
            return json.dumps({"output": f"ran {args['command']}", "exit_code": 0})
        return json.dumps({"error": f"Unknown tool: {tool}"})


class RefsTest(unittest.TestCase):
    def test_resolve_paths_and_functions(self):
        scope = {"s1": json.dumps({"files": ["./a/b.csv"], "n": 1234})}
        self.assertEqual(refs.resolve(scope, "s1.files[0]"), "./a/b.csv")
        self.assertEqual(refs.resolve(scope, "basename(s1.files[0])"), "b.csv")
        self.assertEqual(refs.resolve(scope, "str(s1.n)"), "1234")
        with self.assertRaises(refs.RefError):
            refs.resolve(scope, "s1.files[3]")

    def test_render_keeps_type_and_literals(self):
        scope = {"s1": {"n": 5, "p": "x.csv"}}
        self.assertEqual(refs.render("{{s1.n}}", scope), 5)
        self.assertEqual(refs.render("cat {{s1.p}}", scope), "cat x.csv")
        self.assertEqual(refs.render({"$lit": "keep {{this}}"}, scope), "keep {{this}}")


class AnalyzeTest(unittest.TestCase):
    def setUp(self):
        self.draft = analyze.build(TASK, recorded_calls(DATA), "Done.", "/tmp/x")
        self.steps = {s["id"]: s for s in self.draft["steps"]}

    def test_search_args_are_constants_from_the_task(self):
        self.assertEqual(self.steps["s1"]["args"]["path"], "./data")

    def test_reads_of_every_file_become_a_loop(self):
        loop = self.steps["s2"]
        self.assertEqual(loop["kind"], "foreach")
        self.assertEqual(loop["over"], "s1.files")
        self.assertEqual(loop["args"], {"path": "{{item}}"})
        self.assertEqual(len(self.draft["_scope"]["s2"]), 3)

    def test_report_content_is_a_compute_step(self):
        compute = [s for s in self.draft["steps"] if s["kind"] == "compute"]
        self.assertEqual(compute[0]["for"], "write_file.content")
        write = [s for s in self.draft["steps"] if s.get("tool") == "write_file"][0]
        self.assertEqual(write["args"]["path"], "./refunds.md")
        self.assertEqual(write["args"]["content"], "{{" + compute[0]["id"] + "}}")

    def test_model_only_tools_are_left_out(self):
        self.assertTrue(any(s.startswith("todo") for s in self.draft["skipped"]))

    def test_plain_answer_is_constant(self):
        self.assertEqual(self.steps["answer"]["kind"], "const")

    def test_partial_walk_stays_positional(self):
        calls = recorded_calls(DATA)[:2]  # search + read of the first file only
        calls.append({"tool_name": "terminal", "status": "ok",
                      "args": json.dumps({"command": "wc -l ./data/orders_1.csv"}),
                      "result": json.dumps({"output": "3", "exit_code": 0})})
        draft = analyze.build(TASK, calls, None, "/tmp/x")
        kinds = [s["kind"] for s in draft["steps"]]
        self.assertNotIn("foreach", kinds)
        self.assertEqual(draft["steps"][1]["args"], {"path": "{{s1.files[0]}}"})
        self.assertEqual(draft["steps"][2]["args"]["command"], "wc -l {{s1.files[0]}}")

    def test_flagged_command_is_remembered(self):
        calls = [{"tool_name": "terminal", "status": "ok",
                  "args": json.dumps({"command": "python -c 'print(1)'"}),
                  "result": json.dumps({"output": "1", "exit_code": 0, "error": None,
                                        "approval": "Command was flagged (script execution via "
                                                    "-e/-c flag) and auto-approved by smart approval."})}]
        draft = analyze.build("print one", calls, None, tempfile.gettempdir())
        step = draft["steps"][0]
        self.assertEqual(step["flagged"], "script execution via -e/-c flag")
        wf = {"name": "one", "steps": draft["steps"]}
        self.assertIn("[needs approval]", freezer.describe(wf))
        self.assertIn("Needs approval: s1", freezer.summary(wf))
        self.assertNotIn("Schedule it", freezer.summary(wf))

    def test_ids_from_results_are_bound(self):
        calls = [
            {"tool_name": "terminal", "status": "ok", "args": json.dumps({"command": "gh api issues"}),
             "result": json.dumps({"output": "", "exit_code": 0, "items": [{"number": 70321}]})},
            {"tool_name": "terminal", "status": "ok",
             "args": json.dumps({"command": "gh api issues/70321/comments"}),
             "result": json.dumps({"output": "[]", "exit_code": 0})},
        ]
        draft = analyze.build("latest issue comments", calls, None, "/tmp")
        self.assertEqual(draft["steps"][1]["args"]["command"], "gh api issues/{{str(s1.items[0].number)}}/comments")


class GuardTest(unittest.TestCase):
    def test_partial_walk_is_reported(self):
        calls = recorded_calls(DATA)
        del calls[2]  # the agent never read orders_2.csv
        draft = analyze.build(TASK, calls, None, "/tmp")
        walk = draft["partial_walks"][0]
        self.assertEqual((walk["used"], walk["total"]), ([0, 2], 3))
        self.assertEqual(walk["missing"], ["./data/orders_2.csv"])

    def test_hardcoded_data_is_rejected(self):
        scope = {"s1": {"files": ["./data/orders_1.csv"], "total_count": 1234}}
        code = 'def compute(scope):\n    return "orders_1.csv: " + str(1234) + " orders_"'
        self.assertEqual(compiler.hardcoded(code, scope, TASK), ["'orders_1.csv: '", "1234"])
        clean = 'def compute(scope):\n    return scope["s1"]["files"][0]'
        self.assertEqual(compiler.hardcoded(clean, scope, TASK), [])

    def test_insufficient_reply_stops_compile(self):
        draft = analyze.build(TASK, recorded_calls(DATA), None, "/tmp")
        scope = draft.pop("_scope")
        step = [s for s in draft["steps"] if s["kind"] == "compute"][0]
        res = compiler.compile_step(lambda m: "INSUFFICIENT: no data for orders_2.csv", task=TASK,
                                    steps=draft["steps"], scope=scope, step=step)
        self.assertEqual(res["insufficient"], "no data for orders_2.csv")


class ShortValueTest(unittest.TestCase):
    def test_short_number_from_data_is_derived(self):
        calls = [{"tool_name": "terminal", "status": "ok", "args": json.dumps({"command": "wc -l a b"}),
                  "result": json.dumps({"output": "  3 a\n  2 b\n  5 total", "exit_code": 0})},
                 {"tool_name": "write_file", "status": "ok",
                  "args": json.dumps({"path": "./total.txt", "content": "5"}), "result": "{}"}]
        draft = analyze.build("write the total line count", calls, None, "/tmp")
        self.assertEqual([s["kind"] for s in draft["steps"]], ["tool", "compute", "tool"])

    def test_short_flag_stays_constant(self):
        calls = [{"tool_name": "terminal", "status": "ok", "args": json.dumps({"command": "ls", "mode": "fg"}),
                  "result": json.dumps({"output": "x", "exit_code": 0})}]
        draft = analyze.build("list files", calls, None, "/tmp")
        self.assertEqual(draft["steps"][0]["args"]["mode"], "fg")


class SandboxTest(unittest.TestCase):
    def test_runs_pure_code(self):
        self.assertEqual(sandbox.run("def compute(scope):\n    return scope['a'] * 2", {"a": 21}), 42)

    def test_refuses_dangerous_code(self):
        for code in ["import os\ndef compute(scope): return 1",
                     "def compute(scope): return open('/etc/passwd').read()",
                     "def compute(scope): return ().__class__",
                     "def compute(scope): return __import__('os')"]:
            with self.assertRaises(sandbox.UnsafeCode, msg=code):
                sandbox.run(code, {})

    def test_runtime_import_guard_and_timeout(self):
        with self.assertRaises(sandbox.ComputeError):
            sandbox.run("def compute(scope):\n    while True: pass", {})

    def test_print_does_not_break_output(self):
        self.assertEqual(sandbox.run("def compute(scope):\n    print('hi')\n    return 'ok'", {}), "ok")


class CompileAndReplayTest(unittest.TestCase):
    def freeze(self, replies):
        draft = analyze.build(TASK, recorded_calls(DATA), None, tempfile.gettempdir())
        scope = draft.pop("_scope")
        replies = list(replies)
        prompts = []

        def complete(messages):
            prompts.append(messages)
            return "```python\n" + replies.pop(0) + "\n```"

        for step in draft["steps"]:
            if step["kind"] == "compute":
                res = compiler.compile_step(complete, task=TASK, steps=draft["steps"], scope=scope,
                                            step=step)
                step.update(res)
        return draft, prompts

    def test_wrong_code_is_caught_and_retried(self):
        draft, prompts = self.freeze([WRONG_CODE, GOOD_CODE])
        compute = [s for s in draft["steps"] if s["kind"] == "compute"][0]
        self.assertEqual(compute["attempts"], 2)
        self.assertIn("does not match", prompts[1][-1]["content"])

    def test_three_wrong_attempts_fail(self):
        draft, _ = self.freeze([WRONG_CODE] * 3)
        compute = [s for s in draft["steps"] if s["kind"] == "compute"][0]
        self.assertIn("error", compute)

    def test_replay_on_new_data_without_model(self):
        draft, _ = self.freeze([GOOD_CODE])
        fresh = dict(DATA)
        fresh["./data/orders_4.csv"] = ["7,2,refunded", "8,2,refunded", "9,2,refunded"]
        tools = FakeTools(fresh)
        report = replay.run(draft, tools, log=lambda m: None)
        self.assertTrue(report["ok"], report)
        self.assertEqual(tools.written["./refunds.md"], expected_report(fresh))
        self.assertEqual(report["tool_calls"], 1 + 4 + 1)
        self.assertEqual(report["llm_calls"], 0)

    def test_drift_stops_before_writing(self):
        draft, _ = self.freeze([GOOD_CODE])
        tools = FakeTools(DATA)
        original = tools.__call__

        def broken(tool, args):
            if tool == "read_file" and args["path"].endswith("_2.csv"):
                return json.dumps({"error": "permission denied"})
            return original(tool, args)

        report = replay.run(draft, broken, log=lambda m: None)
        self.assertFalse(report["ok"])
        self.assertEqual(report["drift"]["step"], "s2")
        self.assertEqual(tools.written, {})

    def test_empty_list_is_drift(self):
        draft, _ = self.freeze([GOOD_CODE])
        tools = FakeTools({})
        report = replay.run(draft, tools, log=lambda m: None)
        self.assertFalse(report["ok"])
        self.assertIn("empty now", report["drift"]["reason"])
        self.assertEqual(tools.written, {})

    def test_unsafe_value_never_reaches_the_shell(self):
        wf = {"cwd": "/tmp", "steps": [
            {"id": "s1", "kind": "tool", "tool": "search_files", "args": {"path": "."}},
            {"id": "s2", "kind": "tool", "tool": "terminal", "args": {"command": "wc -l {{s1.files[0]}}"},
             "exit_code": 0},
        ]}
        tools = FakeTools({"x.csv; rm -rf ~": []})
        report = replay.run(wf, tools, log=lambda m: None)
        self.assertFalse(report["ok"])
        self.assertIn("not a plain token", report["drift"]["reason"])
        self.assertEqual([c[0] for c in tools.calls], ["search_files"])

    def test_bound_write_path_must_stay_in_folder(self):
        wf = {"cwd": "/tmp/project", "steps": [
            {"id": "s1", "kind": "tool", "tool": "search_files", "args": {"path": "."}},
            {"id": "s2", "kind": "tool", "tool": "write_file",
             "args": {"path": "{{s1.files[0]}}", "content": "x"}, "recorded_args": {"path": "./a.txt"}},
        ]}
        report = replay.run(wf, FakeTools({"/etc/hosts": []}), log=lambda m: None)
        self.assertFalse(report["ok"])
        self.assertIn("outside", report["drift"]["reason"])

    def test_dry_run_writes_nothing(self):
        draft, _ = self.freeze([GOOD_CODE])
        tools = FakeTools(DATA)
        logs = []
        report = replay.run(draft, tools, dry_run=True, log=logs.append)
        self.assertTrue(report["ok"], report)
        self.assertEqual(tools.written, {})
        self.assertTrue(any("would run write_file" in m for m in logs))


class OverwriteTest(unittest.TestCase):
    """A replay overwrites only files that still hold what freeze wrote there."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "out.md")
        self.wf = {"cwd": self.dir, "steps": [
            {"id": "s1", "kind": "tool", "tool": "write_file",
             "args": {"path": "out.md", "content": "new"},
             "recorded_args": {"path": "out.md", "content": "recorded"}},
        ]}
        self.calls = []

    def dispatch(self, tool, args):
        self.calls.append(tool)
        if tool == "write_file":
            Path(self.path).write_text(args["content"], encoding="utf-8")
            return json.dumps({"bytes_written": len(args["content"])})
        return json.dumps({"content": "1|x", "total_lines": 1})

    def test_recorded_output_is_read_then_overwritten(self):
        Path(self.path).write_text("recorded", encoding="utf-8")
        report = replay.run(self.wf, self.dispatch, log=lambda m: None)
        self.assertTrue(report["ok"], report)
        self.assertEqual(self.calls, ["read_file", "write_file"])
        self.assertEqual(report["written"], {os.path.realpath(self.path): replay._sha("new")})

    def test_last_replay_output_is_overwritten(self):
        Path(self.path).write_text("from last run", encoding="utf-8")
        written = {os.path.realpath(self.path): replay._sha("from last run")}
        report = replay.run(self.wf, self.dispatch, log=lambda m: None, written=written)
        self.assertTrue(report["ok"], report)

    def test_edited_file_is_never_overwritten(self):
        Path(self.path).write_text("my own notes", encoding="utf-8")
        report = replay.run(self.wf, self.dispatch, log=lambda m: None)
        self.assertFalse(report["ok"])
        self.assertIn("changed since freeze last wrote it", report["drift"]["reason"])
        self.assertEqual(self.calls, [])
        self.assertEqual(Path(self.path).read_text(encoding="utf-8"), "my own notes")


class OutputTest(unittest.TestCase):
    def test_drift_message_wraps_without_splitting_paths(self):
        from freeze_core import cli
        path = "/Users/Shared/freeze-demo/refunds.md"
        old = os.environ.get("COLUMNS")
        os.environ["COLUMNS"] = "50"
        try:
            text = cli._result_line({"ok": False, "drift": {
                "step": "s9", "reason": f"{path} changed since freeze last wrote it"}}, {})
        finally:
            if old is None:
                os.environ.pop("COLUMNS", None)
            else:
                os.environ["COLUMNS"] = old
        self.assertIn(path, text)
        self.assertTrue(all(len(line) <= 49 or path in line for line in text.splitlines()))


if __name__ == "__main__":
    unittest.main()
