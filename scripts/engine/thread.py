"""The conversation between a task's agents, kept by the director.

Agents write comments in their own report files, under `## Comments`, one per line:

    - new -> builder, feedback, blocking: Batch processors are never flushed
    - I1 -> code-review, pushback: AC-9 asks for a slug-free reason
    - I1 -> builder, accept

`<id|new> -> <to>, <type>[, <severity>]: <text>` (the leading `- ` is optional; a comment without a
severity is a nit; a new issue may leave out `feedback`; `pushback (blocking)` reads like `pushback, blocking`). The sender is the file's author. The director (this
module, plain code) is the only writer of `threads.jsonl`: it numbers new issues (I1, I2 ...), checks each
`to`, applies the rules below and leaves a decision line. `conversation.md` is the readable copy.

Rules: the raiser's `accept` closes an issue; a pushback on a nit closes it ("not pursued"); a checker
that re-reviews and says nothing about an issue addressed to it closes it as "no reply". Issues belong to a round (a full review pass); the
machine gives a stage `stage.<id>.rounds` rounds to resolve them, then escalates what is left. Names are
step ids: whoever wrote the report is `<id>`, and `to` may be any step of the stage. What two sides cannot
settle goes to a person, who answers with `accept` or `send_back` (a final round that closes the issue).
"""

import json
import re
from pathlib import Path

from . import runners

TYPES = ("feedback", "pushback", "fixed", "accept")
COMMENT = re.compile(r"^\s*(?:[-*]\s+)?\**(new|I\d+)\**\s*(?:->|→)\s*([\w-]+)\s*,\s*(?:(" + "|".join(TYPES) +
                     r")\b)?\s*(?:[,(]?\s*(blocking|nit)\s*\)?)?\s*(?::\s*(.*))?$", re.I)

# a line that is meant to be a comment (an id, then an arrow) but does not parse
LOOSE = re.compile(r"^\s*(?:[-*]\s+)?\**(?:new|I\d+)\**\s*(?:->|→|=>|-->|:)", re.I)


def section(md, title):
    m = re.search(rf"^##\s+{re.escape(title)}\s*$(.*?)(?=^##\s|\Z)", md or "", flags=re.M | re.S)
    return m.group(1).strip() if m else ""


def trim(md, titles):
    """Only the named sections of a report, each under its heading."""
    return "\n\n".join(f"## {t}\n{s}" for t in titles if (s := section(md, t)))


def parse_comments(body, errors=None):
    """The comments in a report's `## Comments`: [{id, to, type, severity, text, detail}]. A line that looks
    like a comment but does not parse is appended to `errors`, never dropped silently."""
    out, cur = [], None
    for line in section(body, "Comments").splitlines():
        m = COMMENT.match(line)
        if m and not m.group(3) and not (m.group(4) and m.group(1).lower() == "new"):
            m = None                                   # a type may be left out only on a new issue that has a severity
        if not m and errors is not None and LOOSE.match(line):
            errors.append(line.strip()[:200])
        if m:
            cur = {"id": m.group(1) if m.group(1).lower() == "new" else m.group(1).upper(),
                   "to": m.group(2).lower(), "type": (m.group(3) or "feedback").lower(),
                   "severity": (m.group(4) or "").lower(), "text": (m.group(5) or "").strip(), "detail": ""}
            out.append(cur)
        elif cur and line.startswith((" ", "\t")) and line.strip():
            cur["detail"] = (cur["detail"] + " " + line.strip()).strip()[:600]
    return out


# ------------------------------------------------------------------ store --

def _path(task_dir):
    return task_dir / "threads.jsonl"


def _records(task_dir):
    p = _path(task_dir)
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def _append(task_dir, *recs):
    with open(_path(task_dir), "a") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def load(task_dir):
    """Replay the log: {id: issue}, each issue with its ordered `lines` (comments and decisions)."""
    issues, done = {}, set()
    for r in _records(task_dir):
        k = r["kind"]
        if k == "src":
            done.add(r["src"])
        elif k == "issue":
            issues[r["id"]] = {"id": r["id"], "title": r["title"], "severity": r["severity"], "by": r["by"],
                               "stage": r.get("stage", ""), "round": r.get("round"), "r": r.get("r", 1),
                               "question": bool(r.get("question")), "state": "open", "flag": "", "lines": []}
        elif r.get("id") in issues:
            i = issues[r["id"]]
            if k == "comment":
                i["lines"].append({k2: r.get(k2, "") for k2 in ("from", "to", "type", "text", "detail")})
            elif k == "severity":
                i["severity"] = r["severity"]
                i["lines"].append({"from": "director", "to": "", "type": "decision",
                                   "text": f"severity changed to {r['severity']} by {r['by']}", "detail": ""})
            elif k == "decision":
                i["state"], i["flag"] = r["state"], r.get("flag", "")
                if not r.get("quiet"):
                    i["lines"].append({"from": "director", "to": "", "type": "decision", "text": r["text"], "detail": ""})
    return issues, done


def _last_comment(issue):
    return next((x for x in reversed(issue["lines"]) if x["type"] != "decision"), None)


def _awaiting(issue):
    c = _last_comment(issue)
    return c["to"] if c else None


# --------------------------------------------------------------- director --

class _Log:
    """Appends to the log while keeping an in-memory copy in step, so rules see their own writes."""

    def __init__(self, task_dir):
        self.dir = task_dir
        self.issues, self.done = load(task_dir)

    def put(self, *recs):
        _append(self.dir, *recs)
        self.issues, self.done = load(self.dir)

    def decide(self, i, state, text, flag="", help_=False):
        recs = [{"kind": "decision", "id": i, "state": state, "text": text, "flag": flag, "quiet": help_}]
        if help_:
            recs.insert(0, {"kind": "comment", "id": i, "from": "director", "to": "human", "type": "help",
                            "text": text, "detail": ""})
        self.put(*recs)


def _clip(s, n=240):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def _headline(s, n=140):
    """A finding's title for a person: its first sentence, or as many whole words as fit (never cut mid-word)."""
    s = re.sub(r"\s+", " ", s or "").strip()
    m = re.match(r"(.{20,%d}?[.!?])(\s|$)" % n, s)
    if m:
        return m.group(1)
    if len(s) <= n:
        return s
    return s[:n].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def ingest(task_dir, sources, stage, rnd, known_to, default_to=None, r=1, exchange=False, advisory=(), errors=None):
    """Read the reports not yet seen, in the order given, and apply the rules. `sources` are
    `{path, author, kind}` with kind `do` (answers threads) or `check` (raises them; `engine: true` = a
    command or skill that cannot answer a thread, so in an `exchange` its verdict is the answer). `known_to` is the set of names a comment may be addressed to, `default_to`
    the step that gets an issue nobody addressed; an `advisory` checker's issues are always nits, and so are
    those of a checker whose verdict is PASS. Comment lines that do not parse are added to `errors`
    as `{src, line}`."""
    log = _Log(task_dir)
    for source in sources:
        f, frm, kind = Path(source["path"]), source["author"], source["kind"]
        src = f"{f.parent.parent.name}/{f.parent.name}/{f.name}"
        if not f.exists() or src in log.done:
            continue
        front, body = runners.read_artifact(f)
        bad = []
        comments = parse_comments(body or "", bad)
        if errors is not None:
            errors += [{"src": src, "line": line} for line in bad]
        checker = kind == "check"
        passing = str((front or {}).get("verdict", "")).upper() == "PASS"
        failing = str((front or {}).get("verdict", "")).upper() in ("FAIL", "CHANGES")
        if checker and failing and not comments:
            comments = [{"id": "new", "to": default_to or "director", "type": "feedback", "severity": "blocking",
                         "text": f"{frm} says {(front or {}).get('verdict')} and gave no comments (see {src})",
                         "detail": ""}]
        if checker and source.get("engine") and exchange:
            ok = str((front or {}).get("verdict", "")).upper() == "PASS"
            mine = [i for i in log.issues.values() if i["state"] == "open" and _awaiting(i) == frm]
            if (front or {}).get("engine") in ("run", "script"):
                # a command re-runs fresh and cannot answer a thread: its verdict is the answer
                answers = [{"id": i["id"], "to": default_to or "director", "type": "accept" if ok else "pushback",
                            "severity": "", "text": "" if ok else f"still reported when run again (see {src})",
                            "detail": ""} for i in mine]
                comments = answers or comments        # failing with no thread open for it: a new issue
            else:
                # a skill looks only at what changed and keeps its new findings: a thread it did not
                # answer or raise again is settled, not disputed
                replied = {c["id"] for c in comments}
                comments += [{"id": i["id"], "to": default_to or "director", "type": "accept", "severity": "",
                              "text": "", "detail": ""} for i in mine if i["id"] not in replied]
        touched = set()
        for c in comments:
            if (frm in advisory or (checker and passing)) and c["id"] == "new":
                c = {**c, "severity": "nit"}
            _apply(log, c, frm, kind, stage, rnd, known_to, default_to, touched, r)
        if checker:
            for i in list(log.issues.values()):
                if i["state"] == "open" and i["id"] not in touched and _awaiting(i) == frm:
                    log.decide(i["id"], "closed", f"closed, no reply from {frm}", flag="no_reply")
        log.put({"kind": "src", "src": src})
    return log.issues


def _apply(log, c, frm, kind, stage, rnd, known_to, default_to, touched, r):
    if c["id"] == "new":
        if c["type"] != "feedback" or kind != "check":
            return
        n = len(log.issues) + 1
        iid = f"I{n}"
        sev = c["severity"] if c["severity"] in ("blocking", "nit") else "nit"
        to = c["to"] if c["to"] in known_to else (default_to or "director")
        title = re.sub(r"^`[^`]+`\s*", "", c["text"]) or c["text"]       # the summary, without the leading `path:line`
        log.put({"kind": "issue", "id": iid, "title": _headline(title), "severity": sev, "by": frm, "stage": stage, "round": rnd, "r": r},
                {"kind": "comment", "id": iid, "from": frm, "to": to, "type": "feedback", "text": _clip(c["text"], 400),
                 "detail": c["detail"]})
        touched.add(iid)
        return
    i = log.issues.get(c["id"])
    if not i or i["state"] in ("closed", "needs_you"):
        return
    touched.add(i["id"])
    to = c["to"] if c["to"] in known_to else (i["by"] if kind == "do" else (default_to or i["by"]))
    log.put({"kind": "comment", "id": i["id"], "from": frm, "to": to, "type": c["type"],
             "text": _clip(c["text"], 400), "detail": c["detail"]})
    if c["severity"] and c["severity"] != i["severity"] and frm == i["by"]:
        log.put({"kind": "severity", "id": i["id"], "severity": c["severity"], "by": frm})
    i = log.issues[i["id"]]
    if c["type"] == "accept" and frm == i["by"]:
        log.decide(i["id"], "closed", f"closed, accepted by {frm}")
    elif c["type"] == "pushback":
        if i["severity"] == "nit":
            log.decide(i["id"], "closed", "closed, nit not pursued", flag="nit")


def answered(issue):
    """Whether anyone but the raiser has said something on the issue."""
    return any(x["type"] != "decision" and x["from"] != issue["by"] for x in issue["lines"])


def escalate(task_dir, why, stage=None, tag=True):
    """Every unresolved blocking issue (of the stage) goes to the person. When rounds ran out (`tag`), an issue
    nobody answered is flagged `late` (found in the last round, never answered), the others `disputed`."""
    log = _Log(task_dir)
    for i in list(log.issues.values()):
        if i["state"] == "open" and i["severity"] == "blocking" and stage in (None, i["stage"]):
            flag = ("disputed" if answered(i) else "late") if tag else "failed"
            log.decide(i["id"], "needs_you", why, flag=flag, help_=True)


def human_decision(task_dir, kind, text="", by="human", to="director"):   # `by` is who, the comment is always from "human"
    """The human's answer to every issue waiting on them: `accept` (accept the work as it is) or
    `send_back` (the checker is right: send the issue back to the do step `to` with these instructions)."""
    log = _Log(task_dir)
    for i in list(log.issues.values()):
        if i["state"] != "needs_you":
            continue
        log.put({"kind": "comment", "id": i["id"], "from": "human", "to": "director", "type": kind,
                 "text": _clip(text or kind, 400), "detail": ""})
        if kind == "accept":
            log.decide(i["id"], "closed", "closed, you accepted it as it is")
        else:
            log.decide(i["id"], "open", "sent back by you", flag="sent_back")
            log.put({"kind": "comment", "id": i["id"], "from": "director", "to": to, "type": "feedback",
                     "text": _clip(text, 400), "detail": ""})
    write_view(task_dir)


# ------------------------------------------------------------------- views --

def awaiting_you(task_dir, stage=None):
    return [i for i in load(task_dir)[0].values() if i["state"] == "needs_you" and stage in (None, i["stage"])]


def awaiting(task_dir, who, stage=None):
    return [i for i in load(task_dir)[0].values()
            if i["state"] == "open" and _awaiting(i) == who and stage in (None, i["stage"])]


def unresolved(task_dir, by, nits=False):
    """Issues a checker raised that still hold the task back: blocking ones (and nits, if they block)
    that are open or need you."""
    return [i for i in load(task_dir)[0].values()
            if i["by"] == by and (nits or i["severity"] == "blocking") and i["state"] in ("open", "needs_you")]


def raised_by(task_dir, by):
    return [i for i in load(task_dir)[0].values() if i["by"] == by]


def open_blocking(task_dir, stage, nits=False):
    """The issues of a stage still being worked on (blocking ones, and nits if they block)."""
    return [i for i in load(task_dir)[0].values()
            if i["stage"] == stage and i["state"] == "open" and (nits or i["severity"] == "blocking")]


def close_stage(task_dir, stage, text):
    """A stage starts over: whatever was left open in an earlier visit is closed."""
    log = _Log(task_dir)
    for i in list(log.issues.values()):
        if i["stage"] == stage and i["state"] in ("open", "needs_you"):
            log.decide(i["id"], "closed", text)


def close_by(task_dir, authors, text):
    """Close the open issues raised by these authors (a person's feedback, once the final round applied it)."""
    log = _Log(task_dir)
    for i in list(log.issues.values()):
        if i["state"] == "open" and i["by"] in authors:
            log.decide(i["id"], "closed", text)
    write_view(task_dir)


def close_notes(task_dir, stage, text="closed, the stage passed"):
    """The stage passed: its open nits (notes) are closed. A question for a person stays until answered."""
    log = _Log(task_dir)
    for i in list(log.issues.values()):
        if i["stage"] == stage and i["state"] == "open" and i["severity"] == "nit" and not i["question"]:
            log.decide(i["id"], "closed", text, flag="note")
    write_view(task_dir)


def close_raised(task_dir, by, stage, text):
    """Close the open issues a command raised in a stage: it passed when run again, which settles them."""
    log = _Log(task_dir)
    for i in list(log.issues.values()):
        if i["state"] == "open" and i["stage"] == stage and i["by"] == by:
            log.decide(i["id"], "closed", text)


def close_awaiting(task_dir, who, stage, text):
    """Close the open issues addressed to a step that cannot reply (a command that ran again)."""
    log = _Log(task_dir)
    for i in list(log.issues.values()):
        if i["state"] == "open" and i["stage"] == stage and _awaiting(i) == who:
            log.decide(i["id"], "closed", text)


def add_question(task_dir, stage, by, to, text, rnd):
    """An agent's open question for a person: an issue that never blocks."""
    log = _Log(task_dir)
    iid = f"I{len(log.issues) + 1}"
    log.put({"kind": "issue", "id": iid, "title": _headline(text), "severity": "nit", "by": by, "stage": stage,
             "round": rnd, "r": 1, "question": True},
            {"kind": "comment", "id": iid, "from": by, "to": to, "type": "feedback", "text": _clip(text, 400),
             "detail": ""})
    return iid


def add_instruction(task_dir, stage, to, text, rnd):
    """A person's instructions for a step (sent back with no issue waiting on them): a blocking issue from
    "human", flagged `sent_back`, so the final round that applies it closes it."""
    log = _Log(task_dir)
    iid = f"I{len(log.issues) + 1}"
    log.put({"kind": "issue", "id": iid, "title": _headline(text), "severity": "blocking", "by": "human",
             "stage": stage, "round": rnd, "r": 1},
            {"kind": "comment", "id": iid, "from": "human", "to": to, "type": "feedback", "text": _clip(text, 400),
             "detail": ""},
            {"kind": "decision", "id": iid, "state": "open", "text": "sent back by you", "flag": "sent_back", "quiet": True})
    write_view(task_dir)
    return iid


def answer_questions(task_dir, stage, answers, to="human"):
    """The person's answers close the questions of the stage."""
    log = _Log(task_dir)
    for i in list(log.issues.values()):
        if i["stage"] == stage and i["state"] == "open" and i["severity"] == "nit" and _awaiting(i) == to:
            log.put({"kind": "comment", "id": i["id"], "from": to, "to": i["by"], "type": "accept",
                     "text": _clip(answers, 400), "detail": ""})
            log.decide(i["id"], "closed", "closed, answered")
    write_view(task_dir)


def close_sent_back(task_dir):
    """The person's instructions were the final round: close every issue they sent back."""
    log = _Log(task_dir)
    for i in list(log.issues.values()):
        if i["state"] == "open" and i["flag"] == "sent_back":
            log.decide(i["id"], "closed", "closed, your instructions were applied")
    write_view(task_dir)


def human_detail(task_dir):
    out = []
    for i in awaiting_you(task_dir):
        d = next((x["text"] for x in reversed(i["lines"]) if x["type"] in ("help", "decision")), "")
        out.append(f"{i['id']} {i['title']} ({d})")
    return " | ".join(out)[:900]


def brief(task_dir, who, nits=False, stage=None):
    """The open threads addressed to one agent, as a compact text: the whole exchange per issue."""
    out = []
    for i in awaiting(task_dir, who, stage):
        if i["severity"] == "nit" and not nits:
            continue
        out.append(f"{i['id']} [{i['severity']}] {i['title']}")
        for x in i["lines"]:
            if x["type"] == "decision":
                out.append(f"  director: {x['text']}")
            else:
                out.append(f"  {x['from']} -> {x['to']} ({x['type']}): {x['text']}"
                           + (f" ({x['detail']})" if x["detail"] else ""))
    return "\n".join(out)


def threads(task_dir):
    """Every issue with its exchange, for the dashboard: unresolved first."""
    order = {"needs_you": 0, "open": 1, "noted": 2, "closed": 3}
    out = []
    for i in load(task_dir)[0].values():
        view = dict(i)
        if i["state"] == "open" and i["severity"] == "nit":
            view["state"] = "noted"
        out.append(view)
    return sorted(out, key=lambda i: (i["r"], order[i["state"]], i["severity"] != "blocking", int(i["id"][1:])))


def render(task_dir):
    """The readable copy: issues grouped by round, one block per issue."""
    out, group = [], None
    for i in threads(task_dir):
        if i["r"] != group:
            group = i["r"]
            out += [f"r{group}", ""]
        out.append(f"  [{'x' if i['state'] == 'closed' else ' '}] {i['id']} - {i['title']} - {i['severity']}")
        for x in i["lines"]:
            if x["type"] == "decision":
                out.append(f"      - director: {x['text']}")
            else:
                out.append(f"      - {x['from']} - {x['to']} - {x['type']}: {x['text']}")
        out.append("")
    return "\n".join(out).rstrip() + "\n" if out else ""


def write_view(task_dir):
    text = render(task_dir)
    if text:
        (task_dir / "conversation.md").write_text(text)
