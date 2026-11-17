"""Approval taken from the host: the PR's approval. The local host has no PRs, so a person approves
with `/epi:approve` (exit 5, MANUAL)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import MANUAL, PASS, WAIT, adapter_error, adapters, host, reply, request, waiter  # noqa: E402


def main():
    req = request()
    cfg, h = host(req)
    if not h.has_prs:
        reply(MANUAL)
    try:
        status = h.call("pr_status", branch=req["branch"])
    except adapters.AdapterError as e:
        adapter_error(e)
    if status.get("approved") or status.get("merged"):
        reply(PASS, summary="approved on the PR")
    reply(WAIT, reason="merge sign-off: waiting for PR approval", hint=f"Review {req.get('pr')}", waiter=waiter(req, "approval"))


main()
