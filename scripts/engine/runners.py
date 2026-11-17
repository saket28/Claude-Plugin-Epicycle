"""Scripted work that costs no orchestrator tokens: `run` steps (repo commands and workflow scripts)
and headless skill reviews."""

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import usage, yamlish

TAIL = 60   # lines of command output kept in artifacts (keeps briefs small)

FINDINGS_FORMAT = """
When you have finished the review, end your reply with exactly one fenced block:
```json
{"findings": [{"severity": "important" | "nit" | "pre-existing", "file": "path",
  "line": 0, "summary": "one sentence", "detail": "why it matters and how to fix it"}],
 "replies": [{"id": "I1", "type": "accept" | "pushback", "text": "one sentence"}]}
```
Use "important" only for issues that should block merging. Use [] when there are no findings.
Fill "replies" only when threads are open for you (see below); leave it out otherwise.
Problems with your environment (you could not run something) belong in your text, not in the findings.
"""

OUTPUTS_FORMAT = """Also put `"outputs": {{{fields}}}` in that block: each value is plain text (one line unless you
need more), empty when there is nothing to report. Later steps read these, so keep them short and exact."""

LATER_ROUND = """This is a later round. You reviewed this change before: your previous report and the threads
open for you are below. Do not review everything again. Look at what changed since your last review
(`git diff {since}...HEAD`) and answer each open thread in "replies": "accept" when the fix is right,
"pushback" when it is not (say why in one sentence). Report a new finding only when the new changes broke
something (a correctness bug, a security problem, a spec violation or broken tests). Never raise an issue that
is already open or closed under a new id, and never raise wording, naming or extra-test suggestions."""


def tail(text, n=TAIL):
    lines = (text or "").rstrip().splitlines()
    return "\n".join(lines[-n:])


def write_artifact(path, front, body):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n" + yamlish.dumps(front) + "\n---\n\n" + body.rstrip() + "\n")


def read_artifact(path):
    path = Path(path)
    if not path.exists():
        return None, None
    try:
        return yamlish.parse_frontmatter(path.read_text())
    except yamlish.YamlError:
        return {"_invalid": True}, path.read_text()


def sh(cmd, cwd, timeout=1800, env=None):
    """Run a shell command; `env` is added to the environment."""
    t = time.time()
    env = {**os.environ, **env} if env else None
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return r.returncode, (r.stdout or "") + (r.stderr or ""), int(time.time() - t)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 124, out + f"\n[timed out after {timeout}s]", int(time.time() - t)


# ------------------------------------------------------------------ run steps --

def command_line(task_id, name):
    """How an agent runs the repo command `name` for a task: through the engine, which the plugin's hook allows
    without a prompt (only in exactly this form)."""
    return f'python3 "{Path(__file__).resolve().parent.parent / "epi.py"}" run {task_id} {name}'


def _failure_comment(to, summary, out):
    return ["## Comments", f"- new -> {to}, feedback, blocking: {summary}",
            "  " + tail(out, 5).replace("\n", " ")[:500]]


def run_command(name, cmd, cwd, out_path, to, timeout=1800, env=None):
    """A repo command as a step: exit 0 = PASS, 126/127 = error (not runnable), else FAIL. A failure
    is a comment addressed to `to` (the stage's do step) with the tail of the output."""
    code, out, secs = sh(cmd, cwd, timeout=timeout, env=env)
    if code in (126, 127):
        return {"error": f"`{cmd}` is not runnable (exit {code}): {tail(out, 5)}"}
    verdict = "PASS" if code == 0 else "FAIL"
    body = [f"# {name}: {verdict} (exit {code}, {secs}s)", f"`{cmd}`", ""]
    if code != 0:
        last = next((l.strip() for l in reversed((out or "").splitlines()) if l.strip()), "")
        body += _failure_comment(to, f"{name} failed (exit {code})" + (f": {last[:160]}" if last else ""), out)
    write_artifact(out_path, {"verdict": verdict, "important": 0 if code == 0 else 1, "nit": 0, "engine": "run"},
                   "\n".join(body))
    return {"verdict": verdict, "usage": {"tokens": 0}}


def no_command(name, out_path):
    write_artifact(out_path, {"verdict": "PASS", "important": 0, "nit": 0, "engine": "run"},
                   f"# {name}: PASS\n\nNo command is configured (global.command.{name}), so there is nothing to run.")
    return {"verdict": "PASS", "usage": {"tokens": 0}}


SCRIPT_EXIT = {0: "PASS", 1: "FAIL", 3: "WAIT", 4: "HUMAN", 5: "MANUAL"}


def run_script(path, request, cwd, out_path, to, timeout=1800, env=None):
    """A workflow script: JSON in on stdin, JSON out. Exit 0 PASS, 1 FAIL (`summary` is the feedback),
    3 WAIT, 4 HUMAN, 5 MANUAL (a person decides); anything else is an error. Returns the script's JSON
    plus `verdict`; PASS and FAIL write the step's report. The script is the repo's copy of the workflow's, so it
    finds the engine by EPI_PLUGIN_ROOT."""
    t = time.time()
    try:
        r = subprocess.run([sys.executable, str(path)], cwd=cwd, input=json.dumps(request), capture_output=True,
                           text=True, timeout=timeout,
                           env={**os.environ, **(env or {}),
                                "EPI_PLUGIN_ROOT": str(Path(__file__).resolve().parent.parent.parent)})
    except subprocess.TimeoutExpired:
        return {"error": f"{Path(path).name} timed out after {timeout}s"}
    verdict = SCRIPT_EXIT.get(r.returncode)
    try:
        data = json.loads(r.stdout) if r.stdout.strip() else {}
    except json.JSONDecodeError:
        data = {}
    if verdict is None or not isinstance(data, dict):
        return {"error": f"{Path(path).name} exited {r.returncode}: {tail((r.stderr or r.stdout), 5)}"}
    data["verdict"] = verdict
    if verdict in ("PASS", "FAIL"):
        body = [f"# {Path(path).stem}: {verdict} ({int(time.time() - t)}s)", "", data.get("summary", "")]
        if verdict == "FAIL":
            body += [""] + _failure_comment(to, data.get("summary") or f"{Path(path).stem} failed", data.get("detail", ""))
        write_artifact(out_path, {"verdict": verdict, "important": int(verdict == "FAIL"), "nit": 0, "engine": "script"},
                       "\n".join(body))
    return data


# ------------------------------------------------------- built-in reviews --

def claude_bin():
    for var in ("EPI_CLAUDE_BIN", "EVAL_EPI_CLAUDE_BIN", "CLAUDE_CODE_EXECPATH"):
        if os.environ.get(var) and Path(os.environ[var]).exists():
            return os.environ[var]          # CLAUDE_CODE_EXECPATH: the Claude Code running us
    found = shutil.which("claude")
    if found:
        return found
    for cand in [Path.home() / ".claude/local/claude", Path.home() / ".local/bin/claude"]:
        if cand.exists():
            return str(cand)
    bundled = sorted(glob.glob(str(Path.home() / ".vscode/extensions/anthropic.claude-code-*/"
                                   "resources/native-binary/claude")))
    if bundled:
        return bundled[-1]
    raise FileNotFoundError("cannot find the `claude` CLI; set EPI_CLAUDE_BIN")


HEADLESS_TIMEOUT = 3600


def headless(prompt, cwd, system_append="", model=None, disallow_edits=True, timeout=HEADLESS_TIMEOUT):
    cmd = [claude_bin(), "-p", prompt, "--output-format", "json"]
    if system_append:
        cmd += ["--append-system-prompt", system_append]
    if disallow_edits:
        cmd += ["--disallowedTools", "Edit", "Write", "NotebookEdit"]
    if model:
        cmd += ["--model", model]
    env = {**os.environ, "EPI_NESTED": "1"}
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env, timeout=timeout)
    try:
        obj = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude -p returned non-JSON (exit {r.returncode}): "
                           f"{(r.stderr or r.stdout)[:500]}")
    if obj.get("is_error"):
        raise RuntimeError(f"claude -p reported an error: {str(obj.get('result'))[:500]}")
    return obj.get("result") or "", usage.from_headless_json(obj)


def _blocks(text):
    """The JSON values in a reply's fenced blocks (or the whole reply), the last first."""
    blocks = re.findall(r"```json\s*([\[{].*?[\]}])\s*```", text or "", flags=re.S)
    stripped = (text or "").strip()
    if stripped.startswith(("[", "{")):
        blocks.append(stripped)
    for b in reversed(blocks):
        try:
            yield json.loads(b)
        except json.JSONDecodeError:
            continue


def parse_outputs(text, declared):
    """The declared outputs of a reply's {"outputs": {...}} block, as text."""
    for data in _blocks(text):
        if isinstance(data, dict) and isinstance(data.get("outputs"), dict):
            return {k: data["outputs"][k] for k in declared if k in data["outputs"]}
    return {}


def parse_review(text):
    """(findings, replies) from our {"findings": [...], "replies": [...]} block or /code-review's own bare
    JSON list; None when neither is there."""
    for data in _blocks(text):
        if isinstance(data, dict) and isinstance(data.get("findings"), list):
            replies = data.get("replies")
            return data["findings"], [r for r in replies if isinstance(r, dict)] if isinstance(replies, list) else []
        if isinstance(data, list) and all(isinstance(x, dict) for x in data):
            return data, []
    if re.search(r"no (issues|findings|bugs) (were )?found|found no (issues|bugs)", text or "", re.I):
        return [], []
    return None


def parse_findings(text):
    parsed = parse_review(text)
    return parsed[0] if parsed else None if parsed is None else []


BLOCKING = ("\"important\" means a correctness bug, a security problem, a violation of the spec, or broken tests. "
            "Docs and docstrings, naming, style, refactoring, missing extra tests and hardening for inputs the spec "
            "does not mention are \"nit\" unless the spec or the repository rules require them.")


def classify(findings, review_rules, cwd, model="haiku"):
    """Decide each finding's severity (important | nit) with BLOCKING's definition and REVIEW.md. Findings that
    claim `important` are checked too, so a reviewer cannot make a docstring block a merge. One cheap headless
    call; on failure, unclassified findings count as important and claimed ones keep their claim."""
    todo = [i for i, f in enumerate(findings) if str(f.get("severity", "")).lower() in ("", "important")]
    if not todo:
        return findings, {"tokens": 0}
    prompt = ("Classify each code review finding as \"important\" or \"nit\". " + BLOCKING + " Reply with only a "
              "JSON list of strings, one per finding, in order.\n\n"
              + ("Repository review rules:\n" + review_rules[:6000] + "\n\n" if review_rules else "")
              + "Findings:\n" + json.dumps([findings[i] for i in todo], indent=1)[:20000])
    try:
        text, use = headless(prompt, cwd, model=model)
        m = re.search(r"\[.*\]", text, flags=re.S)
        labels = json.loads(m.group(0)) if m else []
    except Exception:  # noqa: BLE001
        labels, use = [], {"tokens": 0}
    for n, i in enumerate(todo):
        lab = str(labels[n]).lower() if n < len(labels) else "important"
        findings[i]["severity"] = "nit" if lab.startswith("nit") else "important"
    return findings, use


def findings_artifact(name, engine, findings, out_path, to="director", replies=(), head=None, outputs=None):
    """A review report: findings are addressed to `to` (the stage's Do step); `replies` answer the threads open
    for this reviewer."""
    imp = [f for f in findings if str(f.get("severity", "")).lower() == "important"]
    nit = [f for f in findings if str(f.get("severity", "")).lower() == "nit"]
    body = [f"# Review: {name}", ""]
    if imp or nit or replies:
        body.append("## Comments")
        for r in replies:
            kind = "accept" if str(r.get("type", "")).lower() == "accept" else "pushback"
            body.append(f"- {r.get('id')} -> {to}, {kind}: {r.get('text', '')}")
        for sev, group in (("blocking", imp), ("nit", nit)):
            for f in group:
                loc = f"`{f.get('file')}:{f.get('line')}` " if f.get("file") else ""
                body.append(f"- new -> {to}, feedback, {sev}: {loc}{f.get('summary', '')}")
                for k in ("detail", "failure_scenario"):
                    if f.get(k):
                        body.append(f"   {f[k]}")
        body.append("")
    if not imp and not nit:
        body.append("No findings.")
    front = {"verdict": "CHANGES" if imp else "PASS", "important": len(imp), "nit": len(nit), "engine": engine}
    if head:
        front["head"] = head
    write_artifact(out_path, {**(outputs or {}), **front}, "\n".join(body))


def recheck_prompt(worktree, since):
    """The one small call of a later round: the diff since the last review, to answer the open threads against."""
    diff = subprocess.run(["git", "diff", f"{since}...HEAD"], cwd=worktree, capture_output=True, text=True).stdout
    return ("Re-check the open threads against this diff (what changed since your last review). Reply with the "
            "JSON block only.\n\n```diff\n" + diff[:40000] + "\n```")


def skill_review(cfg, name, worktree, base, out_path, effort="medium", model=None, inputs="", to="director",
                 spec="", threads="", previous="", since=None, recheck="threads", outputs=()):
    """Run a skill headless on the change and write its findings as a review report. It is given the spec
    (so it does not flag a requirement as a bug) and, in a later round, its open threads, its previous
    report and the commit it last looked at (`since`), so it answers its threads and looks only at what
    changed."""
    review_md = cfg.root / "REVIEW.md"
    rules = review_md.read_text() if review_md.exists() else ""
    head = git_head(worktree)
    parts = []
    if rules:
        parts.append("Repository review rules (REVIEW.md):\n" + rules)
    if spec:
        parts.append("The spec this change implements (a requirement in it is not a bug):\n" + spec[:12000])
    if inputs:
        parts.append("Inputs for this review:\n" + inputs)
    if since:
        parts.append(LATER_ROUND.format(since=since))
        if previous:
            parts.append("Your previous report:\n" + previous[:6000])
        parts.append("Threads open for you:\n" + (threads or "(none)"))
    system = "\n\n".join(parts) + "\n" + FINDINGS_FORMAT
    if outputs:
        system += OUTPUTS_FORMAT.format(fields=", ".join(f'"{o}": "..."' for o in outputs))
    start = f"{since}...HEAD" if since else f"{base}...HEAD"
    if since and recheck == "threads":           # a later round is a small re-check, not a second full review
        prompt, model = recheck_prompt(worktree, since), model or cfg.model("judge")
    else:
        prompt = f"/code-review {effort} {start}" if name == "code-review" else f"/{name}"
    text, use = headless(prompt, worktree, system_append=system, model=model)
    parsed = parse_review(text)
    if parsed is not None:
        findings, replies = parsed
        findings, cuse = classify(findings, rules, worktree, model=cfg.model("judge"))
        use = {**use, "tokens": use.get("tokens", 0) + cuse.get("tokens", 0)}
    if parsed is None:
        # hand the raw text to the orchestrator to convert (§5)
        raw = Path(out_path).with_suffix(".raw.md")
        raw.write_text(text)
        return {"convert": str(raw), "usage": use}
    findings_artifact(name, name, findings, out_path, to, replies=replies, head=head,
                      outputs=parse_outputs(text, outputs) if outputs else None)
    return {"usage": use}


def commit_worktree(wt, message):
    """Commit whatever an agent left uncommitted (a denied or forgotten `git commit` must not hide its work
    from the reviewers or from the merge). True when a commit was made."""
    if not subprocess.run(["git", "status", "--porcelain"], cwd=wt, capture_output=True, text=True).stdout.strip():
        return False
    subprocess.run(["git", "add", "-A"], cwd=wt, capture_output=True)
    r = subprocess.run(["git", "-c", "user.name=epi", "-c", "user.email=epi@localhost", "commit", "-q", "-m", message],
                       cwd=wt, capture_output=True, text=True)
    return r.returncode == 0


def git_head(cwd):
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None
