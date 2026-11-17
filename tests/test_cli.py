"""The command line: text from a skill arrives safely, init sets a repo up, small messages are clear."""

import io
import json
import re
import shutil
import socket
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from helpers import ROOT, make_repo
from engine import cli, config
from engine.store import Store


def run(argv, stdin=""):
    buf = io.StringIO()
    with mock.patch.object(sys, "stdin", io.StringIO(stdin)), redirect_stdout(buf):
        try:
            cli.main(argv)
        except SystemExit:
            pass
    return buf.getvalue()


class SkillsAreShellSafeTest(unittest.TestCase):
    """User text (and text from a remote board) must never be pasted into a shell command."""

    def skills(self):
        return sorted((ROOT / "skills").glob("*/SKILL.md"))

    def test_no_skill_puts_arguments_in_a_bang_command(self):
        for f in self.skills():
            for n, line in enumerate(f.read_text().splitlines(), 1):
                if "$ARGUMENTS" in line and "!`" in line:
                    self.fail(f"{f.parent.name}:{n} interpolates $ARGUMENTS into a shell command")

    def test_arguments_in_a_code_block_only_sit_inside_a_quoted_heredoc(self):
        for f in self.skills():
            inside = quoted = False
            for n, line in enumerate(f.read_text().splitlines(), 1):
                if line.startswith("```"):
                    inside = not inside
                    quoted = False
                    continue
                if inside and re.search(r"<<'\w+'", line):
                    quoted = True
                    continue
                if inside and "$ARGUMENTS" in line and not quoted:
                    self.fail(f"{f.parent.name}:{n} uses $ARGUMENTS outside a quoted heredoc")

    def test_no_skill_reads_plugin_files_with_cat(self):
        for f in self.skills():
            self.assertNotIn("cat \"${CLAUDE_PLUGIN_ROOT}", f.read_text(), f.parent.name)


class StdinArgumentsTest(unittest.TestCase):
    def test_free_text_goes_through_stdin_whatever_it_contains(self):
        with mock.patch.object(sys, "stdin", io.StringIO("3 --to builder n<=0 is fine; $(rm -rf x) `y` -1 \"q\"\n")):
            argv = cli.read_stdin_args(["send-back", "--stdin"])
        self.assertEqual(argv, ["send-back", "3", "--to", "builder", "--", "n<=0", "is", "fine;", "$(rm", "-rf", "x)", "`y`", "-1", '"q"'])

    def test_approve_words_are_split(self):
        with mock.patch.object(sys, "stdin", io.StringIO("task-1 budget +500k\n")):
            self.assertEqual(cli.read_stdin_args(["approve", "--stdin"]), ["approve", "task-1", "budget", "+500k"])

    def test_intake_text_with_dashes_is_text(self):
        repo = make_repo("---\n---\n")
        self.addCleanup(shutil.rmtree, repo.parent, ignore_errors=True)
        with mock.patch("engine.config.main_checkout", return_value=repo), mock.patch("os.getcwd", return_value=str(repo)):
            import os
            old = os.getcwd
            os.chdir(repo)
            try:
                run(["intake", "--stdin"], "-x fix n<=0 handling\n")
            finally:
                os.chdir(Path(__file__).parent)
        items = Store(config.load(repo)).backlog_items()
        self.assertEqual(items[0]["title"], "-x fix n<=0 handling")


class InitTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo('---\nglobal:\n  command: {test: "pytest -q", lint: "ruff check"}\n---\n')
        self.addCleanup(shutil.rmtree, self.repo.parent, ignore_errors=True)
        self.cfg = config.load(self.repo)

    def test_the_lock_file_and_local_settings_are_gitignored(self):
        cli.ensure_gitignore(self.repo, self.cfg)
        lines = (self.repo / ".gitignore").read_text().splitlines()
        self.assertIn(".claude/workflows/*/learnings.md.lock", lines)
        self.assertIn(".epi/", lines)
        self.assertEqual(cli.ensure_gitignore(self.repo, self.cfg), [])          # nothing added twice

    def test_agents_may_use_the_worktree_directory_and_no_command_rule_is_written(self):
        added = cli.allow_rules(self.repo, self.cfg)
        data = json.loads((self.repo / ".claude" / "settings.local.json").read_text())
        self.assertNotIn("allow", data["permissions"])               # commands go through `epi.py run`, allowed by the hook
        self.assertEqual(data["permissions"]["additionalDirectories"], [str(self.cfg.worktree_dir)])
        self.assertEqual(cli.allow_rules(self.repo, self.cfg), [])                # and again: nothing new
        self.assertTrue(added)

    def test_existing_permissions_are_kept(self):
        f = self.repo / ".claude" / "settings.local.json"
        f.parent.mkdir(exist_ok=True)
        f.write_text(json.dumps({"permissions": {"allow": ["Bash(ls *)"]}, "model": "opus"}))
        cli.allow_rules(self.repo, self.cfg)
        data = json.loads(f.read_text())
        self.assertEqual((data["model"], data["permissions"]["allow"][0]), ("opus", "Bash(ls *)"))


class SmallThingsTest(unittest.TestCase):
    def test_serve_on_a_busy_port_says_so(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen()
        self.addCleanup(s.close)
        repo = make_repo("---\n---\n")
        self.addCleanup(shutil.rmtree, repo.parent, ignore_errors=True)
        import os
        os.chdir(repo)
        try:
            text = run(["serve", "--port", str(s.getsockname()[1])])
        finally:
            os.chdir(Path(__file__).parent)
        self.assertIn("cannot listen on port", text)
        self.assertNotIn("Traceback", text)

    def test_the_health_check_says_where_the_claude_cli_was_found(self):
        repo = make_repo("---\n---\n")
        self.addCleanup(shutil.rmtree, repo.parent, ignore_errors=True)
        ok = cli.health(config.load(repo))["ok"]
        self.assertTrue(any(x.startswith("claude CLI found at ") and "claude" in x for x in ok), ok)

    def test_the_plugin_texts_are_printed_by_a_command(self):
        self.assertIn("Run the loop", run(["doc", "run-loop"]))
        self.assertIn("unknown text", run(["doc", "../x"]))


if __name__ == "__main__":
    unittest.main()
