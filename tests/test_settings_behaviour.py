"""What the repo-facing settings actually change."""
import contextlib
import io
import os
import shutil
import types
import unittest
from pathlib import Path

from helpers import make_repo, sh
from engine import cli, config, hooks, machine, schema
from engine.store import Store
from test_machine import ARCH, Agents, base_md, drive


class SettingsBehaviourTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("FAKE_REVIEW", None)

    def repo(self, extra=None, approval="none", rounds=3, files=None, edits=(), spec_signoff=False):
        repo = make_repo(base_md(approval, rounds, extra, edits, spec_signoff), files)
        self.addCleanup(shutil.rmtree, repo.parent, ignore_errors=True)
        return repo, config.load(repo)

    def task(self, cfg, text="Say hello"):
        return Store(cfg).create_task(text)

    # ---- validation comes from the schema ---------------------------------
    def test_invalid_values_are_rejected_with_the_allowed_ones(self):
        cases = {
            ("stage", "release", "strategy"): "ff", ("stage", "build", "rounds"): 0,
            ("stage", "build", "requirements"): [1], ("stage", "build", "pr_draft"): "yes",
            ("stage", "release", "message"): "{title} {x}", ("stage", "build", "budget_tokens"): -1,
            ("global", "pipeline", "critical_thinking"): 6, ("global", "pipeline", "nits_block"): "yes",
            ("agent", "builder", "model"): "gpt", ("agent", "builder", "writes"): "anywhere",
            ("agent", "builder", "effort"): "extreme", ("agent", "tester", "paths"): "tests/**",
            ("global", "repo", "branch_name"): "{nope}", ("global", "learning", "on"): ["cancelled"],
            ("global", "command", "timeout"): 0, ("global", "command", "lic"): 5,
            ("global", "human", "approver"): "alice", ("global", "dashboard", "stall_after"): "soon", ("global", "dashboard", "activity"): "many",
        }
        for (tab, sec, key), value in cases.items():
            with self.assertRaises(config.ConfigError, msg=f"{tab}.{sec}.{key}={value!r}"):
                self.repo({tab: {sec: {key: value}}})
        with self.assertRaises(config.ConfigError) as e:
            self.repo({"stage": {"release": {"strategy": "ff"}}})
        self.assertIn("squash, merge, rebase", str(e.exception))

    def test_unknown_names_are_errors_not_ignored(self):
        for bad, word in (({"stage": {"nope": {"rounds": 2}}}, "unknown stage 'nope'"),
                          ({"agent": {"nobody": {"model": "haiku"}}}, "unknown agent 'nobody'"),
                          ({"stage": {"build": {"retries": 2}}}, "unknown setting 'stage.build.retries'"),
                          ({"stage": {"build": {"skip": ["lint"]}}}, "unknown setting 'stage.build.skip'"),      # structure is in workflow.yaml
                          ({"stage": {"build": {"add": {}}}}, "unknown setting 'stage.build.add'"),
                          ({"stage": {"build": {"signoff": False}}}, "unknown setting 'stage.build.signoff'"),
                          ({"release": {"approval": {"mode": "human"}}}, "unknown setting 'release'")):
            with self.assertRaises(config.ConfigError, msg=str(bad)) as e:
                self.repo(bad)
            self.assertIn(word, str(e.exception))

    def test_a_value_added_to_the_schema_is_accepted(self):
        cfg = self.repo()[1]
        f = schema.fields(cfg.schema)["agent.builder.model"]
        self.assertEqual(tuple(f["options"]), schema.options(cfg.schema, "agent.builder.model"))
        self.assertIn("haiku", f["options"])
        self.repo({"agent": {"builder": {"model": "haiku"}}})           # no second list to update

    def test_the_workflow_name_setting_selects_the_workflow(self):
        self.assertEqual(self.repo()[1].workflow_name, "sdlc")
        with self.assertRaisesRegex(config.ConfigError, "no workflow 'nope'"):
            self.repo({"global": {"workflow": {"name": "nope"}}})

    # ---- commands ---------------------------------------------------------
    def test_command_timeout(self):
        repo, cfg = self.repo({"global": {"command": {"test": "sleep 5", "timeout": 1}}}, rounds=1)
        task = self.task(cfg)["id"]
        drive(cfg, task, Agents())
        self.assertIn("timed out after 1s", (Store(cfg).task_dir(task) / "build" / "r1" / "test.md").read_text())

    def test_verify_runs_every_command_so_one_round_reports_every_failure(self):
        repo, cfg = self.repo({"global": {"command": {"lic": "exit 3", "after": "exit 4"}},
                               }, rounds=1, edits=[("add", "build", "verify", "{ run: lic }"), ("add", "build", "verify", "{ run: after }")])
        task = self.task(cfg)["id"]
        drive(cfg, task, Agents())
        d = Store(cfg).task_dir(task)
        self.assertTrue((d / "build" / "r1" / "lic.md").exists() and (d / "build" / "r1" / "after.md").exists())
        titles = [i["title"] for i in __import__("engine.thread", fromlist=["x"]).threads(d)]
        self.assertTrue(any("exit 3" in t for t in titles) and any("exit 4" in t for t in titles), titles)

    def test_the_default_for_a_command_is_to_stop_and_the_tester_is_left_for_the_next_round(self):
        repo, cfg = self.repo({"global": {"command": {"lint": "exit 1"}}}, rounds=1)
        task = self.task(cfg)["id"]
        agents = Agents()
        act = drive(cfg, task, agents)
        self.assertIn("disagreement", act["reason"])
        d = Store(cfg).task_dir(task) / "build" / "r1"
        self.assertTrue((d / "lint.md").exists())
        self.assertFalse((d / "tester.md").exists())
        self.assertNotIn("sdlc-tester", agents.calls)

    # ---- stages -----------------------------------------------------------
    def test_rounds_bound_the_act_and_review_cycles_of_a_stage(self):
        for rounds in (2, 4):
            repo, cfg = self.repo(rounds=rounds)
            task = self.task(cfg)["id"]
            agents = Agents()
            agents.test_verdict, agents.dispute, agents.upheld = "FAIL", True, 1
            act = drive(cfg, task, agents)
            self.assertIn("disagreement", act["reason"])
            self.assertEqual(agents.calls.count("sdlc-builder"), rounds)         # one do step per round

    def test_the_round_folders_count_within_each_stage(self):
        repo, cfg = self.repo()
        task = self.task(cfg)["id"]
        drive(cfg, task, Agents())
        d = Store(cfg).task_dir(task)
        for f in ("spec/r1/spec-writer.md", "build/r1/builder.md", "build/r1/open-pr.md", "release/r1/update-branch.md"):
            self.assertTrue((d / f).exists(), f)

    def test_a_signoff_in_workflow_yaml_asks_a_person_until_it_is_disabled_there(self):
        repo, cfg = self.repo(approval="human", spec_signoff=True)
        task = self.task(cfg)["id"]
        self.assertEqual(drive(cfg, task, Agents())["reason"], "spec sign-off: waiting for approver")
        machine.approve(cfg, task, "spec")
        self.assertEqual(drive(cfg, task, Agents())["reason"], "build sign-off: waiting for approver")
        repo, cfg = self.repo()
        self.assertEqual(drive(cfg, self.task(cfg)["id"], Agents())["action"], "done")

    def test_signoff_waits_until_the_review_has_passed_and_feedback_runs_a_final_round(self):
        repo, cfg = self.repo(approval="human")
        task = self.task(cfg)["id"]
        agents = Agents()
        agents.test_verdict = "FAIL"                       # the review never passes: nobody is asked to sign off
        self.assertEqual(drive(cfg, task, agents)["reason"], "needs-you: disagreement")
        self.assertFalse([e for e in Store(cfg).events(task) if e["type"] == "approval"])
        task = self.task(cfg)["id"]
        agents = Agents()
        self.assertEqual(drive(cfg, task, agents)["reason"], "build sign-off: waiting for approver")
        machine.send_back(cfg, task, "Say howdy instead")
        self.assertEqual(drive(cfg, task, agents)["reason"], "build sign-off: waiting for approver")   # asked again
        rounds = [e for e in Store(cfg).events(task) if e["type"] == "round" and e["stage"] == "build"]
        self.assertTrue(rounds[-1]["final"])
        machine.approve(cfg, task, "build")
        self.assertEqual(drive(cfg, task, agents)["action"], "done")

    def test_a_step_added_to_workflow_yaml_runs_in_its_phase(self):
        repo, cfg = self.repo({"global": {"command": {"lic": "true"}}}, edits=[("add", "release", "finish", "{ run: lic }")])
        self.assertEqual([s["_id"] for s in cfg.stages["release"]["finish"]], ["merge", "cleanup", "lic"])
        task = self.task(cfg)["id"]
        self.assertEqual(drive(cfg, task, Agents())["action"], "done")
        self.assertTrue((Store(cfg).task_dir(task) / "release" / "r1" / "lic.md").exists())
        with self.assertRaisesRegex(config.ConfigError, "unknown key 'later'"):
            self.repo(edits=[("add", "build", "later", "{ uses: agent/x }")])

    def test_a_failing_finalize_step_goes_back_to_the_builder_as_another_round(self):
        once = "test -f ../fin-once || { touch ../fin-once; echo 'CI red'; exit 1; }"
        repo, cfg = self.repo({"global": {"command": {"fin": once}}}, edits=[("add", "build", "finalize", "{ run: fin }")])
        task = self.task(cfg)["id"]
        agents = Agents()
        self.assertEqual(drive(cfg, task, agents)["action"], "done")
        self.assertEqual(agents.calls.count("sdlc-builder"), 2)
        rounds = [(e["stage"], e["number"], e["reason"][:10]) for e in Store(cfg).events(task) if e["type"] == "round"]
        self.assertIn(("build", 2, "fin failed"), rounds)
        self.assertIn("CI red", [b for a, b in agents.briefs if a == "sdlc-builder"][-1])

    def test_a_repo_can_bring_its_own_workflow(self):
        wf = "name: mine\nworkspace: none\non: { manual: {} }\nstages:\n  only:\n    verify:\n      - run: ok\n"
        repo = make_repo("---\nglobal:\n  workflow: { name: mine }\n  command: { ok: 'true' }\n---\n",
                         {".claude/workflows/mine/workflow.yaml": wf})
        self.addCleanup(shutil.rmtree, repo.parent, ignore_errors=True)
        cfg = config.load(repo)
        self.assertEqual(list(cfg.stages), ["only"])
        self.assertEqual(drive(cfg, self.task(cfg)["id"], Agents())["action"], "done")

    def test_a_repos_own_reviewer_is_told_how_to_report_and_the_plugins_tester_is_not(self):
        repo, cfg = self.repo(files={".claude/agents/arch.md": ARCH}, edits=[("add", "build", "review", "{ uses: agent/arch }")])
        task = self.task(cfg)["id"]
        drive(cfg, task, Agents())
        b = Store(cfg).task_dir(task) / "briefs"
        self.assertIn("## How to report", (b / "build-arch-n2.md").read_text())
        self.assertIn("- new -> builder, feedback, blocking:", (b / "build-arch-n2.md").read_text())
        self.assertNotIn("## How to report", (b / "build-tester-n2.md").read_text())

    def test_a_step_disabled_in_workflow_yaml_is_left_out(self):
        repo, cfg = self.repo(edits=[("disable", "build", "tester")])
        self.assertIn(("build", "review", "tester"), cfg.disabled)
        self.assertNotIn("tester", [s["_id"] for s in cfg.stages["build"]["review"]])
        agents = Agents()
        self.assertEqual(drive(cfg, self.task(cfg)["id"], agents)["action"], "done")
        self.assertNotIn("sdlc-tester", agents.calls)

    def test_a_review_step_added_to_workflow_yaml_runs_in_the_stage(self):
        repo, cfg = self.repo(files={".claude/agents/arch.md": ARCH}, edits=[("add", "build", "review", "{ uses: agent/arch }")])
        self.assertEqual([s["_id"] for s in cfg.stages["build"]["review"]][-1], "arch")
        agents = Agents()
        self.assertEqual(drive(cfg, self.task(cfg)["id"], agents)["action"], "done")
        self.assertIn("arch", agents.calls)

    def test_a_stage_budget_stops_its_own_agents(self):
        repo, cfg = self.repo({"stage": {"build": {"budget_tokens": 500}}})
        task = self.task(cfg)["id"]
        agents = Agents()
        agents.run(machine.Machine(cfg, task).next_action())                      # the spec
        Store(cfg).event(task, "agent", key="a1", agent="sdlc-builder", stage="build", data={"tokens": 900})
        act = drive(cfg, task, agents)
        self.assertIn("budget", act["reason"])
        self.assertIn("stage:build", act["detail"])

    def test_requirements_reach_the_agents_of_the_stage(self):
        repo, cfg = self.repo({"stage": {"spec": {"requirements": ["Rollout plan", "Risks"]}}})
        task = self.task(cfg)["id"]
        machine.Machine(cfg, task).next_action()
        brief = (Store(cfg).task_dir(task) / "briefs" / "spec-spec-writer-n1.md").read_text()
        self.assertIn("Extra requirements this repo sets for this stage", brief)
        self.assertIn("- Rollout plan", brief)

    def test_nits_block_only_when_asked(self):
        files = {".claude/agents/arch.md": ARCH}
        edits = [("disable", "build", "code-review"), ("add", "build", "review", "{ uses: agent/arch }")]
        for block, expect in ((False, "done"), (True, "wait")):
            repo, cfg = self.repo({"global": {"pipeline": {"nits_block": block}}},
                                  rounds=1, files=files, edits=edits)
            agents = Agents()
            agents.nits = 2
            act = drive(cfg, self.task(cfg)["id"], agents)
            self.assertEqual(act["action"], expect, act)

    # ---- agents -----------------------------------------------------------
    def test_agent_attempts(self):
        for attempts, spawns in ((1, 1), (3, 3)):
            repo, cfg = self.repo({"global": {"pipeline": {"agent_attempts": attempts}}})
            task = self.task(cfg)["id"]
            acts = [machine.Machine(cfg, task).next_action() for _ in range(spawns + 1)]    # the agent never writes
            self.assertEqual([a["action"] for a in acts], ["spawn"] * spawns + ["failed"])
            self.assertIn(f"{attempts} attempt", acts[-1]["reason"])

    def test_an_agents_critical_thinking_reaches_its_brief(self):
        repo, cfg = self.repo({"agent": {"spec-writer": {"critical_thinking": 4}}})
        task = self.task(cfg)["id"]
        machine.Machine(cfg, task).next_action()
        brief = (Store(cfg).task_dir(task) / "briefs" / "spec-spec-writer-n1.md").read_text()
        self.assertIn("critical thinking 4 of 5", brief)

    def test_a_skills_effort_and_model_reach_the_headless_call(self):
        log = Path(self.repo()[0].parent / "claude.log")
        os.environ["FAKE_CLAUDE_LOG"] = str(log)
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_LOG", None)
        repo, cfg = self.repo({"agent": {"code-review": {"effort": "max"}}})
        os.environ["FAKE_CLAUDE_LOG"] = str(log)
        drive(cfg, self.task(cfg)["id"], Agents())
        self.assertIn("/code-review max main...HEAD", log.read_text())

    def test_later_rounds_recheck_only_unless_the_agent_is_set_to_review_in_full(self):
        for recheck in ("threads", "full"):
            log = Path(self.repo()[0].parent / f"claude-{recheck}.log")
            repo, cfg = self.repo({"agent": {"code-review": {"recheck": recheck}}})
            os.environ["FAKE_CLAUDE_LOG"] = str(log)
            os.environ["FAKE_REVIEW"] = "important"
            self.addCleanup(os.environ.pop, "FAKE_REVIEW", None)
            self.addCleanup(os.environ.pop, "FAKE_CLAUDE_LOG", None)
            agents = Agents()
            agents.dispute = True
            drive(cfg, self.task(cfg)["id"], agents)
            lines = log.read_text().splitlines()
            full = sum(l.startswith("/code-review medium") for l in lines)             # medium: the default effort
            self.assertTrue(full == 1 if recheck == "threads" else full > 1, (recheck, full))
            self.assertEqual(any(l.startswith("Re-check") for l in lines), recheck == "threads")

    def test_where_an_agent_may_write_comes_from_its_settings(self):
        repo, cfg = self.repo({"agent": {"builder": {"writes": "paths", "paths": ["src/**"]}}})
        task = self.task(cfg)["id"]
        machine.Machine(cfg, task, session_id="s1").next_action()
        wt = Path(Store(cfg).load_task(task)["worktree"])
        for path, denied in ((wt / "src" / "a.py", False), (wt / "app.py", True)):
            inp = {"session_id": "s1", "cwd": str(repo), "agent_type": "sdlc-builder", "agent_id": "a",
                   "transcript_path": "", "tool_name": "Write", "tool_input": {"file_path": str(path)}}
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                hooks.pre_tool_use(inp)
            self.assertEqual("deny" in buf.getvalue(), denied, path)

    # ---- people -----------------------------------------------------------
    def test_only_the_members_of_a_role_may_approve_as_it(self):
        repo, cfg = self.repo({"global": {"human": {"approver": ["alice"]}}}, approval="human")
        task = self.task(cfg)["id"]
        self.assertEqual(drive(cfg, task, Agents())["reason"], "build sign-off: waiting for approver")
        with self.assertRaisesRegex(ValueError, "bob is not one of the approvers"):
            machine.approve(cfg, task, "build", by="bob")
        machine.approve(cfg, task, "build", by="alice")
        self.assertEqual(drive(cfg, task, Agents())["action"], "done")

    # ---- the workflow's merge settings ----------------------------------------
    def test_branch_name_template(self):
        repo, cfg = self.repo({"global": {"repo": {"branch_prefix": "feat/", "branch_name": "{prefix}{id}-{slug}"}}})
        self.assertEqual(self.task(cfg, "Add a Login page!")["branch"], "feat/1-add-a-login-page")
        repo, cfg = self.repo()
        self.assertEqual(self.task(cfg)["branch"], "epi/task-1")
        self.assertEqual(Store(cfg).create_task("x", task_id="ABC-9")["branch"], "epi/ABC-9")   # a ticket id is the name

    def test_merge_message_branch_and_worktree_cleanup(self):
        repo, cfg = self.repo({"stage": {"release": {"message": "feat: {title} [{id}]"}}})
        task = self.task(cfg)
        self.assertEqual(drive(cfg, task["id"], Agents())["action"], "done")
        self.assertEqual(sh("git log -1 --format=%s main", repo), "feat: Say hello [1]")
        self.assertEqual(sh(f"git branch --list {task['branch']}", repo), "")            # deleted by default
        self.assertFalse(os.path.exists(task["worktree"]))                                # and its checkout removed
        repo, cfg = self.repo({"stage": {"release": {"delete_branch": False, "cleanup_worktree": False}}})
        task = self.task(cfg)
        drive(cfg, task["id"], Agents())
        self.assertIn(task["branch"], sh(f"git branch --list {task['branch']}", repo))
        self.assertTrue(os.path.exists(task["worktree"]))

    def next_cli(self, repo, task):
        old = os.getcwd()
        os.chdir(repo)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                cli.cmd_next(types.SimpleNamespace(task=task, session=None))
        finally:
            os.chdir(old)

    def test_retro_runs_only_for_the_configured_outcomes(self):
        for on, ran in ((None, False), ([], False), (["done"], True), (["clean"], True), (["helped"], False)):
            repo, cfg = self.repo({"global": {"learning": {"on": on}}} if on is not None else None)   # None: the default skips a clean task
            task = self.task(cfg)["id"]
            drive(cfg, task, Agents())                       # a clean run: every stage passed in round 1
            self.next_cli(repo, task)
            types_ = [e["type"] for e in Store(cfg).events(task)]
            self.assertEqual("retro" in types_, ran, on)
            self.assertEqual("retro_skipped" in types_, not ran, on)     # a skip is recorded, once
            self.next_cli(repo, task)
            if not ran:
                self.assertEqual([e["type"] for e in Store(cfg).events(task)].count("retro_skipped"), 1)

    def test_stall_after_sets_when_a_quiet_task_shows_as_stalled(self):
        import sim
        from engine import taskview
        for minutes, stalled in ((10, True), (30, False)):
            repo, cfg = self.repo({"global": {"dashboard": {"stall_after": minutes}}})
            task = self.task(cfg)["id"]
            machine.Machine(cfg, task, session_id="s1").next_action()
            sim.backdate(cfg, task, minutes=20)
            self.assertEqual(taskview.card(cfg, Store(cfg).load_task(task))["stalled"], stalled, minutes)

    def test_activity_caps_the_feed_and_zero_hides_it(self):
        from engine import serve
        for n, want in ((2, 2), (0, 0)):
            repo, cfg = self.repo({"global": {"dashboard": {"activity": n}}})
            drive(cfg, self.task(cfg)["id"], Agents())               # a few events: a round starts, a sign-off waits
            self.assertEqual(len(serve.board(cfg)["activity"]), want, n)

    def test_context_window_sets_the_context_fill_shown_per_step(self):
        from engine import taskview
        for window, share in ((200_000, 0.5), (1_000_000, 0.1)):
            cfg = self.repo({"global": {"budget": {"context_window": window}}})[1]
            self.assertEqual(taskview.context_share(cfg, {"context": 100_000}), share)

    def test_warn_at_reaches_the_board(self):
        from engine import serve
        cfg = self.repo({"global": {"budget": {"warn_at": 0.5}}})[1]
        self.assertEqual(serve.board(cfg)["warn_at"], 0.5)


if __name__ == "__main__":
    unittest.main()
