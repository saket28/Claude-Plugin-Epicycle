"""Step outputs and outcomes: what a step publishes for the steps below it.

Every step that ran has an `outcome` (success | failure | skipped), its raw `verdict` and the `outputs` it declares.
A round keeps them in `<stage>/r<n>/outputs.json`, {step id: {outcome, verdict, outputs, reused?}}. Agents and
skills write their outputs as fields of their report's frontmatter; a command or a workflow script writes
`name=value` lines (and `name<<EOF` ... `EOF` for several lines) to the file named by $EPI_OUTPUT.
"""

import json
import re
from pathlib import Path

from .store import write_atomic

FILE = "outputs.json"
OUTCOME = {"PASS": "success", "CHANGES": "failure", "FAIL": "failure"}
KEY = re.compile(r"(\w+)\s*(?:=(.*)|<<\s*(\S+)\s*)$")
CLIP = 160


def parse_file(path):
    """{name: value} from an $EPI_OUTPUT file: `name=value` lines and `name<<DELIM` blocks ending at a line DELIM."""
    path = Path(path)
    if not path.exists():
        return {}
    out, lines, i = {}, path.read_text().splitlines(), 0
    while i < len(lines):
        m = KEY.fullmatch(lines[i].strip()) if lines[i].strip() and not lines[i].startswith("#") else None
        i += 1
        if not m:
            continue
        if m.group(3):
            block = []
            while i < len(lines) and lines[i].strip() != m.group(3):
                block.append(lines[i])
                i += 1
            i += 1
            out[m.group(1)] = "\n".join(block)
        else:
            out[m.group(1)] = (m.group(2) or "").strip()
    return out


def text(v):
    """A value as the text steps see: lists are comma-separated, booleans true/false."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple)):
        return ", ".join(text(x) for x in v)
    return "" if v is None else str(v)


def record(verdict, raw, declared):
    """The record of a step that ran: only the declared outputs are kept."""
    verdict = str(verdict or "")
    return {"outcome": OUTCOME.get(verdict.upper(), "success"), "verdict": verdict,
            "outputs": {k: text(raw[k]) for k in declared or () if k in raw}}


SKIPPED = {"outcome": "skipped", "verdict": "", "outputs": {}}


def reused(prev):
    """The record of a step that did not run again because it passed last round: its outputs, republished."""
    return {"outcome": "success", "verdict": prev.get("verdict", ""), "outputs": dict(prev.get("outputs") or {}), "reused": True}


def read(round_dir):
    try:
        return json.loads((Path(round_dir) / FILE).read_text())
    except (OSError, ValueError):
        return {}


def write_step(round_dir, step_id, rec):
    """Store a step's record in the round's file (only when it changed)."""
    all_ = read(round_dir)
    if all_.get(step_id) != rec:
        all_[step_id] = rec
        write_atomic(Path(round_dir) / FILE, json.dumps(all_, indent=1) + "\n")


def view(rec):
    """A record for the dashboard: outcome, and each output as {name, value (clipped), reused}."""
    if not rec:
        return {"outcome": "", "outputs": []}
    return {"outcome": rec.get("outcome", ""),
            "outputs": [{"name": k, "value": v if len(v) <= CLIP else v[:CLIP] + "…", "reused": bool(rec.get("reused"))}
                        for k, v in (rec.get("outputs") or {}).items()]}
