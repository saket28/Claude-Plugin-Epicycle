import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from engine import config, pack as pack_mod, yamlish  # noqa: E402

FAKE_CLAUDE = r'''#!/usr/bin/env python3
"""Stand-in for `claude -p` in tests: answers by prompt type, never calls a model."""
import json, os, sys
args = sys.argv[1:]
prompt = args[args.index("-p") + 1] if "-p" in args else ""
if os.environ.get("FAKE_CLAUDE_LOG"):
    open(os.environ["FAKE_CLAUDE_LOG"], "a").write(prompt.splitlines()[0] + "\n")
mode = os.environ.get("FAKE_REVIEW", "clean")
only, once = os.environ.get("FAKE_ONLY"), os.environ.get("FAKE_ONCE")
if only and not prompt.startswith(only):      # FAKE_ONLY: only prompts starting so get findings, the rest are clean
    mode = "clean"
elif once and mode != "clean":                # FAKE_ONCE: a marker file; the first answer has findings, later ones are clean
    mode = "clean" if os.path.exists(once) else (open(once, "w").close() or mode)
if prompt.startswith(("/code-review", "/security-review", "Re-check the open threads")):
    findings = [] if mode == "clean" else [{"severity": "important", "file": "app.py", "line": 1,
                                           "summary": "bug", "detail": "fix it"}]
    if mode == "garbled":
        result = "I found some things but forgot the format."
    elif mode == "native":   # what the real /code-review returns in -p: a bare list, no severity
        result = json.dumps([{"file": "app.py", "line": 1, "summary": "real bug: crashes on empty input",
                              "failure_scenario": "x"}, {"file": "app.py", "line": 2,
                              "summary": "nit: naming", "failure_scenario": "y"}])
    else:
        outs = json.loads(os.environ.get("FAKE_OUTPUTS") or "{}")     # FAKE_OUTPUTS: what a skill reports as outputs
        outs = outs if mode != "clean" else {k: "" for k in outs}
        result = "Review done.\n```json\n" + json.dumps({"findings": findings, **({"outputs": outs} if outs else {})}) + "\n```"
elif prompt.startswith("Classify each code review finding"):
    items = json.loads(prompt.split("Findings:\n", 1)[1])
    result = json.dumps(["nit" if "nit:" in f["summary"] else "important" for f in items])
elif prompt.startswith("Decide for each condition"):
    result = json.dumps({"arch": {"applies": os.environ.get("FAKE_JUDGE", "no") == "yes", "reason": "fake"}})
else:
    result = json.dumps({"entries": [{"kind": "learning", "roles": ["builder"],
                                      "text": "Run the seed script before tests.", "evidence": "fake"}]})
print(json.dumps({"result": result, "is_error": False, "duration_ms": 1000, "total_cost_usd": 0.01,
                  "usage": {"input_tokens": 1, "output_tokens": 1},
                  "modelUsage": {"claude-opus-5-5": {"inputTokens": 100, "outputTokens": 50,
                                 "cacheCreationInputTokens": 10, "cacheReadInputTokens": 500}}}))
'''


def sh(cmd, cwd):
    r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd}: {r.stderr}")
    return r.stdout.strip()


class Md(str):
    """Settings text (the old single-file shape) plus edits of the installed workflow.yaml; see `make_repo`."""
    edits = ()


def with_edits(text, edits):
    md = Md(text)
    md.edits = list(edits)
    return md


def _block(lines, stage):
    """(start, end) of a stage's lines in workflow.yaml."""
    i = lines.index(f"  {stage}:")
    j = i + 1
    while j < len(lines) and (not lines[j].strip() or lines[j].startswith("   ") or lines[j].lstrip().startswith("#")):
        j += 1
    return i, j


def _step_id(line):
    """The id of the step on a workflow.yaml line (`signoff` for a stage's sign-off), or None."""
    if line.startswith("    signoff:"):
        return "signoff"
    m = re.search(r"- \{ .*?\b(?:id: ([\w-]+)|run: ([\w-]+)|uses: \w+/([\w-]+))", line)
    own = re.search(r"\bid: ([\w-]+)", line)
    return (own.group(1) if own else next((g for g in m.groups() if g), None)) if m else None


def edit_workflow(repo, edits, pack="sdlc"):
    """Line edits of the repo's workflow.yaml: ("disable", stage, step id or "signoff") adds `enabled: false`;
    ("key", stage, step id, "outputs: [x]") adds a key to a step; ("add", stage, phase, "{ uses: agent/x }") adds a
    step line at the end of the phase."""
    path = Path(repo) / ".claude" / "workflows" / pack / "workflow.yaml"
    lines = path.read_text().splitlines()
    for op in edits:
        i, j = _block(lines, op[1])
        if op[0] in ("disable", "key"):
            k = next(n for n in range(i, j) if _step_id(lines[n]) == op[2])
            lines[k] = lines[k].rstrip()[:-1].rstrip() + ", " + (op[3] if op[0] == "key" else "enabled: false") + " }"
        else:
            phase = next((n for n in range(i, j) if lines[n] == f"    {op[2]}:"), None)
            if phase is None:
                lines[j:j] = [f"    {op[2]}:", f"      - {op[3]}"]
            else:
                end = phase + 1
                while end < j and lines[end].startswith("      - "):
                    end += 1
                lines.insert(end, f"      - {op[3]}")
    path.write_text("\n".join(lines) + "\n")


def split_md(text):
    """The old single-file settings text as (epi.md text, settings.md text, learnings.md text)."""
    raw, body = yamlish.parse_frontmatter(text) if text.strip() else ({}, "")
    sections = re.split(r"(?m)^(?=## )", body)
    learn = "".join(x for x in sections if x.startswith(("## Learnings", "## Answers")))
    rules = "".join(x for x in sections if not x.startswith(("## Learnings", "## Answers")))
    rules = re.sub(r"(?m)^## Pipeline rules\n", "", rules)
    mine = lambda tabs: {k: v for k, v in raw.items() if (k == "global") == (tabs == "global")}
    fm = lambda d: "---\n" + (yamlish.dumps(d) + "\n" if d else "") + "---\n"
    return fm(mine("global")), fm(mine("rest")) + ("\n" + rules if rules.strip() else ""), learn


def make_repo(md="", files=None, edits=None, pack="sdlc"):
    """A throwaway git repo with Epicycle set up, on branch main, plus a fake claude CLI. `md` is settings text in
    the old single-file shape (frontmatter with global, stage and agent settings; a body of rules, Learnings and
    Answers), split here into .claude/epi.md, the pack's settings.md and learnings.md. The pack (default sdlc;
    None installs nothing) is installed; `edits` change its workflow.yaml (see `edit_workflow`)."""
    tmp = Path(tempfile.mkdtemp(prefix="epi-test-"))
    repo = tmp / "app"
    repo.mkdir()
    sh("git init -q -b main && git config user.email t@t && git config user.name t", repo)
    epi_text, settings_text, learn_text = split_md(textwrap.dedent(md).lstrip())
    (repo / ".claude").mkdir()
    (repo / ".claude" / "epi.md").write_text(epi_text)
    mine = repo / ".claude" / "workflows" / config.pack_name(epi_text)
    if pack:
        pack_mod.install(repo, pack)
        edit_workflow(repo, [*getattr(md, "edits", ()), *(edits or ())], pack)
    mine.mkdir(parents=True, exist_ok=True)
    (mine / "settings.md").write_text(settings_text)
    (mine / "learnings.md").write_text(learn_text)
    (repo / "app.py").write_text("print('hi')\n")
    (repo / ".gitignore").write_text(".epi/\n*.lock\n")
    for name, text in (files or {}).items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    sh("git add -A && git commit -qm init", repo)
    fake = tmp / "claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(0o755)
    os.environ["EPI_CLAUDE_BIN"] = str(fake)
    return repo
