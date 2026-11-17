"""Settings loading: defaults, the repo's files, and the main checkout.

Settings are always read from the *main checkout*, never from a task worktree. They live in files, each with one owner:

    .claude/epi.md                           the `global` tab (repo-wide), written by people
    .claude/workflows/<pack>/settings.md     the `stage` and `agent` tabs, and the pack's rules for agents (the body)
    .claude/workflows/<pack>/learnings.md    Learnings and Answers, written by the retro

Every setting is <tab>.<section>.<setting>. The engine owns the `global` tab and a template for the
`stage` tab (a section per stage of the workflow) and the `agent` tab (a section per agent or skill the
workflow uses). The workflow adds its own settings and defaults in its settings.schema.json.
"""

import copy
import json
import os
import subprocess
from pathlib import Path

from . import schema, workflow, yamlish

DEFAULTS = {
    "global": {
        "workflow": {"name": "sdlc"},
        "pipeline": {"critical_thinking": 1,     # 0 = follow instructions as given ... 5 = challenge everything
                     "judge_model": "haiku",
                     "agent_attempts": 2,         # runs of an agent for one step before the task fails
                     "nits_block": False},
        "learning": {"mode": "auto", "on": ["helped", "failed"]},
        "repo": {"base_branch": "main", "branch_prefix": "epi/", "branch_name": "{prefix}task-{id}",
                 "work_dir": ".epi", "worktree_dir": None},     # worktree_dir computed: ../<repo>.epi-worktrees
        "command": {"setup": None, "timeout": 1800},             # the workflow adds its own (lint, build, test); any name is allowed
        "board": {"adapter": "files"},
        "budget": {"per_task": 2_000_000, "per_month": 50_000_000, "orchestrate": 200_000,
                   "on_exceed": "escalate", "warn_at": 0.8,
                   "context_window": 200_000},     # tokens one agent's context holds, for the context fill per step
        "dashboard": {"stall_after": 10, "activity": 30},        # minutes
        "wait": {"interval": 60, "max_hours": 12},
        "guardrail": {
            "protected_paths": [".claude/epi.md", ".claude/workflows/**", ".claude/agents/**", ".claude/principles/**",
                                "CLAUDE.md", ".github/workflows/**", "REVIEW.md", "review-*.md"],
            "blocked_commands": ["git push --force", "git push -f", "git reset --hard",
                                 "git push origin {base}", "git checkout {base}", "git switch {base}"]},
        "principle": {"list": []},               # paths of .claude/principles/<name>.md; resolved to {name: spec}
        "human": {"approver": []},              # <role>: the people who may sign off as that role (empty = anyone)
    },
}
# What every stage and every agent has; the workflow overrides some defaults (stage_defaults, agent_defaults).
STAGE_DEFAULTS = {"rounds": 3, "budget_tokens": None, "requirements": []}
AGENT_DEFAULTS = {"model": None, "critical_thinking": None, "effort": "medium", "recheck": "threads", "writes": "output", "paths": []}
OPEN_SECTIONS = {("global", "command"): "string", ("global", "human"): "list"}   # any name is allowed in these

PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
EPI_FILE = ".claude/epi.md"
PLACES = {"global": "epi.md", "stage": "settings.md", "agent": "settings.md"}     # the file each tab is written in


class ConfigError(ValueError):
    pass


def git(*args, cwd=None, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def main_checkout(start=None):
    """Root of the main working tree, even when called from a linked worktree."""
    start = start or os.getcwd()
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=start)
    return Path(common).parent


def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "list":
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# ------------------------------------------------------------------ agents --

def plugin_name():
    return json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text())["name"]


def agent_source(root, name, pack):
    """(origin, qualified name, path) of the agent `name` a step of workflow `pack` uses. Looked up in order:
    the pack's own copy `.claude/agents/<pack>-<name>.md`, a repo agent `.claude/agents/<name>.md`, the plugin's
    `agents/<name>.md`. (None, None, None) when there is none."""
    mine = Path(root) / ".claude" / "agents"
    for origin, qname, p in (("pack", f"{pack}-{name}", mine / f"{pack}-{name}.md"),
                             ("repo", name, mine / f"{name}.md"),
                             ("plugin", f"{plugin_name()}:{name}", PLUGIN_ROOT / "agents" / f"{name}.md")):
        if p.exists():
            return origin, qname, p
    return None, None, None


def agent_file(root, name, pack=None):
    """(qualified name, path) of the agent `name`, or (None, None)."""
    _, qname, p = agent_source(root, name, pack or DEFAULTS["global"]["workflow"]["name"])
    return qname, p


def step_name(cfg, agent_type):
    """The step name (the key of `agent.<name>.*`) of a running subagent's type: `<plugin>:<name>`, `<pack>-<name>`
    or a repo agent's own name."""
    name = agent_type.split(":", 1)[-1]
    pack = cfg.workflow_name + "-"
    return name[len(pack):] if name.startswith(pack) and name[len(pack):] in cfg.data["agent"] else name


def agent_model(root, name, pack=None):
    _, p = agent_file(root, name, pack)
    if not p:
        return None
    try:
        return yamlish.parse_frontmatter(p.read_text())[0].get("model")
    except yamlish.YamlError:
        return None


def agents_of(stages):
    """Names of the agents and skills a workflow uses (in order)."""
    out = [s for st in stages.values() for _, s in workflow.stage_steps(st)]
    names = []
    for s in out:
        if workflow.kind_of(s) in ("agent", "skill") and workflow.name_of(s) not in names:
            names.append(workflow.name_of(s))
    return names


# ---------------------------------------------------------------- defaults --

def command_names(wf_name, raw):
    names = {f["path"].split(".")[2] for t in schema.workflow_schema(wf_name)["tabs"] for s in t["sections"]
             for f in s.get("fields", []) if f["path"].startswith("global.command.")}
    names |= {k for k in (((raw or {}).get("global") or {}).get("command") or {}) if k not in ("setup", "timeout")}
    return names


def defaults(wf_name, stage_ids, agents):
    """Engine defaults + the workflow's, for this workflow's stages and agents."""
    ps = schema.workflow_schema(wf_name)
    d = copy.deepcopy(DEFAULTS)
    d["stage"] = {s: {**STAGE_DEFAULTS, **(ps.get("stage_defaults") or {}).get(s, {})} for s in stage_ids}
    d["agent"] = {a: {**AGENT_DEFAULTS, **(ps.get("agent_defaults") or {}).get(a, {})} for a in agents}
    for t in ps["tabs"]:
        for s in t["sections"]:
            for f in s.get("fields", []):
                if "default" in f:
                    cur = d
                    parts = f["path"].split(".")
                    for k in parts[:-1]:
                        cur = cur.setdefault(k, {})
                    cur[parts[-1]] = copy.deepcopy(f["default"])
    return d


def default_for(path, wf_name="sdlc"):
    """The default of one setting path, for the stages and agents of `wf_name`'s workflow."""
    wf = workflow.load(wf_name, command_names(wf_name, {}))
    d = defaults(wf_name, list(wf["stages"]), agents_of(wf["stages"]))
    cur = d
    for k in path.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return KeyError
        cur = cur[k]
    return cur


class Config:
    def __init__(self, root, data, rules, raw, warnings=None, lenient=False, learnings=""):
        self.warnings = list(warnings or [])
        self.principle_problems = []  # principle files that could not be used: [{name, file, error}]
        self.lenient = lenient       # the dashboard opens even when something is missing
        self.root = Path(root)
        self.data = data
        self.rules = rules          # the pack's rules for agents: the body of its settings.md
        self.learn_text = learnings  # the pack's learnings.md
        self.raw = raw              # overrides only, as written in epi.md and settings.md
        self.workflow_name = data["global"]["workflow"]["name"]
        self.workflow = None        # the workflow's workflow.yaml as written
        self.disabled = []          # (stage, phase, step id) the workflow leaves out with `enabled: false`
        self.stages = {}            # the effective stages: steps with `enabled: false` left out
        self.schema = None          # the concrete settings schema for this workflow
        self.defaults = None

    def __getitem__(self, k):
        return self.data[k]

    def v(self, path):
        """A setting by its <tab>.<section>.<setting> path."""
        cur = self.data
        for k in path.split("."):
            cur = cur[k]
        return cur

    def agent(self, name):
        return self.data["agent"].get(name) or {**AGENT_DEFAULTS}

    def model(self, name):
        """The model for an agent or skill: its setting, else the agent file's own, else none (inherit).
        'judge' is the cheap model for classifying findings and retros."""
        if name == "judge":
            return self.v("global.pipeline.judge_model")
        return self.agent(name).get("model") or agent_model(self.root, name, self.workflow_name)

    def critical_thinking(self, name):
        own = self.agent(name).get("critical_thinking")
        return self.v("global.pipeline.critical_thinking") if own is None else own

    def command(self, name):
        return self.data["global"]["command"].get(name)

    def stage_budget(self, stage):
        """Token cap for a stage or `orchestrate`; None = the task budget."""
        if stage == "orchestrate":
            return self.v("global.budget.orchestrate")
        return (self.data["stage"].get(stage) or {}).get("budget_tokens")

    @property
    def work_dir(self):
        return self.root / self.v("global.repo.work_dir")

    @property
    def worktree_dir(self):
        wd = self.v("global.repo.worktree_dir")
        return Path(wd) if wd and os.path.isabs(wd) else (
            (self.root / wd).resolve() if wd else self.root.parent / f"{self.root.name}.epi-worktrees")

    @property
    def learnings_path(self):
        return learnings_path(self.root, self.workflow_name)

    def learning_sections(self):
        """learnings.md split by '## ' headings (Learnings, Answers)."""
        sections, cur, buf = {}, None, []
        for line in self.learn_text.splitlines():
            if line.startswith("## "):
                if cur:
                    sections[cur] = "\n".join(buf).strip()
                cur, buf = line[3:].strip(), []
            elif cur:
                buf.append(line)
        if cur:
            sections[cur] = "\n".join(buf).strip()
        return sections


def detect_host(root):
    try:
        url = git("remote", "get-url", "origin", cwd=root, check=False)
    except Exception:
        url = ""
    from shutil import which
    return "github" if "github.com" in url and which("gh") else "local"


def check_keys(raw, d):
    """Every setting is <tab>.<section>.<setting>. Anything else is a mistake, not silently ignored."""
    errs = []
    for tab, secs in raw.items():
        if tab not in d:
            errs.append(f"unknown setting '{tab}': settings are <tab>.<section>.<setting>, "
                        f"and the tabs are {', '.join(d)}")
        elif not isinstance(secs, dict):
            errs.append(f"'{tab}' must hold sections (<tab>.<section>.<setting>)")
        else:
            for sec, settings in secs.items():
                if sec not in d[tab]:
                    what = {"stage": "stage", "agent": "agent"}.get(tab)
                    errs.append(f"unknown {what} '{sec}' in {tab}.{sec}: the {what}s are {', '.join(d[tab])}"
                                + (" (an agent must be a step of the workflow or have a file in .claude/agents/)" if what == "agent" else "")
                                if what else f"unknown section '{tab}.{sec}': the sections of {tab} are {', '.join(d[tab])}")
                elif not isinstance(settings, dict):
                    errs.append(f"'{tab}.{sec}' must hold settings (<tab>.<section>.<setting>)")
                elif (tab, sec) not in OPEN_SECTIONS and sec != "host":
                    errs += [f"unknown setting '{tab}.{sec}.{k}': {tab}.{sec} has {', '.join(d[tab][sec])}"
                             for k in settings if k not in d[tab][sec]]
    return errs


def _open_check(raw, d, errs):
    for (tab, sec), kind in OPEN_SECTIONS.items():
        for k, v in (((raw.get(tab) or {}).get(sec)) or {}).items():
            if k in d[tab][sec]:
                continue                                  # a declared setting: the schema checks it
            ok = (v is None or isinstance(v, str)) if kind == "string" else isinstance(v, list) and all(isinstance(x, str) for x in v)
            if not ok:
                errs.append(f"{tab}.{sec}.{k} must be {'text' if kind == 'string' else 'a list of text'}")


def pack_dir(root, pack):
    return Path(root) / ".claude" / "workflows" / pack


def settings_path(root, pack):
    return pack_dir(root, pack) / "settings.md"


def learnings_path(root, pack):
    return pack_dir(root, pack) / "learnings.md"


def from_raw(root, raw, rules="", lenient=False, learnings=""):
    """Defaults + the repo's overrides -> a validated Config. Raises ConfigError."""
    root = Path(root)
    workflow.use_repo(root)
    wf_name = ((raw.get("global") or {}).get("workflow") or {}).get("name") or DEFAULTS["global"]["workflow"]["name"]
    if not isinstance(wf_name, str):
        raise ConfigError("global.workflow.name must be a workflow name")
    try:
        cmds = command_names(wf_name, raw)
        wf = workflow.load(wf_name, cmds)
    except workflow.WorkflowError as e:
        raise ConfigError(str(e))
    names = agents_of(wf["stages"])
    names += [a for a in (raw.get("agent") or {}) if a not in names and agent_file(root, a, wf_name)[1]]
    d = defaults(wf_name, list(wf["stages"]), names)
    errs = check_keys(raw, d)
    _open_check(raw, d, errs)
    if errs:
        raise ConfigError("settings are invalid:\n  - " + "\n  - ".join(errs))
    over = {t: {sec: dict(v) for sec, v in secs.items()} for t, secs in raw.items()}
    plist = (over.get("global") or {}).get("principle", {}).pop("list", None)
    if plist is not None and not isinstance(plist, list):
        raise ConfigError("global.principle.list must be a list of file paths")
    data = deep_merge(d, over)
    bad = [k for k, v in data["global"]["command"].items() if isinstance(v, str) and "${{" in v]
    if bad:
        raise ConfigError("settings are invalid:\n  - " + "\n  - ".join(
            f"global.command.{k}: `${{{{ }}}}` is not allowed in a command; give the step `env:` and read it as $NAME" for k in bad))
    from . import principles
    data["global"]["principle"]["list"], warnings, pprob = principles.load_list(root, plist)
    # Safety lists only ever grow: a repo's entries are added to the defaults, never replace them.
    for key in ("protected_paths", "blocked_commands"):
        extra = ((raw.get("global") or {}).get("guardrail") or {}).get(key) or []
        if not isinstance(extra, list):
            raise ConfigError(f"global.guardrail.{key} must be a list")
        data["global"]["guardrail"][key] = list(dict.fromkeys(DEFAULTS["global"]["guardrail"][key] + extra))
    if "host" in data["global"] and not data["global"]["host"].get("adapter"):     # a workflow setting
        data["global"]["host"]["adapter"] = detect_host(root)
    cfg = Config(root, data, rules, raw, warnings, lenient, learnings)
    cfg.principle_problems = pprob
    cfg.workflow, cfg.defaults = wf, d
    cfg.schema = schema.build(wf_name, list(wf["stages"]), list(d["agent"]))
    cfg.stages, cfg.disabled = workflow.effective(wf), workflow.disabled(wf)
    validate(cfg)
    return cfg


def _read(path):
    return path.read_text() if path.exists() else ""


def pack_name(epi_text):
    """The workflow a repo runs by default: `global.workflow.name` in its epi.md."""
    try:
        raw, _ = yamlish.parse_frontmatter(epi_text)
    except yamlish.YamlError:
        return DEFAULTS["global"]["workflow"]["name"]
    name = ((raw.get("global") or {}).get("workflow") or {}).get("name")
    return name if isinstance(name, str) and name else DEFAULTS["global"]["workflow"]["name"]


def read_sources(root):
    """(epi.md, the pack's settings.md, the pack's learnings.md) as text; '' for a missing file."""
    root = Path(root)
    epi = _read(root / EPI_FILE)
    pack = pack_name(epi)
    return epi, _read(settings_path(root, pack)), _read(learnings_path(root, pack))


def from_texts(root, epi, settings, learnings="", lenient=False):
    """A Config from the text of the three files. A setting in the wrong file is an error."""
    try:
        epi_raw, _ = yamlish.parse_frontmatter(epi)
        set_raw, rules = yamlish.parse_frontmatter(settings)
    except yamlish.YamlError as e:
        raise ConfigError(f"settings are invalid:\n  - {e}")
    errs = [f"'{t}' settings belong in the workflow's settings.md, not in epi.md" for t in epi_raw if t in ("stage", "agent")]
    errs += [f"'{t}' settings belong in epi.md, not in the workflow's settings.md" for t in set_raw if t == "global"]
    if errs:
        raise ConfigError("settings are invalid:\n  - " + "\n  - ".join(errs))
    return from_raw(root, {**epi_raw, **set_raw}, rules, lenient, learnings)


def load(root=None, lenient=False):
    root = Path(root) if root else main_checkout()
    return from_texts(root, *read_sources(root), lenient=lenient)


def validate(cfg):
    """Types, allowed values and ranges come from the schema; what's left here are rules that span
    settings (budgets, agents that must exist)."""
    errs = []
    d = cfg.data
    for path, f in schema.fields(cfg.schema).items():
        default = cfg.defaults
        for k in path.split("."):
            default = default.get(k) if isinstance(default, dict) else None
        bad = schema.check(path, f, cfg.v(path), nullable=default is None)
        if bad:
            errs.append(bad)
    for sid, st in cfg.stages.items():
        for sub in workflow.PHASES:
            for s in st[sub]:
                if workflow.kind_of(s) == "agent" and not agent_file(cfg.root, workflow.name_of(s), cfg.workflow_name)[1]:
                    msg = (f"stage {sid}: agent '{workflow.name_of(s)}' has no file (.claude/agents/{cfg.workflow_name}-"
                           f"{workflow.name_of(s)}.md or {workflow.name_of(s)}.md in the repo, or agents/ in the plugin)")
                    cfg.warnings.append(msg) if cfg.lenient else errs.append(msg)
    budget = d["global"]["budget"]
    over = [s for s in d["stage"]       # only budgets the repo set itself
            if isinstance((((cfg.raw.get("stage") or {}).get(s)) or {}).get("budget_tokens"), int)
            and d["stage"][s]["budget_tokens"] > budget["per_task"]]
    if over:
        errs.append("stage budgets above global.budget.per_task: " + ", ".join(f"stage.{s}.budget_tokens" for s in over))
    if errs:
        raise ConfigError("settings are invalid:\n  - " + "\n  - ".join(errs))
