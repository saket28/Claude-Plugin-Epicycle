"""workflow.yaml edited by line: add, remove, toggle and move keep comments and the one-step-per-line style."""

import shutil
import unittest

from helpers import make_repo
from engine import config, configedit, taskview, workflow, workflowedit, yamlish
from engine.configedit import EditError

BUILD = {"stage": "build", "phase": "review"}


def steps(text, stage="build", phase="review"):
    return [workflow.step_id(s) for s in yamlish.loads(text)["stages"][stage][phase]]


class WorkflowEditTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo("")
        self.addCleanup(shutil.rmtree, self.repo.parent, ignore_errors=True)
        self.cfg = config.load(self.repo)
        self.text = workflowedit.path(self.cfg).read_text()

    def apply(self, *ops):
        return workflowedit.plan(self.cfg, list(ops))["after"]

    def test_add_a_step_at_the_end_of_its_phase_as_one_line_with_its_if(self):
        out = self.apply({"op": "add", **BUILD, "kind": "skill", "name": "a11y", "if": "touches-ui"})
        self.assertEqual(steps(out)[-1], "a11y")
        self.assertIn('      - { uses: skill/a11y, if: touches-ui }', out)
        self.assertEqual(len(out.splitlines()), len(self.text.splitlines()) + 1)
        for line in self.text.splitlines():                      # nothing else changed: comments, style, every step line
            self.assertIn(line, out.splitlines())

    def test_add_a_step_with_an_id_outputs_and_env_and_toggling_it_keeps_them(self):
        sec = {"op": "add", **BUILD, "kind": "skill", "name": "security-review", "id": "security", "outputs": ["blocking"]}
        notify = {"op": "add", **BUILD, "kind": "run", "name": "lint", "id": "notify", "if": "steps.security.outcome == failure",
                  "env": {"CHANNEL": "#sec-alerts", "FINDINGS": "${{ steps.security.outputs.blocking }}"}}
        out = self.apply(sec, notify)
        self.assertIn("      - { uses: skill/security-review, id: security, outputs: [blocking] }", out)
        self.assertIn('id: notify, if: steps.security.outcome == failure, env: { CHANNEL: "#sec-alerts", FINDINGS: "${{ steps.security.outputs.blocking }}" } }', out)
        step = yamlish.loads(out)["stages"]["build"]["review"][-1]
        self.assertEqual(step["env"], {"CHANNEL": "#sec-alerts", "FINDINGS": "${{ steps.security.outputs.blocking }}"})
        off = workflowedit.apply(out, [{"op": "toggle", **BUILD, "id": "notify", "enabled": False}])
        self.assertIn("}, enabled: false }", off)
        self.assertEqual(workflow.disabled(yamlish.loads(off)), [("build", "review", "notify")])
        self.assertEqual(steps(workflowedit.apply(out, [{"op": "remove", **BUILD, "id": "notify"}]))[-1], "security")

    def test_a_bad_id_outputs_env_or_reference_is_refused(self):
        add = {"op": "add", **BUILD, "kind": "run", "name": "lint"}
        for bad, word in (({"id": "a b"}, "an id is"), ({"outputs": ["a-b"]}, "outputs are names"), ({"outputs": "x"}, "outputs are names"),
                          ({"env": {"A B": "1"}}, "NAME: value"), ({"env": {"EPI_X": "1"}}, "EPI_ names"),
                          ({"env": {"A": "${{ steps.nope.outcome }}"}}, "steps.nope"), ({"if": "steps.tester.outputs.q == 1"}, "output 'q'")):
            with self.assertRaises(EditError, msg=str(bad)) as cm:
                self.apply({**add, **bad})
            self.assertIn(word, str(cm.exception))

    def test_add_a_command_and_a_step_to_a_phase_the_stage_does_not_have(self):
        out = self.apply({"op": "add", "stage": "spec", "phase": "verify", "kind": "run", "name": "lint"})
        self.assertEqual(steps(out, "spec", "verify"), ["lint"])
        self.assertLess(out.index("    do:\n      - { uses: agent/spec-writer"), out.index("    verify:"))

    def test_remove_a_step_and_drop_the_header_of_a_phase_left_empty(self):
        out = self.apply({"op": "remove", "stage": "release", "phase": "finish", "id": "merge"})
        self.assertEqual(steps(out, "release", "finish"), ["cleanup"])
        out = self.apply({"op": "remove", "stage": "release", "phase": "finish", "id": "merge"},
                         {"op": "remove", "stage": "release", "phase": "finish", "id": "cleanup"})
        self.assertNotIn("finish", yamlish.loads(out)["stages"]["release"])

    def test_toggle_a_step_and_a_signoff_off_and_on_again(self):
        off = self.apply({"op": "toggle", **BUILD, "id": "code-review", "enabled": False},
                         {"op": "toggle", "stage": "build", "phase": "signoff", "id": "signoff", "enabled": False})
        self.assertIn("always_rerun: true, enabled: false }", off)
        self.assertIn("with: { via: host }, enabled: false }", off)
        self.assertEqual(len(off.splitlines()), len(self.text.splitlines()))
        wf = yamlish.loads(off)
        self.assertEqual(workflow.disabled(wf), [("build", "review", "code-review"), ("build", "signoff", "signoff")])
        again = workflowedit.apply(off, [{"op": "toggle", **BUILD, "id": "code-review", "enabled": True},
                                         {"op": "toggle", "stage": "build", "phase": "signoff", "id": "signoff", "enabled": True},
                                         {"op": "toggle", **BUILD, "id": "tester", "enabled": False},
                                         {"op": "toggle", **BUILD, "id": "tester", "enabled": False}])
        self.assertEqual(again.count(", enabled: false }"), 1)                 # never written twice
        self.assertEqual(workflow.disabled(yamlish.loads(again)), [("build", "review", "tester")])

    def test_move_a_step_swaps_two_lines_and_cannot_leave_its_phase(self):
        out = self.apply({"op": "move", **BUILD, "id": "code-review", "by": -1})
        self.assertEqual(steps(out), ["code-review", "tester", "security-review"])
        out = self.apply({"op": "move", **BUILD, "id": "tester", "by": 1})
        self.assertEqual(steps(out), ["code-review", "tester", "security-review"])
        with self.assertRaisesRegex(EditError, "already there"):
            self.apply({"op": "move", **BUILD, "id": "tester", "by": -1})

    def test_comments_survive(self):
        text = self.text.replace("    review:\n", "    review:\n      # keep this note\n", 1)
        out = workflowedit.apply(text, [{"op": "move", **BUILD, "id": "code-review", "by": -1},
                                        {"op": "add", **BUILD, "kind": "agent", "name": "builder"}])
        self.assertIn("      # keep this note\n", out)
        self.assertTrue(out.startswith("# The SDLC workflow"))

    def test_an_invalid_result_is_rejected(self):
        for op, msg in (({"op": "add", **BUILD, "kind": "agent", "name": "nobody"}, "has no file"),
                        ({"op": "add", **BUILD, "kind": "run", "name": "frobnicate"}, "neither a command"),
                        ({"op": "add", **BUILD, "kind": "skill", "name": "x", "if": "banana"}, "if:"),
                        ({"op": "add", **BUILD, "kind": "skill", "name": "code-review"}, "used twice"),
                        ({"op": "add", **BUILD, "kind": "skill", "name": "bad name"}, "name of an agent")):
            with self.assertRaisesRegex(EditError, msg):
                self.apply(op)
        with self.assertRaisesRegex(EditError, "no step 'ghost'"):
            self.apply({"op": "remove", **BUILD, "id": "ghost"})
        with self.assertRaisesRegex(EditError, "not removed"):
            self.apply({"op": "remove", "stage": "build", "phase": "signoff", "id": "signoff"})
        with self.assertRaisesRegex(EditError, "no stage"):
            self.apply({"op": "add", "stage": "nope", "phase": "do", "kind": "skill", "name": "x"})
        with self.assertRaisesRegex(EditError, "operation must be"):
            self.apply({"op": "explode"})

    def test_save_writes_the_file_and_the_config_loads_with_it(self):
        h = workflowedit.hash_of(self.cfg)
        new = workflowedit.save(self.cfg, [{"op": "add", **BUILD, "kind": "skill", "name": "a11y"}], h)
        self.assertNotEqual(new, h)
        self.assertEqual(steps(workflowedit.path(self.cfg).read_text())[-1], "a11y")
        self.assertIn("a11y", config.load(self.repo).data["agent"])

    def test_a_stale_hash_is_rejected_and_nothing_is_written(self):
        h = workflowedit.hash_of(self.cfg)
        p = workflowedit.path(self.cfg)
        p.write_text(self.text + "# someone else was here\n")
        with self.assertRaisesRegex(EditError, "changed on disk"):
            workflowedit.save(self.cfg, [{"op": "toggle", **BUILD, "id": "tester", "enabled": False}], h)
        self.assertEqual(p.read_text(), self.text + "# someone else was here\n")

    def test_no_workflow_file_of_its_own_says_what_to_do(self):
        workflowedit.path(self.cfg).unlink()
        with self.assertRaisesRegex(EditError, "epi install"):
            workflowedit.plan(self.cfg, [])


class RulesEditTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo("---\nstage:\n  build: { rounds: 4 }\n---\n\n## Pipeline rules\n- Keep it small.\n")
        self.addCleanup(shutil.rmtree, self.repo.parent, ignore_errors=True)
        self.cfg = config.load(self.repo)

    def test_set_body_replaces_only_the_body(self):
        before = configedit.paths(self.cfg)["pack"].read_text()
        after = configedit.set_body(before, "- One.\n- Two.\n")
        self.assertTrue(after.startswith("---\nstage:"))
        self.assertTrue(after.endswith("\n- One.\n- Two.\n"))
        self.assertEqual(configedit.set_body(before, ""), "---\nstage:\n  build: { rounds: 4 }\n---\n")
        self.assertEqual(configedit.set_body("", "x"), "---\n---\n\nx\n")

    def test_save_rules_is_hash_checked_and_the_agents_see_it(self):
        h = configedit.hashes(self.cfg)
        configedit.save_rules(self.cfg, "- Never push on Fridays.", h["pack"])
        cfg = config.load(self.repo)
        self.assertEqual(cfg.rules.strip(), "- Never push on Fridays.")
        self.assertEqual(cfg.v("stage.build.rounds"), 4)
        with self.assertRaisesRegex(EditError, "changed on disk"):
            configedit.save_rules(cfg, "- Again.", h["pack"])                 # the old hash


class WhoLabelTest(unittest.TestCase):
    def test_a_token_row_names_the_step_not_the_agent_file(self):
        repo = make_repo("")
        self.addCleanup(shutil.rmtree, repo.parent, ignore_errors=True)
        cfg = config.load(repo)
        for agent, want in (("sdlc-builder", "Builder"), ("epi:builder", "Builder"), ("sdlc-spec-writer", "Spec writer"),
                            ("headless:code-review", "Code review"), ("orchestrator", "Orchestrator")):
            self.assertEqual(taskview.who_label(cfg, agent), want, agent)
        rows = taskview.cost_rows(cfg, [{"type": "agent", "agent": "sdlc-builder", "data": {"tokens": 5}}])
        self.assertEqual(rows[0]["who"], "Builder")


if __name__ == "__main__":
    unittest.main()
