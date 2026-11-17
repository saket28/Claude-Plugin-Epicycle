"""The dashboard's workflow editor, rules editor and pack upgrade: the API routes and their guards."""

import json
import threading
import unittest
import urllib.error
import urllib.request

from test_pack import CUR, NEXT, PackTestCase, WF
from engine import serve, yamlish

TOGGLE = {"op": "toggle", "stage": "build", "phase": "review", "id": "tester", "enabled": False}


class WorkflowApiTest(PackTestCase):
    def setUp(self):
        super().setUp()
        serve.Handler.token, serve.Handler.root = "tok", self.repo
        self.srv = serve.Server(("127.0.0.1", 0), serve.Handler)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)

    def req(self, path, body=None, token="tok", origin=None, host=None):
        h = {"X-Epi-Token": token} if token else {}
        if body is not None:
            h["Content-Type"] = "application/json"
        if origin:
            h["Origin"] = origin
        if host:
            h["Host"] = host
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                   headers=h, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def yaml(self):
        return (self.repo / WF / "workflow.yaml").read_text()

    def test_the_settings_view_carries_the_workflow_hash_and_the_rules(self):
        (self.repo / WF / "settings.md").write_text("---\n---\n\n- Be kind.\n")
        code, s = self.req("/api/settings")
        self.assertEqual(code, 200)
        self.assertEqual(s["rules"], "- Be kind.")
        self.assertEqual(len(s["workflow_hash"]), 16)
        builder = s["workflow"]["stages"][1]["phases"]["do"][0]
        self.assertEqual((builder["id"], builder["on"]), ("builder", True))

    def test_preview_shows_the_diff_and_writes_nothing_then_save_writes_it(self):
        before = self.yaml()
        code, r = self.req("/api/workflow/preview", {"ops": [TOGGLE]})
        self.assertEqual(code, 200)
        self.assertIn("+      - { uses: agent/tester, name: Tester, doing: Testing against the spec, enabled: false }", r["diff"])
        self.assertEqual(self.yaml(), before)
        h = self.req("/api/settings")[1]["workflow_hash"]
        code, r = self.req("/api/workflow", {"ops": [TOGGLE], "hash": h})
        self.assertEqual((code, r["saved"]), (200, True))
        self.assertIn("enabled: false }", self.yaml())
        view = self.req("/api/settings")[1]["workflow"]
        tester = next(x for x in view["stages"][1]["phases"]["review"] if x["id"] == "tester")
        self.assertFalse(tester["on"])

    def test_add_with_an_if_and_a_bad_result_is_a_400_that_names_the_problem(self):
        op = {"op": "add", "stage": "build", "phase": "review", "kind": "skill", "name": "a11y", "if": "touches-ui"}
        code, r = self.req("/api/workflow/preview", {"ops": [op]})
        self.assertIn("+      - { uses: skill/a11y, if: touches-ui }", r["diff"])
        code, r = self.req("/api/workflow", {"ops": [{**op, "if": "banana"}], "hash": None})
        self.assertEqual(code, 400)
        self.assertIn("if:", r["error"])
        self.assertNotIn("a11y", self.yaml())

    def test_a_stale_hash_is_refused(self):
        h = self.req("/api/settings")[1]["workflow_hash"]
        (self.repo / WF / "workflow.yaml").write_text(self.yaml() + "# edited by hand\n")
        code, r = self.req("/api/workflow", {"ops": [TOGGLE], "hash": h})
        self.assertEqual(code, 400)
        self.assertIn("changed on disk", r["error"])
        self.assertNotIn("enabled: false }", self.yaml().split("# edited")[0].split("name: Tester")[1][:80])

    def test_move_order_is_the_written_order(self):
        op = {"op": "move", "stage": "build", "phase": "review", "id": "code-review", "by": -1}
        h = self.req("/api/settings")[1]["workflow_hash"]
        self.assertEqual(self.req("/api/workflow", {"ops": [op], "hash": h})[0], 200)
        ids = [s.get("id") or s["uses"].split("/")[1] for s in yamlish.loads(self.yaml())["stages"]["build"]["review"]]
        self.assertEqual(ids[:2], ["code-review", "tester"])

    def test_rules_preview_and_save_keep_the_frontmatter(self):
        (self.repo / WF / "settings.md").write_text("---\nstage: { build: { rounds: 4 } }\n---\n\nold\n")
        code, r = self.req("/api/rules/preview", {"text": "new rule"})
        self.assertEqual(code, 200)
        self.assertIn("-old", r["diff"])
        self.assertIn("+new rule", r["diff"])
        self.assertEqual((self.repo / WF / "settings.md").read_text().split("---\n")[-1].strip(), "old")
        h = self.req("/api/settings")[1]["hash"]
        code, r = self.req("/api/rules", {"text": "new rule", "hash": h})
        self.assertEqual(code, 200)
        self.assertEqual((self.repo / WF / "settings.md").read_text(), "---\nstage: { build: { rounds: 4 } }\n---\n\nnew rule\n")
        code, r = self.req("/api/rules", {"text": "again", "hash": h})              # the hash it loaded is stale now
        self.assertEqual(code, 400)
        self.assertIn("changed on disk", r["error"])

    def test_every_new_route_wants_the_token_the_host_and_the_origin(self):
        port = self.srv.server_address[1]
        for path, body in (("/api/workflow/preview", {"ops": []}), ("/api/workflow", {"ops": []}),
                           ("/api/rules/preview", {"text": ""}), ("/api/rules", {"text": ""}), ("/api/pack/upgrade", {})):
            self.assertEqual(self.req(path, body, token=None)[0], 403, path)
            self.assertEqual(self.req(path, body, origin="https://evil.example")[0], 403, path)
            self.assertEqual(self.req(path, body, host=f"evil.example:{port}")[0], 421, path)
        self.assertEqual(self.req("/api/pack", token=None)[0], 403)
        self.assertEqual(self.req("/api/pack", host=f"evil.example:{port}")[0], 421)
        self.assertEqual(self.yaml(), (self.repo / WF / ".base" / "workflow.yaml").read_text())

    def test_pack_status_is_quiet_when_current_and_carries_the_plan_when_the_plugin_is_newer(self):
        code, p = self.req("/api/pack")
        self.assertEqual((code, p["installed"], p["available"], p["newer"]), (200, CUR, CUR, False))
        self.assertNotIn("files", p)
        self.plugin_edit("workflow.yaml", f"version: {CUR}", f"version: {NEXT}")
        self.plugin_edit("scripts/ci.py", "import", "import os, ")
        code, p = self.req("/api/pack")
        self.assertTrue(p["newer"])
        self.assertEqual(p["available"], NEXT)
        actions = {f["file"]: f["action"] for f in p["files"]}
        self.assertEqual(actions[f"{WF}/workflow.yaml"], "replace")
        self.assertEqual(actions[f"{WF}/scripts/ci.py"], "replace")
        self.assertIn(f"+version: {NEXT}", p["diffs"][f"{WF}/workflow.yaml"])
        self.assertNotIn(f"version: {NEXT}", self.yaml())                            # status is a dry run

    def test_upgrade_merges_and_a_conflict_writes_nothing_and_names_the_files(self):
        self.plugin_edit("workflow.yaml", f"version: {CUR}", f"version: {NEXT}")
        self.plugin_edit("scripts/ci.py", "import", "import os, ")
        self.mine("scripts/ci.py", "import", "import sys, ")
        before = self.yaml()
        code, r = self.req("/api/pack/upgrade", {})
        self.assertEqual(code, 200)
        self.assertFalse(r["upgraded"])
        self.assertEqual([c["file"] for c in r["conflicts"]], [f"{WF}/scripts/ci.py"])
        self.assertEqual(self.yaml(), before)
        self.assertEqual(self.req("/api/pack")[1]["conflicts"][0]["file"], f"{WF}/scripts/ci.py")
        self.mine("scripts/ci.py", "import sys, ", "import")                  # resolved
        code, r = self.req("/api/pack/upgrade", {})
        self.assertTrue(r["upgraded"])
        self.assertIn(f"version: {NEXT}", self.yaml())
        self.assertFalse(self.req("/api/pack")[1]["newer"])

    def test_an_upgrade_keeps_a_step_the_repo_added_and_one_it_switched_off(self):
        h = self.req("/api/settings")[1]["workflow_hash"]
        self.req("/api/workflow", {"ops": [TOGGLE, {"op": "add", "stage": "spec", "phase": "do", "kind": "skill", "name": "x"}], "hash": h})
        self.plugin_edit("workflow.yaml", f"version: {CUR}", f"version: {NEXT}")
        self.assertTrue(self.req("/api/pack/upgrade", {})[1]["upgraded"])
        text = self.yaml()
        self.assertIn("skill/x", text)
        self.assertIn("enabled: false }", text)
        self.assertIn(f"version: {NEXT}", text)


if __name__ == "__main__":
    unittest.main()
