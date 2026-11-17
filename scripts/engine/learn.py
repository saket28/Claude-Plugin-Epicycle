"""Retro and learnings.

collect -> phrase (one cheap headless call, skipped when there's nothing new)
-> straight into the workflow's learnings.md (Learnings / Answers), the one file the engine writes on its own. Left
uncommitted for the human.
IDs are a hash of the text at creation and never change afterwards.
"""

import hashlib
import json
import re
import subprocess
from pathlib import Path

from . import briefs, runners, schema
from .config import git
from .store import Store, locked, write_atomic

SECTIONS = ("Learnings", "Answers")


def entry_id(kind, text):
    return f"{'A' if kind == 'answer' else 'L'}-{hashlib.sha1(text.strip().lower().encode()).hexdigest()[:4]}"


def _norm(t):
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()


def collect(cfg, task_id):
    st = Store(cfg)
    d = st.task_dir(task_id)
    cands = []
    for f in sorted(d.rglob("*.md")):
        if f.parent.name == "briefs":
            continue
        m = re.search(r"^##\s+Learning candidates\s*$(.*?)(?=^##\s|\Z)", f.read_text(), flags=re.M | re.S)
        if m:
            for line in m.group(1).splitlines():
                line = line.strip().lstrip("-*0123456789. ").strip()
                if line:
                    cands.append({"source": str(f.relative_to(d)), "text": line})
    for e in st.events(task_id):
        if e["type"] == "learning_candidate":
            cands.append({"source": e.get("source", "event"), "text": e["text"]})
        elif e["type"] == "question":
            cands.append({"source": "answered question", "text": e.get("answers", ""), "answer": True})
        elif e["type"] in ("failed", "guardrail_block") or (e["type"] == "budget" and e.get("level") == "exceeded"):
            cands.append({"source": e["type"], "text": json.dumps({k: v for k, v in e.items() if k != "ts"})})
        elif e["type"] == "round" and e.get("auto"):
            cands.append({"source": "send_back round", "text": e.get("reason", "")})
    return cands


def existing_texts(cfg):
    body = cfg.learning_sections()
    out = set()
    for sec in SECTIONS:
        for e in briefs.parse_entries(body.get(sec, "")):
            out.add(e["id"])
            out.add(_norm(e["text"]))
    return out


def phrase(cfg, task_id, cands):
    roles = "|".join(f'\"{r}\"' for r in (*schema.roles(cfg.workflow_name), "all"))
    prompt = (
        "You turn raw notes from one task into durable guidance for future AI agents.\n"
        "Return only JSON: {\"entries\": [{\"kind\": \"learning\"|\"answer\", "
        f"\"roles\": [{roles}], \"text\": \"...\", "
        "\"evidence\": \"short plain-text evidence\"}]}\n"
        "Rules: keep only things that will help a FUTURE task (skip one-offs). Write each as an "
        "instruction an agent can follow. 'answer' = a question a human answered, phrased as Q? A. "
        "Return {\"entries\": []} if nothing qualifies.\n\n"
        f"Task {task_id} notes:\n" + json.dumps(cands, indent=1)[:20000])
    text, use = runners.headless(prompt, cfg.root, model=cfg.model("judge"))
    Store(cfg).record_usage(task_id, f"retro-{task_id}", use.get("tokens", 0))
    Store(cfg).event(task_id, "agent", key=f"retro-{task_id}", agent="headless:retro", stage="retro", data=use)
    m = re.search(r"\{.*\}", text, flags=re.S)
    return (json.loads(m.group(0)) if m else {}).get("entries", [])


def add_entries(cfg, entries, task_id):
    """Write new entries straight into learnings.md. Returns the lines added."""
    seen = existing_texts(cfg)
    moves = {"Learnings": [], "Answers": []}
    added = []
    for e in entries:
        text = (e.get("text") or "").strip()
        if not text or _norm(text) in seen:
            continue
        kind = "answer" if e.get("kind") == "answer" else "learning"
        eid = entry_id(kind, text)
        if eid in seen:
            continue
        roles = ", ".join(e.get("roles") or ["all"])
        ev = e.get("evidence") or ""
        line = f"- [{eid}] {roles} · {text} (task-{task_id}{', ' + ev if ev else ''})"
        moves["Answers" if kind == "answer" else "Learnings"].append(line)
        seen.update({eid, _norm(text)})
        added.append(line)
    if added:
        path = cfg.learnings_path
        with locked(path):
            path.write_text(_insert_entries(path.read_text() if path.exists() else "", moves))
    return added


def retro(cfg, task_id):
    st = Store(cfg)
    if any(e["type"] == "retro" for e in st.events(task_id)):
        return {"skipped": "retro already ran"}
    if cfg.v("global.learning.mode") == "off":
        st.event(task_id, "retro", added=0)
        return {"added": []}
    cands = collect(cfg, task_id)
    added = []
    if cands:
        try:
            entries = phrase(cfg, task_id, cands)
        except Exception as e:  # noqa: BLE001
            st.event(task_id, "error", stage="retro", detail=str(e))
            return {"error": str(e)}
        added = add_entries(cfg, entries, task_id)
    (st.task_dir(task_id) / "retro.md").write_text(
        "---\nadded: %d\n---\n\n# Retro\n\n%s\n" % (len(added), "\n".join(added) or "Nothing new to learn."))
    st.event(task_id, "retro", added=len(added))
    return {"added": added}


def _insert_entries(text, moves):
    for sec, lines in moves.items():
        if not lines:
            continue
        m = re.search(rf"^## {sec}\s*$", text, flags=re.M)
        block = "\n".join(lines)
        if m:
            nxt = re.search(r"^## ", text[m.end():], flags=re.M)
            at = m.end() + (nxt.start() if nxt else len(text) - m.end())
            text = text[:at].rstrip("\n") + "\n" + block + "\n\n" + text[at:].lstrip("\n")
        else:
            text = text.rstrip("\n") + f"\n\n## {sec}\n{block}\n"
    return text
