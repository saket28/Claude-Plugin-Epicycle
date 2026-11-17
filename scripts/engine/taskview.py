"""What the dashboard shows of a task, in plain words: a card, and a drawer that gives the summary first
and the details on click. Everything here is read from the task's events and reports; nothing is stored.

A task card:  title, one status line, the round. A task that needs a person says so (`needs_you`).
A task drawer: status, a decision box when it needs you, progress (stage > round > step), the
conversation in three groups, and the details.
"""

import datetime as dt
import re

from . import outputs, briefs, machine, runners, thread, workflow
from .machine import NEEDS
from .config import git, step_name
from .store import Store

EXPLAIN = {
    "disagreement": "An issue is still open after the last round: the two sides did not settle it, so you decide. "
                    "Accept the work as it is, or send it back to a step with instructions.",
    "failure": "A step failed and nothing in this stage can fix it. Try again, accept it as it is, or send it back to "
               "an earlier step with instructions.",
    "budget": "The task used up its token budget. Add tokens to carry on, or send it back with new direction.",
    "rules": "A merge rule is not met, so the task cannot merge by itself.",
    "adapter": "A git, pull request, CI or command step failed in a way that could not be recovered automatically.",
    "finish": "A final step kept failing, so the stage cannot finish.",
    "setup": "The setup command failed, so the task's checkout is not ready.",
    "failed": "The task stopped with an error.",
    "questions": "These could not be answered from the code or your notes. Answer them, then continue the task.",
}


def titleize(text):
    return re.sub(r"[-_]+", " ", text or "").strip().capitalize()


def ts(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def label(step):
    return step.get("name") or titleize(workflow.name_of(step))


def short_title(meta, task_dir, facts=None, limit=60):
    """The spec's own short title, else the request cut to `limit` characters."""
    front = (runners.read_artifact(task_dir / f"{facts}.md")[0] or {}) if facts else {}
    title = " ".join(str(front.get("title") or "").split())
    if not title:
        title = " ".join(str(meta.get("title") or "").split())
    return title if len(title) <= limit else title[:limit - 1].rstrip() + "…"


def needs_of(st):
    """{"reason", "text"} when the task waits for a person, else None. Never for a task that is done or cancelled."""
    if st.cancelled or st.stage == "done":
        return None
    if st.failed:
        return {"reason": "failed", "text": NEEDS["failed"]}
    if st.needs_you:
        r = st.needs_you.get("reason")
        return {"reason": r, "text": NEEDS.get(r, r)}
    if st.questions:
        return {"reason": "questions", "text": NEEDS["questions"]}
    if waiting_approval(st)[1]:
        return {"reason": "approval", "text": NEEDS["approval"]}
    return None


def waiting_approval(st):
    sid, g = machine.waiting_approval(st)
    return (sid, g) if g else (None, None)


def first_pass(events):
    """Passed every stage in its first round and never needed a person."""
    return not any(e["type"] in ("needs_you", "failed", "accept", "send_back", "final") or
                   (e["type"] == "round" and (e.get("r", 1) > 1 or e.get("extra"))) for e in events)


def status_line(p, st, needs, current, idle):
    if st.cancelled:
        return "Cancelled"
    if st.stage == "done":
        return "Done, merged" if st.merged else "Done"
    if needs and needs["reason"] == "approval":
        return "Waiting for your sign-off"
    if needs:
        return "Needs you: " + needs["text"]
    if st.stage == "queued":
        return "Waiting to start"
    if idle:
        return f"Paused: run /epi:start task-{p.task_id} in Claude Code to continue"
    if current:
        return current.get("doing") or (f"{label(current)} working" if workflow.kind_of(current) == "agent"
                                        else f"{label(current)} running")
    return p.stages[st.stage].get("name") or st.stage if st.stage in p.stages else st.stage


def current_step(p, st):
    """The step a task is at: the first selected step of the current phase with no report yet."""
    if st.stage in ("queued", "done") or not st.sub or st.sub not in workflow.PHASES or st.stage not in p.stages:
        return None
    for step in p.steps(st.stage, st.sub):
        sel = st.selected.get((st.round, st.sub, step["_id"]))
        if (sel is None or sel["selected"]) and not p.out(st.round, step).exists():
            return step
    return None


def stage_name(p, sid):
    return (p.wf["stages"].get(sid) or {}).get("name") or {"queued": "Backlog", "done": "Done"}.get(sid, sid)


def round_number(st):
    """The current round of the current stage, as shown to people (1, 2, ...)."""
    rounds = [e for e in st.events if e["type"] == "round" and e["stage"] == st.stage]
    return rounds[-1]["number"] if rounds else 0


STALLED = "no activity for {n} min, the session may have stopped"


def quiet_minutes(cfg, meta, last_event, now):
    """Whole minutes since the task last showed life: an event, or a tool call by its session or agents
    (the heartbeat). None while a command or headless run is under way, or with no activity at all."""
    at, until = Store(cfg).last_beat(meta["id"])
    if until and until > now.timestamp():
        return None
    times = [t for t in (last_event and last_event.timestamp(), at) if t]
    return int((now.timestamp() - max(times)) // 60) if times else None


STRIP_STATE = {"PASS": "pass", "CHANGES": "changes", "FAIL": "changes"}


def strip(p, st):
    """The steps of the current stage's latest round, one {label, state} each (pass, changes, running, todo,
    skipped), for the card's row of squares. Empty while the task is queued, done or cancelled."""
    if st.cancelled or st.stage in ("queued", "done") or st.stage not in p.stages:
        return []
    rounds = [e for e in st.events if e["type"] == "round" and e["stage"] == st.stage]
    if not rounds:
        return []
    cur = (current_step(p, st) or {}).get("_id")
    out = []
    for sub in workflow.PHASES:
        for s in p.steps(st.stage, sub):
            v = _step_view(p, st.events, {}, st.stage, sub, s, rounds[-1]["n"], rounds[-1]["number"])
            state = {"done": STRIP_STATE.get(v["verdict"], "pass"), "left out": "skipped"}.get(v["state"])
            out.append({"label": v["label"], "state": state or ("running" if s["_id"] == cur else "todo")})
    return out


def card(cfg, meta, p=None, st=None):
    p = p or machine.Machine(cfg, meta["id"])
    st = st or p.state()
    events = st.events
    done = st.cancelled or st.stage == "done"
    needs = needs_of(st)
    last = max((ts(e["ts"]) for e in events if ts(e.get("ts", ""))), default=None)
    session = meta.get("session") or {}
    now = dt.datetime.now(dt.timezone.utc)
    quiet = quiet_minutes(cfg, meta, last, now)
    stalled = bool(session and not session.get("closing") and not done and quiet is not None
                   and quiet >= cfg.v("global.dashboard.stall_after"))
    idle = not done and bool(not session or session.get("closing")) and bool(last and (now - last).total_seconds() > 60)
    sid, g = waiting_approval(st)
    cur = current_step(p, st)
    total, _ = p.task_usage()
    stage = "cancelled" if st.cancelled else st.stage
    return {"id": meta["id"], "title": short_title(meta, p.dir, p.facts), "status": stage, "stage": st.stage,
            "stage_name": stage_name(p, st.stage), "phase": st.sub or "", "step": cur["_id"] if cur else "",
            "round": round_number(st), "tokens": total, "budget": cfg.v("global.budget")["per_task"] + st.budget_extra,
            "approval": sid, "stalled": stalled, "quiet_minutes": quiet if stalled else None, "needs_you": needs,
            "text": status_line(p, st, needs, cur, idle and not needs and not g), "idle": idle and not done,
            "strip": strip(p, st), "updated": last.isoformat() if last else None, "first_pass": first_pass(events) if st.stage == "done" else None,
            "alerts": 0 if done else len([a for a in Store(cfg).alerts(meta["id"]) if a["kind"] != "needs_you"])}


# ---------------------------------------------------------------- the activity feed --

def _stage_text(e, name):
    return "finished" if e["stage"] == "done" else None      # a round starting already says where it is


# What a person cares about, in plain words: event type -> text (or None to leave this one out).
ACTIVITY = {
    "round": lambda e, name: f"{name(e['stage'])} round {e['number']} started",
    "stage": _stage_text,
    "needs_you": lambda e, name: "needs you: " + NEEDS.get(e.get("reason"), str(e.get("reason"))),
    "failed": lambda e, name: "failed",
    "approval": lambda e, name: {"approved": f"{name(e['approval'])} signed off" + (f" by {e['by']}" if e.get("by") else ""),
                                 "waiting": f"{name(e['approval'])} waiting for your sign-off"}.get(e.get("status")),
                                # "changes" is followed by its send_back event, which says it
    "accept": lambda e, name: "accepted as is",
    "send_back": lambda e, name: f"sent back to {name(e['to'])}",
    "merged": lambda e, name: "merged",
    "cancelled": lambda e, name: "cancelled",
}


def activity(cfg, store, limit):
    """The latest `limit` events across all tasks, newest first: [{task, ts, text}]."""
    if limit <= 0:
        return []
    stages = cfg.workflow["stages"]
    name = lambda sid: (stages.get(sid) or {}).get("name") or titleize(sid)
    rows = []
    for m in store.tasks():
        for e in store.events(m["id"]):
            text = ACTIVITY.get(e["type"], lambda *_: None)(e, name)
            if text and e.get("ts"):
                rows.append({"task": m["id"], "ts": e["ts"], "text": text})
    rows = [r for _, r in sorted(enumerate(rows), key=lambda x: (x[1]["ts"], x[0]), reverse=True)]
    return rows[:limit]


# ---------------------------------------------------------------- the drawer --

def _agent_events(events):
    """{key: data} of the latest usage record of every agent run or headless skill."""
    out = {}
    for e in events:
        if e["type"] == "agent":
            out[e.get("key") or e.get("agent_id")] = {"agent": e.get("agent"), **(e.get("data") or {})}
    return out


def _step_view(p, events, agents, sid, sub, step, n, number):
    sel = next((e for e in events if e["type"] == "selection" and e.get("round") == n and e.get("sub") == sub
                and e["step"] == step["_id"]), None)
    f = p.out(n, step)
    front, body = runners.read_artifact(f)
    ran = f.exists()
    kind = workflow.kind_of(step)
    use = agents.get(f"{sid}-{step['_id']}-n{n}") or agents.get(f"skill-{workflow.name_of(step)}-{sid}-{n}") or {}
    comments = [c for c in thread.parse_comments(body or "") if c["id"] == "new"]
    state = "done" if ran else "left out" if sel and not sel["selected"] else "waiting"
    return {"id": step["_id"], "label": label(step), "kind": kind, "phase": sub, "state": state,
            "verdict": (front or {}).get("verdict") or "", "reason": (sel or {}).get("reason", ""),
            "blocking": not step.get("continue-on-error"),
            "found": [{"severity": c["severity"] or "nit", "text": c["text"]} for c in comments],
            "report": f"{sid}/r{number}/{step['_id']}.md" if ran else None,
            **outputs.view(outputs.read(p.round_dir(n)).get(step["_id"])),
            "who": {"agent": use.get("agent"), "model": use.get("model"), "tokens": use.get("tokens"),
                    "cache_read": use.get("cache_read"), "seconds": use.get("duration_s"),
                    "context": context_share(p.cfg, use)} if use else None}


def context_share(cfg, use):
    """How full the agent's context window got at its fullest (0-1), or None when the run did not report it."""
    w = cfg.v("global.budget.context_window")
    return round(use["context"] / w, 2) if use.get("context") and w else None


def progress(p, st, events):
    """One row per stage; each has its rounds; each round its steps (with phase, verdict, report, who ran it)."""
    agents = _agent_events(events)
    _, by_stage = p.task_usage()
    rows = []
    cur_i = p.order.index(st.stage) if st.stage in p.order else (len(p.order) if st.stage == "done" else -1)
    for i, sid in enumerate(p.order):
        rounds = [e for e in events if e["type"] == "round" and e["stage"] == sid]
        views = []
        for j, e in enumerate(rounds):
            n = e["n"]
            end = rounds[j + 1]["ts"] if j + 1 < len(rounds) else None
            steps = [_step_view(p, events, agents, sid, sub, s, n, e["number"])
                     for sub in workflow.PHASES for s in p.steps(sid, sub)]
            ran = [s for s in steps if s["state"] == "done" and s["verdict"]]
            bad = any(s["verdict"] in ("FAIL", "CHANGES") for s in ran)
            views.append({"number": e["number"], "n": n, "reason": e.get("reason", "") if e.get("extra") or e.get("final") else "",
                          "extra": bool(e.get("extra")), "final": bool(e.get("final")),
                          "verdict": "CHANGES" if bad else "PASS" if ran else "", "steps": steps, "end": end})
        state = "done" if i < cur_i or st.stage == "done" else "current" if i == cur_i else "todo"
        if st.cancelled and state == "current":
            state = "stopped"
        a, b = (ts(rounds[0]["ts"]) if rounds else None), None
        if rounds and state == "done":
            nxt = next((e for e in events if e["type"] == "stage" and ts(e["ts"]) and a and ts(e["ts"]) > a and e["stage"] != sid), None)
            b = ts(nxt["ts"]) if nxt else None
        last = views[-1] if views else None
        waiting = state == "current" and waiting_approval(st)[0] == sid
        accepted = state != "todo" and any(e["type"] == "accept" and e.get("stage") == sid for e in events)
        rows.append({"id": sid, "name": stage_name(p, sid), "state": state, "rounds": views, "rounds_used": len(views),
                     "verdict": "WAITING" if waiting else "ACCEPTED" if accepted else (last or {}).get("verdict", "") if state != "todo" else "",
                     "seconds": int((b - a).total_seconds()) if a and b else None, "tokens": by_stage.get(sid, 0),
                     "round": rounds[-1]["number"] if rounds else 0})
    return rows


def step_label(p, sid, step_id):
    """How people see a step: its `name` in the workflow, else its id."""
    for _, s in workflow.stage_steps(p.stages.get(sid) or {}):
        if s.get("_id", workflow.step_id(s)) == step_id:
            return label(s)
    return titleize(step_id)


def issue_view(i, p, numbers, you=()):
    talk = [x for x in i["lines"] if x["type"] not in ("decision", "help")]
    last = talk[-1] if talk else None
    claim = next((x for x in reversed(talk) if x["from"] == i["by"]), None)
    reply = next((x for x in reversed(talk) if x["from"] not in (i["by"], "human")), None)
    tags = []
    if i.get("flag") == "late":
        tags.append("found in the last round")
    elif i.get("flag") == "disputed":
        tags.append(f"{step_label(p, i['stage'], reply['from']) if reply else 'the other side'} disagrees")
    who = lambda n: "you" if n in ("human", *you) else n
    lines = [{**x, "from": who(x["from"]), "to": who(x.get("to"))} for x in i["lines"]]
    return {"id": i["id"], "title": i["title"], "severity": i["severity"], "by": i["by"], "state": i["state"],
            "stage": i["stage"], "round": numbers.get(i.get("round")), "tags": tags, "sent_back": i.get("flag") == "sent_back",
            "to": (last or {}).get("to"), "last_from": (last or {}).get("from"), "lines": lines,
            "claim": thread._clip(claim["text"], 160) if claim else "",
            "reply": thread._clip(reply["text"], 160) if reply else "",
            "reply_by": step_label(p, i["stage"], reply["from"]) if reply else "", "by_label": step_label(p, i["stage"], i["by"]),
            "failed": i.get("flag") == "failed"}


def conversation(p, st=None):
    """Issues in groups. Notes (nits) are closed on their own when their stage passes. An issue the person
    already gave instructions on is `sent_back` (waiting for the step it went to), not a decision."""
    st = st or p.state()
    numbers = {e["n"]: e["number"] for e in st.events if e["type"] == "round"}
    you = p.human_roles()
    waiting = bool(needs_of(st))
    out = {"needs_decision": [], "in_progress": [], "resolved": [], "notes": []}
    for i in thread.threads(p.dir):
        v = issue_view(i, p, numbers, you)
        if v["sent_back"] and i["state"] == "open" and v["last_from"] in ("director", "you"):
            v.update(state="sent_back", waiting_for=step_label(p, i["stage"], v["to"]))
        elif i["state"] == "needs_you" and not waiting:
            v.update(state="open")
        if i["severity"] == "nit" and not i.get("question"):
            out["notes"].append(v)
        elif v["state"] == "needs_you" or (i.get("question") and i["state"] == "open"):
            out["needs_decision"].append(v)
        elif i["state"] == "closed":
            out["resolved"].append(v)
        else:
            out["in_progress"].append(v)
    return out


def decision(cfg, p, st, conv):
    """The box at the top of the drawer when the task needs you: what is open, and the buttons."""
    needs = needs_of(st)
    sid, g = waiting_approval(st)
    if needs and needs["reason"] != "approval":
        r = needs["reason"]
        acts = {"disagreement": ["accept", "send_back"], "failure": ["retry", "accept", "send_back"],
                "rules": ["accept", "send_back"], "budget": ["budget", "send_back"],
                "questions": ["answer"], "failed": ["retry_task"], "finish": ["retry"], "adapter": ["retry"], "setup": ["retry"]}.get(r, ["retry"])
        return {"kind": r, "title": "Your decision", "text": needs["text"], "explain": EXPLAIN.get(r, ""),
                "targets": targets(p, st) if "send_back" in acts else [],
                "detail": "\n".join(f"{n}. {q}" for n, q in enumerate(st.questions, 1)) if r == "questions"
                else (st.needs_you or st.failed or {}).get("detail") or (st.failed or {}).get("reason", ""),
                "issues": conv["needs_decision"], "actions": acts}
    if g:
        what = f"Read the {p.facts} and approve it, or send it back with instructions." if sid == p.facts else \
            "Look over the change, then approve it or send it back with instructions."
        return {"kind": "approval", "title": "Your sign-off", "text": f"The {stage_name(p, sid).lower()} is ready for your sign-off",
                "explain": what, "detail": "" if sid == p.facts else change_summary(p), "issues": [], "approval": sid,
                "actions": ["approve", "send_back"], "targets": targets(p, st)}
    return None


def targets(p, st):
    """Where a person may send the task back: [{value: "stage/step", label}], nearest first."""
    return [{"value": f"{sid}/{step}", "label": f"{step_label(p, sid, step)} ({stage_name(p, sid)})"}
            for sid, step in machine.send_back_targets(p, st)]


def change_summary(p):
    """What the person is about to approve, in one line: the size of the change and its branch."""
    stat = git("diff", "--shortstat", f"{p.base}...{p.meta['branch']}", cwd=p.cfg.root, check=False)
    return f"{stat.strip() or 'No changes'} on branch {p.meta['branch']}"


def retro_tokens(events):
    """Tokens of the learning run that follows a finished task (events after the task is done)."""
    done = next((i for i, e in enumerate(events) if e["type"] == "stage" and e["stage"] == "done"), None)
    if done is None:
        return None
    return sum((e.get("data") or {}).get("tokens", 0) for e in events[done:] if e["type"] == "agent")


def who_label(cfg, agent):
    """How the steps show who ran: the workflow's step name ("Builder"), not the agent's file name."""
    name = step_name(cfg, agent)
    for st in cfg.workflow["stages"].values():
        for _, s in workflow.stage_steps(st):
            if workflow.kind_of(s) in ("agent", "skill") and workflow.name_of(s) == name:
                return label(s)
    return titleize(name)


def cost_rows(cfg, events):
    """Where the tokens went: [{stage, round, who, tokens}] per stage round and agent, in order; the
    orchestrator and the retro have no round."""
    rows, rnd = {}, None
    for e in events:
        if e["type"] == "round":
            rnd = (e["stage"], e["number"])
        elif e["type"] == "stage" and e["stage"] == "done":
            rnd = ("retro", 0)
        elif e["type"] == "agent" and (e.get("data") or {}).get("tokens"):
            who = who_label(cfg, e["agent"].replace("headless:", ""))
            where = ("", 0) if e["agent"] == "orchestrator" else rnd or ("", 0)
            key = (*where, who)
            rows[key] = rows.get(key, 0) + e["data"]["tokens"]
    return [{"stage": k[0], "round": k[1], "who": k[2], "tokens": t} for k, t in rows.items()]


def details(cfg, meta, p, st, events):
    from . import serve                         # the learnings a task cited live with the dashboard's settings code
    done = st.cancelled or st.stage == "done"
    alerts = [] if done else [a for a in Store(cfg).alerts(meta["id"]) if a["kind"] != "needs_you"]
    return {"request": meta.get("source") or meta.get("title"), "branch": meta["branch"], "pr": meta.get("pr"),
            "created": meta.get("created"), "owner": meta.get("owner"), "applied": serve._applied_entries(cfg, p.dir),
            "accepts": [{"stage": e.get("stage"), "reason": e.get("reason"), "by": e.get("by")} for e in st.accepts],
            "alerts": alerts, "retro_tokens": retro_tokens(events), "cost": cost_rows(cfg, events),
            "problems": [{"type": e["type"], "text": e.get("detail") or e.get("path") or ""}
                         for e in events if e["type"] in ("guardrail_block", "error", "retry")][-20:]}


def task(cfg, meta):
    p = machine.Machine(cfg, meta["id"])
    st = p.state()
    out = card(cfg, meta, p, st)
    conv = conversation(p, st)
    out.update(progress=progress(p, st, st.events), conversation=conv, decision=decision(cfg, p, st, conv),
               details=details(cfg, meta, p, st, st.events),
               rounds_allowed=cfg.v(f"stage.{st.stage}.rounds") if st.stage in cfg.stages else None,
               pr=meta.get("pr"), branch=meta["branch"])
    return out
