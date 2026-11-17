"""The dashboard script: it parses, and its render functions turn the API's JSON into the HTML the design asks for.
Needs `node`; the tests are skipped without it."""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, make_repo
from test_machine import Agents, base_md, drive
from engine import config, machine, serve
from engine.store import Store

HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const data = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const el = () => ({style: {}, classList: {add() {}, remove() {}, toggle() {}, contains: () => false}, addEventListener() {}, querySelectorAll: () => [], set innerHTML(v) {}, set textContent(v) {}, set onclick(v) {}});
global.document = {getElementById: el, addEventListener() {}, querySelector: () => null};
global.location = {search: "?token=t", hash: "", pathname: "/"};
global.history = {replaceState() {}};
global.fetch = () => new Promise(() => {});
global.setInterval = () => 0;
const fn = new Function(src + "\n;return {boardHtml, stripHtml, taskHtml, workflowHtml, packHtml, cardHtml, offlineText, feedHtml, setS: s => { S = s; }, setPack: p => { PACK = p; REVIEW = true; }};");
const api = fn();
const out = {};
out.board = api.boardHtml(data.board, false);
out.strip = api.stripHtml(data.board.needs_you);
out.tasks = Object.fromEntries(Object.entries(data.tasks).map(([k, v]) => [k, api.taskHtml(v)]));
api.setS(data.settings);
out.workflow = api.workflowHtml();
api.setPack({pack: "sdlc", installed: "0.7.0", available: "0.8.0", newer: true,
             files: [{file: ".claude/workflows/sdlc/workflow.yaml", action: "merge"}, {file: "x", action: "same"}],
             diffs: {".claude/workflows/sdlc/workflow.yaml": "--- a\n+++ b\n-old\n+new\n"}});
out.banner = api.packHtml();
api.setPack({pack: "sdlc", installed: "0.7.0", available: "0.8.0", newer: true, files: [], conflicts: [{file: "c.py"}, {file: "w.yaml", stage: "spec", field: "doing", text: "spec/do/x: field 'doing' changed on both sides"}]});
out.conflict = api.packHtml();
api.setPack({pack: "sdlc", installed: "0.8.0", available: "0.8.0", newer: false});
out.nobanner = api.packHtml();
out.offline = [api.offlineText(new Error('x')), api.offlineText({status: 403})];
out.feed = api.feedHtml(data.board.activity);
console.log(JSON.stringify(out));
"""


def node():
    return shutil.which("node")


def script_of(html):
    return html.rsplit("<script>", 1)[1].split("</script>")[0]


@unittest.skipUnless(node(), "node is not installed")
class DashboardScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "templates" / "dashboard.html").read_text()
        cls.tmp = Path(tempfile.mkdtemp())
        (cls.tmp / "dash.js").write_text(script_of(cls.html))
        (cls.tmp / "harness.js").write_text(HARNESS)
        cls.repo = make_repo(base_md("human", 3))
        cfg = config.load(cls.repo)
        st = Store(cfg)
        done = st.create_task("A quick one")["id"]
        a = Agents()
        drive(cfg, done, a)
        machine.approve(cfg, done, "build")
        drive(cfg, done, a)
        stuck = st.create_task("A disputed one")["id"]
        b = Agents()
        b.test_verdict, b.dispute, b.upheld = "FAIL", True, 1
        drive(cfg, stuck, b)
        cls.done, cls.stuck = done, stuck
        serve.Handler.token, serve.Handler.root = "tok", cls.repo
        data = {"board": serve.board(cfg), "settings": serve.settings(cfg),
                "tasks": {t: __import__("engine.taskview", fromlist=["x"]).task(cfg, st.load_task(t)) for t in (done, stuck)}}
        (cls.tmp / "data.json").write_text(json.dumps(data))
        r = subprocess.run([node(), str(cls.tmp / "harness.js"), str(cls.tmp / "dash.js"), str(cls.tmp / "data.json")],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        cls.out = json.loads(r.stdout)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        shutil.rmtree(cls.repo.parent, ignore_errors=True)

    def test_the_script_parses(self):
        r = subprocess.run([node(), "--check", str(self.tmp / "dash.js")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_needs_you_strip_has_one_line_per_task_and_the_card_has_a_red_border(self):
        self.assertIn("Needs you", self.out["strip"])
        self.assertIn(f"task-{self.stuck} · Build · a disagreement to settle", self.out["strip"])
        self.assertEqual(self.out["strip"].count("<a "), 1)
        self.assertIn('class="card needs"', self.out["board"]["html"])
        self.assertIn("Needs you: a disagreement to settle", self.out["board"]["html"])

    def test_a_running_card_has_a_step_strip_with_tooltips(self):
        html = self.out["board"]["html"]
        self.assertIn('<div class="steps">', html)
        self.assertRegex(html, r'<i class="changes" title="[^"]+: changes">')

    def test_the_banner_says_lost_connection_or_wrong_token(self):
        self.assertIn("Lost connection", self.out["offline"][0])
        self.assertIn("token", self.out["offline"][1])

    def test_the_feed_lines_open_their_task(self):
        self.assertIn(f"openTask('{self.stuck}')", self.out["feed"])

    def test_empty_columns_are_compact(self):
        self.assertIn("minmax(84px", self.out["board"]["widths"])

    def test_the_drawer_starts_with_the_title_and_one_status_line_then_the_decision_box_with_buttons(self):
        h = self.out["tasks"][self.stuck]
        self.assertTrue(h.startswith('<h2 class="title">'))
        self.assertLess(h.index("status-line"), h.index("Your decision"))
        self.assertLess(h.index("Your decision"), h.index("Progress"))
        for button in ("Accept as is", "Send back", 'id="send-to"', "Builder (Build)"):
            self.assertIn(button, h)
        self.assertNotIn("Run <code>", h)                                      # no commands to copy
        self.assertNotIn("Conversation", h)                                    # issues sit in their stage, under Progress
        self.assertNotIn("Needs a decision (", h)                              # the decision box already shows it
        build = h.split("<b>Build</b>")[1].split("<b>Merge</b>")[0]
        self.assertIn("1 open issue", build)
        self.assertLess(build.index("Issues"), build.index("Round 1"))
        self.assertIn("Tester says", h)                                        # both sides of the disagreement, no expanding
        self.assertIn("Builder replied", h)

    def test_the_request_is_not_in_the_drawer_head_and_delete_sits_in_the_details(self):
        h = self.out["tasks"][self.done]
        self.assertNotIn("Delete this task", h.split("Details")[0])
        self.assertIn("Delete this task", h.split("Details")[1])
        self.assertIn("Full request", h.split("Details")[1])
        self.assertNotIn("[ ]", h)
        self.assertNotIn("[x]", h)

    def test_progress_is_one_row_per_stage(self):
        h = self.out["tasks"][self.done]
        self.assertEqual(len(re.findall(r"<b>(Spec|Build|Release)</b>", h)), 3)

    def test_the_workflow_tab_lists_each_step_with_its_edit_controls(self):
        w = self.out["workflow"]
        self.assertEqual(w.count('class="stage"'), 3)
        self.assertIn("Sign-off", w.split("<h3>Build</h3>")[1].split('<h4>Finish</h4>')[1])   # sign-off opens Finish
        self.assertGreaterEqual(w.count('class="wstep'), 10)
        self.assertEqual(w.count('type="checkbox"'), w.count('class="wstep'))                 # one on/off switch per step
        self.assertEqual(w.count("+ Add step"), 3)
        self.assertIn('title="Move down"', w)
        self.assertIn('title="Remove"', w)
        self.assertIn("&quot;op&quot;:&quot;remove&quot;", w)
        self.assertIn('id="rules"', w)                                                         # the pack's rules
        self.assertNotIn("send work back", w)
        for phase in ("Do", "Verify", "Review", "Finalize", "Finish"):
            self.assertIn(f"<h4>{phase}</h4>", w)
        row = [r for r in w.split("<h3>Spec</h3>")[1].split("<h3>Build</h3>")[0].split('class="wstep') if "Sign-off" in r][0]
        self.assertNotIn("Remove", row)                                                        # a sign-off is switched off, not removed

    def test_the_upgrade_banner_is_one_line_with_review_and_upgrade_and_shows_diffs(self):
        b = self.out["banner"]
        self.assertIn("sdlc 0.7.0 → 0.8.0 available", b)
        self.assertIn(">Review<", b)
        self.assertIn(">Upgrade<", b)
        self.assertIn("workflow.yaml</b>", b)
        self.assertIn('<span class="add">+new</span>', b)
        self.assertNotIn("<b>x</b>", b)                                   # unchanged files are not listed
        self.assertIn("Conflicts, nothing was written: c.py, w.yaml", self.out["conflict"])
        self.assertIn("spec/do/x: field &#39;doing&#39; changed", self.out["conflict"].replace("&#039;", "&#39;"))
        self.assertEqual(self.out["nobanner"], "")

    def test_token_rows_name_the_step_not_the_agent_file(self):
        d = self.out["tasks"][self.done]
        self.assertNotIn("sdlc-builder", d)


if __name__ == "__main__":
    unittest.main()
