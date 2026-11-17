import json
import shutil
import threading
import unittest
import urllib.request
import urllib.error

from helpers import make_repo
from test_machine import Agents, drive, base_md
from engine import config, machine, serve
from engine.store import Store


class ServeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = make_repo(base_md("none", 3))
        cls.cfg = config.load(cls.repo)
        cls.task = cls.task_done = Store(cls.cfg).create_task("Say hello")["id"]
        drive(cls.cfg, cls.task, Agents())
        serve.Handler.token, serve.Handler.root = "tok", cls.repo
        cls.srv = serve.Server(("127.0.0.1", 0), serve.Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    def setUp(self):                       # the handler's settings are shared: point them at this class's repo
        serve.Handler.token, serve.Handler.root = "tok", self.repo

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.repo.parent, ignore_errors=True)

    def req(self, path, body=None, token="tok", origin=None):
        h = {"X-Epi-Token": token} if token else {}
        if body is not None:
            h["Content-Type"] = "application/json"
        if origin:
            h["Origin"] = origin
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                   headers=h, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(r) as resp:
                t = resp.read().decode()
                return resp.status, (json.loads(t) if resp.headers.get_content_type() == "application/json" else t)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def test_token_required(self):
        self.assertEqual(self.req("/api/board", token=None)[0], 403)

    def test_another_host_name_is_refused_even_with_the_token(self):
        """DNS rebinding: a page on evil.example that points its name at 127.0.0.1 still sends its own Host."""
        port = self.srv.server_address[1]
        for host, code in ((f"evil.example:{port}", 421), (f"localhost:{port}", 200), (f"127.0.0.1:{port}", 200)):
            r = urllib.request.Request(self.base + "/api/board", headers={"X-Epi-Token": "tok", "Host": host})
            try:
                with urllib.request.urlopen(r) as resp:
                    got = resp.status
            except urllib.error.HTTPError as e:
                got = e.code
            self.assertEqual(got, code, host)
        self.assertEqual(self.req("/api/board", token="wrong")[0], 403)

    def test_board_and_task(self):
        code, b = self.req("/api/board")
        self.assertEqual(code, 200)
        self.assertEqual(b["tasks"][0]["status"], "done")
        self.assertEqual(b["columns"], ["queued", "spec", "build", "release", "done"])      # a column per stage
        self.assertEqual(b["labels"]["build"], "Build")
        code, j = self.req(f"/api/task?id={self.task}")
        by = {x["id"]: x for x in j["progress"]}
        self.assertEqual([x["state"] for x in j["progress"]], ["done", "done", "done"])
        self.assertEqual((by["build"]["verdict"], by["build"]["rounds_used"], by["build"]["name"]), ("PASS", 1, "Build"))
        steps = {x["id"]: x for x in by["build"]["rounds"][0]["steps"]}
        self.assertEqual(steps["code-review"]["report"], "build/r1/code-review.md")
        self.assertEqual((steps["code-review"]["state"], steps["code-review"]["label"]), ("done", "Code review"))
        self.assertEqual((steps["security-review"]["state"], steps["security-review"]["reason"].startswith("its `if`")), ("left out", True))
        self.assertEqual((steps["lint"]["kind"], steps["lint"]["phase"]), ("run", "verify"))
        self.assertEqual(steps["builder"]["phase"], "do")
        code, t = self.req(f"/api/file?task={self.task}&path=spec.md")
        self.assertIn("risk: low", t)

    def test_backlog_card_crud(self):
        st = Store(self.cfg)
        st.add_backlog("Legacy one-liner")
        code, r = self.req("/api/backlog", {"action": "add", "title": "Add CSV export",
                                            "description": "Line one\n\nLine three", "priority": "high"})
        self.assertEqual(code, 200, r)
        items = self.req("/api/board")[1]["backlog_items"]
        it = next(i for i in items if i["id"] == r["id"])
        self.assertEqual((it["description"], it["priority"]), ("Line one\n\nLine three", "high"))
        self.assertIn("Legacy one-liner", [i["title"] for i in items])
        self.assertEqual(self.req("/api/backlog", {"action": "update", "id": r["id"], "title": "CSV",
                                                   "description": "new"})[0], 200)
        it = next(i for i in st.backlog_items() if i["id"] == r["id"])
        self.assertEqual((it["title"], it["description"], it["priority"]), ("CSV", "new", "high"))
        self.assertEqual(self.req("/api/backlog", {"action": "add", "title": " "})[0], 400)
        self.assertEqual(self.req("/api/backlog", {"action": "delete", "id": "nope"})[0], 400)
        self.assertEqual(st.pop_backlog_item("CSV")["description"], "new")
        self.assertEqual(st.pop_backlog(), "Legacy one-liner")

    def test_alerts_stored_in_task_folder_and_cleared(self):
        task = Store(self.cfg).create_task("Alerts")["id"]
        st = Store(self.cfg)
        st.event(task, "needs_you", reason="budget", detail="over")
        st.event(task, "failed", reason="boom")
        st.event(task, "budget", level="warn", scope="task", used=8, limit=10)
        self.assertEqual([a["kind"] for a in st.alerts(task)], ["needs_you", "needs_you", "budget-warning"])
        self.assertTrue((st.task_dir(task) / "alerts.json").exists())
        self.assertEqual([a["kind"] for a in self.req(f"/api/task?id={task}")[1]["details"]["alerts"]], ["budget-warning"])
        self.assertEqual(self.req("/api/board")[1]["tasks"][-1]["alerts"], 1)
        self.req("/api/alerts/clear", {"task": task})
        self.assertEqual(self.req(f"/api/task?id={task}")[1]["details"]["alerts"], [])
        self.assertNotIn("needs-you", self.req("/api/board")[1]["columns"])

    def test_delete_learning(self):
        sd = self.repo / ".claude" / "workflows" / "sdlc" / "learnings.md"
        sd.write_text(sd.read_text() + "\n## Learnings\n- [L-abcd] builder · Do X (task-1)\n- [L-ef01] all · Keep Y (task-2)\n")
        self.req("/api/learnings/delete", {"id": "L-abcd"})
        self.assertNotIn("L-abcd", sd.read_text())
        self.assertIn("L-ef01", sd.read_text())
        self.assertEqual(self.req("/api/learnings/delete", {"id": "L-abcd"})[0], 400)

    def test_update_learning(self):
        sd = self.repo / ".claude" / "workflows" / "sdlc" / "learnings.md"
        sd.write_text(sd.read_text() + "\n## Learnings\n- [L-abcd] builder · Do X (task-1)\n")
        self.req("/api/learnings/update", {"id": "L-abcd", "roles": ["tester", "builder"], "text": "Do Z"})
        self.assertIn("- [L-abcd] tester, builder · Do Z", sd.read_text())
        self.assertEqual(self.req("/api/learnings/update", {"id": "L-nope", "text": "x"})[0], 400)
        self.assertEqual(self.req("/api/learnings/update", {"id": "L-abcd", "text": " "})[0], 400)

    def test_help(self):
        code, t = self.req("/api/help")
        self.assertEqual(code, 200)
        self.assertIn("/epi:review", t)

    def test_human_required_is_a_flag_not_a_column(self):
        self.assertNotIn("needs-you", self.req("/api/board")[1]["columns"])
        task = Store(self.cfg).create_task("Stuck task")["id"]
        agents = Agents()
        agents.test_verdict, agents.dispute, agents.upheld = "FAIL", True, 1
        drive(self.cfg, task, agents)
        j = {x["id"]: x for x in self.req("/api/board")[1]["tasks"]}[task]
        self.assertEqual((j["status"], j["stage"], j["needs_you"]["reason"]), ("build", "build", "disagreement"))
        self.assertEqual(j["text"], "Needs you: a disagreement to settle")
        board = self.req("/api/board")[1]
        self.assertIn(f"task-{task} · Build · a disagreement to settle", [n["text"] for n in board["needs_you"]])
        d = self.req(f"/api/task?id={task}")[1]
        self.assertEqual((d["decision"]["title"], d["decision"]["actions"]), ("Your decision", ["accept", "send_back"]))
        self.assertEqual([t["value"] for t in d["decision"]["targets"]], ["build/builder", "spec/spec-writer"])
        self.assertEqual(d["decision"]["targets"][0]["label"], "Builder (Build)")
        self.assertEqual([(i["id"], i["by"]) for i in d["decision"]["issues"]], [("I1", "tester")])
        self.assertIn("Builder disagrees", d["decision"]["issues"][0]["tags"])                   # the builder answered it
        self.assertEqual({a["kind"] for a in d["details"]["alerts"]}, set())          # needs-you shows in the box, not as an alert
        self.assertIsNone({x["id"]: x for x in board["tasks"]}[self.task_done]["needs_you"])

    def test_a_done_task_raises_no_alert(self):
        self.assertEqual(self.req(f"/api/task?id={self.task}")[1]["details"]["alerts"], [])
        self.assertEqual(self.req("/api/board")[1]["tasks"][0]["alerts"], 0)
        self.assertIsNone(self.req("/api/board")[1]["tasks"][0]["needs_you"])

    def test_applied_learnings_come_with_their_text(self):
        import tempfile
        from pathlib import Path
        repo = make_repo("---\n---\n\n## Answers\n- [A-0001] all · Auth on new endpoints? Yes. (task-1)\n\n"
                         "## Learnings\n- [L-ab12] builder, tester · Run the seed first. (task-1)\n")
        self.addCleanup(shutil.rmtree, repo.parent, ignore_errors=True)
        cfg = config.load(repo)
        d = Path(tempfile.mkdtemp())
        (d / "build.md").write_text("applied: L-ab12\napplied: A-0001\napplied: L-dead\n")
        got = {e["id"]: e for e in serve._applied_entries(cfg, d)}
        self.assertEqual(got["L-ab12"]["text"], "Run the seed first. (task-1)")
        self.assertEqual(got["L-ab12"]["roles"], ["builder", "tester"])
        self.assertIn("Auth on new endpoints", got["A-0001"]["text"])
        self.assertEqual((got["L-dead"]["removed"], got["L-dead"]["text"]), (True, ""))     # deleted from the file since

    def test_task_detail_carries_the_conversation(self):
        task = Store(self.cfg).create_task("Talk")["id"]
        d = Store(self.cfg).task_dir(task)
        (d / "build" / "r1").mkdir(parents=True)
        (d / "build" / "r1" / "code-review.md").write_text(
            "---\nverdict: CHANGES\n---\n## Comments\n- new -> builder, feedback, blocking: Bad input\n")
        (d / "build" / "r2").mkdir(parents=True)
        (d / "build" / "r2" / "builder.md").write_text("---\nround: 2\n---\n## Comments\n- I1 -> code-review, fixed: validated\n")
        p = machine.Machine(self.cfg, task)
        p.ev("stage", stage="build")
        for n in (1, 2):
            p.ev("round", n=n, stage="build", number=n, r=n, sub="do")
            p.ingest(p.state(), "build", n)
        conv = self.req(f"/api/task?id={task}")[1]["conversation"]
        got = conv["in_progress"]
        self.assertEqual([(t["id"], t["state"], t["severity"], t["by"], t["round"]) for t in got], [("I1", "open", "blocking", "code-review", 1)])
        self.assertEqual([(x["from"], x["to"], x["type"]) for x in got[0]["lines"]],
                         [("code-review", "builder", "feedback"), ("builder", "code-review", "fixed")])
        self.assertEqual((conv["needs_decision"], conv["resolved"], conv["notes"]), ([], [], []))

    def test_delete_only_a_done_task(self):
        stuck = Store(self.cfg).create_task("Not finished")["id"]
        self.assertEqual(self.req("/api/task/delete", {"id": stuck})[0], 400)
        self.assertEqual(self.req("/api/task/delete", {"id": "999"})[0], 400)
        task = Store(self.cfg).create_task("Finished")["id"]
        machine.Machine(self.cfg, task).ev("stage", stage="done")
        code, r = self.req("/api/task/delete", {"id": task})
        self.assertEqual((code, r), (200, {"deleted": task}))
        self.assertFalse(Store(self.cfg).task_dir(task).exists())
        self.assertNotIn(task, [x["id"] for x in self.req("/api/board")[1]["tasks"]])

    def test_principle_endpoints(self):
        code, s = self.req("/api/settings")
        self.assertEqual(s["effective"]["global"]["principle"]["list"], {})
        code, r = self.req("/api/principle/save", {"name": "api", "front": {"summary": "REST", "applies_to": ["builder"]},
                                                   "body": "Plural nouns."})
        self.assertEqual((code, r), (200, {"saved": ".claude/principles/api.md"}))
        code, s = self.req("/api/settings")
        self.assertEqual(s["effective"]["global"]["principle"]["list"]["api"]["applies_to"], ["builder"])
        self.assertEqual(self.req("/api/principle/save", {"name": "bad name", "body": "x"})[0], 400)
        self.assertEqual(self.req("/api/principle/save", {"name": "empty", "body": ""})[0], 400)
        self.assertEqual(self.req("/api/principle/delete", {"name": "api"})[1], {"removed": ".claude/principles/api.md"})
        self.assertEqual(self.req("/api/principle/delete", {"name": "api"})[0], 400)

    def test_no_path_traversal(self):
        self.assertEqual(self.req(f"/api/file?task={self.task}&path=../../.claude/epi.md")[0], 404)

    def test_settings_preview_save_and_stale_hash(self):
        code, s = self.req("/api/settings")
        self.assertIn("tabs", s["schema"])
        code, p = self.req("/api/settings/preview", {"changes": {"stage.build.rounds": 4}})
        self.assertIn("rounds: 4", p["after"])
        self.assertEqual(p["files"], [".claude/workflows/sdlc/settings.md"])            # a stage setting is the pack's
        code, r = self.req("/api/settings", {"changes": {"stage.build.rounds": 4}, "hash": s["hash"]})
        self.assertEqual(code, 200, r)
        self.assertIn("rounds: 4", (self.repo / ".claude" / "workflows" / "sdlc" / "settings.md").read_text())
        self.assertEqual(r["hash"]["global"], s["hash"]["global"])                      # epi.md was not touched
        code, r = self.req("/api/settings", {"changes": {"stage.build.rounds": 5}, "hash": s["hash"]})
        self.assertEqual(code, 400)                                # stale hash rejected
        self.assertIn("changed on disk", r["error"])

    def test_invalid_settings_rejected(self):
        code, s = self.req("/api/settings")
        code, r = self.req("/api/settings", {"changes": {"stage.release.strategy": "sometimes"}, "hash": s["hash"]})
        self.assertEqual(code, 400)

    def test_the_settings_page_renders_from_the_merged_schema(self):
        code, s = self.req("/api/settings")
        tabs = {t["id"]: t for t in s["schema"]["tabs"]}
        self.assertEqual([x["id"] for x in tabs["stage"]["sections"]], ["spec", "build", "release"])
        self.assertIn("agent.spec-writer.model", {f["path"] for sec in tabs["agent"]["sections"] for f in sec["fields"]})
        wf = s["workflow"]
        self.assertEqual((wf["name"], [x["id"] for x in wf["stages"]]), ("sdlc", ["spec", "build", "release"]))
        build = next(x for x in wf["stages"] if x["id"] == "build")
        self.assertEqual([x["id"] for x in build["phases"]["verify"]], ["lint", "build", "test"])
        self.assertEqual([x["id"] for x in build["phases"]["finalize"]], ["open-pr", "ci"])
        self.assertIn("test", wf["commands"])
        self.assertEqual(wf["lists"], ["plan", "do", "verify", "review", "finalize", "finish"])
        spec = next(x for x in wf["stages"] if x["id"] == "spec")
        self.assertEqual([(x["id"], x["on"]) for x in spec["phases"]["signoff"]], [("signoff", False)])
        self.assertEqual([x["on"] for x in build["phases"]["signoff"]], [False])        # base_md disables it in workflow.yaml
        self.assertTrue(next(x for x in build["phases"]["do"] if x["id"] == "builder")["settings"])
        self.assertNotIn("signoff", s["defaults"]["stage"]["build"])                    # structure is not a setting
        self.assertNotIn("reviewer_fields", s["schema"])

    def test_structure_is_no_longer_a_setting(self):
        for key in ("stage.build.add", "stage.build.skip", "stage.build.signoff"):
            code, r = self.req("/api/settings/preview", {"changes": {key: False}})
            self.assertEqual(code, 400, key)
            self.assertIn("unknown setting", r["error"])

    def test_cross_origin_refused(self):
        code, _ = self.req("/api/settings", {"changes": {}}, origin="https://evil.example")
        self.assertEqual(code, 403)


class DecisionTest(unittest.TestCase):
    """The drawer's buttons call the same functions the commands do."""

    def setUp(self):
        self.repo = make_repo(base_md("human", 3))
        self.addCleanup(shutil.rmtree, self.repo.parent, ignore_errors=True)
        self.cfg = config.load(self.repo)
        serve.Handler.token, serve.Handler.root = "tok", self.repo
        self.srv = serve.Server(("127.0.0.1", 0), serve.Handler)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)

    req = ServeTest.req

    def stuck(self):
        task = Store(self.cfg).create_task("Say hello, in a way that makes a very long request indeed, longer than sixty characters")["id"]
        agents = Agents()
        agents.test_verdict, agents.dispute, agents.upheld = "FAIL", True, 1
        drive(self.cfg, task, agents)
        return task, agents

    def test_accept_as_is_closes_the_dispute_but_the_merge_still_waits_for_a_person(self):
        task, agents = self.stuck()
        code, r = self.req("/api/task/accept", {"task": task})
        self.assertEqual(code, 200, r)
        self.assertEqual(r["next"], f"The dashboard cannot run agents. Run /epi:start task-{task} in Claude Code to continue.")
        act = drive(self.cfg, task, agents)
        self.assertIn("build sign-off", act["reason"])
        self.assertEqual(act["message"], "Waiting for your sign-off")      # plain words, not the raw code
        d = self.req(f"/api/task?id={task}")[1]
        self.assertEqual([r["verdict"] for r in d["progress"] if r["id"] == "build"], ["WAITING"])
        self.assertEqual((d["text"], d["decision"]["actions"]), ("Waiting for your sign-off", ["approve", "send_back"]))
        self.assertRegex(d["decision"]["detail"], r"1 file changed.* on branch epi/task-")   # what you are approving
        self.assertNotIn("approver", d["decision"]["text"])
        cost = d["details"]["cost"]                                       # where the tokens went: per stage round and agent
        self.assertTrue(cost and all(set(c) == {"stage", "round", "who", "tokens"} for c in cost), cost)
        code, r = self.req("/api/task/approve", {"task": task})
        self.assertEqual(code, 200, r)
        d = self.req(f"/api/task?id={task}")[1]
        self.assertEqual([r["verdict"] for r in d["progress"] if r["id"] == "build"], ["ACCEPTED"])   # not "Changes needed"
        self.assertEqual(drive(self.cfg, task, agents)["action"], "done")

    def test_the_spec_writers_questions_reach_the_dashboard_and_are_answered_there(self):
        # live run 3: a task waiting for answers looked like any other task, with no way to answer
        task = Store(self.cfg).create_task("Make the CLI nicer")["id"]
        agents = Agents()
        agents.questions = True
        for _ in range(5):
            act = machine.Machine(self.cfg, task, session_id="s").next_action()
            if act["action"] != "spawn":
                break
            agents.run(act)
        self.assertEqual(act["action"], "ask")
        board = self.req("/api/board")[1]
        self.assertIn(f"task-{task} · Spec · there are questions for you", [n["text"] for n in board["needs_you"]])
        d = self.req(f"/api/task?id={task}")[1]["decision"]
        self.assertEqual((d["actions"], d["detail"]), (["answer"], "1. Which greeting?"))
        self.assertEqual(self.req("/api/task/answer", {"task": task, "note": " "})[0], 400)
        code, r = self.req("/api/task/answer", {"task": task, "note": "Say hello."})
        self.assertEqual(code, 200, r)
        self.assertIsNone(self.req(f"/api/task?id={task}")[1].get("decision"))
        self.assertEqual(drive(self.cfg, task, agents)["action"], "wait")        # on to the merge approval

    def test_send_back_needs_a_note_and_then_the_builder_applies_it(self):
        task, agents = self.stuck()
        self.assertEqual(self.req("/api/task/send_back", {"task": task, "note": "  "})[0], 400)
        code, r = self.req("/api/task/send_back", {"task": task, "note": "do it the tester's way"})
        self.assertEqual(code, 200, r)
        agents.test_verdict, agents.dispute, agents.upheld = "PASS", False, 0
        drive(self.cfg, task, agents)
        self.assertIn("do it the tester's way", [b for a, b in agents.briefs if a == "sdlc-builder"][-1])

    def test_send_back_to_an_earlier_step_reruns_its_stage(self):
        task, agents = self.stuck()
        self.assertEqual(self.req("/api/task/send_back", {"task": task, "note": "x", "to": "nobody"})[0], 400)
        self.assertEqual(self.req("/api/task/send_back", {"task": task, "note": "AC-1 says hello twice",
                                                          "to": "spec/spec-writer"})[0], 200)
        self.assertEqual(machine.Machine(self.cfg, task).state().stage, "spec")
        c = self.req(f"/api/task?id={task}")[1]["conversation"]               # sent back: not a decision any more
        self.assertEqual(c["needs_decision"], [])

    def test_sent_back_issues_say_which_step_has_them(self):
        task, agents = self.stuck()
        self.assertEqual(self.req("/api/task/send_back", {"task": task, "note": "do it the tester's way"})[0], 200)
        c = self.req(f"/api/task?id={task}")[1]["conversation"]
        self.assertEqual([(i["state"], i["waiting_for"]) for i in c["in_progress"]], [("sent_back", "Builder")])

    def test_a_second_click_on_a_decision_is_told_it_was_already_decided(self):
        task, agents = self.stuck()
        self.assertEqual(self.req("/api/task/accept", {"task": task})[0], 200)
        code, r = self.req("/api/task/accept", {"task": task})
        self.assertEqual(code, 400)
        self.assertIn("already decided", r["error"])

    def test_a_disagreement_shows_both_sides_in_one_line_each(self):
        task, agents = self.stuck()
        i = self.req(f"/api/task?id={task}")[1]["decision"]["issues"][0]
        self.assertEqual((i["claim"], i["reply_by"]), ("The spec does ask for it (AC-1).", "Builder"))
        self.assertIn("never asks", i["reply"])

    def test_a_task_that_does_not_need_you_refuses_the_buttons(self):
        task = Store(self.cfg).create_task("Fresh")["id"]
        for action in ("accept", "approve", "send_back"):
            code, r = self.req(f"/api/task/{action}", {"task": task, "note": "x"})
            self.assertEqual(code, 400, action)
            self.assertIn("not waiting for you", r["error"])
        self.assertEqual(self.req("/api/task/accept", {"task": "999"})[0], 400)
        self.assertEqual(self.req("/api/task/accept", {"task": task}, token="bad")[0], 403)
        self.assertEqual(self.req("/api/task/accept", {"task": task}, origin="https://evil.example")[0], 403)

    def test_clearing_alerts_never_hides_a_task_that_still_needs_you(self):
        task, _ = self.stuck()
        self.req("/api/alerts/clear", {"task": task})
        self.assertEqual([a["kind"] for a in Store(self.cfg).alerts(task)], ["needs_you"])
        self.assertIn(task, [n["task"] for n in self.req("/api/board")[1]["needs_you"]])

    def test_the_card_uses_the_specs_short_title_and_the_request_stays_in_the_details(self):
        task, _ = self.stuck()
        long = self.req(f"/api/task?id={task}")[1]
        self.assertLessEqual(len(long["title"]), 60)
        self.assertTrue(long["title"].endswith("…"))
        spec = Store(self.cfg).task_dir(task) / "spec.md"
        spec.write_text(spec.read_text().replace("---\nrisk", "---\ntitle: Say hello\nrisk", 1))
        d = self.req(f"/api/task?id={task}")[1]
        self.assertEqual(d["title"], "Say hello")
        self.assertIn("very long request", d["details"]["request"])

    def test_first_pass_counts_tasks_that_needed_no_second_round_and_no_person(self):
        from engine import taskview
        quick = [{"type": "round", "r": 1}, {"type": "round", "r": 1}, {"type": "stage"}]
        self.assertTrue(taskview.first_pass(quick))
        self.assertFalse(taskview.first_pass(quick + [{"type": "round", "r": 2}]))
        self.assertFalse(taskview.first_pass(quick + [{"type": "accept"}]))
        self.assertFalse(taskview.first_pass(quick + [{"type": "round", "r": 1, "extra": True}]))
        task, agents = self.stuck()
        machine.approve(self.cfg, task, "accept")
        self.assertFalse(taskview.first_pass(Store(self.cfg).events(task)))


if __name__ == "__main__":
    unittest.main()
