"""Hook handlers. Called as `epi.py hook <Event>` with the
hook input JSON on stdin. They do nothing unless a task is active in this session
and EPI_NESTED isn't set, so normal Claude Code use is unaffected."""

import json
import os
import re
import sys
from pathlib import Path

from . import config as config_mod, rules, usage, yamlish
from .store import Store


def _allow(reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": "allow",
                                             "permissionDecisionReason": reason}}))
    return 0


RUN = re.compile(r'python3 "?(?P<script>[^"\s]+)"? run (?:task-)?(?P<task>\w+) (?P<name>[\w-]+)')


def _runs_a_command(cfg, task, cmd):
    """Whether a Bash command is exactly `python3 <this plugin>/scripts/epi.py run <this task> <configured command>`."""
    m = RUN.fullmatch(cmd)
    if not m:
        return False
    names = {k for k, v in cfg.v("global.command").items() if k != "timeout" and v}
    script = Path(__file__).resolve().parent.parent / "epi.py"
    return Path(m["script"]).resolve() == script and m["task"] == str(task["id"]) and m["name"] in names


def _deny(reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))
    return 0


def _context(inp):
    """Return (cfg, store, task) or None when no task is active in this session."""
    if os.environ.get("EPI_NESTED"):
        return None
    cwd = inp.get("cwd") or os.getcwd()
    try:
        root = config_mod.main_checkout(cwd)
    except Exception:  # noqa: BLE001 - not a git repo
        return None
    if not (root / config_mod.EPI_FILE).exists():
        return None
    cfg = config_mod.load(root)
    store = Store(cfg)
    if not store.dir.exists():
        return None
    task = store.find_task_by_session(inp.get("session_id"))
    if not task:
        return None
    return cfg, store, task


def _brief_output(first_prompt):
    m = re.search(r"Read (\S+\.md) first", first_prompt or "")
    if not m:
        return None, None
    try:
        front, _ = yamlish.parse_frontmatter(Path(m.group(1)).read_text())
        return front.get("output"), front
    except (OSError, yamlish.YamlError):
        return None, None


def _agent_transcript(inp):
    p = inp.get("agent_transcript_path")
    if p:
        return Path(p)
    if inp.get("agent_id") and inp.get("transcript_path"):
        return usage.subagent_transcript(inp["transcript_path"], inp["session_id"], inp["agent_id"])
    return None


def pre_tool_use(inp):
    ctx = _context(inp)
    agent_type = inp.get("agent_type") or ""
    if ctx:
        ctx[1].beat(ctx[2]["id"])     # the session is alive (the board marks a task stalled without this)
    if not ctx or not agent_type:
        return 0                     # the orchestrator itself, or no active task
    cfg, store, task = ctx
    tool = inp.get("tool_name")
    ti = inp.get("tool_input") or {}
    session = task.get("session") or {}
    if tool == "Bash":
        cmd = " ".join(str(ti.get("command", "")).split())
        for pat in cfg.v("global.guardrail.blocked_commands"):
            pat = pat.replace("{base}", cfg.v("global.repo.base_branch"))
            if pat in cmd:
                store.event(task["id"], "guardrail_block", agent=agent_type, tool="Bash", detail=pat)
                return _deny(f"Epicycle guardrail: `{pat}` is not allowed for pipeline agents.")
        if _runs_a_command(cfg, task, " ".join(str(ti.get("command", "")).split())):
            return _allow("a configured repo command, run by the epi engine in the task's checkout")
        return 0
    target = ti.get("file_path") or ti.get("notebook_path")
    if not target:
        return 0
    target = Path(target).resolve()
    outputs, _ = _allowed_outputs(inp, session, agent_type)
    if any(target == Path(o).resolve() for o in outputs):
        return 0
    wt = Path(session.get("worktree") or task["worktree"]).resolve()
    rel = _relative(target, wt)
    name = config_mod.step_name(cfg, agent_type)
    agent = cfg.agent(name)           # agent.<name>.writes: output | worktree | paths
    reason = None
    if agent["writes"] == "worktree":
        if rel is None:
            reason = f"{name} may only write inside its worktree {wt}"
        elif any(rules.glob_match(g, rel) for g in cfg.v("global.guardrail.protected_paths")):
            reason = f"{rel} is a protected path"
    elif agent["writes"] == "paths":
        if rel is None or not any(rules.glob_match(g, rel) for g in agent["paths"]):
            reason = f"{name} may only write {', '.join(agent['paths']) or 'nothing but its report'}"
    else:
        reason = "this agent may only write its own output file: " + ", ".join(outputs or ["(none)"])
    if reason:
        store.event(task["id"], "guardrail_block", agent=agent_type, tool=tool, path=str(target))
        return _deny(f"Epicycle guardrail: {reason}.")
    return 0


def _allowed_outputs(inp, session, agent_type):
    tr = _agent_transcript(inp)
    if tr and tr.exists():
        out, front = _brief_output(usage.summarize(tr)["first_prompt"])
        if out:
            return [out], front
    return [s["output"] for s in session.get("spawned", []) if s.get("agent") == agent_type], None


def _relative(target, root):
    try:
        return str(target.relative_to(root))
    except ValueError:
        return None


def subagent_stop(inp):
    ctx = _context(inp)
    if not ctx:
        return 0
    cfg, store, task = ctx
    store.beat(task["id"])
    session = task.get("session") or {}
    tr = _agent_transcript(inp)
    summary = usage.summarize(tr) if tr else {"tokens": 0}
    output, front = _brief_output(summary.get("first_prompt"))
    stage = (front or {}).get("stage") or session.get("stage") or "other"
    data = {k: summary.get(k) for k in ("tokens", "input", "output", "cache_creation", "cache_read",
                                         "duration_s", "tool_uses", "tool_errors", "model", "context")}
    key = inp.get("agent_id") or "agent"
    store.event(task["id"], "agent", key=key, agent_id=inp.get("agent_id"),
                agent=inp.get("agent_type") or "", stage=stage,
                step=(front or {}).get("step"), data=data)
    store.record_usage(task["id"], key, data.get("tokens") or 0)
    if output and not Path(output).exists() and not inp.get("stop_hook_active"):
        print(json.dumps({"decision": "block",
                          "reason": f"You haven't written {output} yet. Write it now, in the format "
                                    f"your brief describes, then finish."}))
    return 0


def stop(inp):
    ctx = _context(inp)
    if not ctx:
        return 0
    cfg, store, task = ctx
    store.beat(task["id"])
    offset = None
    for e in store.events(task["id"]):
        if e.get("agent") == "orchestrator":
            offset = (e.get("data") or {}).get("lines", offset)
    tpath = Path(inp.get("transcript_path", ""))
    if offset is None:
        # first record for this task: start where the session began working on it,
        # so earlier unrelated conversation isn't charged to the task
        offset = 0
        if tpath.exists():
            marker = f"next {task['id']}"
            for i, line in enumerate(tpath.read_text(errors="replace").splitlines()):
                if marker in line:
                    offset = max(i - 2, 0)
                    break
    s = usage.summarize(tpath, start_line=offset)
    if s["tokens"]:
        key = f"orchestrator-{offset}"
        store.event(task["id"], "agent", key=key, agent="orchestrator", stage="orchestrate",
                    data={"tokens": s["tokens"], "cache_read": s["cache_read"], "lines": s["lines"],
                          "model": s["model"]})
        store.record_usage(task["id"], key, s["tokens"])
    if (task.get("session") or {}).get("closing"):
        store.update_task(task["id"], session=None)
    return 0


HANDLERS = {"PreToolUse": pre_tool_use, "SubagentStop": subagent_stop, "Stop": stop}


def main(event):
    try:
        inp = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    handler = HANDLERS.get(event)
    if not handler:
        return 0
    try:
        return handler(inp)
    except Exception as e:  # noqa: BLE001
        # Fail closed only for writes during an active task; never break other hooks.
        if event == "PreToolUse" and inp.get("agent_type"):
            return _deny(f"Epicycle guardrail check failed ({e}); write refused to be safe.")
        print(f"epi hook {event} error: {e}", file=sys.stderr)
        return 0
