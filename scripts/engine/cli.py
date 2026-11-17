"""`epi.py` command line. Every command prints compact output (JSON for actions),
because whatever it prints lands in the orchestrator's context."""

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

from . import adapters, config as config_mod, hooks, learn, machine, pack as pack_mod, principles as principles_mod, runners, taskview, workflow, yamlish
from .store import Store

PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent


def out(obj):
    print(json.dumps(obj, separators=(",", ":")) if not isinstance(obj, str) else obj)


def load():
    return config_mod.load()


# ------------------------------------------------------------------ init ---

def detect(root):
    root = Path(root)
    d = {"language": None, "global": {"command": {}}, "agent": {"tester": {}}, "host": config_mod.detect_host(root),
         "gh": bool(shutil.which("gh")), "has_epi_md": (root / config_mod.EPI_FILE).exists(), "packs": pack_mod.installed_packs(root),
         "has_review_md": (root / "REVIEW.md").exists(), "has_claude_md": (root / "CLAUDE.md").exists()}
    pj = root / "package.json"
    if pj.exists():
        d["language"] = "javascript"
        scripts = json.loads(pj.read_text()).get("scripts", {})
        pm = "pnpm" if (root / "pnpm-lock.yaml").exists() else "yarn" if (root / "yarn.lock").exists() else "npm"
        d["global"]["command"]["setup"] = f"{pm} install" if pm != "npm" else "npm ci" if (root / "package-lock.json").exists() else "npm install"
        for k in ("lint", "build", "test"):
            if k in scripts:
                d["global"]["command"][k] = f"{pm} run {k}" if k != "test" else f"{pm} test"
        d["agent"]["tester"]["paths"] = ["**/*.test.*", "**/*.spec.*", "test/**", "tests/**", "__tests__/**"]
    elif (root / "pyproject.toml").exists() or (root / "setup.py").exists():
        d["language"] = "python"
        d["global"]["command"]["test"] = "pytest -q" if shutil.which("pytest") else "python -m pytest -q"
        if "ruff" in ((root / "pyproject.toml").read_text() if (root / "pyproject.toml").exists() else ""):
            d["global"]["command"]["lint"] = "ruff check ."
        d["agent"]["tester"]["paths"] = ["tests/**", "test/**", "**/test_*.py", "**/*_test.py"]
    elif (root / "go.mod").exists():
        d["language"] = "go"
        d["global"]["command"].update(build="go build ./...", test="go test ./...", lint="go vet ./...")
        d["agent"]["tester"]["paths"] = ["**/*_test.go"]
    elif (root / "Cargo.toml").exists():
        d["language"] = "rust"
        d["global"]["command"].update(build="cargo build", test="cargo test", lint="cargo clippy -- -D warnings")
        d["agent"]["tester"]["paths"] = ["tests/**"]
    if (root / "Makefile").exists():
        mk = (root / "Makefile").read_text(errors="replace")
        for k in ("lint", "build", "test"):
            if f"\n{k}:" in "\n" + mk and k not in d["global"]["command"]:
                d["global"]["command"][k] = f"make {k}"
    return d


def render(template, settings):
    """A settings file from its template: the template's comments, then the settings, then its body."""
    front, body = yamlish.split_frontmatter(template)
    comments = "".join(l for l in front.splitlines(keepends=True) if l.lstrip().startswith("#"))
    return "---\n" + comments + (yamlish.dumps(settings) + "\n" if settings else "") + "---\n" + body


def cmd_init(a):
    root = config_mod.main_checkout()
    if a.detect:
        return out(detect(root))
    settings = json.loads(Path(a.write).read_text()) if a.write else {}
    epi_md, created = root / config_mod.EPI_FILE, []
    existed = epi_md.exists()
    if not existed:
        g = settings.get("global", {})
        mine = {"global": {**g, "principle": {"list": principles_mod.ensure_defaults(root)}}}      # placeholders
        epi_md.parent.mkdir(parents=True, exist_ok=True)
        epi_md.write_text(render((PLUGIN_ROOT / "templates" / "epi.md").read_text(), mine))
        created += [config_mod.EPI_FILE] + principles_mod.ensure_defaults(root)
    default = config_mod.pack_name(epi_md.read_text())
    if not pack_mod.installed_packs(root):
        res = pack_mod.install(root, default)
        tuning = {k: v for k, v in settings.items() if k in ("stage", "agent")}
        if tuning:                                     # init's detection of tester paths and the like
            sp = config_mod.settings_path(root, default)
            sp.write_text(render(sp.read_text(), tuning))
        created += res["wrote"]
    if existed and not (((config_mod.load(root).raw.get("global") or {}).get("principle")) or {}).get("list"):
        created += principles_mod.add_starters(config_mod.load(root))     # an existing epi.md gets the starters too
    if not (root / "REVIEW.md").exists():
        shutil.copy(PLUGIN_ROOT / "templates" / "REVIEW.md", root / "REVIEW.md")
        created.append("REVIEW.md")
    cfg = config_mod.load(root)
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    if ensure_gitignore(root, cfg):
        created.append(".gitignore entries")
    allowed = allow_rules(root, cfg)
    if allowed:
        created.append(".claude/settings.local.json (permissions for agents)")
    out({"created": created, "work_dir": str(cfg.work_dir), "host": cfg.v("global.host")["adapter"],
         "worktree_dir": str(cfg.worktree_dir), "allowed": allowed,
         "note": "Agents work in the task worktree directory, which is allowed in .claude/settings.local.json "
                 "(accept the folder trust prompt for it to apply). They run your configured commands through "
                 "`epi.py run`, which the plugin allows without a prompt."})


def cmd_install(a):
    root = config_mod.main_checkout()
    try:
        out(pack_mod.install(root, a.pack, a.force))
    except pack_mod.PackError as e:
        out({"error": str(e)})
        sys.exit(1)


def cmd_upgrade(a):
    root = config_mod.main_checkout()
    try:
        res = pack_mod.upgrade(root, a.pack, a.dry_run)
    except pack_mod.PackError as e:
        out({"error": str(e)})
        sys.exit(1)
    out(res)
    if res.get("conflicts"):
        sys.exit(1)


def ensure_gitignore(root, cfg):
    """Keep the work folder, the learnings lock file and Claude Code's local settings out of git."""
    gi = root / ".gitignore"
    lines = gi.read_text().splitlines() if gi.exists() else []
    wanted = [cfg.v("global.repo.work_dir").rstrip("/") + "/", ".claude/workflows/*/learnings.md.lock", ".claude/settings.local.json"]
    missing = [w for w in wanted if w not in lines and w.rstrip("/") not in lines]
    if missing:
        gi.write_text("\n".join(lines + missing) + "\n")
    return missing


def allow_rules(root, cfg):
    """Let agents reach the task worktree directory without a prompt each time: `permissions.additionalDirectories`
    in `.claude/settings.local.json`. Existing entries are kept. (Configured commands need no rule: agents run them
    through `epi.py run`, which the plugin's hook allows.)"""
    path = root / ".claude" / "settings.local.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    have = data.setdefault("permissions", {}).setdefault("additionalDirectories", [])
    added = [d for d in [str(cfg.worktree_dir)] if d not in have]
    have += added
    if added:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
    return added


# ------------------------------------------------------------ tasks etc. ----

def cmd_intake(a):
    cfg = load()
    Store(cfg).add_backlog(" ".join(a.text))
    out(f"added to backlog: {' '.join(a.text)}")


def cmd_start(a):
    cfg = load()
    st = Store(cfg)
    arg = " ".join(a.what).strip()
    if arg and (st.task_dir(arg.removeprefix("task-")) / "task.json").exists():
        task_id = arg.removeprefix("task-")
        if machine.retry(cfg, task_id):
            out({"task": task_id, "retrying": True})
            return
        out({"task": task_id, "continuing": True})
        return
    if re.fullmatch(r"(task-)?\d+|b?[0-9a-f]{6,7}", arg) and not any(arg == i["id"] or arg == i["title"] for i in st.backlog_items()):
        return out({"error": f"no task or backlog item '{arg}'. Give a feature description to start a new task, "
                             "or /epi:status to list tasks"})
    item = st.pop_backlog_item(arg or None)
    text = (item or {}).get("title") if item else arg
    if not text:
        return out({"error": "the backlog is empty; give a feature description or /epi:intake one"})
    desc = (item or {}).get("description")
    wanted = (item or {}).get("id", "")
    task_id = wanted if wanted.isdigit() and not st.task_dir(wanted).exists() else None   # the number on the card
    meta = st.create_task(text, source=f"{text}\n\n{desc}" if desc else None, task_id=task_id)
    out({"task": meta["id"], "created": True, "branch": meta["branch"]})


def cmd_next(a):
    cfg = load()
    p = machine.Machine(cfg, a.task, session_id=a.session or os.environ.get("CLAUDE_CODE_SESSION_ID"))
    try:
        act = p.next_action()
    except Exception as e:  # noqa: BLE001
        p.ev("error", stage="next", detail=str(e))
        act = {"action": "failed", "task": a.task, "reason": f"internal error: {e}"}
    if act["action"] in ("done", "failed") and not act.get("cancelled"):
        retro_after(cfg, a.task, act)
    out(act)


def retro_after(cfg, task, act):
    """Run the retro when the task's outcome (clean, helped, failed) is one `global.learning.on` lists; else record the skip."""
    events = Store(cfg).events(task)
    kind = "failed" if act["action"] == "failed" else "clean" if taskview.first_pass(events) else "helped"
    if {kind, act["action"]} & set(cfg.v("global.learning.on")):
        try:
            learn.retro(cfg, task)
        except Exception as e:  # noqa: BLE001
            act["retro_error"] = str(e)
    elif not any(e["type"] == "retro_skipped" for e in events):
        machine.Machine(cfg, task).ev("retro_skipped", outcome=kind)


def cmd_report(a):
    cfg = load()
    if a.answers:
        out(machine.report_answers(cfg, a.task, Path(a.answers).read_text()))
    else:
        out({"error": "nothing to report (use --answers FILE)"})


def cmd_approve(a):
    cfg = load()
    try:
        out(machine.approve(cfg, a.task, a.approval, (a.value or [None])[0]))
    except ValueError as e:
        out({"error": str(e)})


def cmd_send_back(a):
    out(machine.send_back(load(), a.task, " ".join(a.feedback), to=a.to))


def cmd_cancel(a):
    cfg = load()
    machine.cancel(cfg, a.task)
    learn.retro(cfg, a.task)
    out(f"task {a.task} cancelled; its folder and branch are kept")


def cmd_status(a):
    cfg = load()
    st = Store(cfg)
    if a.check:
        return out(health(cfg))
    tasks = st.tasks() if not a.task else [st.load_task(a.task)]
    rows = []
    for m in tasks:
        p = machine.Machine(cfg, m["id"])
        s = p.state()
        total, by_stage = p.task_usage()
        status = "cancelled" if s.cancelled else s.stage
        row = {"task": m["id"], "title": m["title"][:60], "status": status, "round": taskview.round_number(s), "step": s.sub,
               "tokens": total}
        if s.failed:
            row.update(needs_you="failed", why=s.failed.get("reason", "")[:160])
        elif s.needs_you and s.stage != "done":
            row.update(needs_you=machine.plain(s.needs_you.get("reason")),
                       why=machine.plain(s.needs_you.get("reason")) + ": " + s.needs_you.get("detail", "")[:120])
        c = taskview.card(cfg, m, p, s)
        if c["stalled"]:
            row.update(stalled=taskview.STALLED.format(n=c["quiet_minutes"]))
        if a.task:
            row.update(by_stage=by_stage, branch=m["branch"], pr=m.get("pr"),
                       approvals=s.approvals, accepts=len(s.accepts))
        rows.append(row)
    out({"tasks": rows, "backlog": st.backlog()[:10], "month_tokens": st.month_tokens(),
         "month_budget": cfg.v("global.budget")["per_month"]})


def health(cfg):
    issues, ok, notes = [], [], []
    st = Store(cfg)
    ok.append(f"settings valid; workflow {cfg.workflow['name']} ({', '.join(cfg.stages)})")
    for pack in pack_mod.installed_packs(cfg.root):
        try:
            issues.extend(pack_mod.check(cfg.root, pack))
        except pack_mod.PackError:
            notes.append(f"workflow {pack} is the repo's own (no .base): `epi upgrade` does not apply")
    issues.extend(cfg.warnings)                  # e.g. a missing agent file
    if not cfg.command("test"):
        issues.append("global.command.test is not set: verify can't check anything")
    for name in ("lint", "build"):
        if not cfg.command(name):
            notes.append(f"global.command.{name} is not set (optional)")
    if cfg.v("global.host")["adapter"] == "github" and not shutil.which("gh"):
        issues.append("host is github but `gh` is not installed")
    for m in st.tasks():
        c = taskview.card(cfg, m)
        if c["stalled"]:
            issues.append(f"task-{m['id']}: {taskview.STALLED.format(n=c['quiet_minutes'])} (run /epi:start task-{m['id']} to continue)")
    known = {m["branch"] for m in st.tasks()}
    branches = config_mod.git("branch", "--list", cfg.v("global.repo.branch_prefix") + "*", "--format=%(refname:short)",
                              cwd=cfg.root, check=False).split()
    orphans = [b for b in branches if b not in known and not b.startswith(cfg.v("global.repo.branch_prefix") + "learnings")]
    if orphans:
        issues.append("branches with no task folder: " + ", ".join(orphans))
    wts = [p for p in cfg.worktree_dir.glob("*") if p.is_dir()] if cfg.worktree_dir.exists() else []
    known_wt = {str(Path(m["worktree"])) for m in st.tasks()}
    lost = [str(p) for p in wts if str(p) not in known_wt and p.name != "learnings"]
    if lost:
        issues.append("worktrees with no task folder: " + ", ".join(lost))
    try:
        ok.append(f"claude CLI found at {runners.claude_bin()}")
    except FileNotFoundError as e:
        issues.append(str(e))
    try:
        adapter_test(cfg, dry_run=True)
        ok.append(f"adapters configured (board: {cfg.v("global.board")['adapter']}, host: {cfg.v('global.host')['adapter']})")
    except adapters.AdapterError as e:
        issues.append(str(e))
    return {"ok": ok, "issues": issues, "notes": notes}


def adapter_test(cfg, kind=None, dry_run=False):
    """Check the configured adapters. --dry-run checks configuration only; a real run
    calls every operation twice (idempotency) against a test branch and cleans up."""
    results = []
    for k in ([kind] if kind else ["board", "host"]):
        a = adapters.board(cfg) if k == "board" else adapters.host(cfg)
        for op, tmpl in a.overrides.items():
            if op not in a.ops:
                raise adapters.AdapterError(f"{k}.ops.{op} is not a {k} operation ({', '.join(a.ops)})")
            if tmpl != "instructions":
                exe = tmpl.split()[0]
                if not (shutil.which(exe) or (cfg.root / exe).exists()):
                    raise adapters.AdapterError(f"{k}.ops.{op}: `{exe}` not found")
        if k == "host" and cfg.v("global.host")["adapter"] == "github" and not shutil.which("gh"):
            raise adapters.AdapterError("host is github but `gh` is not installed")
        results.append({"adapter": k, "name": a.settings.get("adapter"), "configured": True})
        if dry_run:
            continue
        if k == "host" and isinstance(a, adapters.LocalHost):
            for _ in range(2):
                r = a.call("pr_status", branch="epi/adapter-test")
            results[-1]["pr_status"] = r
    return results


def cmd_wait(a):
    """Background waiter: exits (and so notifies the session) when something changes."""
    cfg = load()
    host = adapters.host(cfg)
    meta = Store(cfg).load_task(a.task)
    interval = a.interval or cfg.v("global.wait.interval")
    deadline = time.time() + (a.max_hours if a.max_hours is not None else cfg.v("global.wait.max_hours")) * 3600
    while time.time() < deadline:
        s = host.call("pr_status", branch=meta["branch"])
        if a.for_ == "ci" and s["ci"] != "pending":
            return out({"task": a.task, "ci": s["ci"], "next": f"/epi:start {a.task}"})
        if a.for_ == "approval" and (s["approved"] or s.get("merged")):
            return out({"task": a.task, "approved": True, "next": f"/epi:start {a.task}"})
        time.sleep(interval)
    out({"task": a.task, "timeout": True})


def review_plan(cfg, names=(), base=None):
    """What `/epi:review` needs: the branch, its diff against base, every reviewer to run (the agent and
    skill steps of the workflow's review lists that only write their report, ignoring `if`: "all" means
    all) and where to write the report."""
    root = cfg.root
    base = base or cfg.v("global.repo.base_branch")
    branch = config_mod.git("rev-parse", "--abbrev-ref", "HEAD", cwd=root)
    if config_mod.git("rev-parse", "--verify", "-q", base, cwd=root, check=False) == "":
        raise RuntimeError(f"base branch '{base}' not found; pass one: review-plan <base>")
    if branch == base:
        raise RuntimeError(f"you are on the base branch '{base}'; check out the branch to review")
    files = config_mod.git("diff", "--name-only", f"{base}...HEAD", cwd=root).split()
    found = {}
    for sid, st in cfg.stages.items():
        if sid == cfg.workflow.get("facts"):
            continue                      # reviews of the spec, not of code
        for step in st["review"]:
            kind, name = workflow.kind_of(step), workflow.name_of(step)
            if kind in ("agent", "skill") and cfg.agent(name)["writes"] == "output":
                found.setdefault(step["_id"], {"name": step["_id"], "kind": kind, "target": name,
                                               "blocking": not step.get("continue-on-error"), "stage": sid})
    unknown = [n for n in names if n not in found]
    if unknown:
        raise RuntimeError(f"unknown reviewer(s): {', '.join(unknown)}; configured: {', '.join(found)}")
    reviewers = [r for n, r in found.items() if not names or n in names]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-")
    return {"branch": branch, "base": base, "files": files,
            "stat": config_mod.git("diff", "--stat", f"{base}...HEAD", cwd=root),
            "reviewers": reviewers, "skipped": [], "has_review_md": (root / "REVIEW.md").exists(),
            "report_dir": str(root / ".epi" / "reviews" / slug)}


def cmd_review_plan(a):
    try:
        out(review_plan(load(), a.names, a.base))
    except RuntimeError as e:
        out({"error": str(e)})
        sys.exit(1)


def cmd_serve(a):
    from . import serve
    try:
        serve.serve(config_mod.load(lenient=True), port=a.port, open_browser=a.open)
    except OSError as e:
        out({"error": f"cannot listen on port {a.port}: {e.strerror or e}. Pick another with --port, or leave it "
                      "out for a free one."})
        sys.exit(1)


DOCS = ("run-loop", "help", "formats", "workflow")


def cmd_doc(a):
    """Print one of the plugin's reference texts, so a skill need not read files outside the working folders."""
    if a.name not in DOCS:
        return out({"error": f"unknown text '{a.name}'; one of {', '.join(DOCS)}"})
    print((PLUGIN_ROOT / "reference" / f"{a.name}.md").read_text())


def cmd_run(a):
    """Run the repo command `global.command.<name>` in the task's checkout: the way agents run tests and builds.
    Takes no other arguments, so the hook can allow it without a prompt. Exits with the command's exit code."""
    cfg = load()
    task = a.task.removeprefix("task-")
    cmds = {k: v for k, v in cfg.v("global.command").items() if k != "timeout" and v}
    if a.name not in cmds:
        print(f"no command '{a.name}' (global.command.{a.name}); configured: {', '.join(cmds) or 'none'}", file=sys.stderr)
        sys.exit(2)
    meta = Store(cfg).load_task(task)
    cwd = Path(meta["worktree"]) if cfg.workflow["workspace"] == "git-worktree" else cfg.root
    Store(cfg).beat(task, busy_for=cfg.v("global.command.timeout"))
    code, output, secs = runners.sh(cmds[a.name], cwd, timeout=cfg.v("global.command.timeout"),
                                    env=machine.Machine(cfg, task).task_env())
    Store(cfg).beat(task)
    print(runners.tail(output, 80))
    print(f"[{a.name}: `{cmds[a.name]}` exit {code}, {secs}s]")
    sys.exit(code)


def cmd_adapter(a):
    cfg = load()
    try:
        out({"results": adapter_test(cfg, a.kind, dry_run=a.dry_run)})
    except adapters.AdapterError as e:
        out({"error": str(e)})
        sys.exit(1)


def cmd_hook(a):
    sys.exit(hooks.main(a.event))


def cmd_config(a):
    cfg = load()
    if a.action == "validate":
        return out("settings are valid")
    out(cfg.data if not a.key else cfg.data.get(a.key))


TEXT_COMMANDS = ("intake", "start", "send-back")


def read_stdin_args(argv):
    """`--stdin`: the arguments come from stdin, never from the shell command line (so a skill can pass
    text it did not write, such as an issue title, through a quoted heredoc). Free text follows `--`, so
    a word starting with `-` is text, not an option."""
    argv = [x for x in argv if x != "--stdin"]
    words = sys.stdin.read().split()
    if argv[0] not in TEXT_COMMANDS:
        return argv + words
    if argv[0] == "send-back":                       # <task> [--to <step>] <instructions>
        head = words[:1] + (words[1:3] if words[1:2] == ["--to"] else [])
        words = words[len(head):]
        return argv + head + ["--"] + words
    return argv + ["--"] + words


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--stdin" in argv and argv:
        argv = read_stdin_args(argv)
    ap = argparse.ArgumentParser(prog="epi.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.add_argument("--detect", action="store_true"); p.add_argument("--write")
    p.set_defaults(fn=cmd_init)
    p = sub.add_parser("install"); p.add_argument("pack", metavar="playbook"); p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_install)
    p = sub.add_parser("upgrade"); p.add_argument("pack", metavar="playbook"); p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_upgrade)
    p = sub.add_parser("intake"); p.add_argument("text", nargs="+"); p.set_defaults(fn=cmd_intake)
    p = sub.add_parser("start"); p.add_argument("what", nargs="*"); p.set_defaults(fn=cmd_start)
    p = sub.add_parser("next"); p.add_argument("task"); p.add_argument("--session")
    p.set_defaults(fn=cmd_next)
    p = sub.add_parser("report"); p.add_argument("task"); p.add_argument("--answers")
    p.set_defaults(fn=cmd_report)
    p = sub.add_parser("approve"); p.add_argument("task"); p.add_argument("approval", nargs="?")
    p.add_argument("value", nargs="*"); p.set_defaults(fn=cmd_approve)
    p = sub.add_parser("send-back"); p.add_argument("task"); p.add_argument("feedback", nargs="+")
    p.add_argument("--to", help="the step to send it back to: <step> or <stage>/<step> (default: the nearest agent)")
    p.set_defaults(fn=cmd_send_back)
    p = sub.add_parser("cancel"); p.add_argument("task"); p.set_defaults(fn=cmd_cancel)
    p = sub.add_parser("status"); p.add_argument("task", nargs="?"); p.add_argument("--check", action="store_true")
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser("wait"); p.add_argument("task"); p.add_argument("--for", dest="for_", choices=["ci", "approval"])
    p.add_argument("--interval", type=int, default=None); p.add_argument("--max-hours", type=float, default=None)
    p.set_defaults(fn=cmd_wait)
    p = sub.add_parser("hook"); p.add_argument("event"); p.set_defaults(fn=cmd_hook)
    p = sub.add_parser("review-plan"); p.add_argument("names", nargs="*"); p.add_argument("--base"); p.set_defaults(fn=cmd_review_plan)
    p = sub.add_parser("doc"); p.add_argument("name"); p.set_defaults(fn=cmd_doc)
    p = sub.add_parser("run"); p.add_argument("task"); p.add_argument("name"); p.set_defaults(fn=cmd_run)
    p = sub.add_parser("serve"); p.add_argument("--port", type=int, default=0)
    p.add_argument("--open", action="store_true"); p.set_defaults(fn=cmd_serve)
    p = sub.add_parser("adapter"); p.add_argument("action", choices=["test"]); p.add_argument("kind", nargs="?",
                                                                                            choices=["board", "host"])
    p.add_argument("--dry-run", action="store_true"); p.set_defaults(fn=cmd_adapter)
    p = sub.add_parser("config"); p.add_argument("action", nargs="?", default="get"); p.add_argument("key", nargs="?")
    p.set_defaults(fn=cmd_config)
    a = ap.parse_args(argv)
    try:
        a.fn(a)
    except config_mod.ConfigError as e:
        out({"error": str(e)})
        sys.exit(2)
    except KeyError as e:
        out({"error": str(e).strip("'\"")})
        sys.exit(2)
