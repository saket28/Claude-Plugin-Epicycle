"""A board full of simulated tasks, for demos and for checking the dashboard by eye. No real agents run.

    python3 tests/sim.py [--port 8767] [--no-serve]

Makes a throwaway repo, drives one task into each situation a person meets, prints the repo and the
task ids as JSON, then serves the dashboard on it (Ctrl-C stops it; the repo is in the system temp folder).
test_sim.py checks every scenario still lands where this says.
"""

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "scripts")]

from helpers import make_repo                                    # noqa: E402
from test_machine import Agents, base_md, drive                 # noqa: E402
from engine import config, machine, serve, store as store_mod   # noqa: E402
from engine.store import Store                                  # noqa: E402

CI_ONCE = "test -f ../ci-failed-once || { touch ../ci-failed-once; echo 'CI: 2 checks failed (lint-js, e2e)'; exit 1; }"
LICENSES = "test ! -f ../fail-licenses || { echo 'GPL-3.0 dependency: left-pad'; exit 1; }"   # fails while the file is there
NOTIFY = "printf '%s|%s\\n' \"$CHANNEL\" \"$FINDINGS\" >> ../notified"        # stands in for a Slack message
SETTINGS = {"global": {"command": {"ci-check": CI_ONCE, "licenses": LICENSES, "notify": NOTIFY}}}
SECURITY = "{ uses: skill/security-review, id: security, outputs: [blocking] }"
ALERT = ('{ run: notify, id: notify, if: "steps.security.outcome == failure", continue-on-error: true, '
         'env: { CHANNEL: "#sec-alerts", FINDINGS: "${{ steps.security.outputs.blocking }}" } }')
EDITS = [("add", "build", "finalize", "{ run: ci-check }"), ("add", "release", "verify", "{ run: licenses }"),
         ("add", "build", "review", SECURITY), ("add", "build", "review", ALERT)]
FINDING = "SQL injection in the login query"

# what each scenario's card should say needs you (None: nothing), checked by test_sim.py
EXPECT = {"spec_signoff": "approval", "disagreement": "disagreement", "build_signoff": "approval",
          "release_failure": "failure", "stalled": None, "done": None, "security_alert": "approval"}


def seed():
    repo = make_repo(base_md("human", 3, SETTINGS, EDITS, spec_signoff=True))
    cfg, st, ids = config.load(repo), Store(config.load(repo)), {}

    def task(title, through=()):
        t, a = st.create_task(title)["id"], Agents()
        for stage in through:                      # sign off the stages it has passed
            drive(cfg, t, a)
            machine.approve(cfg, t, stage)
        return t, a

    t, a = task("Greet the user by name")
    drive(cfg, t, a); ids["spec_signoff"] = t

    t, a = task("Say hello in French", through=["spec"])
    a.test_verdict, a.dispute, a.upheld = "FAIL", True, 1
    drive(cfg, t, a); ids["disagreement"] = t

    t, a = task("Say hello twice", through=["spec"])  # CI fails once in Finalize, the builder fixes it
    drive(cfg, t, a); ids["build_signoff"] = t

    t, a = task("Add a goodbye command", through=["spec", "build"])
    flag = cfg.worktree_dir / "fail-licenses"
    flag.write_text("")
    drive(cfg, t, a); ids["release_failure"] = t
    flag.unlink()

    t, a = task("Harden the login", through=["spec"])       # the security review fails once and tells #sec-alerts
    fake = {"FAKE_REVIEW": "important", "FAKE_ONLY": "/security-review", "FAKE_ONCE": str(cfg.worktree_dir / "security-once"),
            "FAKE_OUTPUTS": json.dumps({"blocking": FINDING})}
    os.environ.update(fake)
    try:
        drive(cfg, t, a)
    finally:
        for k in fake:
            os.environ.pop(k, None)
    ids["security_alert"] = t

    t, a = task("Add a farewell banner", through=["spec"])
    machine.Machine(cfg, t, session_id="sim-session").next_action()   # the builder starts, then nothing more
    backdate(cfg, t, minutes=25); ids["stalled"] = t

    t, a = task("Print the version")
    drive(cfg, t, a); machine.approve(cfg, t, "spec")
    drive(cfg, t, a); machine.approve(cfg, t, "build")
    drive(cfg, t, a)
    st.event(t, "agent", key=builder_key(cfg, t), agent="builder", stage="build",
             data={"tokens": 61_000, "cache_read": 240_000, "duration_s": 312, "model": "claude-sonnet-5-5",
                   "context": 172_000})                        # a builder that got close to a full window
    ids["done"] = t
    return repo, ids


def builder_key(cfg, t):
    """The usage key the SubagentStop hook gives the build stage's first builder run."""
    p = machine.Machine(cfg, t)
    n = min(m for m, (sid, _, _) in p.state().rounds.items() if sid == "build")
    return f"build-builder-n{n}"


def backdate(cfg, t, minutes):
    """Move a task's events and heartbeat back in time, as if its session went quiet that long ago."""
    st = Store(cfg)
    shift = dt.timedelta(minutes=minutes)
    f = st.task_dir(t) / "events.jsonl"
    rows = [json.loads(x) for x in f.read_text().splitlines() if x.strip()]
    for r in rows:
        r["ts"] = (dt.datetime.fromisoformat(r["ts"].replace("Z", "+00:00")) - shift).strftime("%Y-%m-%dT%H:%M:%SZ")
    store_mod.write_atomic(f, "".join(json.dumps(r) + "\n" for r in rows))
    at = (dt.datetime.now(dt.timezone.utc) - shift).timestamp()
    store_mod.write_atomic(st.task_dir(t) / "heartbeat", json.dumps({"at": at, "until": at}))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-serve", action="store_true", help="seed and print, don't start the dashboard")
    a = ap.parse_args(argv)
    repo, ids = seed()
    print(json.dumps({"repo": str(repo), **ids}), flush=True)
    if not a.no_serve:
        serve.serve(config.load(repo, lenient=True), port=a.port)


if __name__ == "__main__":
    main()
