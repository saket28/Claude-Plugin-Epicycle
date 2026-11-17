"""Packs installed into the repo: the layout, three-way upgrades, where settings live, who writes what."""

import io
import re
import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from helpers import ROOT, make_repo, sh
from engine import cli, config, configedit, learn, pack, workflow
from engine.store import Store

WF = ".claude/workflows/sdlc"
CUR = re.search(r"^version: (\S+)", (workflow.WORKFLOWS / "sdlc" / "workflow.yaml").read_text(), re.M).group(1)
NEXT = ".".join([*CUR.split(".")[:-1], str(int(CUR.split(".")[-1]) + 1)])       # the shipped pack's next version


def run(argv, cwd):
    buf = io.StringIO()
    with mock.patch("engine.config.main_checkout", return_value=Path(cwd)), redirect_stdout(buf):
        try:
            cli.main(argv)
        except SystemExit:
            pass
    return buf.getvalue()


class PackTestCase(unittest.TestCase):
    """A repo with the pack installed, and a copy of the plugin that a test may change (the plugin's next version)."""

    def setUp(self):
        self.plugin = Path(tempfile.mkdtemp(prefix="epi-plugin-"))
        for d in ("workflows", "agents", ".claude-plugin"):
            shutil.copytree(ROOT / d, self.plugin / d, ignore=shutil.ignore_patterns("__pycache__"))
        old = (workflow.WORKFLOWS, config.PLUGIN_ROOT)
        workflow.WORKFLOWS, config.PLUGIN_ROOT = self.plugin / "workflows", self.plugin
        self.addCleanup(lambda: setattr(workflow, "WORKFLOWS", old[0]) or setattr(config, "PLUGIN_ROOT", old[1]))
        self.addCleanup(shutil.rmtree, self.plugin, ignore_errors=True)
        self.repo = make_repo("", pack=None)
        self.addCleanup(shutil.rmtree, self.repo.parent, ignore_errors=True)
        shutil.rmtree(self.repo / ".claude" / "workflows")               # make_repo wrote empty seeds: install brings its own
        pack.install(self.repo, "sdlc")

    def plugin_edit(self, rel, old, new):
        p = self.plugin / "workflows" / "sdlc" / rel
        text = p.read_text()
        self.assertIn(old, text)
        p.write_text(text.replace(old, new, 1))

    def mine(self, rel, old=None, new=None):
        p = self.repo / WF / rel
        if old is None:
            return p
        p.write_text(p.read_text().replace(old, new, 1))
        return p


class InstallTest(PackTestCase):
    def test_install_writes_the_pack_its_agents_and_a_pristine_base(self):
        r = self.repo
        for f in ("workflow.yaml", "settings.schema.json", "scripts/_lib.py", "scripts/merge.py", "settings.md", "learnings.md"):
            self.assertTrue((r / WF / f).exists(), f)
        for name in ("spec-writer", "builder", "tester"):
            text = (r / ".claude" / "agents" / f"sdlc-{name}.md").read_text()
            self.assertIn(f"\nname: sdlc-{name}\n", text)
        man = json.loads((r / WF / ".base" / "pack.json").read_text())
        self.assertEqual((man["name"], man["version"]), ("sdlc", CUR))
        self.assertEqual(man["files"]["agents/builder.md"], ".claude/agents/sdlc-builder.md")
        self.assertEqual(man["files"]["scripts/merge.py"], f"{WF}/scripts/merge.py")
        for key, rel in man["files"].items():
            self.assertEqual((r / WF / ".base" / key).read_text(), (r / rel).read_text(), key)
        self.assertNotIn("settings.md", man["files"])             # the repo's own: never upgraded

    def test_install_refuses_a_second_time_and_force_keeps_the_repos_settings(self):
        (self.repo / WF / "settings.md").write_text("---\nstage: { build: { rounds: 5 } }\n---\n")
        self.mine("scripts/merge.py", "import", "import os, ")
        with self.assertRaisesRegex(pack.PackError, "already installed"):
            pack.install(self.repo, "sdlc")
        res = pack.install(self.repo, "sdlc", force=True)
        self.assertNotIn("import os, ", (self.repo / WF / "scripts/merge.py").read_text())
        self.assertIn("rounds: 5", (self.repo / WF / "settings.md").read_text())
        self.assertIn(f"{WF}/settings.md", res["kept"])

    def test_an_unknown_pack_says_which_exist(self):
        with self.assertRaisesRegex(pack.PackError, "playbooks: sdlc"):
            pack.install(self.repo, "nope")

    def test_the_cli_installs_and_prints_what_it_wrote(self):
        shutil.rmtree(self.repo / WF)
        out = json.loads(run(["install", "sdlc"], self.repo))
        self.assertEqual(out["installed"], "sdlc")
        self.assertIn(f"{WF}/workflow.yaml", out["wrote"])
        self.assertIn(".claude/agents/sdlc-builder.md", out["wrote"])
        self.assertIn("already installed", run(["install", "sdlc"], self.repo))


SEC = "- { uses: skill/security-review,"
SEC_IF = 'if: "stages.spec.steps.spec-writer.outputs.risk >= medium"'


class WorkflowMergeTest(PackTestCase):
    """workflow.yaml is merged by step and field, not by line."""

    def setUp(self):
        super().setUp()
        self.plugin_edit("workflow.yaml", f"version: {CUR}", f"version: {NEXT}")
        for a in ("marketing", "ux"):
            (self.repo / ".claude/agents" / f"{a}.md").write_text(f"---\nname: {a}\n---\nReview.\n")

    def wf(self):
        return (self.repo / WF / "workflow.yaml").read_text()

    def steps(self, sid, phase):
        return [workflow.step_id(s) for s in config.load(self.repo).stages[sid][phase]]

    def conflicts(self, res):
        self.assertFalse(res["upgraded"], res)
        return res["conflicts"]

    def test_adjacent_edits_merge_the_agentifi_case(self):
        self.plugin_edit("workflow.yaml", "outputs: [risk] }", "outputs: [risk, files] }")
        self.plugin_edit("workflow.yaml", SEC_IF, 'if: "stages.spec.steps.spec-writer.outputs.risk >= high"')
        self.mine("workflow.yaml", "doing: Waiting for your sign-off }", "doing: Waiting for your sign-off, enabled: false }")
        text = self.mine("workflow.yaml").read_text()
        text = text.replace("    finalize:", "      - { uses: agent/marketing, if: \"paths:[src/content/**]\" }\n"
                            "      - { uses: agent/ux, if: \"touches-ui or paths:[**/*.tsx]\" }\n    finalize:", 1)
        self.mine("workflow.yaml").write_text(text)
        res = pack.upgrade(self.repo, "sdlc")
        self.assertTrue(res["upgraded"], res)
        self.assertEqual({f["file"]: f["action"] for f in res["files"]}[f"{WF}/workflow.yaml"], "merge")
        out = self.wf()
        self.assertIn("outputs: [risk, files] }", out)
        self.assertIn("doing: Waiting for your sign-off, enabled: false }", out)
        self.assertIn("outputs.risk >= high", out)
        self.assertEqual(self.steps("build", "review")[-3:], ["security-review", "marketing", "ux"])
        self.assertEqual(self.steps("build", "review")[:2], ["tester", "code-review"])
        self.assertIn(f"version: {NEXT}", out)

    def test_the_same_field_changed_differently_on_both_sides_is_a_conflict_and_nothing_is_written(self):
        self.plugin_edit("workflow.yaml", "doing: Running the tests }", "doing: Testing }")
        self.mine("workflow.yaml", "doing: Running the tests }", "doing: Go }")
        before = {p: p.read_bytes() for p in (self.repo / WF).rglob("*") if p.is_file()}
        res = pack.upgrade(self.repo, "sdlc")
        c = self.conflicts(res)
        self.assertTrue(all(x["field"] == "doing" and x["file"] == f"{WF}/workflow.yaml" for x in c))
        self.assertEqual({(x["stage"], x["phase"], x["step"]) for x in c}, {("build", "verify", "test"), ("release", "verify", "test")}
                         if len(c) == 2 else {("build", "verify", "test")})
        self.assertEqual(before, {p: p.read_bytes() for p in (self.repo / WF).rglob("*") if p.is_file()})

    def test_a_step_the_repo_removed_and_the_plugin_changed_is_a_conflict(self):
        self.plugin_edit("workflow.yaml", "doing: Opening the pull request }", "doing: Opening the PR }")
        self.mine("workflow.yaml", "      - { run: open-pr, name: Pull request, doing: Opening the pull request }\n", "")
        c = self.conflicts(pack.upgrade(self.repo, "sdlc"))
        self.assertEqual((c[0]["stage"], c[0]["phase"], c[0]["step"]), ("build", "finalize", "open-pr"))
        self.assertIn("the repo removed it, the plugin changed it", c[0]["text"])

    def test_a_step_the_repo_removed_and_the_plugin_left_alone_stays_removed(self):
        self.mine("workflow.yaml", "      - { run: cleanup, name: Clean up, doing: Cleaning up }\n", "")
        pack.upgrade(self.repo, "sdlc")
        self.assertEqual(self.steps("release", "finish"), ["merge"])

    def test_a_step_added_by_the_plugin_and_another_added_by_the_repo_in_one_phase_are_both_kept(self):
        self.plugin_edit("workflow.yaml", "      - { run: lint,", "      - { run: test, id: smoke, name: Smoke }\n      - { run: lint,")
        self.mine("workflow.yaml", "      - { run: build, name: Build check", "      - { run: test, id: mine, name: Mine }\n"
                  "      - { run: build, name: Build check")
        res = pack.upgrade(self.repo, "sdlc")
        self.assertTrue(res["upgraded"], res)
        # the plugin's order, the repo's step after the one it followed (lint)
        self.assertEqual(self.steps("build", "verify"), ["smoke", "lint", "mine", "build", "test"])

    def test_a_step_added_by_the_repo_first_in_its_phase_stays_first(self):
        self.mine("workflow.yaml", "      - { run: lint,", "      - { run: test, id: mine, name: Mine }\n      - { run: lint,")
        self.plugin_edit("workflow.yaml", "doing: Linting }", "doing: Checking style }")
        pack.upgrade(self.repo, "sdlc")
        self.assertEqual(self.steps("build", "verify"), ["mine", "lint", "build", "test"])
        self.assertIn("doing: Checking style }", self.wf())

    def test_a_reorder_by_the_repo_alone_is_kept_and_a_reorder_by_both_takes_the_plugin_order(self):
        lint = "      - { run: lint, name: Lint, doing: Linting }\n"
        test = "      - { run: test, name: Tests, doing: Running the tests }\n"
        self.mine("workflow.yaml", lint, "")
        text = self.mine("workflow.yaml").read_text().replace(test, test + lint, 1)
        self.mine("workflow.yaml").write_text(text)
        self.plugin_edit("workflow.yaml", "      - { run: lint,", "      - { run: test, id: smoke, name: Smoke }\n      - { run: lint,")
        res = pack.upgrade(self.repo, "sdlc", dry_run=True)
        self.assertEqual(self.steps("build", "verify"), ["build", "test", "lint"])      # before: dry-run writes nothing
        res = pack.upgrade(self.repo, "sdlc")
        self.assertEqual(self.steps("build", "verify"), ["smoke", "build", "test", "lint"])
        self.assertTrue(any("kept the repo's order" in n for n in res["notes"]))

    def test_a_comment_of_the_repo_between_steps_is_reported_not_lost_silently(self):
        self.mine("workflow.yaml", "      - { run: lint,", "      # lint first, ask Sam\n      - { run: lint,")
        self.mine("workflow.yaml", "doing: Linting }", "doing: Linting } # trailing")
        self.plugin_edit("workflow.yaml", "doing: Building }", "doing: Compiling }")
        res = pack.upgrade(self.repo, "sdlc")
        got = {c["comment"]: c for c in res["dropped_comments"]}
        self.assertEqual(set(got), {"# lint first, ask Sam", "# trailing"})
        self.assertEqual(got["# lint first, ask Sam"]["source"], "repo")
        self.assertIn("run: lint", got["# lint first, ask Sam"]["near"])
        self.assertNotIn("ask Sam", self.wf())

    def test_the_merged_file_keeps_the_shipped_style_and_validates(self):
        self.plugin_edit("workflow.yaml", "doing: Building }", "doing: Compiling }")
        self.mine("workflow.yaml", "doing: Waiting for your sign-off }", "doing: Waiting for your sign-off, enabled: false }")
        pack.upgrade(self.repo, "sdlc")
        text = self.wf()
        self.assertEqual(text.splitlines()[:8], (workflow.WORKFLOWS / "sdlc" / "workflow.yaml").read_text().splitlines()[:8])
        for line in text.splitlines():
            if line.lstrip().startswith("- "):
                self.assertRegex(line, r"^      - \{ .* \}$")                  # one step per line
        self.assertEqual(workflow.validate(workflow.yamlish.loads(text), "sdlc", config.command_names("sdlc", {})), [])
        config.load(self.repo)

    def test_a_merge_that_makes_the_workflow_invalid_is_a_conflict(self):
        self.plugin_edit("workflow.yaml", "- { run: ci, name: CI,", "- { run: ci, id: gate, name: CI,")
        self.mine("workflow.yaml", "      - { run: cleanup, name: Clean up, doing: Cleaning up }\n",
                  "      - { run: cleanup, name: Clean up, doing: Cleaning up }\n      - { uses: agent/ghost }\n")
        c = self.conflicts(pack.upgrade(self.repo, "sdlc"))
        self.assertIn("invalid", c[0]["text"])
        self.assertIn("ghost", c[0]["text"])
        self.assertNotIn("id: gate", self.wf())


class UpgradeTest(PackTestCase):
    def bump(self):
        self.plugin_edit("workflow.yaml", f"version: {CUR}", f"version: {NEXT}")

    def test_a_file_the_repo_never_changed_is_replaced(self):
        self.bump()
        self.plugin_edit("scripts/ci.py", "import", "import os, ")
        res = pack.upgrade(self.repo, "sdlc")
        self.assertEqual((res["from"], res["to"], res["upgraded"]), (CUR, NEXT, True))
        self.assertIn("import os, ", (self.repo / WF / "scripts/ci.py").read_text())
        self.assertEqual(json.loads((self.repo / WF / ".base/pack.json").read_text())["version"], NEXT)
        self.assertIn("import os, ", (self.repo / WF / ".base/scripts/ci.py").read_text())

    def test_a_file_only_the_repo_changed_is_kept(self):
        self.bump()
        self.mine("scripts/merge.py", "import", "import os, ")
        res = pack.upgrade(self.repo, "sdlc")
        self.assertEqual({f["file"]: f["action"] for f in res["files"]}[f"{WF}/scripts/merge.py"], "keep")
        self.assertIn("import os, ", (self.repo / WF / "scripts/merge.py").read_text())
        self.assertNotIn("import os, ", (self.repo / WF / ".base/scripts/merge.py").read_text())   # base is the plugin's

    def test_a_file_both_changed_is_merged_when_the_changes_do_not_meet(self):
        self.bump()
        self.plugin_edit("workflow.yaml", "- { run: cleanup, name: Clean up,", "- { run: cleanup, id: tidy, name: Clean up,")
        self.mine("workflow.yaml", "- { uses: agent/spec-writer, name: Spec writer,",
                  "- { uses: agent/spec-writer, enabled: false, name: Spec writer,")
        res = pack.upgrade(self.repo, "sdlc")
        text = (self.repo / WF / "workflow.yaml").read_text()
        self.assertTrue(res["upgraded"] and "enabled: false" in text and "id: tidy" in text and f"version: {NEXT}" in text)
        self.assertEqual({f["file"]: f["action"] for f in res["files"]}[f"{WF}/workflow.yaml"], "merge")
        config.load(self.repo)                                     # and it is still a valid workflow

    def test_a_conflict_writes_nothing_for_any_file_and_reports_the_text(self):
        self.bump()
        self.plugin_edit("scripts/ci.py", "import", "import os, ")                     # would merge fine alone
        self.plugin_edit("workflow.yaml", "name: Spec writer, doing: Writing the spec", "name: Spec writer, doing: Drafting")
        self.mine("workflow.yaml", "name: Spec writer, doing: Writing the spec", "name: Spec writer, doing: Thinking")
        before = {p: p.read_bytes() for p in (self.repo / WF).rglob("*") if p.is_file()}
        res = pack.upgrade(self.repo, "sdlc")
        self.assertFalse(res["upgraded"])
        self.assertEqual([c["file"] for c in res["conflicts"]], [f"{WF}/workflow.yaml"])
        c = res["conflicts"][0]
        self.assertEqual((c["stage"], c["phase"], c["step"], c["field"]), ("spec", "do", "spec-writer", "doing"))
        self.assertEqual((c["base"], c["repo"], c["plugin"]), ("Writing the spec", "Thinking", "Drafting"))
        self.assertIn("spec/do/spec-writer: field 'doing' changed on both sides", c["text"])
        self.assertEqual(before, {p: p.read_bytes() for p in (self.repo / WF).rglob("*") if p.is_file()})
        self.assertNotIn("import os, ", (self.repo / WF / "scripts/ci.py").read_text())       # all or nothing

    def test_the_cli_exits_with_an_error_on_conflict(self):
        self.plugin_edit("workflow.yaml", "doing: Writing the spec", "doing: Drafting")
        self.mine("workflow.yaml", "doing: Writing the spec", "doing: Thinking")
        out = json.loads(run(["upgrade", "sdlc"], self.repo))
        self.assertTrue(out["conflicts"] and out["upgraded"] is False)

    def test_dry_run_prints_the_plan_and_diffs_and_writes_nothing(self):
        self.bump()
        self.plugin_edit("scripts/ci.py", "import", "import os, ")
        before = {p: p.read_bytes() for p in (self.repo / WF).rglob("*") if p.is_file()}
        res = pack.upgrade(self.repo, "sdlc", dry_run=True)
        self.assertTrue(res["dry_run"] and not res["upgraded"])
        self.assertIn("+import os, ", res["diffs"][f"{WF}/scripts/ci.py"])
        self.assertEqual({f["action"] for f in res["files"] if f["file"].endswith("merge.py")}, {"same"})
        self.assertEqual(before, {p: p.read_bytes() for p in (self.repo / WF).rglob("*") if p.is_file()})
        out = json.loads(run(["upgrade", "sdlc", "--dry-run"], self.repo))
        self.assertIn("diffs", out)

    def test_new_files_are_added_and_removed_ones_are_reported_when_the_repo_changed_them(self):
        (self.plugin / "workflows/sdlc/scripts/extra.py").write_text("print(1)\n")
        (self.plugin / "workflows/sdlc/scripts/cleanup.py").unlink()
        self.mine("scripts/ci.py", "import", "import os, ")
        (self.plugin / "workflows/sdlc/scripts/ci.py").unlink()
        res = pack.upgrade(self.repo, "sdlc")
        self.assertEqual((self.repo / WF / "scripts/extra.py").read_text(), "print(1)\n")
        self.assertFalse((self.repo / WF / "scripts/cleanup.py").exists())              # untouched: removed with the pack's
        self.assertTrue((self.repo / WF / "scripts/ci.py").exists())                    # the repo changed it: kept
        self.assertEqual(res["removed_from_pack"], [f"{WF}/scripts/ci.py"])
        self.assertNotIn("scripts/ci.py", json.loads((self.repo / WF / ".base/pack.json").read_text())["files"])

    def test_a_second_upgrade_has_nothing_to_do_and_settings_and_learnings_are_never_touched(self):
        self.bump()
        (self.repo / WF / "learnings.md").write_text("## Learnings\n- [L-1] all · mine\n")
        (self.repo / WF / "settings.md").write_text("---\nstage: { build: { rounds: 5 } }\n---\n")
        pack.upgrade(self.repo, "sdlc")
        again = pack.upgrade(self.repo, "sdlc")
        self.assertFalse(again["upgraded"])
        self.assertEqual({f["action"] for f in again["files"]}, {"same"})
        self.assertIn("[L-1]", (self.repo / WF / "learnings.md").read_text())
        self.assertIn("rounds: 5", (self.repo / WF / "settings.md").read_text())

    def test_an_agent_the_plugin_changed_is_upgraded_under_its_pack_name(self):
        self.bump()
        p = self.plugin / "agents" / "builder.md"
        p.write_text(p.read_text() + "\nOne more rule.\n")
        pack.upgrade(self.repo, "sdlc")
        text = (self.repo / ".claude/agents/sdlc-builder.md").read_text()
        self.assertIn("One more rule.", text)
        self.assertIn("\nname: sdlc-builder\n", text)

    def test_upgrading_a_pack_that_is_not_installed_says_so(self):
        shutil.rmtree(self.repo / WF / ".base")
        with self.assertRaisesRegex(pack.PackError, "not installed"):
            pack.upgrade(self.repo, "sdlc")


class StatusCheckTest(PackTestCase):
    def test_it_reports_an_available_upgrade_and_conflict_markers(self):
        cfg = config.load(self.repo)
        self.assertEqual(pack.check(self.repo, "sdlc"), [])
        self.plugin_edit("workflow.yaml", f"version: {CUR}", f"version: {NEXT}")
        issues = cli.health(cfg)["issues"]
        self.assertTrue(any(f"{CUR} is installed; the plugin has {NEXT}" in i and "epi upgrade sdlc" in i for i in issues), issues)
        self.mine("scripts/ci.py", "import", "<<<<<<< repo\nimport")
        self.assertTrue(any("scripts/ci.py has conflict markers" in i for i in pack.check(self.repo, "sdlc")))


class AgentResolutionTest(PackTestCase):
    def test_the_pack_copy_wins_then_a_repo_agent_then_the_plugins(self):
        r = self.repo
        a = lambda name: config.agent_source(r, name, "sdlc")[:2]
        self.assertEqual(a("builder"), ("pack", "sdlc-builder"))
        (r / ".claude/agents/builder.md").write_text("---\nname: builder\n---\n")
        self.assertEqual(a("builder"), ("pack", "sdlc-builder"))                    # still the pack's
        (r / ".claude/agents/marketing.md").write_text("---\nname: marketing\n---\n")
        self.assertEqual(a("marketing"), ("repo", "marketing"))
        (r / ".claude/agents/sdlc-builder.md").unlink()
        self.assertEqual(a("builder"), ("repo", "builder"))
        (r / ".claude/agents/builder.md").unlink()
        self.assertEqual(a("builder"), ("plugin", "epi:builder"))
        self.assertEqual(a("nobody"), (None, None))

    def test_the_machine_spawns_the_resolved_agent_and_settings_stay_keyed_by_the_step(self):
        (self.repo / WF / "settings.md").write_text("---\nagent: { builder: { effort: high } }\n---\n")
        cfg = config.load(self.repo)
        self.assertEqual(cfg.agent("builder")["effort"], "high")
        from engine import machine
        task = Store(cfg).create_task("x")["id"]
        self.assertEqual(machine.Machine(cfg, task).agent_name("builder"), "sdlc-builder")
        (self.repo / ".claude/agents/sdlc-builder.md").unlink()
        self.assertEqual(machine.Machine(config.load(self.repo), task).agent_name("builder"), "epi:builder")


class SettingsFilesTest(PackTestCase):
    def test_configedit_writes_each_setting_to_the_file_of_its_tab(self):
        cfg = config.load(self.repo)
        epi, settings = self.repo / ".claude/epi.md", self.repo / WF / "settings.md"
        h = configedit.hashes(cfg)
        configedit.save(cfg, {"global.budget.per_task": 1_500_000, "stage.build.rounds": 4, "agent.builder.effort": "high"}, h)
        self.assertIn("per_task: 1500000", epi.read_text())
        self.assertNotIn("rounds", epi.read_text())
        self.assertIn("rounds: 4", settings.read_text())
        self.assertIn("effort: high", settings.read_text())
        self.assertNotIn("per_task", settings.read_text())
        self.assertIn("# Tuning for the sdlc workflow", settings.read_text())          # comments survive
        cfg = config.load(self.repo)
        self.assertEqual((cfg.v("global.budget.per_task"), cfg.v("stage.build.rounds")), (1_500_000, 4))

    def test_the_hash_is_checked_per_file(self):
        cfg = config.load(self.repo)
        h = configedit.hashes(cfg)
        (self.repo / ".claude/epi.md").write_text("---\nglobal: { budget: { per_task: 1900000 } }\n---\n")   # a person edited epi.md
        configedit.save(cfg, {"stage.build.rounds": 2}, h)                                  # settings.md is unchanged: fine
        with self.assertRaisesRegex(configedit.EditError, "epi.md changed on disk"):
            configedit.save(config.load(self.repo), {"global.budget.per_month": 9_000_000}, h)

    def test_a_setting_in_the_wrong_file_is_an_error(self):
        epi, settings = self.repo / ".claude/epi.md", self.repo / WF / "settings.md"
        epi.write_text("---\nstage: { build: { rounds: 2 } }\n---\n")
        with self.assertRaisesRegex(config.ConfigError, "'stage' settings belong in the workflow's settings.md"):
            config.load(self.repo)
        epi.write_text("---\n---\n")
        settings.write_text("---\nglobal: { budget: { per_task: 1000000 } }\n---\n")
        with self.assertRaisesRegex(config.ConfigError, "'global' settings belong in epi.md"):
            config.load(self.repo)
        settings.write_text("---\n---\n")
        with self.assertRaises(config.ConfigError):
            configedit.save(config.load(self.repo), {"stage.build.skip": ["lint"]}, None)

    def test_the_rules_are_the_settings_body_and_learnings_come_from_learnings_md(self):
        (self.repo / WF / "settings.md").write_text("---\n---\n\n<!-- a note -->\nSplit big features.\n")
        (self.repo / WF / "learnings.md").write_text("## Learnings\n- [L-1] builder · Seed first (task-1)\n\n## Answers\n"
                                                     "- [A-1] all · Q? A. (task-1)\n")
        cfg = config.load(self.repo)
        brief = Path(self.repo / "brief.md")
        from engine import briefs
        briefs.write(cfg, brief, {"task": "1"}, "builder", [("Task", "do it")])
        text = brief.read_text()
        self.assertIn("Split big features.", text)
        self.assertNotIn("a note", text)
        self.assertIn("[L-1] builder · Seed first", text)
        self.assertIn("[A-1] all · Q? A.", text)

    def test_the_retro_writes_only_learnings_md(self):
        cfg = config.load(self.repo)
        sh("git add -A && git commit -qm pack", self.repo)
        task = Store(cfg).create_task("x")["id"]
        added = learn.add_entries(cfg, [{"kind": "learning", "roles": ["builder"], "text": "Seed first", "evidence": "e"},
                                        {"kind": "answer", "roles": ["all"], "text": "Auth? Yes."}], task)
        self.assertEqual(len(added), 2)
        status = sh("git status --porcelain", self.repo).splitlines()
        self.assertEqual([l.split()[-1] for l in status], [f"{WF}/learnings.md"])
        text = (self.repo / WF / "learnings.md").read_text()
        self.assertIn("Seed first", text)
        self.assertIn("Auth? Yes.", text)
        self.assertEqual(config.load(self.repo).learning_sections()["Learnings"].count("Seed first"), 1)

    def test_agents_cannot_edit_settings_workflows_agents_principles_or_ci(self):
        paths = config.DEFAULTS["global"]["guardrail"]["protected_paths"]
        for p in (".claude/epi.md", ".claude/workflows/**", ".claude/agents/**", ".claude/principles/**", "CLAUDE.md",
                  ".github/workflows/**", "REVIEW.md", "review-*.md"):
            self.assertIn(p, paths)


class InitTest(unittest.TestCase):
    def test_init_creates_epi_md_and_installs_the_default_pack(self):
        tmp = Path(tempfile.mkdtemp(prefix="epi-init-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        sh("git init -q -b main && git config user.email t@t && git config user.name t", tmp)
        (tmp / "pyproject.toml").write_text("[project]\nname='x'\n")
        sh("git add -A && git commit -qm init", tmp)
        write = tmp / "answers.json"
        write.write_text(json.dumps({"global": {"command": {"test": "pytest -q"}}, "agent": {"tester": {"paths": ["tests/**"]}}}))
        out = json.loads(run(["init", "--write", str(write)], tmp))
        self.assertIn(".claude/epi.md", out["created"])
        self.assertIn(f"{WF}/workflow.yaml", out["created"])
        self.assertIn("test: pytest -q", (tmp / ".claude/epi.md").read_text())
        self.assertNotIn("tester", (tmp / ".claude/epi.md").read_text())                  # tuning is the pack's
        self.assertIn("paths:", (tmp / WF / "settings.md").read_text())
        self.assertFalse((tmp / "SDLC.md").exists())
        cfg = config.load(tmp)
        self.assertEqual(cfg.v("global.command.test"), "pytest -q")
        again = json.loads(run(["init"], tmp))
        self.assertNotIn(".claude/epi.md", again["created"])
        self.assertEqual(pack.installed_packs(tmp), ["sdlc"])


if __name__ == "__main__":
    unittest.main()
