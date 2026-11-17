"""Edit the repo's `workflow.yaml` line by line.

The file is written one step per line (`- { uses: agent/x, ... }`), so an edit is a line edit: comments and
the style stay. Operations (a list, applied in order):

    {"op": "add", "stage", "phase", "kind": agent|skill|run, "name", optional: "if", "id", "outputs": [names],
     "env": {NAME: value}}
    {"op": "remove", "stage", "phase", "id"}
    {"op": "toggle", "stage", "phase", "id", "enabled": true|false}      phase "signoff" for the stage's sign-off
    {"op": "move", "stage", "phase", "id", "by": -1|1}                   earlier or later in its phase

The result is validated (workflow rules, agent files) before a preview or a save is returned, and a save is
refused when the file changed on disk since the hash the caller holds.
"""

import re

from . import config, configedit, pack as pack_mod, workflow, yamlish
from .configedit import EditError, file_hash

STEP = re.compile(r"^(\s*)- (\{.*\})\s*(#.*)?$")
KINDS = ("agent", "skill", "run")
NAME = re.compile(r"[\w.-]+")


def path(cfg):
    return config.pack_dir(cfg.root, cfg.workflow_name) / "workflow.yaml"


def hash_of(cfg):
    p = path(cfg)
    return file_hash(p.read_text() if p.exists() else "")


def _end(lines, i, indent):
    return configedit._block_end(lines, i, indent)


def _stage(lines, sid):
    """(start, end) of the stage's lines."""
    root = next((i for i, l in enumerate(lines) if re.match(r"^stages\s*:", l)), None)
    if root is None:
        raise EditError("workflow.yaml has no stages")
    i, _ = configedit._find(lines, sid, 2, root + 1, _end(lines, root, 0))
    if i is None:
        raise EditError(f"no stage '{sid}'")
    return i, _end(lines, i, 2)


def _phase(lines, sid, phase):
    """(header line or None, first line after the phase's block, stage end) for a list phase."""
    i, j = _stage(lines, sid)
    h, _ = configedit._find(lines, phase, 4, i + 1, j)
    return (h, _end(lines, h, 4), j) if h is not None else (None, None, j)


def step_of(line):
    """The step map written on a line, or None."""
    m = STEP.match(line)
    if not m:
        return None
    try:
        return yamlish.loads("s: " + m.group(2))["s"]
    except (yamlish.YamlError, KeyError):
        return None


def _steps(lines, h, e):
    return [n for n in range(h + 1, e) if step_of(lines[n]) is not None]


def _locate(lines, sid, phase, step):
    """Index of the line of step `step` (an id) in the phase; the sign-off line for phase 'signoff'."""
    if phase == "signoff":
        i, j = _stage(lines, sid)
        k, _ = configedit._find(lines, "signoff", 4, i + 1, j)
        if k is None:
            raise EditError(f"stage {sid} has no sign-off")
        return k
    if phase not in workflow.LISTS:
        raise EditError(f"unknown phase '{phase}'")
    h, e, _ = _phase(lines, sid, phase)
    if h is not None:
        for n in _steps(lines, h, e):
            if workflow.step_id(step_of(lines[n])) == step:
                return n
    raise EditError(f"no step '{step}' in {sid}.{phase}")


def _close(line):
    """(index of the closing brace, the line's trailing comment start or len) of a one-line flow map."""
    code = yamlish._strip_comment(line)
    return code.rfind("}"), len(code)


def _set_enabled(line, enabled):
    """Add `enabled: false`, or drop `enabled` (a step is on by default), on a step line; the rest is kept."""
    out = re.sub(r",\s*enabled:\s*(true|false)\s*(?=\})", " ", line)
    out = re.sub(r"(?<=\{)\s*enabled:\s*(true|false)\s*,", " ", out)
    out = re.sub(r"\{\s*enabled:\s*(true|false)\s*\}", "{ }", out)
    if enabled:
        return out
    k, _ = _close(out)
    if k < 0:
        raise EditError("cannot edit that line")
    return out[:k].rstrip() + ", enabled: false " + out[k:]


def _new_line(op):
    kind, name = op.get("kind"), str(op.get("name") or "").strip()
    if kind not in KINDS:
        raise EditError("kind is agent, skill or command")
    if not NAME.fullmatch(name):
        raise EditError("give the name of an agent, skill or command")
    parts = [f"run: {name}" if kind == "run" else f"uses: {kind}/{name}"]
    sid, outs, env = str(op.get("id") or "").strip(), op.get("outputs") or [], op.get("env") or {}
    if sid and not re.fullmatch(r"[\w-]+", sid):
        raise EditError("an id is letters, digits, - and _")
    if not isinstance(outs, list) or not all(isinstance(o, str) and re.fullmatch(r"\w+", o) for o in outs):
        raise EditError("outputs are names: letters, digits and _")
    if not isinstance(env, dict) or not all(isinstance(k, str) and re.fullmatch(r"[A-Za-z_]\w*", k) for k in env):
        raise EditError("env is NAME: value, names in letters, digits and _")
    if sid:
        parts.append(f"id: {sid}")
    if (op.get("if") or "").strip():
        parts.append("if: " + yamlish.dump_inline(" ".join(str(op["if"]).split())))
    if outs:
        parts.append("outputs: " + yamlish.dump_inline(outs))
    if env:
        parts.append("env: " + yamlish.dump_inline({k: str(v) for k, v in env.items()}))
    return "      - { " + ", ".join(parts) + " }"


def _add(lines, op):
    sid, phase = op.get("stage"), op.get("phase")
    if phase not in workflow.LISTS:
        raise EditError(f"unknown phase '{phase}'")
    new = _new_line(op)
    h, e, j = _phase(lines, sid, phase)
    if h is not None:
        at = (_steps(lines, h, e) or [h])[-1] + 1
        lines.insert(at, new)
        return
    i, j = _stage(lines, sid)                      # a phase the stage doesn't have yet: after the last earlier one
    order = [*workflow.PHASES]
    before = [p for p in order[:order.index(phase)]]
    at = i + 1
    for p in before:
        k, _ = configedit._find(lines, p, 4, i + 1, j)
        if k is not None:
            at = max(at, _end(lines, k, 4) if p != "signoff" else k + 1)
    while at > i + 1 and not lines[at - 1].strip():
        at -= 1
    lines[at:at] = [f"    {phase}:", new]


def _remove(lines, op):
    n = _locate(lines, op.get("stage"), op.get("phase"), op.get("id"))
    if op["phase"] == "signoff":
        raise EditError("a sign-off is turned off, not removed")
    h, e, _ = _phase(lines, op["stage"], op["phase"])
    del lines[n]
    if not _steps(lines, h, e - 1):                # last step of the phase: drop its header too
        del lines[h:_end(lines, h, 4)]


def _toggle(lines, op):
    n = _locate(lines, op.get("stage"), op.get("phase"), op.get("id"))
    if not isinstance(op.get("enabled"), bool):
        raise EditError("enabled is true or false")
    lines[n] = _set_enabled(lines[n], op["enabled"])


def _move(lines, op):
    n = _locate(lines, op.get("stage"), op.get("phase"), op.get("id"))
    if op["phase"] == "signoff" or op.get("by") not in (-1, 1):
        raise EditError("move a step by -1 or 1 within its phase")
    h, e, _ = _phase(lines, op["stage"], op["phase"])
    mine = _steps(lines, h, e)
    k = mine.index(n) + op["by"]
    if not 0 <= k < len(mine):
        raise EditError("the step is already there")
    lines[n], lines[mine[k]] = lines[mine[k]], lines[n]


OPS = {"add": _add, "remove": _remove, "toggle": _toggle, "move": _move}


def apply(text, ops):
    """The workflow.yaml text with the operations applied (not validated; see `check`)."""
    lines = text.split("\n")
    trailing = lines[-1] == ""
    if trailing:
        lines.pop()
    for op in ops:
        if not isinstance(op, dict) or op.get("op") not in OPS:
            raise EditError(f"operation must be one of: {', '.join(OPS)}")
        OPS[op["op"]](lines, op)
    return "\n".join(lines) + ("\n" if trailing else "")


def check(cfg, text):
    """Raise EditError unless `text` is a valid workflow.yaml for this repo (rules, existing agent files)."""
    try:
        wf = yamlish.loads(text)
    except yamlish.YamlError as e:
        raise EditError(f"workflow.yaml: {e}")
    errs = workflow.validate(wf, cfg.workflow_name, config.command_names(cfg.workflow_name, cfg.raw))
    if not errs:
        for sid, st in wf["stages"].items():
            for _, s in workflow.stage_steps(st):
                if workflow.kind_of(s) == "agent" and not config.agent_file(cfg.root, workflow.name_of(s), cfg.workflow_name)[1]:
                    errs.append(f"stage {sid}: agent '{workflow.name_of(s)}' has no file in .claude/agents/")
    if errs:
        raise EditError("the workflow would be invalid:\n  - " + "\n  - ".join(errs))


def plan(cfg, ops):
    """{path, before, after, diff} for the operations; raises EditError when the result is not valid."""
    p = path(cfg)
    if not p.exists():
        raise EditError("this repo has no workflow.yaml of its own: run `epi install " + cfg.workflow_name + "`")
    before = p.read_text()
    after = apply(before, ops)
    check(cfg, after)
    rel = p.relative_to(cfg.root).as_posix()
    return {"path": rel, "before": before, "after": after, "diff": pack_mod.diff(rel, before, after)}


def save(cfg, ops, expected_hash):
    """Write the operations to workflow.yaml. Refused when the file changed on disk since `expected_hash`.
    Returns the new hash."""
    done = plan(cfg, ops)
    if expected_hash and file_hash(done["before"]) != expected_hash:
        raise EditError("workflow.yaml changed on disk since you loaded it; reload and try again")
    p = path(cfg)
    tmp = p.with_suffix(".yaml.tmp")
    tmp.write_text(done["after"])
    tmp.replace(p)
    try:
        config.load(cfg.root)
    except config.ConfigError as e:                # something the checks above did not see: put the file back
        p.write_text(done["before"])
        raise EditError(str(e))
    return hash_of(cfg)
