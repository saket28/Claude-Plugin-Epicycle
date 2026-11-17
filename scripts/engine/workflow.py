"""Workflows: the workflow's `workflow.yaml`, its validator, and the effective stages.

A workflow is stages that run in order (like the jobs of a GitHub Actions workflow). A stage's phases, each a list
of steps (any may be empty):

    plan                      once, before the loop
    do -> verify -> review    the loop: one pass is a round, repeated until Verify and Review pass
    finalize                  once the loop passed: prepares what a person signs off (a PR, CI); a failure loops back
    finish                    starts with the person's sign-off (`signoff: {by: <role>}`), then what cannot be undone

Verify holds commands (lint, build, tests); Review (agents, skills) runs only when Verify passed. The sign-off is
asked only after Finalize passed. A step or a sign-off with `enabled: false` is left out. A step is one of five kinds,
picked by its `uses` prefix or by `run`:

    uses: agent/<name>   a subagent        uses: skill/<name>   a skill, run headless
    uses: human/<role>   waits for a person   uses: wait/<event>  waits for an event (parsed only)
    run: <name>          a repo command (global.command.<name>) or a workflow script (<workflow>/scripts)

Steps run in the order written: a step may read (`if`, `env`, `with`) any step above it, in its stage or in an
earlier stage: `steps.<id>.outcome | verdict | outputs.<name>`, `stages.<stage>.steps.<id>...`. A step lists the
outputs it publishes in `outputs`; reading an undeclared output, a step below or an unknown one is an error here.
`${{ expr }}` is allowed only in the values of `env` and `with`, never in a command, so that a value an agent wrote
can not become shell code.

Workflows live in the plugin's `workflows/<name>/`, or in a repo's own `.claude/workflows/<name>/` (`use_repo`),
which wins over the plugin's: `epi install <name>` copies the plugin's into the repo, and the repo owns the copy.
A workflow has a `version` (x.y.z), which `epi upgrade` compares.
"""

import re
from pathlib import Path

from . import rules, yamlish

PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOWS = PLUGIN_ROOT / "workflows"
PHASES = ("plan", "do", "verify", "review", "finalize", "signoff", "finish")
LISTS = ("plan", "do", "verify", "review", "finalize", "finish")        # the phases written as lists of steps
LOOP = ("do", "verify", "review")
STAGE_KEYS = {"name", *PHASES}
SIGNOFF_KEYS = {"by", "name", "doing", "with", "enabled"}
VERSION = re.compile(r"\d+\.\d+\.\d+")
SIGNOFF_ID = "signoff"
STEP_KEYS = {"uses", "run", "id", "name", "doing", "with", "if", "continue-on-error", "timeout-minutes",
             "always_rerun", "on_fail", "enabled", "outputs", "env"}
NAME = re.compile(r"\w+")
RESERVED = ("verdict", "important", "nit", "engine", "head", "output", "round", "worktree", "task")   # report fields of the engine
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
TEMPLATED = ("env", "with")            # the only keys whose values may hold `${{ }}`
KINDS = ("agent", "skill", "human", "wait")
ON_FAIL = ("retry", "do", "human")
REPO_DIRS = []            # a repo's own .claude/workflows, searched before the plugin's
TRIGGERS_NOT_YET = ("schedule", "board", "event")
WORKSPACES = ("git-worktree", "none")


class WorkflowError(ValueError):
    pass


def use_repo(root):
    """Let the repo at `root` bring its own workflows (`.claude/workflows/<name>/`)."""
    REPO_DIRS[:] = [Path(root) / ".claude" / "workflows"]


def workflow_dir(wf_name):
    for d in REPO_DIRS:
        if (d / wf_name / "workflow.yaml").exists():
            return d / wf_name
    return WORKFLOWS / wf_name


def scripts(wf_name):
    """Names of the workflow's scripts: workflows/<name>/scripts/<name>.py, with `_` written as `-`."""
    d = workflow_dir(wf_name) / "scripts"
    return sorted(p.stem.replace("_", "-") for p in d.glob("*.py") if not p.name.startswith("_")) if d.exists() else []


def script_path(wf_name, name):
    return workflow_dir(wf_name) / "scripts" / (name.replace("-", "_") + ".py")


def kind_of(step):
    return "run" if "run" in step else step["uses"].split("/", 1)[0]


def name_of(step):
    return step["run"] if "run" in step else step["uses"].split("/", 1)[1]


def step_id(step):
    return step.get("id") or name_of(step)


def signoff_step(stage):
    """The stage's sign-off as a human step (id `signoff`), or None when the stage declares none."""
    so = stage.get("signoff")
    if not isinstance(so, dict) or not isinstance(so.get("by"), str):
        return None
    step = {"uses": f"human/{so['by']}", "id": SIGNOFF_ID, "name": so.get("name") or "Sign-off",
            "doing": so.get("doing") or "Waiting for your sign-off"}
    for k in ("with", "enabled"):
        if so.get(k) is not None:
            step[k] = so[k]
    return step


def stage_steps(stage):
    """(phase, step) of a stage, the sign-off included: as written (`signoff` a map) or effective (a list)."""
    out = [(sub, s) for sub in LISTS for s in stage.get(sub) or []]
    if isinstance(stage.get("signoff"), list):
        return out + [("signoff", s) for s in stage["signoff"]]
    so = signoff_step(stage)
    return out + ([("signoff", so)] if so else [])


def ordered(stage):
    """(phase, step) of a stage in the order they run, the sign-off after Finalize (steps as written)."""
    so, out = signoff_step(stage), []
    for sub in PHASES:
        if sub == "signoff":
            out += [("signoff", so)] if so else []
        else:
            out += [(sub, s) for s in stage.get(sub) or [] if isinstance(s, dict)]
    return out


def _safe_id(step):
    try:
        i = step_id(step)
    except (KeyError, AttributeError, IndexError):
        return None
    return i if isinstance(i, str) else None


class Scope:
    """What a step may read: the steps above it in its stage (`same`), the steps of earlier stages (`prior`),
    and every step's id (`ids`), to tell a step that runs later from one that does not exist."""

    def __init__(self, sid, ids, same, prior):
        self.sid, self.ids, self.same, self.prior = sid, ids, same, prior


def check_refs(where, what, found, scope):
    errs = []
    for r in found:
        if r[0] == "steps":
            _, x, field, name = r
            target = scope.same.get(x)
            if target is None:
                errs.append(f"{where}: {what} reads steps.{x}, which "
                            + ("runs later: a step reads only the steps above it" if x in scope.ids.get(scope.sid, ())
                               else f"is not a step of stage {scope.sid}"))
                continue
        else:
            _, sid, x, field, name = r
            if sid == scope.sid:
                errs.append(f"{where}: {what} reads stages.{sid}, this stage: write steps.{x}")
                continue
            if sid not in scope.ids:
                errs.append(f"{where}: {what} reads stage '{sid}', which does not exist")
                continue
            target = (scope.prior.get(sid) or {}).get(x)
            if target is None:
                errs.append(f"{where}: {what} reads stages.{sid}.steps.{x}, which "
                            + ("runs later" if sid not in scope.prior or x in scope.ids[sid] else f"is not a step of stage {sid}"))
                continue
        if field == "outputs" and name not in (target.get("outputs") or []):
            have = ", ".join(target.get("outputs") or []) or "none"
            errs.append(f"{where}: {what} reads output '{name}' of step '{_safe_id(target)}', which declares: {have}")
    return errs


def reads(step):
    """The ids of the steps of its own stage that a step reads in its `if`, `env` and `with`."""
    texts = [step["if"]] if isinstance(step.get("if"), str) else []
    for key in TEMPLATED:
        for v in (step.get(key) or {}).values():
            texts += rules.templates(v) if isinstance(v, str) else []
    found = set()
    for t in texts:
        found |= {r[1] for r in rules.refs(t) if r[0] == "steps"}
    return found


def check_expr(where, what, text, scope):
    """Problems of one expression (`if`, or the inside of a `${{ }}`)."""
    try:
        return check_refs(where, what, rules.refs(text), scope)
    except rules.RuleError as e:
        return [f"{where}: {what}: {e}"]


def check_templates(where, s, scope):
    errs = []
    for key in s:
        if key in TEMPLATED or key == "if" or not isinstance(s[key], (str, list, dict)):
            continue
        if "${{" in yamlish.dump_inline(s[key]):
            errs.append(f"{where}: `${{{{ }}}}` is only for the values of `env` and `with`, not `{key}` "
                        "(a command reads a value from its environment)")
    for key in TEMPLATED:
        if not isinstance(s.get(key), dict):
            continue
        for k, v in s[key].items():
            if not isinstance(v, str):
                continue
            try:
                inner = rules.templates(v)
            except rules.RuleError as e:
                errs.append(f"{where}: {key}.{k}: {e}")
                continue
            for expr in inner:
                errs += check_expr(where, f"{key}.{k}", expr, scope)
    return errs


def check_step(where, s, sub, stage, stage_ids, commands, script_names, scope=None):
    """The problems of one step (a list of messages)."""
    if not isinstance(s, dict):
        return [f"{where}: a step is a map with `uses` or `run`"]
    errs = [f"{where}: unknown key '{k}' (steps have {', '.join(sorted(STEP_KEYS))})" for k in s if k not in STEP_KEYS]
    if ("uses" in s) == ("run" in s):
        return errs + [f"{where}: needs exactly one of `uses` or `run`"]
    if "run" in s:
        name = s["run"]
        if not isinstance(name, str) or not name.strip():
            errs.append(f"{where}: `run` needs a command name")
        elif name not in commands and name not in script_names:
            errs.append(f"{where}: `run: {name}` is neither a command (global.command.{name}) nor a workflow script "
                        f"(scripts: {', '.join(script_names) or 'none'})")
    else:
        kind, _, name = str(s["uses"]).partition("/")
        if kind not in KINDS or not name:
            errs.append(f"{where}: `uses` must be {' | '.join(k + '/<name>' for k in KINDS)}")
    kind = kind_of(s) if not errs else None
    if "enabled" in s and not isinstance(s["enabled"], bool):
        errs.append(f"{where}: `enabled` is true or false")
    scope = scope or Scope("", {}, {}, {})
    if "if" in s:
        errs += check_expr(where, "if", s["if"], scope)
    if "with" in s and not isinstance(s["with"], dict):
        errs.append(f"{where}: `with` must be a map")
    if "outputs" in s:
        o = s["outputs"]
        if not isinstance(o, list) or not all(isinstance(x, str) and NAME.fullmatch(x) for x in o) or len(set(o)) != len(o):
            errs.append(f"{where}: `outputs` is a list of distinct names (letters, digits, _), e.g. [risk, files]")
        else:
            errs += [f"{where}: '{x}' is a field of the engine's reports, name the output something else" for x in o if x in RESERVED]
    if "env" in s:
        e = s["env"]
        if kind != "run":
            errs.append(f"{where}: `env` is for run steps (commands and workflow scripts)")
        elif not isinstance(e, dict) or not all(isinstance(k, str) and ENV_NAME.fullmatch(k) for k in e) or \
                not all(isinstance(v, (str, int, float, bool)) for v in e.values()):
            errs.append(f"{where}: `env` is a map of NAME: value, e.g. {{ CHANNEL: \"#ops\" }}")
        else:
            errs += [f"{where}: env {k}: EPI_ names are the engine's" for k in e if k.startswith("EPI_")]
    errs += check_templates(where, s, scope)
    via = s["with"].get("via") if isinstance(s.get("with"), dict) else None
    if via and kind != "human":
        errs.append(f"{where}: `with.via` is only for human steps")
    elif via and f"via-{via}" not in script_names:
        errs.append(f"{where}: `via: {via}` needs the workflow script via-{via}")
    for key in ("continue-on-error", "always_rerun"):
        if key in s and (not isinstance(s[key], bool) or sub not in ("verify", "review")):
            errs.append(f"{where}: `{key}` is a true/false option of verify and review steps")
    if "timeout-minutes" in s:
        t = s["timeout-minutes"]
        if kind not in ("run", "wait") or not isinstance(t, int) or isinstance(t, bool) or t < 1:
            errs.append(f"{where}: `timeout-minutes` is a whole number for run and wait steps")
    if "on_fail" in s:
        if sub != "finish" or s["on_fail"] not in ON_FAIL:
            errs.append(f"{where}: `on_fail` is for finish steps: {' | '.join(ON_FAIL)}")
        elif s["on_fail"] == "do" and not stage.get("do"):
            errs.append(f"{where}: `on_fail: do` needs do step in the stage")
    return errs


def validate(wf, wf_name="sdlc", commands=(), agents_dir=None, script_names=None):
    """Every problem in a parsed workflow, as a list of messages (empty = fine)."""
    errs = []
    if not isinstance(wf, dict):
        return ["workflow.yaml must be a map"]
    for k in wf:
        if k not in ("name", "version", "workspace", "facts", "on", "stages"):
            errs.append(f"unknown workflow key '{k}'")
    if not isinstance(wf.get("name"), str):
        errs.append("name is required")
    if "version" in wf and not (isinstance(wf["version"], str) and VERSION.fullmatch(wf["version"])):
        errs.append("version must be x.y.z, e.g. 0.7.0")
    if wf.get("workspace") not in WORKSPACES:
        errs.append(f"workspace must be one of: {', '.join(WORKSPACES)}")
    on = wf.get("on")
    if not isinstance(on, dict) or not on:
        errs.append("`on` needs a trigger: on: {manual: {inputs: {request: string}}}")
    else:
        for k in on:
            if k in TRIGGERS_NOT_YET:
                errs.append(f"on.{k} is not supported yet (only `manual`)")
            elif k != "manual":
                errs.append(f"unknown trigger 'on.{k}'")
        manual = on.get("manual")
        if "manual" in on and manual is not None and not isinstance(manual, dict):
            errs.append("on.manual must be a map")
        elif isinstance(manual, dict) and not isinstance(manual.get("inputs", {}), dict):
            errs.append("on.manual.inputs must be a map of name: type")
    stages = wf.get("stages")
    if not isinstance(stages, dict) or not stages:
        return errs + ["stages: at least one stage is required"]
    ids = list(stages)
    if wf.get("facts") is not None and wf["facts"] not in stages:
        errs.append(f"facts: '{wf['facts']}' is not a stage")
    names = scripts(wf_name) if script_names is None else sorted(script_names)
    all_ids = {sid: {i for _, x in ordered(st) if (i := _safe_id(x))} for sid, st in stages.items() if isinstance(st, dict)}
    prior = {}
    for sid, st in stages.items():
        w = f"stage {sid}"
        if not isinstance(st, dict):
            errs.append(f"{w}: must be a map")
            continue
        errs += [f"{w}: unknown key '{k}' (stages have {', '.join(sorted(STAGE_KEYS))})" for k in st if k not in STAGE_KEYS]
        if not any(st.get(sub) for sub in PHASES):
            errs.append(f"{w}: every phase is empty")
        seen = {SIGNOFF_ID} if "signoff" in st else set()
        if "signoff" in st:
            so = st["signoff"]
            if not isinstance(so, dict) or not isinstance(so.get("by"), str) or not so["by"].strip():
                errs.append(f"{w}.signoff: a map naming the role that signs off, e.g. signoff: {{by: approver}}")
            else:
                errs += [f"{w}.signoff: unknown key '{k}' (signoff has {', '.join(sorted(SIGNOFF_KEYS))})"
                         for k in so if k not in SIGNOFF_KEYS]
        for sub in LISTS:
            items = st.get(sub)
            if items is not None and not isinstance(items, list):
                errs.append(f"{w}.{sub}: must be a list of steps")
        same = {}
        for sub, s in ordered(st):
            if sub == "signoff":
                where, n = f"{w}.signoff", None
            else:
                n = next(i for i, x in enumerate(st[sub]) if x is s)
                where = f"{w}.{sub}[{n}]"
            bad = check_step(where, s, sub, st, ids, commands, names, Scope(sid, all_ids, same, prior))
            errs += bad
            if not bad:
                if sub != "signoff" and step_id(s) in seen:
                    errs.append(f"{where}: id '{step_id(s)}' is used twice; give one an `id`")
                seen.add(step_id(s))
                same[step_id(s)] = s
        prior[sid] = same
        for sub in LISTS:                      # steps that are not maps: the same message as before
            for n, s in enumerate(st.get(sub) or [] if isinstance(st.get(sub), list) else []):
                if not isinstance(s, dict):
                    errs.append(f"{w}.{sub}[{n}]: a step is a map with `uses` or `run`")
    if wf.get("facts") in stages and not (stages[wf["facts"]] or {}).get("do"):
        errs.append(f"facts: stage '{wf['facts']}' has no do step to produce facts")
    return errs


def load(wf_name="sdlc", commands=()):
    path = workflow_dir(wf_name) / "workflow.yaml"
    if not path.exists():
        raise WorkflowError(f"no workflow '{wf_name}' (looked for {path})")
    try:
        wf = yamlish.loads(path.read_text())
    except yamlish.YamlError as e:
        raise WorkflowError(f"{path.name}: {e}")
    errs = validate(wf, wf_name, commands)
    if errs:
        raise WorkflowError(f"{path.name} is invalid:\n  - " + "\n  - ".join(errs))
    return wf


def effective(wf):
    """The workflow as it runs: steps and sign-offs with `enabled: false` left out. Every step gets an `_id`.
    Returns {id: stage}."""
    out = {}
    for sid, st in wf["stages"].items():
        new = {k: v for k, v in st.items() if k not in PHASES}
        so = signoff_step(st)
        new["signoff"] = [dict(so, _id=SIGNOFF_ID)] if so and so.get("enabled", True) else []
        for sub in LISTS:
            new[sub] = [dict(s, _id=step_id(s)) for s in st.get(sub) or [] if s.get("enabled", True)]
        out[sid] = new
    return out


def disabled(wf):
    """(stage, phase, step id) of every step the workflow leaves out with `enabled: false`."""
    return [(sid, sub, step_id(s)) for sid, st in wf["stages"].items()
            for sub, s in stage_steps(st) if s.get("enabled") is False]
