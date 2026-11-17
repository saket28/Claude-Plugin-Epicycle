"""Rebase the task branch onto base, push it and open or update the PR (local host: rebase only)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import FAIL, PASS, adapter_error, adapters, host, reply, request  # noqa: E402


def pr_body(req):
    lines = [f"Task {req['task']}: {req['title']}", ""]
    spec = Path(req["task_dir"]) / "spec.md"
    if spec.exists():
        lines += ["## Spec", "", spec.read_text(), ""]
    if req.get("reviews"):
        lines += ["## Reviews", ""]
        lines += [f"- {r['id']}: {r['verdict']} ({r['important']} important, {r['nit']} nit)" for r in req["reviews"]]
    lines += ["", f"Tokens used so far: {req.get('tokens', 0):,}", "", "_Opened by Epicycle._"]
    return "\n".join(lines)


def main():
    req = request()
    cfg, h = host(req)
    s = req["settings"]
    try:
        if h.has_prs:
            meta = {}
            if not req.get("pr"):
                body = Path(req["round_dir"]) / "pr-body.md"
                body.write_text(pr_body(req))
                r = h.call("open_pr", branch=req["branch"], base=req["base"], title=req["title"],
                           body_file=str(body), worktree=req["worktree"], draft=s.get("pr_draft", False),
                           labels=s.get("pr_labels") or [], reviewers=s.get("pr_reviewers") or [])
                meta = {"pr": r.get("pr")}
            status = h.call("pr_status", branch=req["branch"])
            if status.get("behind") and not status.get("merged"):
                r = h.call("update_branch", branch=req["branch"])
                if r.get("conflict"):
                    reply(FAIL, summary="merge conflict with " + req["base"], meta=meta)
            reply(PASS, summary="PR " + str(meta.get("pr") or req.get("pr")), meta=meta, event="pr")
        r = h.call("update_branch", worktree=req["worktree"], base=req["base"])
        if r.get("conflict"):
            reply(FAIL, summary="rebase conflict with " + req["base"], detail=r.get("reason", ""))
        if not r.get("updated") and "up to date" not in r.get("reason", ""):
            reply(4, reason="adapter", detail="rebase failed: " + r.get("reason", ""))
        reply(PASS, summary="rebased onto " + req["base"])
    except adapters.AdapterError as e:
        adapter_error(e)


main()
