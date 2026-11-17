"""Shared by the sdlc wf_name's scripts. A script reads one JSON request on stdin and prints one JSON reply;
the exit code is the verdict (0 PASS, 1 FAIL, 3 WAIT, 4 HUMAN, 5 MANUAL).

The request carries: task, stage, step, round, root (the main checkout), worktree, branch, base, title, pr,
task_dir, round_dir, with, settings (this stage's settings), guardrail, changed_paths, open_issues,
skip_rules, reviews. A reply may carry: summary, detail (feedback on FAIL), reason and hint (WAIT, HUMAN),
waiter, meta (task fields to store, e.g. {"pr": url}) and event.
"""

import json
import os
import sys
from pathlib import Path

PLUGIN = Path(os.environ.get("EPI_PLUGIN_ROOT") or Path(__file__).resolve().parents[3])    # set by the engine
sys.path.insert(0, str(PLUGIN / "scripts"))

from engine import adapters, config  # noqa: E402

PASS, FAIL, WAIT, HUMAN, MANUAL = 0, 1, 3, 4, 5


def request():
    return json.loads(sys.stdin.read() or "{}")


def reply(code, **data):
    print(json.dumps(data))
    sys.exit(code)


def host(req):
    cfg = config.load(Path(req["root"]))
    return cfg, adapters.host(cfg)


def waiter(req, what):
    return f"python3 {PLUGIN / 'scripts' / 'epi.py'} wait {req['task']} --for {what}"


def adapter_error(e):
    reply(HUMAN, reason="adapter", detail=str(e))
