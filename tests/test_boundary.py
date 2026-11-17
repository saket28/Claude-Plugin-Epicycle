"""The engine stays generic: words of the sdlc workflow (its agents, its spec) never reach the engine's own
messages. Only docstrings, comments and the listed exceptions may name them. This keeps the engine ready to be
split from the workflows it runs."""
import ast
import re
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "scripts" / "engine"
WORDS = re.compile(r"\b(builder|tester|spec[- ]writer|spec owner|code-review)\b", re.I)
PACK = re.compile(r"\bsdlc\b(?!\.md)", re.I)     # the bare word: only the default pack name may be spelled out
ALLOWED = {
    "cli.py": "init's repo detection proposes tester paths; it moves to the sdlc workflow when the plugin splits",
    "runners.py": "/code-review is the name of the skill a headless review runs",
}


def strings(tree):
    """The string constants of a module that are not docstrings."""
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
            and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]


class BoundaryTest(unittest.TestCase):
    def test_engine_messages_do_not_name_the_sdlc_workflows_agents(self):
        for f in sorted(ENGINE.glob("*.py")):
            if f.name in ALLOWED:
                continue
            bad = [s for s in strings(ast.parse(f.read_text())) if WORDS.search(s)]
            self.assertEqual(bad, [], f.name)

    def test_engine_strings_say_sdlc_only_as_the_default_pack_name(self):
        for f in sorted(ENGINE.glob("*.py")):
            bad = [s for s in strings(ast.parse(f.read_text())) if PACK.search(s) and s != "sdlc"]
            self.assertEqual(bad, [], f.name)


if __name__ == "__main__":
    unittest.main()
