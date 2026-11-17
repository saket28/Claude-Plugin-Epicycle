"""Land the branch on base. First the rule check (protected paths, no open issues), then the host's merge."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import FAIL, HUMAN, PASS, WAIT, adapter_error, adapters, host, reply, request, waiter  # noqa: E402
from engine import rules  # noqa: E402


def rule_problems(req):
    problems = []
    protected = [p for p in req["changed_paths"] for g in req["guardrail"]["protected_paths"] if rules.glob_match(g, p)]
    if protected:
        problems.append("protected paths changed: " + ", ".join(sorted(set(protected))))
    if req["open_issues"]:
        problems.append("issues still open: " + ", ".join(req["open_issues"]))
    return problems


def main():
    req = request()
    cfg, h = host(req)
    s = req["settings"]
    if not req.get("skip_rules"):
        problems = rule_problems(req)
        if problems:
            reply(HUMAN, reason="rules", detail="; ".join(problems))
    message = s["message"].format(title=req["title"], id=req["task"], branch=req["branch"])
    try:
        if h.has_prs:
            status = h.call("pr_status", branch=req["branch"])
            if status.get("merged"):
                reply(PASS, summary=f"Merged {req['branch']} into {req['base']}")
            if status["behind"]:
                r = h.call("update_branch", branch=req["branch"])
                if r.get("conflict"):
                    reply(FAIL, summary="merge conflict with " + req["base"])
                reply(WAIT, reason="branch updated; waiting for CI", hint=str(req.get("pr")), waiter=waiter(req, "ci"))
            r = h.call("merge", branch=req["branch"], strategy=s["strategy"], message=message,
                       delete_branch=s["delete_branch"])
            if not r.get("merged"):
                reply(WAIT, reason=f"merge refused: {r.get('reason')}", hint=f"Check {req.get('pr')}",
                      waiter=waiter(req, "approval"))
        else:
            r = h.call("merge", branch=req["branch"], base=req["base"], strategy=s["strategy"], message=message)
            if not r.get("merged"):
                if "rebase first" in (r.get("reason") or ""):
                    reply(FAIL, summary=f"{req['base']} moved on: {r.get('reason')}")
                reply(WAIT, reason=f"cannot merge yet: {r.get('reason')}",
                      hint=f"Fix that, then /epi:start {req['task']}")
    except adapters.AdapterError as e:
        adapter_error(e)
    reply(PASS, summary=f"Merged {req['branch']} into {req['base']}", event="merged")


main()
