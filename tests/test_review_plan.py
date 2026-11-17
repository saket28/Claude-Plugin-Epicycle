import shutil
import unittest

from helpers import make_repo, sh
from engine import cli, config

EDITS = [("add", "build", "review", "{ uses: agent/a11y, if: touches-ui }"),
         ("add", "build", "review", "{ uses: agent/advisor, continue-on-error: true }")]
AGENT = "---\nname: {n}\ndescription: x\ntools: Read\n---\nReview it.\n"


class ReviewPlanTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo("", {".claude/agents/a11y.md": AGENT.format(n="a11y"),
                                     ".claude/agents/advisor.md": AGENT.format(n="advisor")}, edits=EDITS)

    def tearDown(self):
        shutil.rmtree(self.repo.parent, ignore_errors=True)

    def test_refuses_on_base_branch(self):
        with self.assertRaisesRegex(RuntimeError, "base branch"):
            cli.review_plan(config.load(self.repo))

    def test_lists_the_agent_and_skill_review_steps_and_changed_files(self):
        sh("git checkout -qb feat/x && echo 1 > new.py && git add -A && git commit -qm x", self.repo)
        plan = cli.review_plan(config.load(self.repo))
        self.assertEqual(plan["files"], ["new.py"])
        names = {r["name"]: r for r in plan["reviewers"]}
        # security-review runs despite its `if`; the tester writes files, so it is not a reviewer
        self.assertEqual(set(names), {"code-review", "security-review", "a11y", "advisor"})
        self.assertEqual(names["a11y"]["kind"], "agent")
        self.assertEqual(names["code-review"]["kind"], "skill")
        self.assertFalse(names["advisor"]["blocking"])
        only = cli.review_plan(config.load(self.repo), ["a11y"])
        self.assertEqual([r["name"] for r in only["reviewers"]], ["a11y"])
        with self.assertRaisesRegex(RuntimeError, "unknown reviewer"):
            cli.review_plan(config.load(self.repo), ["nope"])
        self.assertTrue(plan["report_dir"].endswith(".epi/reviews/feat-x"))


if __name__ == "__main__":
    unittest.main()
