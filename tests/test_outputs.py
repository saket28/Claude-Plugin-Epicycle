"""Step outputs and outcomes: the file format, what a step publishes, who may read it, what a command is given."""
import json
import os
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

from helpers import ROOT  # noqa: F401
from engine import config, outputs, taskview, workflow
from engine.store import Store
from test_workflow import HEAD, TestWorkflow, parse, problems

WRITES = """
    import json, os, sys
    json.load(sys.stdin)
    with open(os.environ["EPI_OUTPUT"], "a") as f:
        f.write(%r)
    print(json.dumps({"summary": "ok"}))
"""
DUMP = """
    import json, os, sys
    req = json.load(sys.stdin)
    keys = ["EPI_TASK", "EPI_STAGE", "EPI_ROUND", "EPI_TITLE", "EPI_REPORTS", "EPI_DASHBOARD", "EPI_OUTPUT", "WHO", "WHAT"]
    open(req["task_dir"] + "/env.json", "w").write(json.dumps({k: os.environ.get(k) for k in keys}))
    print(json.dumps({"summary": "ok"}))
"""


class OutputFileTest(unittest.TestCase):
    def parse(self, text):
        f = Path(tempfile.mkdtemp()) / "out"
        self.addCleanup(shutil.rmtree, f.parent, ignore_errors=True)
        f.write_text(text)
        return outputs.parse_file(f)

    def test_name_value_lines_and_multiline_blocks(self):
        got = self.parse("a=1\nb = two words\n# a note\nlist<<EOF\nline 1\nline 2\nEOF\nc=\nz<<END\nx=y\nEND\n")
        self.assertEqual(got, {"a": "1", "b": "two words", "list": "line 1\nline 2", "c": "", "z": "x=y"})
        self.assertEqual(outputs.parse_file("/nonexistent/file"), {})

    def test_the_verdict_maps_to_an_outcome_and_only_declared_outputs_are_kept(self):
        for verdict, outcome in (("PASS", "success"), ("CHANGES", "failure"), ("FAIL", "failure"), (None, "success")):
            self.assertEqual(outputs.record(verdict, {"a": 1, "b": 2}, ["a"])["outcome"], outcome, verdict)
        self.assertEqual(outputs.record("FAIL", {"a": True, "b": [1, 2]}, ["a", "b", "c"]),
                         {"outcome": "failure", "verdict": "FAIL", "outputs": {"a": "true", "b": "1, 2"}})

    def test_the_dashboard_view_clips_long_values_and_marks_reused(self):
        v = outputs.view({"outcome": "success", "outputs": {"a": "x" * 500}, "reused": True})
        self.assertEqual(v["outcome"], "success")
        self.assertTrue(v["outputs"][0]["value"].endswith("…") and len(v["outputs"][0]["value"]) < 200)
        self.assertTrue(v["outputs"][0]["reused"])
        self.assertEqual(outputs.view(None), {"outcome": "", "outputs": []})


class ValidationTest(unittest.TestCase):
    def has(self, stages, word):
        errs = problems(HEAD + textwrap.dedent(stages))
        self.assertTrue(any(word in e for e in errs), f"{word!r} not in {errs}")

    def test_a_step_reads_only_declared_outputs_of_steps_above_it(self):
        ok = "stages:\n  a:\n    do:\n      - { run: test, outputs: [x] }\n    review:\n      - { run: test, id: t2, if: 'steps.test.outputs.x == 1' }\n"
        self.assertEqual(problems(HEAD + ok), [])
        self.has(ok.replace("outputs.x ==", "outputs.y =="), "reads output 'y' of step 'test', which declares: x")
        self.has(ok.replace("steps.test", "steps.nope"), "steps.nope, which is not a step of stage a")
        self.has("stages:\n  a:\n    do:\n      - { run: test, if: 'steps.t2.outcome == success' }\n    review:\n      - { run: test, id: t2 }\n",
                 "steps.t2, which runs later")

    def test_a_step_reads_earlier_stages_only(self):
        two = ("stages:\n  a:\n    do:\n      - { run: test, outputs: [x] }\n      - { run: test, id: later, if: '%s' }\n"
               "  b:\n    do:\n      - { run: test, id: t2, outputs: [y], if: '%s' }\n")
        self.assertEqual(problems(HEAD + two % ("always", "stages.a.steps.test.outputs.x")), [])
        self.has(two % ("stages.b.steps.t2.outputs.y", "always"), "stages.b.steps.t2, which runs later")
        self.has(two % ("always", "stages.zzz.steps.test.outcome == success"), "stage 'zzz', which does not exist")
        self.has(two % ("always", "stages.b.steps.test.outcome"), "this stage: write steps.test")
        self.has(two % ("always", "stages.a.steps.test.outputs.q"), "output 'q'")

    def test_a_template_is_only_for_env_and_with_never_a_command(self):
        base = "stages:\n  a:\n    do:\n      - { run: test, outputs: [x] }\n    review:\n      - %s\n"
        self.assertEqual(problems(HEAD + base % "{ run: test, id: n, env: { A: '${{ steps.test.outputs.x }}' }, with: { b: '${{ risk }}' } }"), [])
        self.has(base % "{ run: 'test ${{ steps.test.outputs.x }}' }", "only for the values of `env` and `with`")
        self.has(base % "{ run: test, id: n, name: '${{ risk }}' }", "not `name`")
        self.has(base % "{ run: test, id: n, env: { A: '${{ steps.nope.outcome }}' } }", "env.A reads steps.nope")
        self.has(base % "{ run: test, id: n, env: { A: '${{ steps.test.outputs.x ' } }", "never closed")

    def test_outputs_and_env_shapes(self):
        base = "stages:\n  a:\n    do:\n      - %s\n"
        self.has(base % "{ run: test, outputs: x }", "list of distinct names")
        self.has(base % "{ run: test, outputs: [a, a] }", "list of distinct names")
        self.has(base % "{ run: test, outputs: [verdict] }", "field of the engine's reports")
        self.has(base % "{ uses: agent/x, env: { A: 1 } }", "`env` is for run steps")
        self.has(base % "{ run: test, env: { EPI_TASK: x } }", "EPI_ names are the engine's")
        self.has(base % "{ run: test, env: { 'A B': x } }", "NAME: value")

    def test_a_command_setting_cannot_hold_a_template(self):
        from helpers import make_repo
        repo = make_repo("---\nglobal:\n  command:\n    test: \"echo ${{ steps.a.outputs.b }}\"\n---\n")
        self.addCleanup(shutil.rmtree, repo.parent, ignore_errors=True)
        with self.assertRaises(config.ConfigError) as cm:
            config.load(repo)
        self.assertIn("global.command.test", str(cm.exception))
        self.assertIn("$NAME", str(cm.exception))


class RunTest(unittest.TestCase):
    def folder(self, p):
        return Store(p.cfg).task_dir(p.task)

    def records(self, p, stage="main", r=1):
        return json.loads((self.folder(p) / stage / f"r{r}" / "outputs.json").read_text())

    def test_a_script_publishes_outputs_through_epi_output_and_a_later_step_reads_them(self):
        p = TestWorkflow(self, """
            stages:
              main:
                do:
                  - { run: emit, outputs: [one, many, unlisted] }
                  - { run: ok, id: go, if: "steps.emit.outputs.one == 1 and steps.emit.outputs.many != ''" }
                  - { run: ok2, id: stop, if: "steps.emit.outputs.one == 2" }
            """, scripts={"emit": WRITES % "one=1\nmany<<EOF\nfirst\nsecond\nEOF\nundeclared=x\n"})
        self.assertEqual(p.drive()["action"], "done")
        rec = self.records(p)
        self.assertEqual(rec["emit"], {"outcome": "success", "verdict": "PASS", "outputs": {"one": "1", "many": "first\nsecond"}})
        self.assertEqual((p.count("go"), p.count("stop")), (1, 0))
        self.assertEqual(rec["stop"], {"outcome": "skipped", "verdict": "", "outputs": {}})        # skipped: nothing published

    def test_a_command_gets_the_task_env_and_its_own_with_templates_filled_in(self):
        who = "ana smith; echo hacked"                      # an output is data: it never reaches a shell as code
        p = TestWorkflow(self, """
            stages:
              main:
                do:
                  - { run: emit, outputs: [who] }
                  - { run: dump, env: { WHO: "${{ steps.emit.outputs.who }}", WHAT: "no ${{ risk }}, ${{ 1 < 2 }}" } }
            """, scripts={"emit": WRITES % f"who={who}\n", "dump": DUMP})
        (p.cfg.work_dir).mkdir(parents=True, exist_ok=True)
        (p.cfg.work_dir / "dashboard.url").write_text("http://127.0.0.1:8765/?token=t\n")
        p.drive()
        env = json.loads((self.folder(p) / "env.json").read_text())
        self.assertEqual(env["WHO"], who)
        self.assertEqual(env["WHAT"], "no low, true")
        self.assertEqual((env["EPI_TASK"], env["EPI_STAGE"], env["EPI_ROUND"], env["EPI_TITLE"]), (p.task, "main", "1", "go"))
        self.assertEqual(env["EPI_REPORTS"], str(self.folder(p) / "main" / "r1"))
        self.assertEqual(env["EPI_OUTPUT"], str(self.folder(p) / "main" / "r1" / "dump.output"))
        self.assertEqual(env["EPI_DASHBOARD"], f"http://127.0.0.1:8765/?token=t#task-{p.task}")

    def test_a_repo_command_gets_the_env_and_writes_outputs_too(self):
        cmd = "echo seen=$EPI_STAGE-$GREETING >> $EPI_OUTPUT"
        p = TestWorkflow(self, """
            stages:
              main:
                do:
                  - { run: lint, outputs: [seen], env: { GREETING: hi } }
            """, settings=f"  command:\n    lint: '{cmd}'\n")
        p.drive()
        self.assertEqual(self.records(p)["lint"]["outputs"], {"seen": "main-hi"})
        self.assertEqual(self.records(p)["lint"]["outcome"], "success")

    def test_a_failing_step_has_outcome_failure_and_its_verdict(self):
        p = TestWorkflow(self, """
            stages:
              main:
                do:
                  - run: ok
                verify:
                  - { run: fail-always, continue-on-error: true }
                  - { run: ok2, if: "steps.fail-always.outcome == failure and steps.fail-always.verdict == FAIL" }
            """)
        p.drive()
        self.assertEqual(self.records(p)["fail-always"]["outcome"], "failure")
        self.assertEqual(p.count("ok2"), 1)

    def test_each_round_starts_clean_and_a_step_that_passed_last_round_republishes_as_reused(self):
        p = TestWorkflow(self, """
            stages:
              main:
                do:
                  - run: ok
                verify:
                  - { run: stable, outputs: [v], always_rerun: false }
                  - { run: once, outputs: [w] }
                finish:
                  - { run: fail-once, on_fail: do }
            """, scripts={
            "stable": WRITES % "v=kept\n",
            "once": """
                import json, os, sys
                req = json.load(sys.stdin)
                if not os.path.exists(req["task_dir"] + "/once.ran"):
                    open(req["task_dir"] + "/once.ran", "w").write("1")
                    open(os.environ["EPI_OUTPUT"], "w").write("w=first round only\\n")
                print(json.dumps({"summary": "ok"}))
            """})
        self.assertEqual(p.drive()["action"], "done")
        r1, r2 = self.records(p, r=1), self.records(p, r=2)
        self.assertEqual(r1["stable"]["outputs"], {"v": "kept"})
        self.assertNotIn("reused", r1["stable"])
        self.assertEqual(r2["stable"], {"outcome": "success", "verdict": "PASS", "outputs": {"v": "kept"}, "reused": True})
        self.assertEqual(r1["once"]["outputs"], {"w": "first round only"})
        self.assertEqual(r2["once"]["outputs"], {})                    # ran again, wrote nothing: nothing carried over

    def test_the_drawer_lists_outcome_and_outputs_and_marks_reused(self):
        p = TestWorkflow(self, """
            stages:
              main:
                do:
                  - run: ok
                verify:
                  - { run: stable, outputs: [v], always_rerun: false }
                finish:
                  - { run: fail-once, on_fail: do }
            """, scripts={"stable": WRITES % "v=kept\n"})
        p.drive()
        d = taskview.task(p.cfg, Store(p.cfg).load_task(p.task))
        rounds = d["progress"][0]["rounds"]
        stable = [next(s for s in r["steps"] if s["id"] == "stable") for r in rounds]
        self.assertEqual([(s["outcome"], s["outputs"]) for s in stable],
                         [("success", [{"name": "v", "value": "kept", "reused": False}]),
                          ("success", [{"name": "v", "value": "kept", "reused": True}])])
        self.assertEqual([next(s for s in r["steps"] if s["id"] == "fail-once")["outcome"] for r in rounds], ["failure", "success"])
