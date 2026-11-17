"""The workflow validator, and the step machine on small workflows of its own (a test workflow)."""
import json
import re
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

from helpers import make_repo
from engine import config, machine, schema, workflow, yamlish
from engine.store import Store

HEAD = "name: t\nworkspace: none\non:\n  manual:\n    inputs:\n      request: string\n"


def parse(text):
    return yamlish.loads(textwrap.dedent(text))


def problems(text, **kw):
    return workflow.validate(parse(text), "sdlc", commands=kw.get("commands", ("test",)))


class ValidatorTest(unittest.TestCase):
    def has(self, text, word):
        errs = problems(HEAD + textwrap.dedent(text))
        self.assertTrue(any(word in e for e in errs), f"{word!r} not in {errs}")

    def test_the_sdlc_pack_is_valid(self):
        wf = workflow.load("sdlc", config.command_names("sdlc", {}))
        self.assertEqual(list(wf["stages"]), ["spec", "build", "release"])
        self.assertEqual(wf["on"], {"manual": {"inputs": {"request": "string"}}})

    def test_on_stays_a_string_key_in_yaml(self):
        self.assertEqual(list(yamlish.loads("on:\n  manual: {}\nif: x")), ["on", "if"])

    def test_only_the_manual_trigger_is_supported(self):
        for trigger in ("schedule", "board", "event"):
            errs = problems(f"name: t\nworkspace: none\non:\n  {trigger}: {{}}\nstages:\n  a:\n    do:\n      - run: test\n")
            self.assertTrue(any(f"on.{trigger} is not supported yet" in e for e in errs), errs)
        self.assertTrue(any("unknown trigger" in e for e in problems("name: t\nworkspace: none\non:\n  push: {}\nstages:\n  a:\n    do:\n      - run: test\n")))

    def test_a_stage_with_nothing_in_it_is_an_error(self):
        self.has("stages:\n  a:\n    name: A\n", "every phase is empty")

    def test_the_five_kinds_are_known(self):
        self.has("stages:\n  a:\n    do:\n      - uses: robot/x\n", "`uses` must be")
        self.has("stages:\n  a:\n    do:\n      - { uses: agent/x, run: test }\n", "exactly one of")
        self.has("stages:\n  a:\n    do:\n      - run: nope\n", "neither a command")
        self.has("stages:\n  a:\n    do:\n      - { uses: agent/x, colour: red }\n", "unknown key 'colour'")

    def test_option_placement(self):
        self.has("stages:\n  a:\n    do:\n      - { uses: agent/x, always_rerun: true }\n", "verify and review steps")
        self.has("stages:\n  a:\n    do:\n      - { uses: agent/x, on_fail: do }\n", "finish steps")
        self.has("stages:\n  a:\n    review:\n      - { uses: agent/x, timeout-minutes: 5 }\n", "run and wait")
        self.has("stages:\n  a:\n    do:\n      - { uses: agent/x }\n    finish:\n      - { run: test, on_fail: sometimes }\n", "retry | do")
        self.has("stages:\n  a:\n    review:\n      - { run: test }\n    finish:\n      - { run: test, on_fail: do }\n", "needs do step")

    def test_targets_must_exist(self):
        self.has("stages:\n  a:\n    do:\n      - uses: agent/x\n    send_back_to: zzz\n", "unknown key 'send_back_to'")
        self.has("stages:\n  a:\n    do:\n      - { uses: agent/x, if: 'stages.zzz.steps.x.outputs.risk >= low' }\n", "stage 'zzz', which does not exist")
        self.has("facts: zzz\nstages:\n  a:\n    do:\n      - uses: agent/x\n", "not a stage")
        self.has("stages:\n  a:\n    review:\n      - { uses: human/m, with: { via: carrier } }\n", "via-carrier")

    def test_rules_and_ids(self):
        self.has("stages:\n  a:\n    do:\n      - { uses: agent/x, if: 'whenever' }\n", "unknown term")
        self.has("stages:\n  a:\n    do:\n      - uses: agent/x\n    review:\n      - uses: agent/x\n", "used twice")
        errs = problems(HEAD + "stages:\n  a:\n    do:\n      - uses: agent/x\n    review:\n      - { uses: agent/x, id: x2 }\n")
        self.assertEqual(errs, [])                                                  # an `id` tells them apart

    def test_enabled_false_leaves_a_step_or_a_signoff_out(self):
        text = (workflow.WORKFLOWS / "sdlc" / "workflow.yaml").read_text()
        text = text.replace("- { run: lint,", "- { enabled: false, run: lint,")
        text = text.replace("signoff: { by: approver, name: Sign-off, doing: Waiting for your sign-off }",
                            "signoff: { by: approver, name: Sign-off, doing: Waiting for your sign-off, enabled: false }")
        wf = yamlish.loads(text)
        self.assertEqual(workflow.validate(wf, "sdlc", config.command_names("sdlc", {})), [])
        eff = workflow.effective(wf)
        self.assertEqual(eff["spec"]["signoff"], [])
        self.assertEqual([(s["_id"], s["uses"]) for s in eff["build"]["signoff"]], [("signoff", "human/approver")])
        self.assertEqual([s["_id"] for s in eff["build"]["verify"]], ["build", "test"])
        self.assertEqual(workflow.disabled(wf), [("spec", "signoff", "signoff"), ("build", "verify", "lint")])
        bad = yamlish.loads(text.replace("enabled: false, run: lint", "enabled: maybe, run: lint"))
        self.assertTrue(any("`enabled` is true or false" in e for e in workflow.validate(bad, "sdlc", config.command_names("sdlc", {}))))

    def test_a_workflow_has_a_version_that_looks_like_one(self):
        self.has("version: 1.0\nstages:\n  a:\n    do:\n      - uses: agent/x\n", "version must be x.y.z")
        self.assertEqual(problems(HEAD + "version: 1.2.3\nstages:\n  a:\n    do:\n      - uses: agent/x\n"), [])
        self.assertRegex(yamlish.loads((workflow.WORKFLOWS / "sdlc" / "workflow.yaml").read_text())["version"], r"^\d+\.\d+\.\d+$")

    def test_the_shipped_pack_keeps_one_step_per_line_in_flow_style(self):
        """A tool edits workflow.yaml line by line; a step written as a block would break that."""
        text = (workflow.WORKFLOWS / "sdlc" / "workflow.yaml").read_text()
        for n, line in enumerate(text.splitlines(), 1):
            if re.match(r"\s+- ", line):
                self.assertRegex(line, r"^\s+- \{ .* \}$", f"line {n}")
            if re.match(r"\s+signoff:", line):
                self.assertRegex(line, r"^\s+signoff: \{ .* \}$", f"line {n}")


class SignoffShapeTest(unittest.TestCase):
    def wf(self, signoff):
        return {"name": "w", "workspace": "none", "on": {"manual": None},
                "stages": {"a": {"do": [{"run": "x"}], "signoff": signoff}}}

    def test_a_signoff_names_a_role_and_takes_only_its_keys(self):
        self.assertEqual(workflow.validate(self.wf({"by": "approver", "name": "Sign-off"}), "w", ("x",)), [])
        self.assertTrue(any("naming the role" in e for e in workflow.validate(self.wf("approver"), "w", ("x",))))
        self.assertTrue(any("unknown key 'when'" in e for e in workflow.validate(self.wf({"by": "a", "when": 1}), "w", ("x",))))
        self.assertTrue(any("via-host" in e for e in workflow.validate(self.wf({"by": "a", "with": {"via": "host"}}), "w", ("x",))))


class TestWorkflow:
    """A throwaway workflow: a workflow and a few scripts, swapped in for the real workflows folder."""

    OK = "import json,sys\njson.load(sys.stdin)\nprint(json.dumps({'summary': 'ok'}))\n"
    COUNTED = textwrap.dedent("""
        import json, sys, pathlib
        req = json.load(sys.stdin)
        f = pathlib.Path(req["task_dir"]) / (req["step"] + ".count")
        n = int(f.read_text()) + 1 if f.exists() else 1
        f.write_text(str(n))
        if n <= FAIL_UNTIL:
            print(json.dumps({"summary": "failed try %d" % n})); sys.exit(1)
        print(json.dumps({"summary": "ok"}))
        """)

    def __init__(self, test, stages, settings="", scripts=None):
        self.root = Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        wf = self.root / "workflows" / "tflow"
        (wf / "scripts").mkdir(parents=True)
        (wf / "workflow.yaml").write_text(HEAD + textwrap.dedent(stages))
        (wf / "settings.schema.json").write_text(json.dumps({"tabs": []}))
        for name, until in (("ok", 0), ("ok2", 0), ("fail_once", 1), ("fail_always", 99)):
            (wf / "scripts" / f"{name}.py").write_text("import json\n" + self.COUNTED.replace("FAIL_UNTIL", str(until)))
        for name, source in (scripts or {}).items():
            (wf / "scripts" / f"{name.replace('-', '_')}.py").write_text(textwrap.dedent(source))
        self.old = (workflow.WORKFLOWS, schema.PLUGIN_ROOT)
        workflow.WORKFLOWS, schema.PLUGIN_ROOT = self.root / "workflows", self.root
        test.addCleanup(self.restore)
        repo = make_repo("---\nglobal:\n  workflow: {name: tflow}\n" + settings + "---\n", pack=None)
        test.addCleanup(shutil.rmtree, repo.parent, ignore_errors=True)
        self.cfg = config.load(repo)
        self.task = Store(self.cfg).create_task("go")["id"]

    def restore(self):
        workflow.WORKFLOWS, schema.PLUGIN_ROOT = self.old

    def drive(self):
        return machine.Machine(self.cfg, self.task).next_action()

    def count(self, step):
        f = Store(self.cfg).task_dir(self.task) / f"{step}.count"
        return int(f.read_text()) if f.exists() else 0

    def events(self, kind):
        return [e for e in Store(self.cfg).events(self.task) if e["type"] == kind]


class MachineOnAWorkflowTest(unittest.TestCase):
    def test_finish_on_fail_act_runs_the_act_steps_again_then_finishs_again(self):
        p = TestWorkflow(self, """
            stages:
              main:
                do:
                  - run: ok
                finish:
                  - run: fail-once
                    on_fail: do
            """)
        self.assertEqual(p.drive()["action"], "done")
        self.assertEqual((p.count("ok"), p.count("fail-once")), (2, 2))
        self.assertEqual([(e["stage"], e["r"]) for e in p.events("round")], [("main", 1), ("main", 2)])

    def test_finish_on_fail_act_is_bounded_by_rounds(self):
        p = TestWorkflow(self, """
            stages:
              main:
                do:
                  - run: ok
                finish:
                  - run: fail-always
                    on_fail: do
            """)
        act = p.drive()
        self.assertEqual(act["action"], "wait")
        self.assertIn("disagreement", act["reason"])
        self.assertEqual(p.count("ok"), 3)                                   # stage.main.rounds defaults to 3

    def test_finish_default_retries_once_then_asks_a_human(self):
        p = TestWorkflow(self, "stages:\n  main:\n    do:\n      - run: ok\n    finish:\n      - run: fail-always\n")
        act = p.drive()
        self.assertIn("finish", act["reason"])
        self.assertEqual(p.count("fail-always"), 2)

    def test_finish_on_fail_human_asks_at_once(self):
        p = TestWorkflow(self, "stages:\n  main:\n    do:\n      - run: ok\n    finish:\n      - { run: fail-always, on_fail: human }\n")
        self.assertIn("finish", p.drive()["reason"])
        self.assertEqual(p.count("fail-always"), 1)

    def test_a_stage_with_no_do_agent_whose_check_fails_needs_you(self):
        p = TestWorkflow(self, "stages:\n  main:\n    verify:\n      - run: fail-always\n")
        self.assertEqual(p.drive()["reason"], "needs-you: failure")

    def test_plan_runs_once_before_act_and_a_wait_step_returns_a_wait_action(self):
        p = TestWorkflow(self, """
            stages:
              main:
                plan:
                  - run: ok
                do:
                  - run: ok2
                review:
                  - uses: wait/deploy
            """)
        act = p.drive()
        self.assertEqual((act["action"], act["reason"]), ("wait", "waiting for deploy"))
        self.assertEqual((p.count("ok"), p.count("ok2")), (1, 1))
        self.assertEqual(p.drive()["action"], "wait")                       # still waiting, nothing ran again
        self.assertEqual((p.count("ok"), p.count("ok2")), (1, 1))

    def test_if_reads_the_verdict_of_a_step_above(self):
        for rule, runs in (("steps.ok.verdict == PASS", True), ("steps.ok.verdict == FAIL", False)):
            p = TestWorkflow(self, f"""
                stages:
                  main:
                    do:
                      - run: ok
                    review:
                      - run: fail-always
                        if: "{rule}"
                """)
            p.drive()
            self.assertEqual(p.count("fail-always") > 0, runs, rule)


if __name__ == "__main__":
    unittest.main()
