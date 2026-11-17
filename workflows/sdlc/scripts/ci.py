"""Host CI status: pending waits, failing is a failure; the local host has no CI."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import FAIL, PASS, WAIT, adapter_error, adapters, host, reply, request, waiter  # noqa: E402


def main():
    req = request()
    cfg, h = host(req)
    if not h.has_prs or not req["settings"].get("ci_required", True):
        reply(PASS, summary="no CI to wait for")
    try:
        status = h.call("pr_status", branch=req["branch"])
    except adapters.AdapterError as e:
        adapter_error(e)
    if status.get("merged") or status["ci"] in ("pass", "none"):
        reply(PASS, summary=f"CI {status['ci']}")
    if status["ci"] == "pending":
        reply(WAIT, reason="waiting for CI", hint=f"PR {req.get('pr')}", waiter=waiter(req, "ci"))
    reply(FAIL, summary="CI failed", detail=f"CI failed on PR {req.get('pr')}")


main()
