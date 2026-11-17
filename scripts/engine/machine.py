"""The step machine: runs a workflow's stages, one task at a time.

`next_action(task)` runs every scripted step it can and returns the single action that needs Claude:

    {"action": "spawn",  "agents": [{"agent", "prompt", "output", "key"}]}
    {"action": "ask",    "questions": [...], "file": path}      # answers -> report
    {"action": "convert","raw": path, "output": path}            # rare skill-output fallback
    {"action": "wait",   "reason": ..., "hint": ..., "waiter": cmd | None}
    {"action": "done"}  /  {"action": "failed", "reason": ...}

A stage's phases (workflow.py): Plan once; the loop Do, Verify, Review; Finalize; then Finish, which starts with
a person's sign-off. A round is one pass of the loop. Round 1 does every Do step; later rounds only those with an
open thread. Verify (commands) runs every round; Review runs only when Verify passed, and a review step runs again
in a later round if it has an open thread, is `always_rerun`, or never ran. Findings become threads addressed to
the Do steps (thread.py). The loop ends when no blocking issue is open; then Finalize runs, and a failing Finalize
step starts another round. When `stage.<id>.rounds` is used up with blocking issues open, the task needs you; an
issue raised in the last round that nobody answered gets one extra round first. A failure nothing in the stage can
fix (no Do agent) needs you too. A person may send the task back to any earlier agent step with instructions.
Round folders are `<stage>/r<number>`, the number counting within the stage over all its visits.

State is derived from the task's events every time, so the loop is resumable at any point and a
deleted task folder simply stops existing.
"""

import json
import os
from pathlib import Path

from . import adapters, briefs, config as config_mod, outputs, principles, rules, runners, schema, thread, usage, workflow
from .config import git
from .store import Store

PASSED_LAST = "passed last round"
WAIT_REASONS = ("disagreement", "rules", "adapter", "setup", "budget", "failure", "finish")


NEEDS = {                      # reason -> the phrase on cards, in the strip and in CLI messages
    "disagreement": "a disagreement to settle",
    "failure": "a step failed and nothing here can fix it",
    "budget": "out of token budget",
    "rules": "a merge rule is not met",
    "adapter": "a command or host step failed",
    "finish": "a final step kept failing",
    "setup": "setup failed",
    "failed": "it failed",
    "approval": "waiting for your sign-off",
    "questions": "there are questions for you",
}


REPORT_FORMAT = """Write exactly one file, `{output}`:

```markdown
---
verdict: PASS | CHANGES
---
## Findings
(what you checked and what you found)
## Comments
- new -> {to}, feedback, blocking: one finding per line (blocking only if it should stop the change)
- new -> {to}, feedback, nit: a minor finding (never blocks)
```

PASS means nothing blocking; a PASS report cannot raise blocking issues. If threads are open for you above, answer
each on its own line: `- I3 -> {to}, accept` (fixed) or `- I3 -> {to}, pushback: why it is still wrong`. You are
`{me}`."""


def plain(reason):
    return NEEDS.get(reason, reason)


class State:
    """Replays a task's events."""

    def __init__(self, events):
        self.events = events
        self.stage = "queued"
        self.sub = None
        self.round = 0             # the running count of rounds over all stages (the folder is <stage>/r<number>)
        self.r = 0                 # the round within the stage visit
        self.final = False         # the round applies a person's instructions
        self.final_next = False    # a person's feedback is waiting for its final round
        self.visit_rounds = []     # the rounds of the current stage visit
        self.extra_used = False    # the visit already had its one extra round for late issues
        self.rounds = {}           # round -> (stage, r, final)
        self.needs_you = None
        self.failed = None
        self.cancelled = False
        self.approvals = {}            # stage -> {step, role, status, n}
        self.spawns = {}
        self.tries = {}
        self.selected = {}         # (round, sub, step id) -> {"selected": bool, "reason": str}
        self.budget_extra = 0
        self.skip_rules = False
        self.accepts = []
        self.questions = None                # what the spec owner asked a person and nobody has answered yet
        self.setup_done = False
        self.asked = {}            # (round, step id) -> questions asked
        self.answered = {}
        self.merged = False
        self.warned = set()
        for e in events:
            self.apply(e)

    def apply(self, e):
        t = e["type"]
        if t == "stage":
            self.stage, self.sub, self.r, self.final, self.final_next = e["stage"], None, 0, False, False
            self.visit_rounds, self.extra_used = [], False
        elif t == "round":
            self.round, self.r = e["n"], e["r"]
            self.final, self.final_next = bool(e.get("final")), False
            self.sub = e.get("sub", "do")
            self.rounds[self.round] = (e.get("stage"), self.r, self.final)
            self.visit_rounds.append(self.round)
            self.extra_used = self.extra_used or bool(e.get("extra"))
        elif t == "sub":
            self.sub = e["sub"]
        elif t == "final":
            self.final_next = True
        elif t == "needs_you":
            self.needs_you = e
        elif t == "resume":
            self.needs_you = None
        elif t == "failed":
            self.failed = e
        elif t == "retry_task":
            self.failed = None
            self.spawns = {}
        elif t == "cancelled":
            self.cancelled = True
        elif t == "approval":
            self.approvals[e["approval"]] = {k: e.get(k) for k in ("step", "role", "status", "n")}
        elif t == "spawn":
            self.spawns[e["key"]] = self.spawns.get(e["key"], 0) + 1
        elif t == "try":
            self.tries[e["key"]] = self.tries.get(e["key"], 0) + 1
        elif t == "selection":
            self.selected[(e["round"], e.get("sub", "review"), e["step"])] = {"selected": e["selected"],
                                                                              "reason": e.get("reason", "")}
        elif t == "budget_extra":
            self.budget_extra += int(e.get("tokens", 0))
        elif t == "accept":
            self.accepts.append(e)
            if e.get("skip_rules"):
                self.skip_rules = True
        elif t == "setup":
            self.setup_done = True
        elif t == "asked":
            self.asked[(e["round"], e["step"])] = e["k"]
            self.questions = e.get("questions") or ["(see the spec's Questions)"]
        elif t == "answers":
            self.answered[(e["round"], e["step"])] = e["k"]
            self.questions = None
        elif t == "merged":
            self.merged = True
        elif t == "budget" and e.get("level") == "warn":
            self.warned.add(e.get("scope"))


class Machine:
    def __init__(self, cfg, task_id, session_id=None):
        self.cfg = cfg
        self.store = Store(cfg)
        self.task_id = str(task_id)
        self.session_id = session_id
        self.meta = self.store.load_task(self.task_id)
        self._numbers = {}
        self.dir = self.store.task_dir(self.task_id)
        self.base = cfg.v("global.repo.base_branch")
        self.wf = cfg.workflow
        self.stages = cfg.stages
        self.order = list(self.stages)
        self.facts = self.wf.get("facts")

    # ---------------------------------------------------------- helpers --
    @property
    def wt(self):
        return Path(self.meta["worktree"])

    @property
    def cwd(self):
        """Where steps run: the task's checkout, or the repo once the checkout is gone (a step after clean-up)."""
        return self.wt if self.wf["workspace"] == "git-worktree" and self.wt.exists() else self.cfg.root

    def ev(self, type_, **f):
        return self.store.event(self.task_id, type_, **f)

    def state(self):
        return State(self.store.events(self.task_id))

    def round_dir(self, n):
        """Where round `n` keeps its reports: <stage>/r<number>, the number counting within the stage."""
        if n not in self._numbers:
            self._numbers = {e["n"]: (e["stage"], e["number"]) for e in self.store.events(self.task_id)
                             if e["type"] == "round"}
        sid, number = self._numbers[n]
        return self.dir / sid / f"r{number}"

    def round_number(self, n):
        """The round as the stage counts it (1 for the stage's first round), the number agents and people see."""
        return int(self.round_dir(n).name[1:])

    def out(self, n, step):
        return self.round_dir(n) / f"{step['_id']}.md"

    def stage_of(self, sid):
        return self.stages[sid]

    def steps(self, sid, sub):
        return self.stages[sid][sub]

    def first_do(self, sid):
        acts = self.steps(sid, "do")
        return acts[0]["_id"] if acts else None

    def fixers(self, sid):
        return [s for s in self.steps(sid, "do") if workflow.kind_of(s) == "agent"]

    def human_roles(self):
        return {workflow.name_of(s) for st in self.stages.values() for _, s in workflow.stage_steps(st)
                if workflow.kind_of(s) == "human"}

    def human_ids(self):
        """The step ids that speak for a person (a sign-off writes its feedback as that step)."""
        return {workflow.step_id(s) for st in self.stages.values() for _, s in workflow.stage_steps(st)
                if workflow.kind_of(s) == "human"}

    def approver(self):
        """The role a question to a person goes to: the first human role of the workflow."""
        return next(iter(sorted(self.human_roles())), "approver")

    def spec(self):
        front, body = runners.read_artifact(self.dir / f"{self.facts}.md") if self.facts else (None, None)
        return front or {}, body or ""

    def link_session(self, closing=False, spawned=None, stage=None, rnd=None):
        if not self.session_id:
            return
        self.meta = self.store.update_task(self.task_id, session={
            "id": self.session_id, "closing": closing, "spawned": spawned or [],
            "stage": stage, "round": rnd, "worktree": str(self.wt)})

    def needs_you(self, reason, stage, detail="", hint=None):
        self.ev("needs_you", reason=reason, stage=stage, detail=detail)
        return self._wait(f"needs-you: {reason}", hint or self._approve_hint(reason), detail=detail)

    def _approve_hint(self, reason):
        j = self.task_id
        return {"budget": f"/epi:approve {j} budget +500k  (or /epi:send-back {j} \"...\")",
                "disagreement": f"/epi:approve {j} accept  or /epi:send-back {j} \"what to do\"",
                "failure": f"/epi:approve {j} retry  or /epi:send-back {j} --to <step> \"what to do\"",
                "rules": f"/epi:approve {j} accept  or /epi:send-back {j} \"...\"",
                "finish": f"/epi:approve {j} retry",
                "adapter": f"/epi:approve {j} retry"}.get(reason, f"/epi:status {j}")

    def _wait(self, reason, hint, waiter=None, detail=""):
        self.link_session(closing=True)
        what = "Waiting for your sign-off" if "sign-off: waiting for" in reason else plain(reason.removeprefix("needs-you: "))
        return {"action": "wait", "task": self.task_id, "reason": reason, "message": what[:1].upper() + what[1:], "hint": hint,
                "waiter": waiter, "detail": detail}

    # ------------------------------------------------------------ budgets --
    def task_usage(self):
        latest, by_stage = {}, {}
        for e in self.store.events(self.task_id):
            if e["type"] == "agent":
                latest[e.get("key") or e.get("agent_id")] = e
        for e in latest.values():
            st = e.get("stage", "other")
            by_stage[st] = by_stage.get(st, 0) + e.get("data", {}).get("tokens", 0)
        return sum(by_stage.values()), by_stage

    def budget_check(self, stage):
        b = self.cfg.v("global.budget")
        st = self.state()
        total, by_stage = self.task_usage()
        limits = [("task", total, b["per_task"] + st.budget_extra),
                  ("stage:" + stage, by_stage.get(stage, 0),
                   (self.cfg.stage_budget(stage) or b["per_task"]) + st.budget_extra),
                  ("month", self.store.month_tokens(), b["per_month"])]
        for scope, used, limit in limits:
            if limit and used >= limit:
                self.ev("budget", level="exceeded", scope=scope, used=used, limit=limit)
                if b["on_exceed"] == "escalate":
                    return self.needs_you("budget", stage, f"{scope}: {used:,} of {limit:,} tokens")
            elif limit and used >= limit * b.get("warn_at", 0.8) and scope not in st.warned:
                self.ev("budget", level="warn", scope=scope, used=used, limit=limit)
        return None

    def role_of(self, step, sub):
        rid = step["_id"]
        return rid if rid in schema.roles(self.cfg.workflow_name) else ("reviewer" if sub == "review" else rid)

    def principles_for(self, role, stage):
        """The principles an agent should be pointed at: [{path, summary}]. Paths are in the main
        checkout, because a task's worktree may not have files that aren't committed yet."""
        specs = self.cfg.v("global.principle.list")
        if not specs:
            return []
        try:
            ctx = None if stage == self.facts or not (self.dir / f"{self.facts}.md").exists() else self.rule_ctx(stage)
        except Exception:  # noqa: BLE001 - filtering is best effort; never block a spawn on it
            ctx = None
        return [{"path": str(self.cfg.root / s["file"]), "summary": s.get("summary", "")}
                for s in principles.applicable(specs, role, ctx)]

    # ------------------------------------------------------------- spawn --
    def spawn(self, items, stage, rnd):
        """items: [(key, agent, role, brief_meta, sections, output)]"""
        st = self.state()
        over = self.budget_check(stage)
        if over:
            return over
        out = []
        for key, agent, role, meta, sections, output in items:
            attempts = self.cfg.v("global.pipeline.agent_attempts")
            if st.spawns.get(key, 0) >= attempts:
                self.ev("failed", reason=f"{agent} did not produce {Path(output).name} after {attempts} attempt(s)", key=key)
                self.link_session(closing=True)
                return {"action": "failed", "task": self.task_id,
                        "reason": f"{agent} did not produce {Path(output).name} after {attempts} attempt(s). "
                                  f"Fix the cause, then run /epi:start {self.task_id}."}
            brief = self.dir / "briefs" / f"{key}.md"
            name = config_mod.step_name(self.cfg, agent)
            meta = {"critical_thinking": self.cfg.critical_thinking(name), **meta}
            briefs.write(self.cfg, brief, {"task": self.task_id, "stage": stage, "round": self.round_number(rnd),
                                           "agent": agent, "output": str(output),
                                           "worktree": str(self.wt), **{k: v for k, v in meta.items() if k != "model"}},
                         role, sections, principles=self.principles_for(role, stage))
            self.ev("spawn", key=key, agent=agent, output=str(output), attempt=st.spawns.get(key, 0) + 1)
            entry = {"agent": agent, "key": key, "output": str(output),
                     "prompt": briefs.prompt_line(self.task_id, stage, rnd, brief)}
            model = meta.get("model") or self.cfg.model(name)
            if model and model != "inherit":
                entry["model"] = model               # the orchestrator passes this to the Agent tool
            out.append(entry)
        self.link_session(spawned=[{"agent": o["agent"], "output": o["output"]} for o in out],
                          stage=stage, rnd=rnd)
        return {"action": "spawn", "task": self.task_id, "agents": out,
                "note": "Spawn all agents in parallel and wait for every one, then call next again."}

    # --------------------------------------------------------- worktree --
    def ensure_worktree(self):
        wt = self.wt
        if wt.exists() and (wt / ".git").exists():
            return
        wt.parent.mkdir(parents=True, exist_ok=True)
        root = self.cfg.root
        if git("rev-parse", "--verify", "--quiet", self.meta["branch"], cwd=root, check=False):
            git("worktree", "add", str(wt), self.meta["branch"], cwd=root)
        else:
            git("worktree", "add", "-b", self.meta["branch"], str(wt), self.base, cwd=root)

    def changed_paths(self):
        out = git("diff", "--name-only", f"{self.base}...{self.meta['branch']}", cwd=self.cfg.root,
                  check=False)
        return [p for p in out.splitlines() if p]

    def records(self, n):
        """The round's published steps: {step id: {outcome, verdict, outputs, reused?}} (outputs.json)."""
        return outputs.read(self.round_dir(n))

    def stage_steps_ctx(self, st, sid, n=None):
        """What steps of stage `sid` published: its round `n`, or the latest round each step ran in. A step with no
        record (left out, disabled, not reached) is skipped."""
        out = {workflow.step_id(s): dict(outputs.SKIPPED) for _, s in workflow.stage_steps(self.wf["stages"][sid])}
        rounds = [n] if n is not None else [m for m, (x, _, _) in sorted(st.rounds.items()) if x == sid]
        for m in rounds:
            for k, rec in self.records(m).items():
                if n is not None or rec.get("outcome") != "skipped" or out.get(k, {}).get("outcome") == "skipped":
                    out[k] = rec
        return out

    def rule_ctx(self, sid, st=None, n=None):
        """What an `if` or a `${{ }}` of a step of stage `sid` (in round `n`) can read."""
        st = st or self.state()
        front = self.spec()[0]
        paths = list(front.get("paths") or []) if sid == self.facts else self.changed_paths()
        before = self.order[:self.order.index(sid)]
        return {"risk": front.get("risk", "low"), "touches_ui": bool(front.get("touches_ui")), "paths": paths,
                "steps": self.stage_steps_ctx(st, sid, n if n is not None else st.round or None),
                "stages": {x: {"steps": self.stage_steps_ctx(st, x)} for x in before}}

    def with_of(self, st, sid, step, n):
        """The step's `with`, `${{ }}` filled in."""
        w = step.get("with") or {}
        if not any(isinstance(v, str) and "${{" in v for v in w.values()):
            return dict(w)
        ctx = self.rule_ctx(sid, st, n)
        return {k: rules.interpolate(v, ctx) for k, v in w.items()}

    def dashboard_link(self):
        f = self.cfg.work_dir / "dashboard.url"
        return f"{f.read_text().strip()}#task-{self.task_id}" if f.exists() and f.read_text().strip() else ""

    def task_env(self, n=None):
        """The environment of every command: the task, its stage and round, where its reports are."""
        st = self.state()
        n = n or st.round
        known = n in st.rounds
        return {"EPI_TASK": self.task_id, "EPI_STAGE": st.stage, "EPI_ROUND": str(self.round_number(n)) if known else "",
                "EPI_TITLE": self._short_title(), "EPI_REPORTS": str(self.round_dir(n)) if known else "",
                "EPI_DASHBOARD": self.dashboard_link(), "EPI_OUTPUT": os.devnull}

    def output_file(self, n, step):
        return self.round_dir(n) / f"{step['_id']}.output"

    def step_env(self, st, sid, step, n):
        """A run step's environment: the task's, its own output file, and its `env` with `${{ }}` filled in."""
        env = {**self.task_env(n), "EPI_OUTPUT": str(self.output_file(n, step)), "EPI_STAGE": sid}
        mine = step.get("env") or {}
        ctx = self.rule_ctx(sid, st, n) if any("${{" in str(v) for v in mine.values()) else {}
        return {**env, **{k: rules.interpolate(rules._text(v), ctx) for k, v in mine.items()}}

    def publish(self, st, sid, sub, step, n):
        """Record the step's outcome and outputs for this round (outputs.json): what it reported, or that it was
        left out, or (it passed last round and was not run again) what it published then, marked `reused`."""
        sel = st.selected.get((n, sub, step["_id"])) or {"selected": True, "reason": ""}
        if sel["selected"]:
            front = runners.read_artifact(self.out(n, step))[0] or {}
            raw = outputs.parse_file(self.output_file(n, step)) if workflow.kind_of(step) == "run" else front
            rec = outputs.record(front.get("verdict"), raw, step.get("outputs"))
        elif sel["reason"] == PASSED_LAST:
            prev = next((r for m in reversed(st.visit_rounds) if m < n
                         for r in [self.records(m).get(step["_id"])] if r and r["outcome"] != "skipped"), None)
            rec = outputs.reused(prev) if prev else dict(outputs.SKIPPED)
        else:
            rec = dict(outputs.SKIPPED)
        outputs.write_step(self.round_dir(n), step["_id"], rec)

    # ------------------------------------------------------------- stages --
    def first_sub(self, sid, after=None):
        subs = workflow.PHASES
        for sub in subs[(subs.index(after) + 1 if after else 0):]:
            if self.steps(sid, sub):
                return sub
        return None

    def start_round(self, sid, r, reason, final=False, sub="do", extra=False):
        events = self.store.events(self.task_id)
        n = max([e["n"] for e in events if e["type"] == "round"] or [0]) + 1
        number = sum(1 for e in events if e["type"] == "round" and e["stage"] == sid) + 1
        self.ev("round", n=n, stage=sid, number=number, r=r, final=final, extra=extra, reason=reason, sub=sub)
        self.round_dir(n).mkdir(parents=True, exist_ok=True)
        return n

    def enter_stage(self, sid, reason="start"):
        thread.close_stage(self.dir, sid, "closed, the stage started over")
        self.ev("stage", stage=sid)
        self.start_round(sid, 1, reason, sub=self.first_sub(sid) or "review")

    def advance(self, st, sid):
        """After a phase: the next one, or the next stage, or done."""
        nxt = self.first_sub(sid, st.sub)
        if nxt:
            self.ev("sub", sub=nxt)
            return None
        thread.close_notes(self.dir, sid)
        i = self.order.index(sid)
        if i + 1 < len(self.order):
            self.enter_stage(self.order[i + 1])
            return None
        return self.finish(st)

    def cannot_fix(self, sid, reason, detail=""):
        """A failure no step of this stage can fix: a person decides (try again, accept, or send it back to an
        earlier step with instructions)."""
        thread.escalate(self.dir, reason, sid, tag=False)
        thread.write_view(self.dir)
        return self.needs_you("failure", sid, f"{reason}: {detail}" if detail else reason)

    # ------------------------------------------------------------- main --
    def next_action(self):
        for _ in range(80):           # scripted steps loop until Claude is needed
            st = self.state()
            if st.cancelled:
                return {"action": "done", "task": self.task_id, "cancelled": True}
            if st.failed:
                return {"action": "failed", "task": self.task_id, "reason": st.failed.get("reason"),
                        "hint": f"/epi:start {self.task_id} retries it"}
            if st.needs_you:
                nh = st.needs_you
                return self._wait(f"needs-you: {nh['reason']}", self._approve_hint(nh["reason"]),
                                  detail=nh.get("detail", ""))
            if st.stage == "queued":
                result = self.sub_queued(st)
            elif st.stage == "done":
                return {"action": "done", "task": self.task_id}
            else:
                result = getattr(self, "sub_" + (st.sub or "review"))(st)
            if result is not None:
                return result
        raise RuntimeError("the machine made no progress after 80 scripted steps")

    def sub_queued(self, st):
        if self.wf["workspace"] == "git-worktree":
            self.ensure_worktree()
            setup = self.cfg.command("setup")
            if setup and not st.setup_done:
                code, out, _ = runners.sh(setup, self.wt, timeout=self.cfg.v("global.command.timeout"))
                if code != 0:
                    return self.needs_you("setup", "queued", runners.tail(out, 20))
        self.ev("setup")
        self.enter_stage(self.order[0])
        return None

    # ------------------------------------------------------ phase: plan --
    def sub_plan(self, st):
        sid = st.stage
        action, _ = self.run_list(st, sid, "plan", self.steps(sid, "plan"), st.round)
        if action:
            return action
        return self.advance(st, sid)

    # ------------------------------------------------------ phase: do --
    def sub_do(self, st):
        sid, n = st.stage, st.round
        action, failed = self.run_list(st, sid, "do", self.steps(sid, "do"), n)
        if action:
            return action
        if failed is not None:
            return self.cannot_fix(sid, f"{failed['_id']} failed", self._summary(n, failed))
        for step in self.steps(sid, "do"):            # a command cannot answer a thread: running again is its answer
            if workflow.kind_of(step) != "agent" and self.is_selected(st, "do", step, n):
                thread.close_awaiting(self.dir, step["_id"], sid, f"closed, {step['_id']} ran again")
        ask = self.questions(st, sid, n)
        if ask:
            return ask
        self.publish_facts(sid, n)
        if st.final:                      # the person's instructions were applied: they close what was sent back
            thread.close_sent_back(self.dir)
            thread.close_by(self.dir, self.human_roles() | self.human_ids(), "closed, your instructions were applied")
        return self.advance(st, sid)

    def publish_facts(self, sid, n):
        """The facts stage's first do-step report is the task's facts doc (<facts>.md): later steps read it."""
        if sid == self.facts and self.first_do(sid):
            f = self.round_dir(n) / f"{self.first_do(sid)}.md"
            if f.exists():
                (self.dir / f"{self.facts}.md").write_text(f.read_text())

    def questions(self, st, sid, n):
        """A do agent's open questions become issues for the human role, and an `ask` action."""
        attempts = self.cfg.v("global.pipeline.agent_attempts")
        role = self.approver()
        for step in self.steps(sid, "do"):
            if workflow.kind_of(step) != "agent" or not self.is_selected(st, "do", step, n):
                continue
            f = self.out(n, step)
            front, body = runners.read_artifact(f)
            key = (n, step["_id"])
            asked, answered = st.asked.get(key, 0), st.answered.get(key, 0)
            qs = [q for q in _questions(body or "")]
            if int((front or {}).get("open_questions") or 0) > 0 and asked == answered and asked < attempts:
                asked += 1
                self.ev("asked", round=n, step=step["_id"], k=asked, questions=qs)
                for q in qs:
                    thread.add_question(self.dir, sid, step["_id"], role, q, n)
            if asked > answered:
                return {"action": "ask", "task": self.task_id, "questions": qs or ["(see the report)"],
                        "file": str(f), "spec": str(f),
                        "note": "Answer from the workflow's learnings.md Answers/Learnings and CLAUDE.md where you can; ask the "
                                "human only what's left. Then: epi.py report <task> --answers <file>."}
        return None

    # ---------------------------------------------- phases: verify, review --
    def commit(self, sid, n):
        if self.wf["workspace"] == "git-worktree" and self.wt.exists():   # checks look at commits: keep what an agent left
            runners.commit_worktree(self.wt, f"task-{self.task_id}: {sid} round {self.round_number(n)}")

    def settle_passed(self, st, sid, sub, n):
        """A command that passes this round settles the issues it raised before (it cannot reply in a thread)."""
        for s in self.steps(sid, sub):
            if workflow.kind_of(s) == "run" and self.is_selected(st, sub, s, n) and \
                    (runners.read_artifact(self.out(n, s))[0] or {}).get("verdict") == "PASS":
                thread.close_raised(self.dir, s["_id"], sid, f"closed, {s['_id']} passed when run again")

    def sub_verify(self, st):
        """Every command runs (so one round reports every failure); Review runs only when they all passed."""
        sid, n = st.stage, st.round
        self.commit(sid, n)
        action, _ = self.run_list(st, sid, "verify", self.steps(sid, "verify"), n)
        if action:
            return action
        bad = [s for s in self.steps(sid, "verify") if self.is_selected(st, "verify", s, n)
               and self.failing(s, runners.read_artifact(self.out(n, s))[0])]
        self.ingest(st, sid, n)
        self.settle_passed(st, sid, "verify", n)
        if bad or not self.steps(sid, "review"):
            return self.end_of_round(st, sid, n)
        self.ev("sub", sub="review")
        return None

    def sub_review(self, st):
        sid, n = st.stage, st.round
        self.commit(sid, n)
        self.ingest(st, sid, n)
        action, _ = self.run_list(st, sid, "review", self.steps(sid, "review"), n)
        if action:
            return action
        self.ingest(st, sid, n)
        return self.end_of_round(st, sid, n)

    def end_of_round(self, st, sid, n):
        """The director's decision once the loop's checks are in: another round, a person, or on."""
        if thread.awaiting_you(self.dir, sid):
            return self.needs_you("disagreement", sid, thread.human_detail(self.dir))
        nits = self.cfg.v("global.pipeline.nits_block")
        later = {s["_id"] for s in self.steps(sid, "finalize")}     # settled when Finalize runs again, after the loop
        open_ = [i for i in thread.open_blocking(self.dir, sid, nits) if i["by"] not in later]
        if not open_:
            nxt = self.first_sub(sid, "review")         # finalize, sign-off or finish
            if nxt:
                self.ev("sub", sub=nxt)
                return None
            return self.advance(self.state(), sid)
        rounds = self.cfg.v(f"stage.{sid}.rounds")
        ids = ", ".join(i["id"] for i in open_)
        if not self.fixers(sid):                 # nothing in this stage can fix it: a person
            return self.cannot_fix(sid, "changes requested" if st.final_next else "a check failed",
                                   "; ".join(f"{i['id']} {i['title']}" for i in open_))
        if st.final_next:
            self.start_round(sid, st.r + 1, "your instructions: " + ids, final=True)
            return None
        if st.r >= rounds or st.final:
            late = [i for i in open_ if i["round"] == n and not thread.answered(i)]
            if late and not st.final and not st.extra_used:       # raised in the last round: one fix round first
                self.start_round(sid, st.r + 1, "one extra round for issues found in the last round: "
                                 + ", ".join(i["id"] for i in late), extra=True)
                return None
            why = "raised after your instructions, so nobody has answered it" if st.final else \
                f"still open after {rounds} round{'s' if rounds != 1 else ''}"
            thread.escalate(self.dir, why, sid)
            thread.write_view(self.dir)
            return self.needs_you("disagreement", sid, thread.human_detail(self.dir))
        self.start_round(sid, st.r + 1, "round {}: ".format(st.r + 1) + ids)
        return None

    # ------------------------------------------------- phase: finalize --
    def sub_finalize(self, st):
        """Prepare what the person signs off (a PR, CI). A failure goes back into the loop as another round."""
        sid, n = st.stage, st.round
        if st.final_next:                        # a person's instructions after a failure here
            self.ingest(st, sid, n)
            self.start_round(sid, st.r + 1, "your instructions", final=True)
            return None
        action, failed = self.run_list(st, sid, "finalize", self.steps(sid, "finalize"), n)
        if action:
            return action
        self.ingest(st, sid, n)
        self.settle_passed(st, sid, "finalize", n)
        if failed is None:
            return self.advance(st, sid)
        summary = self._summary(n, failed)
        if not self.fixers(sid):
            return self.cannot_fix(sid, f"{failed['_id']} failed", summary)
        if st.r >= self.cfg.v(f"stage.{sid}.rounds") or st.final:
            thread.escalate(self.dir, f"{failed['_id']} still fails after the last round", sid)
            return self.needs_you("disagreement", sid, f"{failed['_id']} failed: {summary}")
        self.start_round(sid, st.r + 1, f"{failed['_id']} failed: {summary}")
        return None

    # ------------------------------------------------- phase: sign-off --
    def sub_signoff(self, st):
        """A person signs off on the stage. Asked only once the review list has passed; asked again after the
        final round that applied their feedback."""
        sid, n = st.stage, st.round
        steps = self.steps(sid, "signoff")
        action, _ = self.run_list(st, sid, "signoff", steps, n)
        if action:
            return action
        front, _ = runners.read_artifact(self.out(n, steps[0])) if steps else (None, None)
        if (front or {}).get("verdict") != "CHANGES":
            return self.advance(st, sid)
        self.ingest(st, sid, n)
        open_ = thread.open_blocking(self.dir, sid, True)
        if not self.fixers(sid):                 # nothing in this stage can fix it: a person picks a step to send it to
            return self.cannot_fix(sid, "changes requested", "; ".join(f"{i['id']} {i['title']}" for i in open_))
        self.start_round(sid, st.r + 1, "your instructions", final=True)
        return None

    # -------------------------------------------------- phase: finish --
    def sub_finish(self, st):
        sid, n = st.stage, st.round
        if st.final_next:                        # a person's feedback arrived while finalizing
            self.ingest(st, sid, n)
            self.start_round(sid, st.r + 1, "your instructions", final=True)
            return None
        for step in self.steps(sid, "finish"):
            if not self.is_selected(st, "finish", step, n):
                continue
            f = self.out(n, step)
            if not f.exists():
                action, _ = self.run_list(st, sid, "finish", [step], n)
                if action:
                    return action
            front, _ = runners.read_artifact(f)
            if (front or {}).get("verdict") in ("PASS", None):
                continue
            return self.finish_failed(st, sid, n, step)
        return self.advance(st, sid)

    def finish_failed(self, st, sid, n, step):
        policy = step.get("on_fail")
        summary = self._summary(n, step)
        key = f"{sid}-{step['_id']}-n{n}"
        if policy is None:                       # retry once, then a human
            if st.tries.get(key, 0) < 1:
                self.ev("try", key=key)
                self.out(n, step).unlink()
                return None
            policy = "human"
        if policy == "retry":
            if st.tries.get(key, 0) < 2:
                self.ev("try", key=key)
                self.out(n, step).unlink()
                return None
            policy = "human"
        if policy == "do":
            self.ingest(st, sid, n)
            rounds = self.cfg.v(f"stage.{sid}.rounds")
            if st.r >= rounds:
                return self.needs_you("disagreement", sid, f"{step['_id']} still fails after {rounds} round(s): {summary}")
            self.start_round(sid, st.r + 1, f"{step['_id']} failed: {summary}")
            return None
        return self.needs_you("finish", sid, f"{step['_id']} failed: {summary}")

    def finish(self, st):
        st = self.state()                       # the merge was recorded after `st` was read
        decision = {"decision": "DONE", "accept": bool(st.accepts)}
        body = ["# Decision", "", f"Workflow {self.wf['name']} finished."]
        if st.merged:
            body.append(f"Merged {self.meta['branch']} into {self.base}.")
        for o in st.accepts:
            body.append(f"- Accepted as is at {o.get('stage')}: {o.get('reason')} (by {o.get('by')})")
        runners.write_artifact(self.dir / "decision.md", decision, "\n".join(body))
        self.ev("stage", stage="done")
        self.link_session(closing=True)
        return {"action": "done", "task": self.task_id, "merged": st.merged,
                "decision": str(self.dir / "decision.md")}

    # ---------------------------------------------------------- step lists --
    def agent_name(self, name):
        return config_mod.agent_file(self.cfg.root, name, self.cfg.workflow_name)[0] or name

    def _summary(self, n, step):
        front, body = runners.read_artifact(self.out(n, step))
        lines = [l for l in (body or "").splitlines() if l.strip() and not l.startswith(("#", "`"))]
        return (lines[0].strip("-* ")[:300] if lines else f"{step['_id']} failed")

    def is_selected(self, st, sub, step, n):
        """Whether a step takes part in this round; decided once and recorded (so a dashboard can show why)."""
        key = (n, sub, step["_id"])
        if key in st.selected:
            return st.selected[key]["selected"]
        sid, r = st.stage, st.r
        sel, reason = True, ""
        awaiting = bool(thread.awaiting(self.dir, step["_id"], sid))
        if sub == "do" and r > 1:
            sel, reason = awaiting, "open thread" if awaiting else "nothing open for it"
        elif sub in ("verify", "review") and r > 1:
            ran = any((self.round_dir(m) / f"{step['_id']}.md").exists() for m in st.visit_rounds if m != n)
            again = step.get("always_rerun", workflow.kind_of(step) == "run")
            if workflow.kind_of(step) == "human":       # asked again after it asked for changes
                again = any((runners.read_artifact(self.round_dir(m) / f"{step['_id']}.md")[0] or {}).get("verdict") == "CHANGES"
                            for m in st.visit_rounds if m != n)
            sel = awaiting or again or not ran
            reason = "open thread" if awaiting else "re-run" if again else "never ran" if not ran else PASSED_LAST
        elif sub == "signoff" and r > 1:      # approved earlier in this visit (a rebase or a retry since): not asked again
            ok = [m for m in st.visit_rounds if m != n and
                  (runners.read_artifact(self.round_dir(m) / f"{step['_id']}.md")[0] or {}).get("verdict") == "PASS"]
            sel, reason = (False, f"approved in round {self.round_number(ok[-1])}") if ok else (True, "")
        if sel and step.get("if") is not None:
            sel = rules.evaluate(step["if"], self.rule_ctx(sid, st, n))
            reason = f"if: {step['if']}" if sel else f"its `if` was not met ({step['if']})"
        st.selected[key] = {"selected": sel, "reason": reason}
        self.ev("selection", round=n, sub=sub, step=step["_id"], selected=sel, reason=reason)
        return sel

    def failing(self, step, front):
        return str((front or {}).get("verdict", "")).upper() in ("FAIL", "CHANGES") \
            and not step.get("continue-on-error")

    def run_list(self, st, sid, sub, steps, n):
        """Run a list of steps in the order written. Returns (action | None, failed step | None). Consecutive agents
        are started together, except that a step reading an output or outcome of one of them (`if`, `env`, `with`)
        starts a new batch. In Do and Finalize a failing command ends the list (what follows depends on it); in
        Verify and Review every step runs, so one round reports everything."""
        batch = []
        for step in steps:
            if batch and workflow.reads(step) & {b["_id"] for b in batch}:
                return self.spawn_agents(st, sid, sub, batch, n), None
            if not self.is_selected(st, sub, step, n):
                self.publish(st, sid, sub, step, n)
                continue
            kind, f = workflow.kind_of(step), self.out(n, step)
            if kind == "agent":
                if not f.exists():
                    batch.append(step)
                else:
                    self.publish(st, sid, sub, step, n)
                continue
            if batch:
                return self.spawn_agents(st, sid, sub, batch, n), None
            if not f.exists():
                action = self.exec_step(st, sid, sub, step, n)
                if action:
                    return action, None
            self.publish(st, sid, sub, step, n)
            front, _ = runners.read_artifact(f)
            bad = (front or {}).get("verdict") in ("FAIL", "CHANGES")
            if bad and (sub == "finalize" or (sub == "do" and kind == "run")):
                return None, step
        if batch:
            return self.spawn_agents(st, sid, sub, batch, n), None
        return None, None

    # ------------------------------------------------------------- agents --
    def spawn_agents(self, st, sid, sub, steps, n):
        items = []
        for step in steps:
            k = st.answered.get((n, step["_id"]), 0)
            items.append((f"{sid}-{step['_id']}-n{n}" + (f"-q{k}" if k else ""), self.agent_name(workflow.name_of(step)),
                          self.role_of(step, sub), {"step": step["_id"], **self.agent_meta(step)},
                          self.agent_sections(st, sid, sub, step, n), self.out(n, step)))
        return self.spawn(items, sid, n)

    def agent_meta(self, step):
        name = workflow.name_of(step)
        meta = {}
        if self.cfg.agent(name).get("critical_thinking") is not None:
            meta["critical_thinking"] = self.cfg.critical_thinking(name)
        if step.get("continue-on-error"):
            meta["blocking"] = False
        return meta

    def agent_sections(self, st, sid, sub, step, n):
        cfg, me = self.cfg, step["_id"]
        name = workflow.name_of(step)
        spec_file = self.dir / f"{self.facts}.md" if self.facts else None
        spec = spec_file.read_text() if spec_file and spec_file.exists() else ""
        title = (self.facts or "spec").title()
        producing = sid == self.facts and sub == "do"
        s = []
        if producing:
            s.append(("Feature", self.meta["source"]))
            if spec and (st.r > 1 or len(st.visit_rounds) > 1 or self.send_back_note(sid)):
                s.append((f"Current {self.facts} (revise it)", spec))
            ans = self.dir / "answers.md"
            if ans.exists():
                s.append(("Answers to your questions", ans.read_text()))
        elif spec:
            s.append((title, spec))
        reqs = cfg.v(f"stage.{sid}.requirements")
        if reqs:
            s.append(("Extra requirements this repo sets for this stage",
                      "Meet these in addition to the standard ones:\n" + "\n".join(f"- {x}" for x in reqs)))
        note = self.send_back_note(sid) if st.r == 1 and sub == "do" else None
        if note:
            s.append(note)
        for p in self.steps(sid, "plan"):
            pf = self.out(st.visit_rounds[0] if st.visit_rounds else n, p)
            if pf.exists() and sub != "plan":
                s.append((f"Plan: {p['_id']}", pf.read_text()))
        if sub == "review" and sid != self.facts:
            s.append(("How to see the change", f"Run `git diff {self.base}...HEAD` in {self.wt}."))
            for a in self.steps(sid, "do"):
                f = self.out(n, a)
                if f.exists() and workflow.kind_of(a) == "agent":
                    notes = thread.trim(f.read_text(), ("Deviations",))
                    s.append((f"Notes from {a['_id']} (full file: {f})", notes or "(none)"))
        agent = cfg.agent(name)
        if agent["writes"] != "output":
            cmds = {k: v for k, v in cfg.v("global.command").items() if v and k != "timeout"}
            if cmds:
                s.append(("Repo commands", "Run them like this (it runs in your worktree, with no permission "
                                           "prompt; it takes no other arguments):\n" + "\n".join(
                    f"- {k} (`{v}`): `{runners.command_line(self.task_id, k)}`" for k, v in cmds.items())))
        if agent["writes"] == "paths":
            s.append(("Paths you may write", ", ".join(agent["paths"]) or "(none)"))
        mine = thread.brief(self.dir, me, sub != "do" or cfg.v("global.pipeline.nits_block"), sid)
        if mine:
            s.append(("Open threads addressed to you", mine))
        if st.final:
            s.append(("This is the final round", "A person gave instructions on the open issues (see the threads "
                                                  "above). Apply them; that closes them."))
        elif st.r >= 2:
            s.append(("This is a later round", f"Round {st.r} of {cfg.v(f'stage.{sid}.rounds')}. Answer the threads "
                      "addressed to you. Raise a new issue only if the fix itself broke something."))
        if step.get("with"):
            s.append(("Inputs", "\n".join(f"- {k}: {v}" for k, v in self.with_of(st, sid, step, n).items())))
        if step.get("outputs"):
            s.append(("Outputs you must write", "Put these fields in the frontmatter of your report, each as plain text "
                      "on one line (empty when there is nothing to report). Steps after yours read them: "
                      + ", ".join(step["outputs"]) + "."))
        if sub in ("verify", "review") and config_mod.agent_source(cfg.root, name, cfg.workflow_name)[0] == "repo":
            # a repo's own reviewer does not know the report format (the pack's and the plugin's agents do)
            s.append(("How to report", REPORT_FORMAT.format(output=self.out(n, step), to=self.first_do(sid) or "director",
                                                            me=me)))
        return s

    def send_back_note(self, sid):
        """Why the current visit of stage `sid` began, when a failure or a person sent the task back to it."""
        pending = current = None
        for e in self.store.events(self.task_id):
            if e["type"] == "send_back" and e["to"] == sid:
                pending = e
            elif e["type"] == "stage" and e["stage"] == sid:
                current, pending = pending, None
        if not current:
            return None
        if current.get("human"):
            return ("A person sent the task back to you with these instructions", current.get("detail", ""))
        return (f"Why {sid} was sent back (by {current['from']})",
                f"{current['reason']}\n{current.get('detail', '')}".strip())

    # ----------------------------------------------------- other step kinds --
    def exec_step(self, st, sid, sub, step, n):
        kind = workflow.kind_of(step)
        return getattr(self, "exec_" + kind)(st, sid, sub, step, n)

    def request(self, st, sid, sub, step, n):
        """The JSON a workflow script gets on stdin."""
        reviews = []
        for earlier in self.order[:self.order.index(sid) + 1]:
            for s in self.stages[earlier]["verify"] + self.stages[earlier]["review"]:
                for m in reversed([m for m, (x, _, _) in st.rounds.items() if x == earlier]):
                    front, _ = runners.read_artifact(self.out(m, s))
                    if front:
                        reviews.append({"id": s["_id"], "verdict": front.get("verdict"),
                                        "important": front.get("important", 0), "nit": front.get("nit", 0)})
                        break
        nits = self.cfg.v("global.pipeline.nits_block")
        total, _ = self.task_usage()
        return {"task": self.task_id, "stage": sid, "step": step["_id"], "round": n, "root": str(self.cfg.root),
                "worktree": str(self.wt), "branch": self.meta["branch"], "base": self.base,
                "title": self._short_title(), "pr": self.meta.get("pr"), "task_dir": str(self.dir),
                "round_dir": str(self.round_dir(n)), "with": self.with_of(st, sid, step, n),
                "settings": self.cfg.v(f"stage.{sid}"),
                "guardrail": self.cfg.v("global.guardrail"), "changed_paths": self.changed_paths(),
                "open_issues": [i["id"] for x in self.order for i in thread.open_blocking(self.dir, x, nits)],
                "skip_rules": st.skip_rules, "reviews": reviews, "tokens": total}

    def _short_title(self):
        from . import taskview                       # the spec's own short title, for PRs and merge commits
        return taskview.short_title(self.meta, self.dir, self.facts)

    def timeout(self, step):
        m = step.get("timeout-minutes")
        return m * 60 if m else self.cfg.v("global.command.timeout")

    def apply_reply(self, name, data):
        if data.get("meta"):
            self.meta = self.store.update_task(self.task_id, **data["meta"])
        if data.get("event"):
            self.ev(data["event"], branch=self.meta["branch"], **(data.get("meta") or {}))

    def exec_run(self, st, sid, sub, step, n):
        self.store.beat(self.task_id, busy_for=self.timeout(step))   # quiet until it ends, not stalled
        try:
            return self._exec_run(st, sid, sub, step, n)
        finally:
            self.store.beat(self.task_id)

    def _exec_run(self, st, sid, sub, step, n):
        name, out = workflow.name_of(step), self.out(n, step)
        to = self.first_do(sid) or "director"
        commands = self.cfg.v("global.command")
        if name in commands:
            cmd = commands[name]
            if not cmd:
                runners.no_command(name, out)
                return None
            env = self.step_env(st, sid, step, n)
            self.output_file(n, step).unlink(missing_ok=True)
            r = runners.run_command(name, cmd, self.cwd, out, to, self.timeout(step), env=env)
            if r.get("error"):                        # not runnable: retry once, then a human
                self.ev("retry", stage=sid, step=step["_id"], detail=r["error"])
                r = runners.run_command(name, cmd, self.cwd, out, to, self.timeout(step), env=env)
                if r.get("error"):
                    return self.needs_you("adapter", sid, r["error"])
            return None
        self.output_file(n, step).unlink(missing_ok=True)
        data = runners.run_script(workflow.script_path(self.cfg.workflow_name, name), self.request(st, sid, sub, step, n),
                                  self.cwd, out, to, self.timeout(step), env=self.step_env(st, sid, step, n))
        return self.script_result(sid, step, data)

    def script_result(self, sid, step, data):
        """What a workflow script's reply means for the task. None = it passed or failed (the report says which)."""
        v = data.get("verdict")
        if v in ("PASS", "FAIL"):
            self.apply_reply(step["_id"], data)
            return None
        if v == "WAIT":
            return self._wait(data.get("reason", "waiting"), data.get("hint", ""), waiter=data.get("waiter"),
                              detail=data.get("detail", ""))
        if v == "HUMAN":
            return self.needs_you(data.get("reason", "adapter"), sid, data.get("detail", ""))
        return self.needs_you("adapter", sid, data.get("error", "the script failed"))

    def exec_skill(self, st, sid, sub, step, n):
        self.store.beat(self.task_id, busy_for=runners.HEADLESS_TIMEOUT)
        try:
            return self._exec_skill(st, sid, sub, step, n)
        finally:
            self.store.beat(self.task_id)

    def _exec_skill(self, st, sid, sub, step, n):
        name, out = workflow.name_of(step), self.out(n, step)
        over = self.budget_check(sid)
        if over:
            return over
        agent = self.cfg.agent(name)
        inputs = "\n".join(f"- {k}: {v}" for k, v in self.with_of(st, sid, step, n).items())
        prev = self.previous_report(st, sid, step, n)
        pfront, pbody = runners.read_artifact(prev) if prev else (None, None)
        try:
            r = runners.skill_review(self.cfg, name, self.wt, self.base, out, effort=agent["effort"], recheck=agent["recheck"],
                                     outputs=step.get("outputs") or (), model=self.cfg.model(name), inputs=inputs, to=self.first_do(sid) or "director",
                                     spec=self.spec()[1] if self.facts else "",
                                     threads=thread.brief(self.dir, step["_id"], True, sid),
                                     previous=thread.trim(pbody or "", ("Comments",)) or (pbody or "")[:1500],
                                     since=(pfront or {}).get("head") if prev else None)
        except Exception as e:      # noqa: BLE001 - surfaced to the human
            return self.needs_you("adapter", sid, f"{name}: {e}")
        self.record_headless(f"skill-{name}-{sid}-{n}", sid, name, r["usage"])
        if r.get("convert"):
            return {"action": "convert", "task": self.task_id, "raw": r["convert"], "output": str(out),
                    "format": "reference/formats.md#reviews"}
        return None

    def previous_report(self, st, sid, step, n):
        """The latest earlier report of this step in this stage (an earlier round or visit), or None."""
        for m in sorted((m for m, (x, _, _) in st.rounds.items() if x == sid and m < n), reverse=True):
            f = self.out(m, step)
            if f.exists():
                return f
        return None

    def exec_human(self, st, sid, sub, step, n):
        role, out = workflow.name_of(step), self.out(n, step)
        via = (step.get("with") or {}).get("via")
        if via:
            data = runners.run_script(workflow.script_path(self.cfg.workflow_name, f"via-{via}"),
                                      self.request(st, sid, sub, step, n), self.cwd, out, self.first_do(sid) or "director")
            if data.get("verdict") != "MANUAL":
                return self.script_result(sid, step, data)
        g = st.approvals.get(sid) or {}
        if g.get("status") != "waiting" or g.get("step") != step["_id"] or g.get("n") != n:
            self.ev("approval", approval=sid, step=step["_id"], role=role, status="waiting", n=n)
        spec = self.dir / f"{self.facts}.md"
        look = f"Read {spec}" if sid == self.facts else f"Review the branch {self.meta['branch']}"
        return self._wait(f"{sid} sign-off: waiting for {role}", f"{look}, then /epi:approve {self.task_id} {sid}")

    def exec_wait(self, st, sid, sub, step, n):
        return self._wait(f"waiting for {workflow.name_of(step)}", "wait steps are not supported yet")

    def exec_agent(self, *a):               # agents are started in batches by run_list
        raise AssertionError("agents are spawned by run_list")

    def record_headless(self, key, stage, name, use):
        self.ev("agent", key=key, agent=f"headless:{name}", stage=stage, data=use)
        self.store.record_usage(self.task_id, key, use.get("tokens", 0))

    # ------------------------------------------------------- conversation --
    def sources(self, sid, n):
        """The reports of round n in the order the director reads them."""
        out = []
        for sub, kind in (("do", "do"), ("verify", "check"), ("review", "check"), ("finalize", "check"),
                          ("signoff", "check"), ("finish", "check")):
            for step in self.steps(sid, sub):
                k = workflow.kind_of(step)
                out.append({"path": self.out(n, step), "author": step["_id"],
                            "kind": "do" if k == "agent" and sub == "do" else kind,
                            "engine": k in ("run", "skill") and sub != "do"})
        return out

    def ingest(self, st, sid, n):
        ids = {s["_id"] for _, s in workflow.stage_steps(self.stages[sid])}
        known = ids | {"director"}
        advisory = {s["_id"] for sub in ("verify", "review") for s in self.steps(sid, sub) if s.get("continue-on-error")}
        r = self.state().rounds.get(n, (sid, st.r, False))[1]
        errors = []
        thread.ingest(self.dir, self.sources(sid, n), sid, n, known, default_to=self.first_do(sid), r=r,
                      exchange=r >= 2, advisory=advisory, errors=errors)
        for e in errors:     # a comment line that did not parse is reported, never dropped
            self.ev("error", stage=sid, source=e["src"], detail=f"a comment in {e['src']} was not understood: {e['line']}")
        thread.write_view(self.dir)


def _questions(body):
    import re
    return re.findall(r"^\s*(?:\d+\.|-)\s+(.*)$", thread.section(body, "Questions"), flags=re.M)


# ----------------------------------------------------------- human actions --

def waiting_approval(st, approval=None):
    """(stage, approval record) of the approval a person is asked to open, matching `approval` (a stage, a role or a step id)."""
    for sid, g in st.approvals.items():
        if g.get("status") == "waiting" and approval in (None, sid, g.get("role"), g.get("step")):
            return sid, g
    return None, None


def approve(cfg, task_id, approval, value=None, by=None):
    p = Machine(cfg, task_id)
    st = p.state()
    nh = st.needs_you
    by = by or __import__("os").environ.get("USER", "human")
    if approval == "budget":
        tokens = _parse_tokens(value or "+500k")
        p.ev("budget_extra", tokens=tokens, by=by)
        if nh and nh["reason"] == "budget":
            p.ev("resume", by=by)
        return f"budget raised by {tokens:,} tokens"
    sid, g = waiting_approval(st, approval)
    if g:
        members = cfg.v("global.human").get(g["role"]) or []
        if members and by not in members:
            raise ValueError(f"{by} is not one of the {g['role']}s ({', '.join(members)})")
        runners.write_artifact(p.round_dir(g["n"]) / f"{g['step']}.md", {"verdict": "PASS", "by": by},
                               f"# Approved by {by}")
        p.ev("approval", approval=sid, step=g["step"], role=g["role"], status="approved", n=g["n"], by=by)
        return f"{sid} signed off by {by}"
    if not nh:
        raise ValueError(f"task {task_id} isn't waiting for '{approval}'")
    reason = nh["reason"]
    if approval == "retry" and reason == "failure":              # run the stage's loop again
        thread.close_stage(p.dir, st.stage, "closed, you asked to try again")   # raised again if it still fails
        p.ev("resume", by=by)
        p.start_round(st.stage, st.r + 1, "tried again by you", sub=p.first_sub(st.stage) or "verify")
        return f"task {task_id} tries again; run /epi:start {task_id} to continue"
    if reason in ("disagreement", "rules", "failure"):
        thread.human_decision(p.dir, "accept", value or "", by)
        p.ev("accept", stage=nh.get("stage"), reason=f"{reason}: {nh.get('detail', '')}", by=by, skip_rules=reason == "rules")
        p.ev("resume", by=by)
        if st.sub in ("do", "finalize") and st.stage in p.stages and any(
                p.failing(s, runners.read_artifact(p.out(st.round, s))[0]) for s in p.steps(st.stage, st.sub)):
            p.advance(p.state(), st.stage)                  # a failed step accepted as it is: go on past it
        return f"accepted as it is; run /epi:start {task_id} to continue"
    p.ev("resume", by=by)
    return f"{reason} approved; run /epi:start {task_id} to continue"


def retry(cfg, task_id):
    p = Machine(cfg, task_id)
    if p.state().failed:
        p.ev("retry_task")
        return True
    return False


def cancel(cfg, task_id):
    p = Machine(cfg, task_id)
    p.ev("cancelled")
    if p.wf["workspace"] == "git-worktree":
        git("worktree", "remove", "--force", str(p.wt), cwd=cfg.root, check=False)
    p.link_session(closing=True)


def send_back_targets(p, st):
    """The steps a person may send the task back to: the Do agents of the current stage and of every stage before
    it, nearest first, as (stage, step id)."""
    last = p.order.index(st.stage) if st.stage in p.order else len(p.order) - 1
    return [(sid, s["_id"]) for sid in reversed(p.order[:last + 1]) for s in p.steps(sid, "do")
            if workflow.kind_of(s) == "agent"]


def send_back(cfg, task_id, feedback, to=None):
    """A person's instructions for an earlier step (`to`: `step` or `stage/step`; default the nearest Do agent).
    In the current stage they start a final round: at a sign-off as its CHANGES, at a disagreement as the answer
    to the issues waiting on the person, otherwise as a new issue for that step. In an earlier stage, that stage
    runs again with the instructions, then the stages after it."""
    p = Machine(cfg, task_id)
    st = p.state()
    text = " ".join(str(feedback).split())
    targets = send_back_targets(p, st)
    pick = [t for t in targets if to in (None, t[1], f"{t[0]}/{t[1]}")]
    if not pick:
        raise ValueError(f"cannot send it back to '{to}'; choose one of: " + ", ".join(f"{a}/{b}" for a, b in targets)
                         if targets else "there is no earlier step that can take instructions")
    tsid, tstep = pick[0]
    p.ev("learning_candidate", source="send_back", text=text)
    sid, g = waiting_approval(st)
    if tsid == st.stage:
        if g:
            runners.write_artifact(p.round_dir(g["n"]) / f"{g['step']}.md", {"verdict": "CHANGES", "important": 1, "nit": 0},
                                   f"# Changes requested by {g['role']}\n\n## Comments\n- new -> {tstep}, feedback, blocking: {text}")
            p.ev("approval", approval=sid, step=g["step"], role=g["role"], status="changes", n=g["n"])
        elif thread.awaiting_you(p.dir, st.stage):
            thread.human_decision(p.dir, "send_back", text, to=tstep)
        else:
            thread.add_instruction(p.dir, st.stage, tstep, text, st.round)
        if st.needs_you:
            p.ev("resume", by="send_back")
        p.ev("final")
        return f"task {task_id}: a final round starts with your instructions for {tstep}"
    if g:
        p.ev("approval", approval=sid, step=g["step"], role=g["role"], status="changes", n=g["n"])
    thread.close_stage(p.dir, st.stage, f"closed, you sent the task back to {tstep}")
    if st.needs_you:
        p.ev("resume", by="send_back")
    p.ev("send_back", **{"from": st.stage, "to": tsid, "step": tstep, "reason": "sent back by you", "detail": text,
                         "human": True})
    p.enter_stage(tsid, reason=f"sent back by you to {tstep}")
    return f"task {task_id}: {tsid} runs again with your instructions for {tstep}, then the stages after it"


def report_answers(cfg, task_id, answers_text):
    p = Machine(cfg, task_id)
    st = p.state()
    pending = [(k, st.asked[k]) for k in st.asked if st.asked[k] > st.answered.get(k, 0)]
    if not pending:
        raise ValueError("nothing is waiting for answers")
    (n, step), k = pending[-1]
    path = p.dir / "answers.md"
    prev = path.read_text() if path.exists() else ""
    path.write_text(prev + f"\n## Answers for round {n}\n\n" + answers_text.strip() + "\n")
    report = p.round_dir(n) / f"{step}.md"
    if report.exists():
        report.rename(p.round_dir(n) / f"{step}.q{k}.md")
    sid = st.stage
    role = p.approver()
    thread.answer_questions(p.dir, sid, answers_text.strip(), to=role)
    p.ev("question", rev=k, answers=answers_text.strip()[:2000])
    p.ev("answers", round=n, step=step, k=k)
    return "answers recorded"


def _parse_tokens(v):
    v = str(v).strip().lstrip("+").lower().replace(",", "")
    mult = 1_000_000 if v.endswith("m") else 1_000 if v.endswith("k") else 1
    return int(float(v.rstrip("mk")) * mult)
