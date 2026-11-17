"""End-to-end machine tests with simulated agents and a fake `claude` CLI.
No model is called; agents are replaced by functions that write their outputs."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import make_repo, sh, with_edits
from engine import config, learn, machine, thread, yamlish
from engine.store import Store

SPEC = """---
risk: {risk}
touches_ui: false
change_type: feat
breaking: false
paths: ["app.py"]
open_questions: {q}
---
## Problem
Say hello.
## Acceptance criteria
1. AC-1 prints hello
## Questions
{questions}
"""

ARCH = "---\nname: arch\ndescription: Checks the architecture.\ntools: Read, Grep, Glob, Bash, Write\nmodel: sonnet\n---\nCheck it.\n"


def base_md(merge_gate="human", max_rounds=3, extra=None, edits=(), spec_signoff=False):
    """Settings text for tests (see helpers.make_repo). `merge_gate` none = nobody signs off the build; the spec
    never asks unless `spec_signoff`. `extra` is a nested dict of settings merged over the base ones; `edits` change workflow.yaml."""
    settings = {"stage": {"build": {"rounds": max_rounds}, "spec": {"rounds": max_rounds}},
                "global": {"command": {"test": "python3 -c 'print(1)'"}}}
    settings = config.deep_merge(settings, extra or {})
    off = ([] if spec_signoff else [("disable", "spec", "signoff")]) + ([("disable", "build", "signoff")] if merge_gate == "none" else [])
    return with_edits("---\n" + yamlish.dumps(settings) + "\n---\n\n## Pipeline rules\n- Keep it small.\n", [*off, *edits])


class Agents:
    """Simulated agents. Tweak attributes to change behaviour."""

    def __init__(self):
        self.questions = False
        self.review_verdict = "PASS"
        self.test_verdict = "PASS"
        self.touch = "app.py"
        self.dispute = False         # the builder disputes findings from round 2 on
        self.upheld = 0              # the tester and reviewers keep what the builder disputed
        self.nits = 0                # Nit findings the reviewers report
        self.risk = "low"
        self.commits = True          # False: the builder leaves its work uncommitted (a denied `git commit`)
        self.calls = []
        self.briefs = []             # every brief the agents were given
        self.batches = []            # the agents of each spawn action, in order
        self.outputs = {}            # agent -> the outputs it writes in its report's frontmatter

    def extra(self, a):
        """Frontmatter lines for the outputs this agent is set to write."""
        return "".join(f"{k}: {v}\n" for k, v in self.outputs.get(a["agent"], {}).items())

    def run(self, action):
        self.batches.append([a["agent"] for a in action["agents"]])
        for a in action["agents"]:
            brief = re.search(r"Read (\S+) first", a["prompt"]).group(1)
            front, body = yamlish.parse_frontmatter(Path(brief).read_text())
            self.calls.append(a["agent"])
            self.briefs.append((a["agent"], body))
            out = Path(front["output"])
            out.parent.mkdir(parents=True, exist_ok=True)
            wt = Path(front["worktree"])
            if a["agent"] == "sdlc-spec-writer":
                q = self.questions and "Answers to your questions" not in body
                out.write_text(SPEC.format(risk=self.risk, q=1 if q else 0,
                                           questions="1. Which greeting?" if q else ""))
            elif a["agent"] == "sdlc-builder":
                f = wt / self.touch
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(f"print('hello task {front['task']} round {front['round']}')\n")
                if self.commits:
                    sh(f"git add -A && git commit -qm 'build {front['round']}'", wt)
                mine = re.findall(r"^(I\d+) \[blocking\].*\n\s+([\w-]+) -> builder", body, flags=re.M)
                verb = "pushback: The check is wrong, the spec never asks for it." if self.dispute else "fixed: done."
                replies = "".join(f"- {i} -> {to}, {verb}\n" for i, to in mine)
                out.write_text(f"---\nround: {front['round']}\n---\n## Changes\nhello\n"
                               f"{'## Comments' + chr(10) + replies if replies else ''}"
                               "## Learning candidates\n- The integration tests need the seed script first.\n")
            elif a["agent"] == "sdlc-tester":
                asked = re.search(r"^(I\d+) \[", body, flags=re.M)
                if self.test_verdict == "FAIL" and self.upheld and asked:
                    c = f"- {asked.group(1)} -> builder, pushback: The spec does ask for it (AC-1).\n"
                elif self.test_verdict == "FAIL":
                    c = "- new -> builder, feedback, blocking: greeting is wrong\n"
                elif asked:
                    c = f"- {asked.group(1)} -> builder, accept\n"
                else:
                    c = ""
                out.write_text(f"---\nverdict: {self.test_verdict}\n{self.extra(a)}---\n## Coverage\nAC-1 ok\n"
                               f"{'## Comments' + chr(10) + c if c else ''}")
            else:                                      # a repo-defined review agent
                imp = 1 if self.review_verdict == "CHANGES" else 0
                asked = re.search(r"^(I\d+) \[", body, flags=re.M)
                if imp and self.upheld and asked:
                    c = f"- {asked.group(1)} -> builder, pushback: Still a bug: the input is not validated.\n"
                elif imp:
                    c = "- new -> builder, feedback, blocking: input is not validated\n"
                elif asked:
                    c = f"- {asked.group(1)} -> builder, accept\n"
                else:
                    c = ""
                c += "- new -> builder, feedback, nit: naming\n" * self.nits
                out.write_text(f"---\nverdict: {self.review_verdict}\nimportant: {imp}\nnit: {self.nits}\n{self.extra(a)}---\n"
                               f"{'## Comments' + chr(10) + c if c else ''}")


def drive(cfg, task, agents, max_steps=60, answers="A: hello"):
    for _ in range(max_steps):
        act = machine.Machine(cfg, task, session_id="sess-1").next_action()
        if act["action"] == "spawn":
            agents.run(act)
        elif act["action"] == "ask":
            f = cfg.work_dir / "answers.txt"
            f.write_text(answers)
            machine.report_answers(cfg, task, f.read_text())
        else:
            return act
    raise AssertionError("the machine did not settle")


def ingest_round(cfg, task, stage, n):
    m = machine.Machine(cfg, task)
    m.ingest(m.state(), stage, n)


class MachineTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("FAKE_REVIEW", None)

    def repo(self, merge_gate="human", max_rounds=3, extra=None, files=None, edits=()):
        repo = make_repo(base_md(merge_gate, max_rounds, extra, edits), files)
        self.addCleanup(shutil.rmtree, repo.parent, ignore_errors=True)
        return repo, config.load(repo)

    def new_task(self, cfg, text="Say hello"):
        return Store(cfg).create_task(text)["id"]

    def test_happy_path_local_merge(self):
        repo, cfg = self.repo()
        task = self.new_task(cfg)
        agents = Agents()
        act = drive(cfg, task, agents)
        self.assertEqual(act["action"], "wait")
        self.assertIn("build sign-off", act["reason"])
        machine.approve(cfg, task, "build")
        act = drive(cfg, task, agents)
        self.assertEqual(act["action"], "done", act)
        self.assertEqual(agents.calls, ["sdlc-spec-writer", "sdlc-builder", "sdlc-tester"])
        self.assertIn("hello", (repo / "app.py").read_text())                 # fast-forwarded main
        d = Store(cfg).task_dir(task)
        for f in ("spec.md", "spec/r1/spec-writer.md", "build/r1/builder.md", "build/r1/test.md", "build/r1/tester.md",
                  "build/r1/code-review.md", "build/r1/open-pr.md", "release/r1/update-branch.md", "release/r1/test.md",
                  "release/r1/merge.md", "decision.md"):
            self.assertTrue((d / f).exists(), f)
        self.assertFalse((d / "build/r1/security-review.md").exists())       # risk low: not selected
        usage = [e for e in Store(cfg).events(task) if e["type"] == "agent"]
        self.assertTrue(any(e["agent"] == "headless:code-review" for e in usage))
        self.assertGreater(Store(cfg).month_tokens(), 0)

    def test_security_review_runs_for_a_risky_spec(self):
        repo, cfg = self.repo(merge_gate="none")
        task = self.new_task(cfg)
        agents = Agents()
        agents.risk = "high"
        self.assertEqual(drive(cfg, task, agents)["action"], "done")
        self.assertTrue((Store(cfg).task_dir(task) / "build/r1/security-review.md").exists())

    def _merged_commits(self, extra):
        repo, cfg = self.repo(merge_gate="none", extra=extra)
        task = self.new_task(cfg)
        self.assertEqual(drive(cfg, task, Agents())["action"], "done")
        msgs = sh("git log --format=%s main", repo).split("\n")
        branch = Store(cfg).load_task(task)["branch"]
        return repo, [m for m in msgs if m], branch

    def test_merge_strategy_squash_is_default_and_one_commit(self):
        repo, msgs, _ = self._merged_commits({})
        self.assertEqual(len(msgs), 2)                       # init + one squash commit
        self.assertIn("(task-", msgs[0])

    def test_merge_strategy_rebase_keeps_commits_and_merge_makes_merge_commit(self):
        repo, msgs, branch = self._merged_commits({"stage": {"release": {"strategy": "rebase", "delete_branch": False}}})
        tip = subprocess.run(["git", "rev-parse", "main", branch], cwd=repo, capture_output=True, text=True).stdout.split()
        self.assertEqual(tip[0], tip[1])                      # main is the task branch, nothing rewritten
        repo, msgs, branch = self._merged_commits({"stage": {"release": {"strategy": "merge"}}})
        parents = subprocess.run(["git", "log", "-1", "--format=%P", "main"], cwd=repo, capture_output=True, text=True).stdout.split()
        self.assertEqual(len(parents), 2)

    def test_spawn_carries_the_configured_model(self):
        repo, cfg = self.repo(extra={"agent": {"spec-writer": {"model": "haiku"}}})
        task = self.new_task(cfg)
        act = machine.Machine(cfg, task).next_action()
        self.assertEqual(act["action"], "spawn")
        self.assertEqual(act["agents"][0]["model"], "haiku")

    def test_the_agent_files_own_model_is_the_default(self):
        repo, cfg = self.repo()
        act = machine.Machine(cfg, self.new_task(cfg)).next_action()
        self.assertEqual(act["agents"][0]["model"], "opus")

    def test_review_issues_use_up_their_rounds_then_a_human_decides(self):
        repo, cfg = self.repo(merge_gate="none")
        task = self.new_task(cfg)
        agents = Agents()
        agents.dispute = True
        os.environ["FAKE_REVIEW"] = "important"
        act = drive(cfg, task, agents)
        self.assertEqual(act["action"], "wait")
        self.assertIn("disagreement", act["reason"])
        self.assertEqual(agents.calls.count("sdlc-builder"), 4)                # rounds 1-3, then one for the issue found in the last
        machine.approve(cfg, task, "accept")
        act = drive(cfg, task, agents)
        self.assertEqual(act["action"], "done", act)
        front, _ = yamlish.parse_frontmatter((Store(cfg).task_dir(task) / "decision.md").read_text())
        self.assertTrue(front["accept"])

    def test_disagreement_when_the_tester_holds_out_against_a_dispute(self):
        repo, cfg = self.repo(merge_gate="none")
        task = self.new_task(cfg)
        agents = Agents()
        agents.test_verdict, agents.dispute, agents.upheld = "FAIL", True, 1
        act = drive(cfg, task, agents)
        self.assertEqual(act["action"], "wait")
        self.assertIn("disagreement", act["reason"])
        self.assertEqual(agents.calls.count("sdlc-builder"), 3)              # round 1, then rounds 2 and 3
        st = machine.Machine(cfg, task).state()
        self.assertEqual((st.needs_you["reason"], st.stage), ("disagreement", "build"))
        self.assertIn("I1 greeting is wrong", st.needs_you["detail"])
        self.assertIn("still open after 3 rounds", st.needs_you["detail"])
        view = (Store(cfg).task_dir(task) / "conversation.md").read_text()      # the readable copy
        self.assertIn("[ ] I1 - greeting is wrong - blocking", view)
        self.assertIn("- builder - tester - pushback: The check is wrong", view)
        self.assertIn("- director - human - help: still open after 3 rounds", view)
        machine.approve(cfg, task, "accept")                                # the human sides with the builder
        self.assertEqual(drive(cfg, task, agents)["action"], "done")
        view = (Store(cfg).task_dir(task) / "conversation.md").read_text()
        self.assertIn("[x] I1", view)
        self.assertIn("- human - director - accept", view)

    def test_accepting_a_disagreement_closes_only_those_issues_and_the_merge_still_waits_for_a_person(self):
        repo, cfg = self.repo(merge_gate="human")
        task = self.new_task(cfg)
        agents = Agents()
        agents.test_verdict, agents.dispute, agents.upheld = "FAIL", True, 1
        self.assertIn("disagreement", drive(cfg, task, agents)["reason"])
        machine.approve(cfg, task, "accept")
        self.assertFalse(machine.Machine(cfg, task).state().skip_rules)       # only a `rules` accept lets the merge skip its rules
        act = drive(cfg, task, agents)
        self.assertEqual(act["action"], "wait", act)
        self.assertIn("build sign-off", act["reason"])
        self.assertFalse((Store(cfg).task_dir(task) / "merge" / "r1" / "signoff.md").exists())
        machine.approve(cfg, task, "build")
        self.assertEqual(drive(cfg, task, agents)["action"], "done")

    def test_an_issue_found_in_the_last_round_gets_one_extra_round_and_is_labelled_when_it_needs_you(self):
        repo, cfg = self.repo(merge_gate="none", max_rounds=1)
        task = self.new_task(cfg)
        agents = Agents()
        agents.test_verdict = "FAIL"                       # the tester raises a new blocking issue every round
        act = drive(cfg, task, agents)
        self.assertIn("disagreement", act["reason"])
        self.assertEqual(agents.calls.count("sdlc-builder"), 2)              # the round, plus the one extra round
        events = Store(cfg).events(task)
        self.assertEqual([e["extra"] for e in events if e["type"] == "round" and e["stage"] == "build"], [False, True])
        issues = thread.load(Store(cfg).task_dir(task))[0]
        self.assertEqual(issues["I2"]["flag"], "late")                         # raised in the extra round, never answered

    def test_a_comment_line_that_does_not_parse_is_reported_as_an_error(self):
        repo, cfg = self.repo(merge_gate="none")
        task = self.new_task(cfg)
        agents = Agents()
        agents.test_verdict = "FAIL"
        real = agents.run

        def run(action):
            real(action)
            for a in action["agents"]:
                if a["agent"] == "sdlc-tester":
                    out = Path(re.search(r"Read (\S+) first", a["prompt"]).group(1))
                    front, _ = yamlish.parse_frontmatter(out.read_text())
                    f = Path(front["output"])
                    f.write_text(f.read_text() + "I1 => builder fixed it\n")
        agents.run = run
        drive(cfg, task, agents)
        errs = [e for e in Store(cfg).events(task) if e["type"] == "error"]
        self.assertTrue(errs and "was not understood" in errs[0]["detail"], errs)

    def test_disagreement_when_an_added_review_agent_holds_out(self):
        repo, cfg = self.repo(merge_gate="none", files={".claude/agents/arch.md": ARCH},
                              edits=[("disable", "build", "code-review"), ("add", "build", "review", "{ uses: agent/arch }")])
        task = self.new_task(cfg)
        agents = Agents()
        agents.review_verdict, agents.dispute, agents.upheld = "CHANGES", True, 1
        act = drive(cfg, task, agents)
        self.assertIn("disagreement", act["reason"])
        self.assertEqual(machine.Machine(cfg, task).state().stage, "build")
        self.assertIn("arch", agents.calls)

    def test_a_nit_pushback_is_not_pursued_and_never_reaches_a_human(self):
        d = tempfile.mkdtemp()
        task_dir = Path(d)
        (task_dir / "round-1").mkdir()
        (task_dir / "round-1" / "code-review.md").write_text(
            "---\nverdict: PASS\n---\n## Comments\n- new -> builder, feedback, nit: naming\n")
        known = {"builder", "tester", "code-review", "director"}
        src = lambda rd, name, kind: [{"path": task_dir / rd / f"{name}.md", "author": name, "kind": kind}]
        thread.ingest(task_dir, src("round-1", "code-review", "check"), "build", 1, known, default_to="builder")
        (task_dir / "round-2").mkdir()
        (task_dir / "round-2" / "builder.md").write_text("---\nround: 2\n---\n## Comments\n- I1 -> code-review, pushback: house style\n")
        thread.ingest(task_dir, src("round-2", "builder", "do"), "build", 2, known, default_to="builder", r=2)
        i = thread.load(task_dir)[0]["I1"]
        self.assertEqual((i["state"], i["flag"]), ("closed", "nit"))
        shutil.rmtree(d)

    def _flip_tests_after_first_run(self, agents):
        real = agents.run

        def run(action):
            real(action)
            if action["agents"][0]["agent"] == "sdlc-tester":
                agents.test_verdict = "PASS"
        agents.run = run

    def test_resolved_issues_lead_to_another_round_then_the_merge(self):
        repo, cfg = self.repo(merge_gate="none")
        task = self.new_task(cfg)
        agents = Agents()
        agents.test_verdict = "FAIL"
        self._flip_tests_after_first_run(agents)
        act = drive(cfg, task, agents)
        self.assertEqual(act["action"], "done", act)
        self.assertEqual(agents.calls.count("sdlc-builder"), 2)              # the build, then the round that fixed it
        rounds = [(e["stage"], e["r"]) for e in Store(cfg).events(task) if e["type"] == "round"]
        self.assertEqual(rounds, [("spec", 1), ("build", 1), ("build", 2), ("release", 1)])
        view = (Store(cfg).task_dir(task) / "conversation.md").read_text()
        self.assertIn("[x] I1", view)
        self.assertIn("director: closed, accepted by tester", view)

    def test_a_later_round_reruns_only_steps_with_an_open_thread_or_always_rerun(self):
        repo, cfg = self.repo(merge_gate="none")
        task = self.new_task(cfg)
        agents = Agents()
        agents.test_verdict = "FAIL"
        self._flip_tests_after_first_run(agents)
        drive(cfg, task, agents)
        d = Store(cfg).task_dir(task)
        for f in ("tester.md", "code-review.md", "lint.md", "test.md"):        # asked, always_rerun, commands
            self.assertTrue((d / "build" / "r2" / f).exists(), f)
        sel = {e["step"]: e["selected"] for e in Store(cfg).events(task) if e["type"] == "selection" and e["round"] == 3 and e["sub"] == "review"}
        self.assertEqual(sel["security-review"], False)                       # passed last round: not run again
        self.assertEqual(agents.calls.count("sdlc-tester"), 2)

    def test_instructions_from_a_person_are_the_final_round_and_close_the_issue(self):
        repo, cfg = self.repo(merge_gate="none")
        task = self.new_task(cfg)
        agents = Agents()
        agents.test_verdict, agents.dispute, agents.upheld = "FAIL", True, 1
        self.assertIn("disagreement", drive(cfg, task, agents)["reason"])
        machine.send_back(cfg, task, "do it the tester's way")
        agents.test_verdict, agents.dispute, agents.upheld = "PASS", False, 0
        act = drive(cfg, task, agents)
        self.assertEqual(act["action"], "done", act)
        view = (Store(cfg).task_dir(task) / "conversation.md").read_text()
        self.assertIn("- human - director - send_back: do it the tester's way", view)
        self.assertIn("[x] I1", view)
        self.assertIn("do it the tester's way", [b for a, b in agents.briefs if a == "sdlc-builder"][-1])

    def test_a_person_can_send_the_task_back_to_an_earlier_stage(self):
        repo, cfg = self.repo(merge_gate="none")
        task = self.new_task(cfg)
        agents = Agents()
        agents.test_verdict, agents.dispute, agents.upheld = "FAIL", True, 1
        drive(cfg, task, agents)
        self.assertEqual([t for t in machine.send_back_targets(machine.Machine(cfg, task), machine.Machine(cfg, task).state())],
                         [("build", "builder"), ("spec", "spec-writer")])
        machine.send_back(cfg, task, "AC-1 should say hello twice", to="spec-writer")
        agents.test_verdict, agents.dispute, agents.upheld = "PASS", False, 0
        act = drive(cfg, task, agents)
        self.assertEqual(act["action"], "done", act)
        spec_briefs = [b for a, b in agents.briefs if a == "sdlc-spec-writer"]
        self.assertIn("A person sent the task back to you", spec_briefs[-1])
        self.assertIn("AC-1 should say hello twice", spec_briefs[-1])
        self.assertEqual(Store(cfg).events(task)[-1]["type"], "stage")
        self.assertEqual(agents.calls.count("sdlc-spec-writer"), 2)

    def test_the_approver_asking_for_changes_starts_a_final_round_then_asks_again(self):
        repo, cfg = self.repo()
        task = self.new_task(cfg)
        agents = Agents()
        act = drive(cfg, task, agents)
        self.assertIn("build sign-off", act["reason"])
        machine.send_back(cfg, task, "Use the word 'howdy'")                       # CHANGES at the approver's step
        act = drive(cfg, task, agents)
        self.assertEqual(act["action"], "wait")
        self.assertIn("build sign-off", act["reason"])                              # asked again after the fix
        self.assertEqual(agents.calls.count("sdlc-builder"), 2)
        self.assertIn("howdy", [b for a, b in agents.briefs if a == "sdlc-builder"][-1])
        self.assertIn("[x] I1", (Store(cfg).task_dir(task) / "conversation.md").read_text())
        machine.approve(cfg, task, "build")
        self.assertEqual(drive(cfg, task, agents)["action"], "done")

    def test_no_disagreement_without_a_dispute(self):
        repo, cfg = self.repo(merge_gate="none")
        task = self.new_task(cfg)
        agents = Agents()
        agents.test_verdict, agents.upheld = "FAIL", 1                        # the builder only ever says "fixed"
        act = drive(cfg, task, agents)
        self.assertIn("disagreement", act["reason"])                            # the tester still disagrees after 3 rounds
        self.assertEqual(agents.calls.count("sdlc-builder"), 3)

    def test_failed_tests_start_new_rounds(self):
        repo, cfg = self.repo(merge_gate="none")
        task = self.new_task(cfg)
        agents = Agents()
        agents.test_verdict = "FAIL"
        act = drive(cfg, task, agents)
        self.assertIn("disagreement", act["reason"])
        # the tester raises a new blocking issue in each round, so the last one (round 3) is found late and
        # gets one extra round before a person is asked
        self.assertEqual(agents.calls.count("sdlc-tester"), 4)
        extra = [e for e in Store(cfg).events(task) if e["type"] == "round" and e.get("extra")]
        self.assertEqual([(e["stage"], e["r"]) for e in extra], [("build", 4)])

    def test_later_briefs_carry_only_the_open_items(self):
        repo, cfg = self.repo(merge_gate="none")
        task = self.new_task(cfg)
        agents = Agents()
        agents.test_verdict = "FAIL"
        drive(cfg, task, agents)
        builds = [b for a, b in agents.briefs if a == "sdlc-builder"]
        testers = [b for a, b in agents.briefs if a == "sdlc-tester"]
        self.assertNotIn("Open threads addressed to you", builds[0])
        self.assertIn("Open threads addressed to you", builds[1])
        self.assertIn("I1 [blocking] greeting is wrong", builds[1])
        self.assertNotIn("AC-1 ok", builds[1])                               # the coverage table is not resent
        self.assertNotIn("AC-1 ok", testers[1])

    def test_spec_questions_are_asked_then_answered(self):
        repo, cfg = self.repo(merge_gate="none")
        task = self.new_task(cfg)
        agents = Agents()
        agents.questions = True
        act = drive(cfg, task, agents)
        self.assertEqual(act["action"], "done", act)
        self.assertEqual(agents.calls.count("sdlc-spec-writer"), 2)
        self.assertTrue(any(e["type"] == "question" for e in Store(cfg).events(task)))
        i = thread.load(Store(cfg).task_dir(task))[0]["I1"]                     # the question was a thread to the approver
        self.assertEqual((i["by"], i["state"], i["lines"][0]["to"]), ("spec-writer", "closed", "approver"))

    def test_budget_stops_then_approve_reruns_stage(self):
        repo, cfg = self.repo(merge_gate="none", extra={"global": {"budget": {"per_task": 500}}})
        task = self.new_task(cfg)
        agents = Agents()
        st = Store(cfg)
        p = machine.Machine(cfg, task)
        act = p.next_action()                      # spawns the spec-writer
        agents.run(act)
        st.event(task, "agent", key="a1", agent="sdlc-spec-writer", stage="spec", data={"tokens": 900})
        act = drive(cfg, task, agents)
        self.assertIn("budget", act["reason"])
        self.assertEqual(agents.calls, ["sdlc-spec-writer"])
        machine.approve(cfg, task, "budget", "+10k")
        act = drive(cfg, task, agents)
        self.assertEqual(act["action"], "done", act)
        self.assertIn("sdlc-builder", agents.calls)

    def test_protected_path_change_is_caught_at_merge(self):
        repo, cfg = self.repo(merge_gate="none", extra={"global": {"guardrail": {"protected_paths": ["config/**"]}}})
        task = self.new_task(cfg)
        agents = Agents()
        agents.touch = "config/secret.yml"          # as if written through Bash
        act = drive(cfg, task, agents)
        self.assertEqual(act["action"], "wait")
        self.assertIn("rules", act["reason"])
        self.assertIn("config/secret.yml", act["detail"])

    def test_dirty_checkout_waits_and_keeps_user_changes(self):
        repo, cfg = self.repo(merge_gate="none")
        task = self.new_task(cfg)
        agents = Agents()
        (repo / "notes.txt").write_text("x")
        sh("git add notes.txt && git commit -qm notes", repo)
        (repo / "notes.txt").write_text("my unsaved work")
        act = drive(cfg, task, agents)
        self.assertEqual(act["action"], "wait")
        self.assertIn("uncommitted", act["reason"])
        self.assertIn("notes.txt", act["reason"])                 # it says which files
        self.assertEqual((repo / "notes.txt").read_text(), "my unsaved work")

    def test_uncommitted_retro_learnings_in_sdlc_md_do_not_block_the_merge(self):
        repo, cfg = self.repo(merge_gate="none")
        task = self.new_task(cfg)
        with open(repo / ".claude" / "workflows" / "sdlc" / "learnings.md", "a") as f:
            f.write("- [L-1] builder · a learning the retro left uncommitted\n")
        act = drive(cfg, task, Agents())
        self.assertEqual(act["action"], "done", act)
        self.assertIn("a learning", (repo / ".claude" / "workflows" / "sdlc" / "learnings.md").read_text())
        self.assertTrue(act["merged"], act)
        self.assertIn("Merged", (Store(cfg).task_dir(task) / "decision.md").read_text())

    def test_a_step_added_to_the_spec_stage_runs_when_its_rule_matches(self):
        for risk, ran in (("high", True), ("low", False)):
            repo, cfg = self.repo(merge_gate="none", files={".claude/agents/arch.md": ARCH},
                                  edits=[("add", "spec", "review", '{ uses: agent/arch, if: "steps.spec-writer.outputs.risk >= high" }')])
            agents = Agents()
            agents.risk = risk
            self.assertEqual(drive(cfg, self.new_task(cfg), agents)["action"], "done")
            self.assertEqual("arch" in agents.calls, ran, risk)

    def outputs_repo(self, expr, **kw):
        edits = [("key", "build", "tester", "outputs: [sev]"), ("disable", "build", "code-review"),
                 ("disable", "build", "security-review"),
                 ("add", "build", "review", '{ uses: agent/arch, if: "%s" }' % expr)]
        return self.repo(merge_gate="none", files={".claude/agents/arch.md": ARCH}, edits=edits, **kw)

    def test_an_agent_writes_outputs_in_its_frontmatter_and_a_step_below_reads_them(self):
        for sev, ran in (("high", True), ("low", False)):
            repo, cfg = self.outputs_repo("steps.tester.outputs.sev == high")
            agents, task = Agents(), self.new_task(cfg)
            agents.outputs = {"sdlc-tester": {"sev": sev, "ignored": "x"}}
            self.assertEqual(drive(cfg, task, agents)["action"], "done")
            recs = json.loads((Store(cfg).task_dir(task) / "build" / "r1" / "outputs.json").read_text())
            self.assertEqual(recs["tester"], {"outcome": "success", "verdict": "PASS", "outputs": {"sev": sev}})   # only what it declared
            self.assertEqual("arch" in agents.calls, ran, sev)
            self.assertEqual(recs["arch"]["outcome"], "success" if ran else "skipped")
            if not ran:
                self.assertEqual(recs["arch"]["outputs"], {})
            tester_brief = next(b for a, b in agents.briefs if a == "sdlc-tester")
            self.assertIn("Outputs you must write", tester_brief)
            self.assertRegex(tester_brief, r"Steps after yours read them: sev\.")

    def test_a_step_reading_an_agent_of_its_batch_waits_for_the_next_batch(self):
        repo, cfg = self.outputs_repo("steps.tester.outcome == success")
        agents = Agents()
        drive(cfg, self.new_task(cfg), agents)
        self.assertIn(["sdlc-tester"], agents.batches)                # alone: the next step reads what it wrote
        self.assertIn(["arch"], agents.batches)
        repo, cfg = self.outputs_repo("always")
        agents = Agents()
        drive(cfg, self.new_task(cfg), agents)
        self.assertIn(["sdlc-tester", "arch"], agents.batches)        # no reference: they still run together

    def test_a_step_reads_the_outputs_of_an_earlier_stage(self):
        for risk, ran in (("high", True), ("low", False)):
            repo, cfg = self.outputs_repo("stages.spec.steps.spec-writer.outputs.risk >= high")
            agents = Agents()
            agents.risk = risk
            drive(cfg, self.new_task(cfg), agents)
            self.assertEqual("arch" in agents.calls, ran, risk)

    def test_a_failure_nothing_in_the_stage_can_fix_needs_you_and_you_pick_where_it_goes(self):
        repo, cfg = self.repo(merge_gate="none", extra={
            "global": {"command": {"lic": "exit 3"}}}, edits=[("add", "release", "verify", "{ run: lic }")])
        task = self.new_task(cfg)
        agents = Agents()
        act = drive(cfg, task, agents)
        self.assertEqual(act["reason"], "needs-you: failure")                 # no automatic bounce back to Build
        self.assertEqual(agents.calls.count("sdlc-builder"), 1)
        lic = next(i for i in thread.load(Store(cfg).task_dir(task))[0].values() if i["by"] == "lic")
        self.assertTrue(lic["title"].startswith("lic failed (exit 3)"), lic["title"])     # the command's name, not its text
        self.assertEqual((lic["state"], lic["flag"]), ("needs_you", "failed"))           # not "found in the last round"
        machine.send_back(cfg, task, "add the license header", to="builder")
        drive(cfg, task, agents)
        self.assertEqual(agents.calls.count("sdlc-builder"), 2)
        self.assertIn("add the license header", [b for a, b in agents.briefs if a == "sdlc-builder"][-1])
        sent = [(e["from"], e["to"], e["step"]) for e in Store(cfg).events(task) if e["type"] == "send_back"]

        self.assertEqual(sent, [("release", "build", "builder")])

    def test_try_again_runs_the_stage_loop_again_and_accept_goes_past_the_failure(self):
        repo, cfg = self.repo(merge_gate="none", extra={
            "global": {"command": {"lic": "exit 3"}}}, edits=[("add", "release", "verify", "{ run: lic }")])
        task = self.new_task(cfg)
        agents = Agents()
        self.assertEqual(drive(cfg, task, agents)["reason"], "needs-you: failure")
        machine.approve(cfg, task, "retry")
        self.assertEqual(drive(cfg, task, agents)["reason"], "needs-you: failure")      # still failing
        rounds = [e["number"] for e in Store(cfg).events(task) if e["type"] == "round" and e["stage"] == "release"]
        self.assertEqual(rounds, [1, 2])
        machine.approve(cfg, task, "accept")
        self.assertEqual(drive(cfg, task, agents)["action"], "done")

    def test_native_code_review_output_is_parsed_and_classified(self):
        # live run 1: /code-review returns a bare list without severities
        repo, cfg = self.repo(merge_gate="none", max_rounds=1)
        os.environ["FAKE_REVIEW"] = "native"
        task = self.new_task(cfg)
        act = drive(cfg, task, Agents())
        self.assertIn("disagreement", act["reason"])             # the important finding blocks
        front, body = yamlish.parse_frontmatter(
            (Store(cfg).task_dir(task) / "build/r1/code-review.md").read_text())
        self.assertEqual((front["important"], front["nit"]), (1, 1))
        self.assertIn("crashes on empty input", body)
        usage = [e for e in Store(cfg).events(task) if e.get("agent") == "headless:code-review"]
        self.assertEqual(usage[0]["data"]["tokens"], 160 + 160)   # review + classification, via modelUsage

    def test_dirty_tracked_files_in_worktree_are_not_a_conflict(self):
        # live run 1: tracked .pyc files changed by test runs made `git rebase` refuse
        repo, cfg = self.repo(merge_gate="none", files={"cache.bin": "v1"})
        task = self.new_task(cfg)
        agents = Agents()
        real_run = agents.run

        def run(action):
            real_run(action)
            if action["agents"][0]["agent"] == "sdlc-builder":
                wt = Path(Store(cfg).load_task(task)["worktree"])
                (wt / "cache.bin").write_text("changed by a test run")
                (repo / "other.txt").write_text("main moved on")      # so a rebase is really needed
                sh("git add other.txt && git commit -qm 'main moves'", repo)
        agents.run = run
        act = drive(cfg, task, agents)
        self.assertIn("main moved on", (repo / "other.txt").read_text())
        self.assertIn("hello", (repo / "app.py").read_text())             # task merged on top of main
        self.assertEqual(act["action"], "done", act)
        self.assertEqual(agents.calls.count("sdlc-builder"), 1)

    def test_work_a_builder_left_uncommitted_is_committed_for_the_reviewers_and_merged(self):
        # live run 3: Bash was denied to the builder, its fixes stayed uncommitted, the reviewer kept
        # reviewing the old commit and the task ended in a disagreement about work nobody could see
        repo, cfg = self.repo(merge_gate="none")
        agents = Agents()
        agents.commits = False
        act = drive(cfg, self.new_task(cfg), agents)
        self.assertEqual(act["action"], "done", act)
        self.assertIn("hello task", (repo / "app.py").read_text())

    def test_the_briefs_round_is_the_number_within_the_stage(self):
        repo, cfg = self.repo(merge_gate="none")
        task = self.new_task(cfg)
        drive(cfg, task, Agents())
        brief = Store(cfg).task_dir(task) / "briefs" / "build-builder-n2.md"     # the build stage's first round
        self.assertIn("round: 1\n", brief.read_text())

    def test_main_moving_while_the_merge_waits_for_approval_costs_no_agent_and_no_second_approval(self):
        # live run 3: "merge failed: main moved on: epi/task-1 is not ahead of main" after the person approved
        repo, cfg = self.repo(merge_gate="human")
        task = self.new_task(cfg)
        agents = Agents()
        self.assertIn("build sign-off", drive(cfg, task, agents)["reason"])
        (repo / "other.txt").write_text("a commit on main while the approval waited")
        sh("git add other.txt && git commit -qm 'main moves'", repo)
        machine.approve(cfg, task, "build")
        calls = list(agents.calls)
        self.assertEqual(drive(cfg, task, agents)["action"], "done")
        self.assertEqual(agents.calls, calls)                            # nobody ran again
        self.assertEqual(sum(1 for e in Store(cfg).events(task) if e["type"] == "approval" and e["status"] == "waiting"), 1)
        self.assertTrue((repo / "other.txt").exists() and "hello" in (repo / "app.py").read_text())

    def test_the_human_gate_lists_every_open_issue(self):
        repo, cfg = self.repo(merge_gate="none", max_rounds=2)
        agents = Agents()
        agents.test_verdict = "FAIL"
        act = drive(cfg, self.new_task(cfg), agents)
        self.assertIn("greeting is wrong", act["detail"])
        self.assertIn("still open after 2 rounds", act["detail"])

    def test_deleting_a_task_folder_breaks_nothing(self):
        repo, cfg = self.repo(merge_gate="none")
        st = Store(cfg)
        j1 = self.new_task(cfg)
        drive(cfg, j1, Agents())
        tokens = st.month_tokens()
        shutil.rmtree(st.task_dir(j1))
        self.assertEqual(st.tasks(), [])
        self.assertEqual(st.month_tokens(), tokens)                         # ledger keeps the spend
        j2 = self.new_task(cfg)
        self.assertEqual(int(j2), int(j1) + 1)                              # number never reused
        self.assertEqual(drive(cfg, j2, Agents())["action"], "done")

    def test_retro_writes_learnings_to_sdlc_md_and_briefs_use_them(self):
        repo, cfg = self.repo(merge_gate="none")
        task = self.new_task(cfg)
        drive(cfg, task, Agents())
        res = learn.retro(cfg, task)
        self.assertEqual(len(res["added"]), 1)
        self.assertIn("seed script", res["added"][0])
        self.assertIn("Run the seed script", (repo / ".claude" / "workflows" / "sdlc" / "learnings.md").read_text())  # written to learnings.md, not committed
        self.assertEqual(learn.retro(cfg, task), {"skipped": "retro already ran"})
        cfg2 = config.load(repo)
        j2 = self.new_task(cfg2, "Second")
        agents = Agents()
        agents.run(machine.Machine(cfg2, j2).next_action())           # spec-writer
        act = machine.Machine(cfg2, j2).next_action()                 # builder
        self.assertEqual(act["agents"][0]["agent"], "sdlc-builder")
        build_brief = next((Store(cfg2).task_dir(j2) / "briefs").glob("build-builder*.md")).read_text()
        self.assertIn("seed script", build_brief)                           # builder sees the learning


if __name__ == "__main__":
    unittest.main()

class ShortTitleTest(unittest.TestCase):
    def test_the_merge_commit_uses_the_specs_short_title(self):
        repo = make_repo(base_md("none", 3))
        self.addCleanup(shutil.rmtree, repo.parent, ignore_errors=True)
        cfg = config.load(repo)
        task = Store(cfg).create_task("Please add a really long winded greeting feature " * 4)["id"]
        agents = Agents()
        real = machine.Machine._short_title
        seen = []
        machine.Machine._short_title = lambda self: seen.append(real(self)) or seen[-1]
        self.addCleanup(setattr, machine.Machine, "_short_title", real)
        drive(cfg, task, agents)
        self.assertTrue(seen and all(len(t) <= 60 for t in seen))
        self.assertIn("Please add a really long", seen[0])
        spec = Store(cfg).task_dir(task) / "spec.md"
        spec.write_text(spec.read_text().replace("---\nrisk", "---\ntitle: Greeting\nrisk", 1))
        self.assertEqual(real(machine.Machine(cfg, task)), "Greeting")


class SkillReviewTest(unittest.TestCase):
    """A skill reviewer is given the spec, its open threads and its previous report (not only the diff)."""

    def setUp(self):
        self.repo = make_repo("---\n---\n")
        self.addCleanup(shutil.rmtree, self.repo.parent, ignore_errors=True)
        self.cfg = config.load(self.repo)
        self.calls = []
        from engine import runners
        self.runners, self.real = runners, runners.headless

        def fake(prompt, cwd, system_append="", model=None, disallow_edits=True, timeout=3600):
            self.calls.append((prompt, system_append))
            return ("```json\n" + json.dumps({"findings": [
                {"severity": "important", "file": "a.py", "line": 1, "summary": "AC-3 is wrong"}],
                "replies": [{"id": "I1", "type": "accept", "text": "fixed"}]}) + "\n```", {"tokens": 1})
        runners.headless = fake
        self.addCleanup(setattr, runners, "headless", self.real)

    def run_review(self, **kw):
        out = self.repo / "out.md"
        self.runners.skill_review(self.cfg, "code-review", self.repo, "main", out, to="builder", spec="# Spec\nAC-3 prints hi",
                                  **kw)
        return out.read_text()

    def test_the_first_round_gets_the_spec_and_findings_go_to_the_do_step(self):
        text = self.run_review()
        prompt, system = self.calls[0]
        self.assertIn("AC-3 prints hi", system)
        self.assertIn("main...HEAD", prompt)
        self.assertNotIn("later round", system)
        self.assertIn("- new -> builder, feedback, blocking:", text)

    def test_a_later_round_gets_its_threads_its_previous_report_and_only_the_diff_since_last_time(self):
        text = self.run_review(threads="I1 [blocking] bad input", previous="## Comments\n- new -> builder, feedback, blocking: bad input",
                               since="abc123", recheck="full")
        prompt, system = self.calls[0]
        self.assertIn("abc123...HEAD", prompt)
        self.assertIn("I1 [blocking] bad input", system)
        self.assertIn("Your previous report", system)
        self.assertIn("- I1 -> builder, accept: fixed", text)
        self.assertIn("head: ", text)                                   # the commit this review looked at

    def test_a_later_round_is_by_default_one_small_recheck_on_the_judge_model_not_a_full_review(self):
        models = []
        real = self.runners.headless
        self.runners.headless = lambda prompt, cwd, system_append="", model=None, **kw: (
            models.append(model) or real(prompt, cwd, system_append=system_append, model=model))
        self.addCleanup(setattr, self.runners, "headless", real)
        self.run_review(threads="I1 [blocking] bad input", previous="", since=self.runners.git_head(self.repo))
        prompt, system = self.calls[0]
        self.assertTrue(prompt.startswith("Re-check the open threads"))
        self.assertNotIn("/code-review", prompt)
        self.assertEqual(models[0], "haiku")                           # global.pipeline.judge_model
        self.assertIn("never raise wording", system)

    def test_the_classifier_defines_blocking_strictly_and_rechecks_claimed_important_findings(self):
        seen = []
        real = self.runners.headless
        self.runners.headless = lambda prompt, cwd, **kw: (seen.append(prompt) or ('["nit", "important"]', {"tokens": 1}))
        self.addCleanup(setattr, self.runners, "headless", real)
        found = [{"severity": "important", "summary": "docstring does not state the unit"},
                 {"summary": "crashes on empty input"}, {"severity": "nit", "summary": "rename-me"}]
        out, _ = self.runners.classify(found, "", self.repo)
        self.assertEqual([f["severity"] for f in out], ["nit", "important", "nit"])
        self.assertIn("a correctness bug, a security problem, a violation of the spec, or broken tests", seen[0])
        self.assertNotIn("rename-me", seen[0])                           # a nit is never sent up again

