"""The simulator's tasks land where it says (it doubles as a check of every situation a person meets), plus the
liveness signals it shows: stalled tasks and context fill."""
import contextlib
import datetime as dt
import io
import json
import shutil
import unittest

import sim
from helpers import make_repo
from engine import config, hooks, machine, serve, taskview, usage
from engine.store import Store
from test_machine import base_md


class SimTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo, cls.ids = sim.seed()
        cls.cfg = config.load(cls.repo, lenient=True)
        cls.cards = {k: taskview.card(cls.cfg, Store(cls.cfg).load_task(t)) for k, t in cls.ids.items()}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.repo.parent, ignore_errors=True)

    def test_every_scenario_needs_what_it_says(self):
        for name, reason in sim.EXPECT.items():
            got = (self.cards[name]["needs_you"] or {}).get("reason")
            self.assertEqual(got, reason, name)
        self.assertEqual(self.cards["done"]["status"], "done")

    def test_the_notifier_ran_only_when_security_failed_and_got_its_findings(self):
        sent = (self.cfg.worktree_dir / "notified").read_text().splitlines()
        self.assertEqual(sent, [f"#sec-alerts|{sim.FINDING}"])             # once: round 1; the security review passed in round 2
        d = taskview.task(self.cfg, Store(self.cfg).load_task(self.ids["security_alert"]))
        rounds = next(r for r in d["progress"] if r["id"] == "build")["rounds"]
        step = lambda r, i: next(s for s in rounds[r]["steps"] if s["id"] == i)
        self.assertEqual((step(0, "security")["outcome"], step(0, "notify")["outcome"]), ("failure", "success"))
        self.assertEqual(step(0, "security")["outputs"][0]["value"], sim.FINDING)
        self.assertEqual(step(0, "notify")["state"], "done")
        self.assertEqual((step(1, "security")["outcome"], step(1, "security")["outputs"][0]["value"]), ("success", ""))
        self.assertEqual((step(1, "notify")["outcome"], step(1, "notify")["outputs"]), ("skipped", []))
        for name, t in self.ids.items():                                    # no other task's security review failed
            for f in (Store(self.cfg).task_dir(t) / "build").glob("r*/outputs.json") if name != "security_alert" else ():
                self.assertNotEqual(json.loads(f.read_text()).get("notify", {}).get("outcome"), "success", f"{name} {f}")

    def test_the_quiet_task_is_stalled_and_listed_as_needing_you(self):
        c = self.cards["stalled"]
        self.assertTrue(c["stalled"])
        self.assertGreaterEqual(c["quiet_minutes"], 25)
        self.assertFalse(any(self.cards[k]["stalled"] for k in self.cards if k != "stalled"))
        needs = serve.board(self.cfg)["needs_you"]
        self.assertIn("stalled", [n["reason"] for n in needs if n["task"] == self.ids["stalled"]])

    def test_the_drawer_shows_how_full_the_builder_context_got(self):
        d = taskview.task(self.cfg, Store(self.cfg).load_task(self.ids["done"]))
        build = next(r for r in d["progress"] if r["id"] == "build") if isinstance(d["progress"], list) else None
        steps = [s for r in (build or {}).get("rounds", []) for s in r["steps"] if s["id"] == "builder"]
        self.assertEqual(steps[0]["who"]["context"], 0.86)          # 172k of the default 200k window

    def test_the_strip_has_a_square_per_step_with_its_state(self):
        strip = self.cards["disagreement"]["strip"]
        self.assertTrue(strip)
        self.assertEqual(set(strip[0]), {"label", "state"})
        self.assertIn("changes", [s["state"] for s in strip])
        self.assertIn("pass", [s["state"] for s in strip])
        self.assertIn("running", [s["state"] for s in self.cards["stalled"]["strip"]])
        self.assertEqual(self.cards["done"]["strip"], [])

    def test_the_board_lists_recent_events_newest_first_with_their_task(self):
        feed = serve.board(self.cfg)["activity"]
        self.assertTrue(feed)
        self.assertEqual([a["ts"] for a in feed], sorted((a["ts"] for a in feed), reverse=True))
        self.assertLessEqual(len(feed), 30)
        self.assertTrue(all(set(a) == {"task", "ts", "text"} and a["text"] for a in feed))
        self.assertIn("merged", [a["text"] for a in feed if a["task"] == self.ids["done"]])

    def test_feed_names_the_stage_and_never_calls_a_send_back_a_wait(self):
        from engine import taskview
        name = lambda sid: {"build": "Build"}[sid]
        say = lambda status, **kw: taskview.ACTIVITY["approval"]({"approval": "build", "status": status, **kw}, name)
        self.assertEqual(say("approved", by="ana"), "Build signed off by ana")
        self.assertEqual(say("waiting"), "Build waiting for your sign-off")
        self.assertIsNone(say("changes"))           # its send_back event says what happened

class LivenessTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo(base_md("none", 3))
        self.addCleanup(shutil.rmtree, self.repo.parent, ignore_errors=True)
        self.cfg = config.load(self.repo)
        self.st = Store(self.cfg)
        self.task = self.st.create_task("hello")["id"]
        machine.Machine(self.cfg, self.task, session_id="s1").next_action()     # running, session linked

    def card(self):
        return taskview.card(self.cfg, self.st.load_task(self.task))

    def test_a_tool_call_by_the_session_is_a_heartbeat(self):
        sim.backdate(self.cfg, self.task, minutes=30)
        self.assertTrue(self.card()["stalled"])
        with contextlib.redirect_stdout(io.StringIO()):
            hooks.pre_tool_use({"session_id": "s1", "cwd": str(self.repo), "tool_name": "Bash",
                                "tool_input": {"command": "ls"}})
        self.assertFalse(self.card()["stalled"])

    def test_a_long_command_is_not_taken_for_a_stall(self):
        sim.backdate(self.cfg, self.task, minutes=30)
        at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)).timestamp()
        (self.st.task_dir(self.task) / "heartbeat").write_text(json.dumps({"at": at, "until": at + 3600}))
        self.assertFalse(self.card()["stalled"])

    def test_a_task_waiting_for_a_person_is_never_stalled(self):
        self.st.update_task(self.task, session={"id": "s1", "closing": True})
        sim.backdate(self.cfg, self.task, minutes=30)
        self.assertFalse(self.card()["stalled"])

    def test_context_is_the_fullest_request_not_the_sum(self):
        f = self.repo.parent / "t.jsonl"
        rows = [{"type": "assistant", "message": {"id": f"m{i}", "role": "assistant", "usage": {
            "input_tokens": 10, "cache_creation_input_tokens": c, "cache_read_input_tokens": r, "output_tokens": 5}}}
            for i, (c, r) in enumerate([(1000, 0), (500, 40_000), (200, 90_000)])]
        f.write_text("".join(json.dumps(x) + "\n" for x in rows))
        self.assertEqual(usage.summarize(f)["context"], 90_210)
