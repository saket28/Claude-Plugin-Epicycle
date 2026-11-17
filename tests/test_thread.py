"""The director: comment parsing, the rules, and how threads read."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine import thread  # noqa: E402

KNOWN = {"builder", "tester", "spec-writer", "director", "code-review"}
RAISE = "---\nverdict: CHANGES\n---\n## Comments\n- new -> builder, feedback, blocking: Bad redirect\n    Tabs bypass it.\n- new -> builder, feedback, nit: Tiny\n"


class ParseTest(unittest.TestCase):
    def test_comments_have_a_target_type_severity_and_detail(self):
        cs = thread.parse_comments(RAISE)
        self.assertEqual([(c["id"], c["to"], c["type"], c["severity"]) for c in cs],
                         [("new", "builder", "feedback", "blocking"), ("new", "builder", "feedback", "nit")])
        self.assertEqual((cs[0]["text"], cs[0]["detail"]), ("Bad redirect", "Tabs bypass it."))

    def test_the_forms_agents_really_wrote_are_understood(self):
        # live run 3: both were reported to the person as "not understood"
        bad = []
        cs = thread.parse_comments("## Comments\n- new -> builder, nit: no test checks the help\n"
                                   "- I1 -> code-review, pushback (blocking): already fixed\n"
                                   "- I2 -> builder, nit: no type on a reply\n", bad)
        self.assertEqual([(c["id"], c["type"], c["severity"]) for c in cs],
                         [("new", "feedback", "nit"), ("I1", "pushback", "blocking")])
        self.assertEqual(len(bad), 1)                                       # a reply must say what it is

    def test_replies_may_have_no_text(self):
        cs = thread.parse_comments("## Comments\n- i3 -> builder, accept\n- I4 -> spec-writer, pushback: the spec is silent\n")
        self.assertEqual([(c["id"], c["to"], c["type"], c["text"]) for c in cs],
                         [("I3", "builder", "accept", ""), ("I4", "spec-writer", "pushback", "the spec is silent")])

    def test_the_leading_bullet_is_optional(self):
        cs = thread.parse_comments("## Comments\nI1 -> tester, fixed: validated the input\nnew -> builder, feedback, blocking: x\n")
        self.assertEqual([(c["id"], c["to"], c["type"], c["text"]) for c in cs],
                         [("I1", "tester", "fixed", "validated the input"), ("new", "builder", "feedback", "x")])

    def test_a_comment_without_a_severity_is_a_nit(self):
        self.assertEqual(thread.parse_comments("## Comments\n- new -> builder, feedback: x\n")[0]["severity"], "")

    def test_a_line_that_looks_like_a_comment_but_does_not_parse_is_reported(self):
        errors = []
        thread.parse_comments("## Comments\n- I1 => tester fixed it\nplain prose is fine\n- I2 -> builder, accept\n", errors)
        self.assertEqual(errors, ["- I1 => tester fixed it"])


class DirectorTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d)

    def sources(self, rd, **files):
        return [{"path": rd / ((name[:-3] if name.endswith("_md") else name.replace("_", "-")) + ".md"),
                 "author": (name[:-3] if name.endswith("_md") else name.replace("_", "-")),
                 "kind": "do" if name.endswith("_md") else "check", "engine": name.startswith("engine_")}
                for name in files]

    def ingest(self, n, rd, r=1, exchange=False, **files):
        thread.ingest(self.d, self.sources(rd, **files), "build", n, KNOWN, default_to="builder", r=r, exchange=exchange)

    def round(self, n, **files):
        rd = self.d / "build" / f"r{n}"
        rd.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (rd / ((name[:-3] if name.endswith("_md") else name.replace("_", "-")) + ".md")).write_text(text)
        self.ingest(n, rd, r=n, **files)
        return thread.load(self.d)[0]

    def test_new_issues_are_numbered_and_addressed(self):
        iss = self.round(1, code_review=RAISE)
        self.assertEqual([(i["id"], i["severity"], i["by"]) for i in iss.values()],
                         [("I1", "blocking", "code-review"), ("I2", "nit", "code-review")])
        self.assertEqual([i["id"] for i in thread.awaiting(self.d, "builder")], ["I1", "I2"])

    def test_an_issue_title_is_a_whole_sentence_or_whole_words_never_cut_mid_word(self):
        long = "print() of the truncate result, which contains the non-ASCII character, raises an error when stdout is not UTF-8. More detail."
        self.assertEqual(thread._headline(long), long.split(". More")[0] + ".")
        words = "word " * 60
        t = thread._headline(words)
        self.assertTrue(t.endswith("word…") and len(t) <= 141)
        self.assertEqual(thread._headline("short"), "short")

    def test_issues_without_a_severity_are_nits(self):
        iss = self.round(1, code_review="---\nverdict: CHANGES\n---\n## Comments\n- new -> builder, feedback: maybe rename\n")
        self.assertEqual(iss["I1"]["severity"], "nit")

    def test_a_review_that_passes_cannot_raise_a_blocking_issue(self):
        iss = self.round(1, code_review="---\nverdict: PASS\n---\n## Comments\n- new -> builder, feedback, blocking: x\n")
        self.assertEqual(iss["I1"]["severity"], "nit")

    def test_escalation_tells_an_unanswered_issue_from_a_disputed_one(self):
        self.round(1, code_review="---\nverdict: CHANGES\n---\n## Comments\n- new -> builder, feedback, blocking: a\n- new -> builder, feedback, blocking: b\n")
        thread._append(self.d, {"kind": "comment", "id": "I2", "from": "builder", "to": "code-review", "type": "pushback", "text": "no", "detail": ""})
        thread.escalate(self.d, "still open", "build")
        got = {i["id"]: i["flag"] for i in thread.load(self.d)[0].values()}
        self.assertEqual(got, {"I1": "late", "I2": "disputed"})

    def test_notes_are_closed_when_their_stage_passes_but_a_question_for_a_person_stays(self):
        self.round(1, code_review=RAISE)
        q = thread.add_question(self.d, "build", "builder", "approver", "Which greeting?", 1)
        thread.close_notes(self.d, "build")
        got = {i["id"]: (i["state"], i["flag"]) for i in thread.load(self.d)[0].values()}
        self.assertEqual(got["I2"], ("closed", "note"))
        self.assertEqual(got["I1"][0], "open")
        self.assertEqual(got[q][0], "open")

    def test_an_unknown_addressee_is_redirected(self):
        self.round(1, code_review="---\nverdict: CHANGES\n---\n## Comments\n- new -> nobody, feedback, blocking: x\n")
        self.assertEqual([i["id"] for i in thread.awaiting(self.d, "builder")], ["I1"])

    def test_the_raisers_accept_closes_it(self):
        self.round(1, code_review=RAISE)
        self.round(2, builder_md="---\nround: 2\n---\n## Comments\n- I1 -> code-review, fixed: done\n")
        iss = self.round(2, code_review="---\nverdict: PASS\n---\n## Comments\n- I1 -> builder, accept\n")
        self.assertEqual((iss["I1"]["state"], iss["I1"]["lines"][-1]["text"]), ("closed", "closed, accepted by code-review"))

    def test_silence_closes_it_as_no_reply(self):
        self.round(1, code_review=RAISE)
        self.round(2, builder_md="---\nround: 2\n---\n## Comments\n- I1 -> code-review, fixed: done\n")
        iss = self.round(2, code_review="---\nverdict: PASS\n---\n")
        self.assertEqual((iss["I1"]["state"], iss["I1"]["flag"]), ("closed", "no_reply"))

    def test_pushbacks_go_back_and_forth_until_the_pipeline_escalates(self):
        self.round(1, code_review=RAISE)
        self.round(2, builder_md="---\nround: 2\n---\n## Comments\n- I1 -> code-review, pushback: validated upstream\n")
        iss = self.round(2, code_review="---\nverdict: CHANGES\n---\n## Comments\n- I1 -> builder, pushback: not on this path\n")
        self.assertEqual(iss["I1"]["state"], "open")                         # the director leaves the count to the pipeline
        thread.escalate(self.d, "still open after 2 rounds")
        self.assertIn("I1 Bad redirect", thread.human_detail(self.d))
        self.assertEqual([i["id"] for i in thread.awaiting_you(self.d)], ["I1"])

    def test_a_new_issue_in_an_exchange_is_still_taken(self):
        self.round(1, code_review=RAISE)
        rd = self.d / "round-2"
        rd.mkdir()
        (rd / "code-review.md").write_text("---\nverdict: CHANGES\n---\n## Comments\n- new -> builder, feedback, blocking: late find\n")
        self.ingest(2, rd, r=1, exchange=True, code_review=None)
        self.assertEqual(sorted(thread.load(self.d)[0]), ["I1", "I2", "I3"])  # nothing a checker raises is dropped

    def test_a_command_reviewer_answers_with_its_verdict(self):
        self.round(1, code_review=RAISE)
        rd = self.d / "round-2"
        rd.mkdir()
        (rd / "builder.md").write_text("---\nround: 2\n---\n## Comments\n- I1 -> code-review, fixed: done\n")
        self.ingest(2, rd, r=2, exchange=True, builder_md=None)
        (rd / "code-review.md").write_text("---\nverdict: PASS\n---\n")
        thread.ingest(self.d, [{"path": rd / "code-review.md", "author": "code-review", "kind": "check", "engine": True}],
                      "build", 2, KNOWN, default_to="builder", r=2, exchange=True)
        self.assertEqual(thread.load(self.d)[0]["I1"]["lines"][-1]["text"], "closed, accepted by code-review")

    def rerun(self, front, body=""):
        self.round(1, code_review=RAISE)
        rd = self.d / "round-2"
        rd.mkdir()
        (rd / "builder.md").write_text("---\nround: 2\n---\n## Comments\n- I1 -> code-review, fixed: done\n- I2 -> code-review, fixed: done\n")
        self.ingest(2, rd, r=2, exchange=True, builder_md=None)
        (rd / "code-review.md").write_text(f"---\n{front}\n---\n{body}")
        thread.ingest(self.d, [{"path": rd / "code-review.md", "author": "code-review", "kind": "check", "engine": True}],
                      "build", 2, KNOWN, default_to="builder", r=2, exchange=True)
        return thread.load(self.d)[0]

    def test_a_skill_that_finds_something_new_does_not_dispute_the_threads_it_did_not_raise_again(self):
        # live run 3: a new finding made the engine call every fixed thread "still reported"
        iss = self.rerun("verdict: CHANGES\nengine: code-review",
                         "## Comments\n- new -> builder, feedback, blocking: Another bug\n")
        self.assertEqual(iss["I1"]["state"], "closed")
        self.assertEqual(iss["I3"]["title"], "Another bug")                 # its new finding is kept

    def test_a_failing_command_still_disputes_every_open_thread_it_has(self):
        iss = self.rerun("verdict: FAIL\nengine: run")
        self.assertIn("still reported when run again", iss["I1"]["lines"][-1]["text"])

    def test_instructions_close_what_they_sent_back_even_if_nobody_answers(self):
        self.round(1, code_review=RAISE)
        thread.escalate(self.d, "still open")
        thread.human_decision(self.d, "send_back", "do it my way")
        self.assertEqual(thread.load(self.d)[0]["I1"]["state"], "open")
        thread.close_sent_back(self.d)
        self.assertEqual(thread.load(self.d)[0]["I1"]["lines"][-1]["text"], "closed, your instructions were applied")

    def test_issues_are_grouped_by_round(self):
        self.round(1, code_review=RAISE)
        rd = self.d / "round-2"
        rd.mkdir()
        (rd / "code-review.md").write_text("---\nverdict: CHANGES\n---\n## Comments\n- new -> builder, feedback, blocking: another\n")
        self.ingest(2, rd, r=2, code_review=None)
        text = thread.render(self.d)
        self.assertLess(text.index("r1"), text.index("I1"))
        self.assertLess(text.index("I2"), text.index("r2"))
        self.assertLess(text.index("r2"), text.index("I3"))

    def test_the_human_overrides_or_reworks(self):
        for kind, state in (("accept", "closed"), ("send_back", "open")):
            self.d = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, self.d)
            self.round(1, code_review=RAISE)
            thread.escalate(self.d, "rounds used up")
            self.assertEqual(thread.load(self.d)[0]["I1"]["state"], "needs_you")
            self.assertEqual(thread.load(self.d)[0]["I2"]["state"], "open")      # a nit is never escalated
            thread.human_decision(self.d, kind, "remove the redirect", to="builder")
            i = thread.load(self.d)[0]["I1"]
            self.assertEqual(i["state"], state)
            self.assertIn("human", [x["from"] for x in i["lines"]])
            if kind == "send_back":
                self.assertEqual(thread.awaiting(self.d, "builder")[0]["id"], "I1")

    def test_a_checker_failing_with_no_comments_still_makes_an_issue(self):
        iss = self.round(1, code_review="---\nverdict: CHANGES\n---\nbody\n")
        self.assertEqual(iss["I1"]["severity"], "blocking")

    def test_severity_changes_only_by_the_raiser(self):
        self.round(1, code_review=RAISE)
        self.round(2, builder_md="---\nround: 2\n---\n## Comments\n- I1 -> code-review, pushback: only a nit\n")
        iss = self.round(2, code_review="---\nverdict: PASS\n---\n## Comments\n- I1 -> builder, feedback, nit: fair\n")
        self.assertEqual(iss["I1"]["severity"], "nit")

    def test_readable_copy(self):
        self.round(1, code_review=RAISE)
        text = thread.render(self.d)
        self.assertIn("r1\n\n  [ ] I1 - Bad redirect - blocking\n      - code-review - builder - feedback: Bad redirect", text)


if __name__ == "__main__":
    unittest.main()
