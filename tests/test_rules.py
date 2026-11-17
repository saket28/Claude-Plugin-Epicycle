import unittest
from helpers import ROOT  # noqa: F401
from engine import rules


class RulesTest(unittest.TestCase):
    def test_forms(self):
        ctx = {"risk": "medium", "touches_ui": False, "paths": ["src/auth/login.py", "README.md"]}
        self.assertTrue(rules.evaluate("always", ctx))
        self.assertFalse(rules.evaluate("never", ctx))
        self.assertFalse(rules.evaluate("touches-ui", ctx))
        self.assertTrue(rules.evaluate("risk>=medium", ctx))
        self.assertFalse(rules.evaluate("risk>=high", ctx))
        self.assertTrue(rules.evaluate("paths:[src/auth/**]", ctx))
        self.assertTrue(rules.evaluate("risk>=high or paths:[src/auth/**, x/**]", ctx))
        self.assertFalse(rules.evaluate("paths:[src/api/**]", ctx))

    def test_steps_stages_and_comparisons(self):
        ctx = {"risk": "low", "steps": {"scan": {"outcome": "failure", "verdict": "CHANGES", "outputs": {"n": "12", "who": "Ana"}}},
               "stages": {"spec": {"steps": {"writer": {"outcome": "success", "outputs": {"risk": "high"}}}}}}
        yes = ["steps.scan.outcome == failure", "steps.scan.verdict == changes", "steps.scan.outputs.n > 9", "steps.scan.outputs.n >= 12",
               "steps.scan.outputs.n != 3", "steps.scan.outputs.who == 'ana'", "stages.spec.steps.writer.outputs.risk >= medium",
               "steps.scan.outputs.who", "steps.nope.outcome == ''", "steps.scan.outputs.zzz == \"\"", "true", "1 < 2", "'b' > 'a'"]
        for e in yes:
            self.assertTrue(rules.evaluate(e, ctx), e)
        no = ["steps.scan.outcome == success", "steps.scan.outputs.n < 9", "steps.nope.outputs.x", "stages.nope.steps.a.outcome == success",
              "false", "steps.scan.outputs.zzz", "risk >= medium"]
        for e in no:
            self.assertFalse(rules.evaluate(e, ctx), e)

    def test_precedence_not_and_parentheses(self):
        ctx = {"steps": {"a": {"outcome": "success"}}, "risk": "low"}
        ev = lambda e: rules.evaluate(e, ctx)
        self.assertTrue(ev("never or always and always"))             # and binds tighter than or
        self.assertFalse(ev("(never or always) and never"))
        self.assertTrue(ev("never or (always and always)"))
        self.assertTrue(ev("not never"))
        self.assertTrue(ev("!never"))
        self.assertFalse(ev("!steps.a.outcome == success"))            # `!` takes the comparison
        self.assertTrue(ev("!(steps.a.outcome == failure) and not risk >= high"))
        self.assertTrue(ev("not not always"))

    def test_text_is_false_when_empty_false_or_zero(self):
        ctx = {"steps": {"a": {"outputs": {"e": "", "f": "false", "z": "0", "t": "yes"}}}}
        for name, want in (("e", False), ("f", False), ("z", False), ("t", True)):
            self.assertEqual(rules.evaluate(f"steps.a.outputs.{name}", ctx), want, name)

    def test_templates_fill_in_values_and_a_missing_one_is_empty(self):
        ctx = {"risk": "high", "steps": {"a": {"outcome": "failure", "outputs": {"x": "two words"}}}}
        self.assertEqual(rules.interpolate("a ${{ steps.a.outputs.x }} b ${{steps.a.outcome}} ${{ risk >= medium }}${{ steps.q.outputs.z }}.", ctx),
                         "a two words b failure true.")
        self.assertEqual(rules.templates("x ${{ a }} y ${{ b == 1 }}"), ["a", "b == 1"])

    def test_references_are_listed(self):
        self.assertEqual(rules.refs("steps.a.outputs.x == 1 or not stages.s.steps.b.outcome"),
                         [("steps", "a", "outputs", "x"), ("stages", "s", "b", "outcome", None)])

    def test_globs(self):
        self.assertTrue(rules.glob_match("**/*.test.ts", "a/b/c.test.ts"))
        self.assertTrue(rules.glob_match("**/*.test.ts", "c.test.ts"))
        self.assertTrue(rules.glob_match("review-*.md", "review-a11y.md"))
        self.assertFalse(rules.glob_match("review-*.md", "docs/review-a11y.md"))
        self.assertTrue(rules.glob_match(".github/workflows/**", ".github/workflows/ci.yml"))
        self.assertTrue(rules.glob_match("src/**", "src/api/**"))       # planned paths in specs

    def test_errors(self):
        for bad in ("sometimes", "risk>=huge", "paths:[]", "", "(always", "always or", "steps.a", "steps.a.foo", "steps.a.outputs",
                    "stages.s.a.outcome", "a ==", "always always", "x $ y", "${{ always }}", "steps.a.outcome.extra"):
            with self.assertRaises(rules.RuleError, msg=bad):
                rules.parse(bad)
        with self.assertRaises(rules.RuleError) as cm:
            rules.parse("steps.a.outcome == failure and oops")
        self.assertIn("steps.a.outcome == failure and oops", str(cm.exception))          # names the offending text
        with self.assertRaises(rules.RuleError):
            rules.templates("${{ never closed")


if __name__ == "__main__":
    unittest.main()
