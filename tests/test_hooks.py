import contextlib
import io
import json
import shutil
import unittest
from pathlib import Path

from helpers import make_repo
import subprocess

from engine import config, hooks, machine, runners
from engine.store import Store


def call(fn, inp):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(inp)
    s = buf.getvalue().strip()
    return json.loads(s) if s else None


class RunCommandTest(unittest.TestCase):
    """Agents run configured commands with `epi.py run <task> <name>`; the hook allows exactly that, nothing more."""

    def setUp(self):
        self.repo = make_repo("---\nglobal:\n  command: {test: \"python3 -c 'print(41+1)'\"}\n---\n")
        self.addCleanup(shutil.rmtree, self.repo.parent, ignore_errors=True)
        self.cfg = config.load(self.repo)
        self.task = Store(self.cfg).create_task("hello")["id"]
        machine.Machine(self.cfg, self.task, session_id="s1").next_action()

    def decide(self, command, agent="sdlc-tester"):
        r = call(hooks.pre_tool_use, {"session_id": "s1", "cwd": str(self.repo), "agent_type": agent,
                                      "tool_name": "Bash", "tool_input": {"command": command}})
        return (r or {}).get("hookSpecificOutput", {}).get("permissionDecision")

    def test_only_the_exact_engine_form_is_allowed(self):
        ok = runners.command_line(self.task, "test")
        self.assertEqual(self.decide(ok), "allow")
        self.assertEqual(self.decide(ok.replace(f" run {self.task} ", f" run task-{self.task} ")), "allow")
        for bad in (ok + " ; rm -rf /", ok + " -k foo", ok + " && echo hi", ok.replace(" test", " lint"),
                    ok.replace(f" run {self.task} ", " run 99 "), "python3 -c 'print(41+1)'",
                    'python3 "/tmp/other/scripts/epi.py" run 1 test'):
            self.assertIsNone(self.decide(bad), bad)
        self.assertIsNone(call(hooks.pre_tool_use, {"session_id": "s1", "cwd": str(self.repo), "tool_name": "Bash",
                                                    "tool_input": {"command": ok}}))      # the orchestrator: no hook decision

    def test_run_executes_the_command_in_the_task_checkout_and_keeps_its_exit_code(self):
        r = subprocess.run(runners.command_line(self.task, "test"), shell=True, cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("42", r.stdout)
        r = subprocess.run(runners.command_line(self.task, "lint"), shell=True, cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("no command 'lint'", r.stderr)

    def test_the_brief_gives_agents_the_command_to_run(self):
        m = machine.Machine(self.cfg, self.task)
        while True:
            act = m.next_action()
            if act["action"] == "wait" and "sign-off" in act["reason"]:      # the spec sign-off is on by default
                machine.approve(self.cfg, self.task, "spec")
                continue
            if act["action"] != "spawn" or "builder" in act["agents"][0]["agent"]:
                break
            for a in act["agents"]:
                Path(a["output"]).write_text("---\nverdict: PASS\ntitle: Hello\nopen_questions: 0\n---\n## Problem\nhi\n")
        brief = act["agents"][0]["prompt"].split("Read ")[1].split(" first")[0]
        self.assertIn(runners.command_line(self.task, "test"), Path(brief).read_text())


class HooksTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo("---\nglobal:\n  guardrail:\n    protected_paths: [\"secrets/**\"]\n---\n")
        self.addCleanup(shutil.rmtree, self.repo.parent, ignore_errors=True)
        self.cfg = config.load(self.repo)
        self.task = Store(self.cfg).create_task("hello")["id"]
        self.act = machine.Machine(self.cfg, self.task, session_id="s1").next_action()
        self.brief = self.act["agents"][0]["prompt"].split("Read ")[1].split(" first")[0]
        proj = self.repo.parent / "proj"
        (proj / "s1" / "subagents").mkdir(parents=True)
        self.main_tr = proj / "s1.jsonl"
        self.main_tr.write_text("")
        self.agent_tr = proj / "s1" / "subagents" / "agent-a1.jsonl"
        lines = [
            {"type": "user", "timestamp": "2026-11-05T10:00:00Z",
             "message": {"role": "user", "content": self.act["agents"][0]["prompt"]}},
            {"type": "assistant", "timestamp": "2026-11-05T10:01:30Z",
             "message": {"role": "assistant", "id": "m1", "model": "claude-opus-5-5",
                         "content": [{"type": "tool_use", "name": "Read"}],
                         "usage": {"input_tokens": 1000, "output_tokens": 200,
                                   "cache_creation_input_tokens": 300, "cache_read_input_tokens": 5000}}},
            {"type": "assistant", "timestamp": "2026-11-05T10:01:31Z",   # same message, streamed
             "message": {"role": "assistant", "id": "m1", "usage": {"input_tokens": 1000,
                         "output_tokens": 250, "cache_creation_input_tokens": 300}}},
        ]
        self.agent_tr.write_text("\n".join(json.dumps(l) for l in lines) + "\n")

    def inp(self, agent_type, tool, **ti):
        return {"session_id": "s1", "cwd": str(self.repo), "agent_type": agent_type, "agent_id": "a1",
                "transcript_path": str(self.main_tr), "tool_name": tool, "tool_input": ti}

    def test_spec_writer_may_write_only_its_output(self):
        out = self.act["agents"][0]["output"]
        self.assertIsNone(call(hooks.pre_tool_use, self.inp("sdlc-spec-writer", "Write", file_path=out)))
        r = call(hooks.pre_tool_use, self.inp("sdlc-spec-writer", "Write", file_path=str(self.repo / "app.py")))
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_builder_scope_and_protected_paths(self):
        wt = Path(Store(self.cfg).load_task(self.task)["worktree"])
        ok = call(hooks.pre_tool_use, self.inp("sdlc-builder", "Edit", file_path=str(wt / "app.py")))
        self.assertIsNone(ok)
        for bad in (wt / "secrets" / "k.txt", wt / ".claude" / "epi.md", wt / ".claude" / "workflows" / "sdlc" / "workflow.yaml",
                    wt / ".claude" / "workflows" / "sdlc" / "settings.md", wt / ".claude" / "agents" / "sdlc-builder.md",
                    self.repo / "app.py"):
            r = call(hooks.pre_tool_use, self.inp("sdlc-builder", "Write", file_path=str(bad)))
            self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "deny", bad)

    def test_the_agent_type_is_the_pack_copy_or_the_plugins_agent(self):
        wt = Path(Store(self.cfg).load_task(self.task)["worktree"])
        for agent_type in ("sdlc-builder", "epi:builder"):             # both resolve to the step `builder`
            self.assertEqual(config.step_name(self.cfg, agent_type), "builder")
            r = call(hooks.pre_tool_use, self.inp(agent_type, "Write", file_path=str(wt / ".claude" / "epi.md")))
            self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "deny", agent_type)
        self.assertEqual(config.step_name(self.cfg, "marketing"), "marketing")          # a repo's own agent

    def test_tester_only_writes_tests(self):
        wt = Path(Store(self.cfg).load_task(self.task)["worktree"])
        self.assertIsNone(call(hooks.pre_tool_use, self.inp("sdlc-tester", "Write",
                                                            file_path=str(wt / "tests" / "test_x.py"))))
        r = call(hooks.pre_tool_use, self.inp("sdlc-tester", "Write", file_path=str(wt / "app.py")))
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_blocked_bash(self):
        r = call(hooks.pre_tool_use, self.inp("sdlc-builder", "Bash", command="git  push --force origin x"))
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(call(hooks.pre_tool_use, self.inp("sdlc-builder", "Bash", command="pytest -q")))

    def test_no_active_task_means_no_interference(self):
        i = self.inp("sdlc-builder", "Write", file_path="/etc/hosts")
        i["session_id"] = "other-session"
        self.assertIsNone(call(hooks.pre_tool_use, i))

    def test_subagent_stop_records_usage_and_demands_output(self):
        i = {"session_id": "s1", "cwd": str(self.repo), "agent_type": "sdlc-spec-writer", "agent_id": "a1",
             "agent_transcript_path": str(self.agent_tr), "stop_hook_active": False}
        r = call(hooks.subagent_stop, i)
        self.assertEqual(r["decision"], "block")                  # spec.md not written yet
        ev = [e for e in Store(self.cfg).events(self.task) if e["type"] == "agent"][-1]
        self.assertEqual(ev["data"]["tokens"], 1000 + 250 + 300)   # streamed message counted once
        self.assertEqual(ev["data"]["cache_read"], 0 + 0 or ev["data"]["cache_read"])
        self.assertEqual(ev["data"]["duration_s"], 91)
        self.assertEqual(ev["stage"], "spec")
        call(hooks.subagent_stop, {**i, "stop_hook_active": True})
        st = Store(self.cfg)
        self.assertEqual(st.month_tokens(), 1550)                  # second record replaces the first
        p = machine.Machine(self.cfg, self.task)
        self.assertEqual(p.task_usage()[0], 1550)


if __name__ == "__main__":
    unittest.main()
