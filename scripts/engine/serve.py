"""Local dashboard server. Standard library only.

Listens on 127.0.0.1, random port, with a per-run token. Reads task folders on
every request (no stored board), and writes only the settings files, workflow.yaml, learnings.md and principle files,
through configedit / workflowedit / principles, and upgrades the pack like `epi upgrade`.
"""

import os
import http.server
import json
import re
import secrets
import socketserver
import threading
import urllib.parse
from pathlib import Path

from . import briefs, config as config_mod, configedit, machine, pack as pack_mod, principles as principles_mod, taskview, workflow, workflowedit
from .store import Store

PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent


def columns(cfg):
    """The board: the backlog, a column per stage of the workflow, done. "Needs you" is a flag on a task,
    never a column."""
    return ["queued", *cfg.stages, "done"]


def labels(cfg):
    return {"queued": "Backlog", "done": "Done",
            **{sid: cfg.workflow["stages"][sid].get("name") or sid for sid in cfg.stages}}


def _applied_entries(cfg, d):
    """The learnings and answers a task cited (`applied: L-xxxx`), with their text from learnings.md.
    One that has since been deleted from it is still listed, marked as removed."""
    body = cfg.learning_sections()
    known = {e["id"]: e for sec in ("Learnings", "Answers") for e in briefs.parse_entries(body.get(sec, ""))}
    return [{"id": i, "roles": (known.get(i) or {}).get("roles", []), "text": (known.get(i) or {}).get("text", ""),
             "removed": i not in known} for i in sorted(set(_applied(d)))]


def _applied(d):
    for f in d.rglob("*.md"):
        if "briefs" in f.parts:
            continue
        yield from re.findall(r"applied:\s*([LA]-[0-9a-f]+)", f.read_text(errors="replace"))


def board(cfg):
    st = Store(cfg)
    tasks = [taskview.card(cfg, m) for m in st.tasks()]
    done = [j for j in tasks if j["status"] == "done"][-30:]
    trends = {"tasks": len(done),
              "avg_tokens": int(sum(j["tokens"] for j in done) / len(done)) if done else 0,
              "first_pass": round(sum(1 for j in done if j["first_pass"]) / len(done), 2) if done else None}
    needs = [{"task": j["id"], "title": j["title"], "stage": j["stage"], "stage_name": j["stage_name"],
              "reason": j["needs_you"]["reason"],
              "text": f"task-{j['id']} · {j['stage_name']} · {j['needs_you']['text']}"} for j in tasks if j["needs_you"]]
    needs += [{"task": j["id"], "title": j["title"], "stage": j["stage"], "stage_name": j["stage_name"], "reason": "stalled",
               "text": f"task-{j['id']} · {j['stage_name']} · {taskview.STALLED.format(n=j['quiet_minutes'])}"}
              for j in tasks if j["stalled"] and not j["needs_you"]]
    return {"columns": columns(cfg), "labels": labels(cfg), "tasks": tasks, "needs_you": needs, "backlog": st.backlog(),
            "backlog_items": st.backlog_items(), "activity": taskview.activity(cfg, st, cfg.v("global.dashboard.activity")),
            "month": {"tokens": st.month_tokens(), "budget": cfg.v("global.budget")["per_month"]},
            "trends": trends, "repo": cfg.root.name, "warn_at": cfg.v("global.budget.warn_at")}


def settings(cfg):
    body = cfg.learning_sections()
    sch = json.loads(json.dumps(cfg.schema))
    for sec in next(t for t in sch["tabs"] if t["id"] == "stage")["sections"]:
        sec["title"] = labels(cfg).get(sec["id"], sec["id"])
    for tab in sch["tabs"]:                       # names the repo added to a section that takes any: show them too
        for sec in tab["sections"]:
            kind = config_mod.OPEN_SECTIONS.get((tab["id"], sec["id"]))
            have = {f["path"] for f in sec.get("fields", [])}
            for k in (cfg.data.get(tab["id"], {}).get(sec["id"]) or {}) if kind else []:
                if f"{tab['id']}.{sec['id']}.{k}" not in have:
                    sec.setdefault("fields", []).append({"path": f"{tab['id']}.{sec['id']}.{k}", "type": kind,
                                                         "help": "Added by this repo"})
    return {"schema": sch, "effective": cfg.data, "overrides": cfg.raw, "hash": configedit.hashes(cfg), "files": {f: str(p.relative_to(cfg.root)) for f, p in configedit.paths(cfg).items()},
            "defaults": cfg.defaults, "workflow": workflow_view(cfg),
            "workflow_hash": workflowedit.hash_of(cfg), "rules": cfg.rules.strip("\n"),
            "principle_problems": cfg.principle_problems,
            "learnings": {s: briefs.parse_entries(body.get(s, "")) for s in ("Learnings", "Answers")},
            "usage": {"month": Store(cfg).month_tokens()}}


def workflow_view(cfg):
    """The workflow as the Workflow tab draws it: stages left to right, each with its steps by phase. A step knows
    whether it is on (`enabled` in workflow.yaml) and which settings section it has."""
    stages = []
    for sid, st in cfg.workflow["stages"].items():
        phases = {}
        for sub in workflow.PHASES:
            so = workflow.signoff_step(st)
            mine = ([so] if so else []) if sub == "signoff" else list(st.get(sub) or [])
            phases[sub] = [{"id": workflow.step_id(s), "label": taskview.label(s), "kind": workflow.kind_of(s),
                            "name": workflow.name_of(s), "if": s.get("if"), "on": s.get("enabled", True),
                            "settings": workflow.name_of(s) in cfg.data["agent"] and workflow.kind_of(s) in ("agent", "skill")}
                           for s in mine]
        stages.append({"id": sid, "name": st.get("name") or sid, "signoff": bool(st.get("signoff")), "phases": phases})
    commands = sorted({k for k, v in cfg.v("global.command").items() if k not in ("setup", "timeout") and v}
                      | set(workflow.scripts(cfg.workflow_name)))
    return {"name": cfg.workflow["name"], "stages": stages, "phase_labels": PHASES, "commands": commands,
            "lists": list(workflow.LISTS)}


PHASES = {"plan": "Plan", "do": "Do", "verify": "Verify", "review": "Review", "finalize": "Finalize",
          "signoff": "Sign-off", "finish": "Finish"}
CONTINUE = "The dashboard cannot run agents. Run /epi:start task-{id} in Claude Code to continue."


ACTION_LOCK = threading.Lock()


def task_action(cfg, action, req):
    """A person's decision on a task, from the dashboard: the same functions the commands use. One at a time,
    so a double click is decided once and the second click is told so."""
    with ACTION_LOCK:
        return _task_action(cfg, action, req)


def _task_action(cfg, action, req):
    tid = str(req.get("task") or "")
    meta = Store(cfg).load_task(tid)                    # KeyError -> 400 when unknown
    p = machine.Machine(cfg, tid)
    st = p.state()
    note = " ".join(str(req.get("note") or "").split())
    by = os.environ.get("USER", "dashboard")
    need = taskview.needs_of(st)
    if action != "retry_task" and not taskview.decision(cfg, p, st, {"needs_decision": []}):
        raise ValueError(f"task-{tid} is not waiting for you any more (already decided?)")
    if action == "accept":
        if not need or need["reason"] not in ("disagreement", "failure", "rules"):
            raise ValueError("there is nothing to accept as it is")
        msg = machine.approve(cfg, tid, "accept", by=by)
    elif action == "send_back":
        if not note:
            raise ValueError("write what should change")
        msg = machine.send_back(cfg, tid, note, to=req.get("to") or None)
    elif action == "answer":
        if not (req.get("note") or "").strip():
            raise ValueError("write your answers")
        msg = machine.report_answers(cfg, tid, req["note"])
    elif action == "approve":
        sid, g = taskview.waiting_approval(st)
        if not g:
            raise ValueError("no approval is waiting")
        msg = machine.approve(cfg, tid, sid, by=by)
    elif action == "budget":
        msg = machine.approve(cfg, tid, "budget", req.get("value") or "+500k", by=by)
    elif action == "retry":
        msg = machine.approve(cfg, tid, "retry", by=by)
    elif action == "retry_task":
        if not machine.retry(cfg, tid):
            raise ValueError("this task has not failed")
        msg = "ready to try again"
    else:
        raise ValueError("unknown action")
    return {"ok": True, "message": msg, "next": CONTINUE.format(id=tid)}


def clear_alerts(cfg, tid, ids):
    """Clearing hides notices, never a task that still needs you: its needs-you alerts stay."""
    store = Store(cfg)
    store.load_task(tid)
    if taskview.needs_of(machine.Machine(cfg, tid).state()):
        keep = [a["id"] for a in store.alerts(tid) if a["kind"] == "needs_you"]
        wanted = ids if ids else [a["id"] for a in store.alerts(tid)]
        ids = [i for i in wanted if i not in keep]
        if not ids:
            return 0
    return store.clear_alerts(tid, ids)


class Handler(http.server.BaseHTTPRequestHandler):
    token = None
    root = None

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json"
                                                      else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self, q):
        t = self.headers.get("X-Epi-Token") or (q.get("token") or [None])[0]
        return t and secrets.compare_digest(t, self.token)

    @property
    def hosts(self):
        port = self.server.server_address[1]
        return f"127.0.0.1:{port}", f"localhost:{port}"

    def _refused(self, q):
        """Why a request is refused, or None. The Host must be this server (a page on another site that
        rebinds its name to 127.0.0.1 sends its own name), then the token must match."""
        if self.headers.get("Host") not in self.hosts:
            return 421, "wrong Host"
        if not self._authorized(q):
            return 403, "missing or wrong token"
        return None

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if bad := self._refused(q):
            return self._send(bad[0], {"error": bad[1]})
        cfg = config_mod.load(self.root, lenient=True)
        if u.path == "/":
            return self._send(200, (PLUGIN_ROOT / "templates" / "dashboard.html").read_text(), "text/html")
        if u.path == "/api/board":
            return self._send(200, board(cfg))
        if u.path == "/api/task":
            meta = Store(cfg).load_task(q["id"][0])
            return self._send(200, taskview.task(cfg, meta))
        if u.path == "/api/file":
            d = Store(cfg).task_dir(q["task"][0]).resolve()
            f = (d / q["path"][0]).resolve()
            if d not in f.parents or f.suffix != ".md" or not f.exists():
                return self._send(404, {"error": "not found"})
            return self._send(200, f.read_text(errors="replace"), "text/plain")
        if u.path == "/api/settings":
            return self._send(200, settings(cfg))
        if u.path == "/api/pack":
            return self._send(200, pack_mod.status(cfg.root, cfg.workflow_name) or {"pack": cfg.workflow_name})
        if u.path == "/api/help":
            return self._send(200, (PLUGIN_ROOT / "reference" / "help.md").read_text(), "text/plain")
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if bad := self._refused({}):
            return self._send(bad[0], {"error": bad[1]})
        if self.headers.get("Origin") not in (None, *(f"http://{h}" for h in self.hosts)):
            return self._send(403, {"error": "cross-origin request refused"})
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self._send(415, {"error": "JSON only"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(min(n, 1_000_000)) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad JSON"})
        cfg = config_mod.load(self.root, lenient=True)
        try:
            if u.path == "/api/settings/preview":
                done = configedit.plan(cfg, req.get("changes", {}))
                texts = {f: (p.read_text() if p.exists() else "") for f, p in configedit.paths(cfg).items()}
                texts.update({f: new for f, (_, _, new) in done.items()})
                config_mod.from_texts(cfg.root, texts["global"], texts["pack"], cfg.learn_text)
                head = lambda p: f"# {p.relative_to(cfg.root)}\n"
                return self._send(200, {"before": "".join(head(p) + b for p, b, _ in done.values()),
                                        "after": "".join(head(p) + a for p, _, a in done.values()),
                                        "files": [str(p.relative_to(cfg.root)) for p, _, _ in done.values()]})
            if u.path == "/api/settings":
                h = configedit.save(cfg, req.get("changes", {}), req.get("hash"))
                return self._send(200, {"saved": True, "hash": h})
            if u.path == "/api/workflow/preview":
                return self._send(200, workflowedit.plan(cfg, req.get("ops") or []))
            if u.path == "/api/workflow":
                return self._send(200, {"saved": True, "hash": workflowedit.save(cfg, req.get("ops") or [], req.get("hash"))})
            if u.path == "/api/rules/preview":
                p, before, after = configedit.plan_rules(cfg, req.get("text"))
                rel = p.relative_to(cfg.root).as_posix()
                return self._send(200, {"path": rel, "diff": pack_mod.diff(rel, before, after)})
            if u.path == "/api/rules":
                return self._send(200, {"saved": True, "hash": configedit.save_rules(cfg, req.get("text"), (req.get("hash") or {}).get("pack"))})
            if u.path == "/api/pack/upgrade":
                return self._send(200, pack_mod.upgrade(cfg.root, cfg.workflow_name))
            if u.path == "/api/principle/save":
                return self._send(200, {"saved": principles_mod.save(cfg, req.get("name"), req.get("front") or {},
                                                                     req.get("body") or "")})
            if u.path == "/api/principle/delete":
                return self._send(200, {"removed": principles_mod.delete(cfg, req.get("name"))})
            if u.path == "/api/backlog":
                return self._send(200, backlog_action(cfg, req))
            if u.path == "/api/alerts/clear":
                return self._send(200, {"cleared": clear_alerts(cfg, req["task"], req.get("ids"))})
            if u.path.startswith("/api/task/") and u.path[len("/api/task/"):] in (
                    "accept", "send_back", "approve", "budget", "retry", "retry_task", "answer"):
                return self._send(200, task_action(cfg, u.path[len("/api/task/"):], req))
            if u.path == "/api/task/delete":
                return self._send(200, delete_task(cfg, req.get("id")))
            if u.path == "/api/learnings/update":
                return self._send(200, update_learning(cfg, req.get("id"), req.get("roles"), req.get("text")))
            if u.path == "/api/learnings/delete":
                return self._send(200, delete_learning(cfg, req.get("id")))
        except (configedit.EditError, config_mod.ConfigError, pack_mod.PackError, ValueError, KeyError) as e:
            return self._send(400, {"error": str(e)})
        return self._send(404, {"error": "not found"})


def backlog_action(cfg, req):
    st, action = Store(cfg), req.get("action")
    pri = req.get("priority")
    if pri not in (None, "", "low", "medium", "high"):
        raise ValueError("priority must be low, medium or high")
    if action == "add":
        if not (req.get("title") or "").strip():
            raise ValueError("title is required")
        return {"id": st.add_backlog(req["title"], req.get("description") or "", pri or "")}
    if action == "update":
        st.update_backlog(req.get("id"), req.get("title"), req.get("description"), pri)
        return {"saved": True}
    if action == "delete":
        st.delete_backlog(req.get("id"))
        return {"deleted": True}
    raise ValueError("action must be add, update or delete")


def delete_task(cfg, task_id):
    """Remove a finished task's folder (events, reports, alerts). The month's token ledger, the
    merged code and its branch are left alone. Only a done task can be deleted."""
    import shutil
    store = Store(cfg)
    store.load_task(task_id)                                    # KeyError -> 400 when unknown
    if machine.Machine(cfg, task_id).state().stage != "done":
        raise ValueError(f"task {task_id} isn't done; cancel it instead")
    shutil.rmtree(store.task_dir(task_id))
    return {"deleted": task_id}


def delete_learning(cfg, entry_id):
    """Remove one Learning or Answer line from learnings.md."""
    path = cfg.learnings_path
    text = path.read_text() if path.exists() else ""
    lines = text.splitlines(keepends=True)
    keep = [l for l in lines if not (briefs.ENTRY.match(l) and briefs.ENTRY.match(l).group("id") == entry_id)]
    if len(keep) == len(lines):
        raise KeyError(f"no learning or answer {entry_id}")
    path.write_text("".join(keep))
    return {"deleted": entry_id}


def update_learning(cfg, entry_id, roles, text):
    """Rewrite one Learning or Answer line, keeping its id."""
    text = " ".join(str(text or "").split())
    if not text:
        raise ValueError("text is required")
    roles = ", ".join(str(r).strip() for r in (roles or []) if str(r).strip()) or "all"
    path = cfg.learnings_path
    lines = (path.read_text() if path.exists() else "").splitlines(keepends=True)
    for i, l in enumerate(lines):
        m = briefs.ENTRY.match(l)
        if m and m.group("id") == entry_id:
            lines[i] = f"- [{entry_id}] {roles} · {text}\n"
            path.write_text("".join(lines))
            return {"updated": entry_id}
    raise KeyError(f"no learning or answer {entry_id}")


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def serve(cfg, port=0, open_browser=False):
    token = secrets.token_urlsafe(18)
    srv = Server(("127.0.0.1", port), Handler)
    Handler.token, Handler.root = token, cfg.root
    url = f"http://127.0.0.1:{srv.server_address[1]}/?token={token}"
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    (cfg.work_dir / "dashboard.url").write_text(url + "\n")
    print(json.dumps({"url": url}), flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    srv.serve_forever()
