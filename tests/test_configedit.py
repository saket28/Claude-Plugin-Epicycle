import unittest
from helpers import ROOT  # noqa: F401
from engine import configedit, yamlish

DOC = """---
# team settings
global:
  command:
    test: npm test   # keep this comment
  budget: { per_task: 1500000 }
stage:
  release: { strategy: merge, rounds: 1 }
---

## Pipeline rules
- Keep it small.
"""


def fm(text):
    return yamlish.parse_frontmatter(text)[0]


class ConfigEditTest(unittest.TestCase):
    def test_changes_only_touched_keys_and_keeps_comments(self):
        out = configedit.apply(DOC, {"global.command.lint": "npm run lint", "stage.spec.rounds": 5})
        self.assertIn("# team settings", out)
        self.assertIn("test: npm test   # keep this comment", out)
        self.assertIn("- Keep it small.", out)
        d = fm(out)
        self.assertEqual(d["global"]["command"], {"test": "npm test", "lint": "npm run lint"})
        self.assertEqual(d["stage"]["spec"], {"rounds": 5})
        self.assertEqual(d["stage"]["release"], {"strategy": "merge", "rounds": 1})

    def test_default_values_are_removed(self):
        out = configedit.apply(DOC, {"stage.release.strategy": "squash", "stage.release.rounds": 3})   # both are defaults
        self.assertNotIn("release", fm(out).get("stage", {}))
        out = configedit.apply(DOC, {"global.budget.per_task": 2000000})
        self.assertNotIn("budget", fm(out)["global"])
        self.assertEqual(fm(out)["global"]["command"], {"test": "npm test"})
        out = configedit.apply(DOC, {"agent.builder.critical_thinking": None})  # unset = inherit, nothing to write
        self.assertNotIn("agent", fm(out))

    def test_deep_and_new_keys(self):
        out = configedit.apply(DOC, {"global.budget.orchestrate": 800000, "global.pipeline.agent_attempts": 4})
        d = fm(out)
        self.assertEqual(d["global"]["budget"], {"per_task": 1500000, "orchestrate": 800000})
        self.assertEqual(d["global"]["pipeline"]["agent_attempts"], 4)

    def test_remove_key(self):
        out = configedit.apply(DOC, {"global.command.test": None})
        self.assertNotIn("test", fm(out))

    def test_hash(self):
        self.assertEqual(configedit.file_hash(DOC), configedit.file_hash(DOC))
        self.assertNotEqual(configedit.file_hash(DOC), configedit.file_hash(DOC + " "))

    def test_a_hyphenated_agent_can_be_written(self):
        out = configedit.apply(DOC, {"agent.spec-writer.model": "haiku"})
        self.assertEqual(fm(out)["agent"]["spec-writer"], {"model": "haiku"})

    def test_the_agents_and_principles_are_protected_from_agents(self):
        from engine import config
        paths = config.DEFAULTS["global"]["guardrail"]["protected_paths"]
        self.assertIn(".claude/agents/**", paths)
        self.assertIn(".claude/workflows/**", paths)


if __name__ == "__main__":
    unittest.main()
