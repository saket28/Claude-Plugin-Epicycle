"""Settings schema: the one place for a setting's type, allowed values, range, help text and where it
appears in the settings page. Validation and the dashboard both read it.

Two files make it: the engine's `templates/epi.schema.json` (global settings, plus a template for
the settings every stage has and every agent has) and the workflow's `settings.schema.json` (installed in the repo
at `.claude/workflows/<name>/`, else the plugin's `workflows/<name>/`)
(its own settings, with `default`s, and per-stage and per-agent default overrides). `build` expands
the stage and agent templates for the workflow's stages and agents into one concrete schema of
`<tab>.<section>.<setting>` fields. Engine defaults live in config.DEFAULTS; a test keeps them in
step with the schema."""

import copy
import json
import string
from functools import lru_cache
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
ENGINE = PLUGIN_ROOT / "templates" / "epi.schema.json"
DEFAULT_WORKFLOW = "sdlc"


@lru_cache(maxsize=None)
def _parsed(path, mtime):
    return json.loads(Path(path).read_text())


def _load(path):
    return _parsed(path, Path(path).stat().st_mtime_ns)      # an installed workflow's schema can change on disk


def engine_schema():
    return _load(str(ENGINE))


def workflow_schema(wf_name=DEFAULT_WORKFLOW):
    from . import workflow                      # a repo's own workflow (.claude/workflows) wins over the plugin's
    p = workflow.workflow_dir(wf_name) / "settings.schema.json"
    return _load(str(p)) if p.exists() else {"tabs": []}


def roles(wf_name=DEFAULT_WORKFLOW):
    return tuple(workflow_schema(wf_name).get("roles", ()))


def _expand(template_fields, tab, section):
    return [{"path": f"{tab}.{section}.{f['key']}", **{k: v for k, v in f.items() if k != "key"}}
            for f in template_fields]


def build(wf_name=DEFAULT_WORKFLOW, stages=(), agents=()):
    """The concrete schema for a workflow: {"tabs": [{id, title, sections: [{id, title, fields}]}]}."""
    eng = copy.deepcopy(engine_schema())
    extra = copy.deepcopy(workflow_schema(wf_name).get("tabs", []))
    tabs = []
    for tab in eng["tabs"]:
        dyn = tab.pop("dynamic", None)
        if dyn:
            ids = stages if dyn == "stages" else agents
            tab["sections"] = [{"id": i, "title": i, "dynamic": True, "fields": _expand(tab["fields"], tab["id"], i)}
                               for i in ids]
            del tab["fields"]
        for ptab in [t for t in extra if t["id"] == tab["id"]]:
            for psec in ptab["sections"]:
                mine = next((s for s in tab["sections"] if s["id"] == psec["id"]), None)
                if mine is None:
                    if dyn:
                        continue                         # a workflow section for a stage or agent that does not exist
                    tab["sections"].append(psec)
                else:
                    mine["fields"] = mine.get("fields", []) + psec.get("fields", [])
                    for k in ("freeform", "open"):
                        if k in psec:
                            mine[k] = psec[k]
        tabs.append(tab)
    return {"tabs": tabs, "roles": workflow_schema(wf_name).get("roles", [])}


def fields(sch):
    """{path: field} for every setting in the form."""
    return {f["path"]: f for t in sch["tabs"] for s in t["sections"] for f in s.get("fields", [])}


def options(sch, path):
    return tuple(fields(sch)[path]["options"])


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def check(path, f, value, nullable=False):
    """What is wrong with `value` for setting `f`, or None."""
    if value is None:
        return None if nullable else f"{path} needs a value"
    t = f["type"]
    if t == "enum":
        if f.get("value_type") == "int":
            ok = isinstance(value, int) and not isinstance(value, bool) and value in f["options"]
        else:
            ok = value in f["options"] or (f.get("allow_custom") and isinstance(value, str) and value)
        return None if ok else f"{path} must be one of: {', '.join(str(o) for o in f['options'])}"
    if t == "bool":
        return None if isinstance(value, bool) else f"{path} must be true or false"
    if t in ("int", "float"):
        if not _num(value) or (t == "int" and not isinstance(value, int)):
            return f"{path} must be {'a whole number' if t == 'int' else 'a number'}"
        if "min" in f and value < f["min"]:
            return f"{path} must be at least {f['min']}"
        if "max" in f and value > f["max"]:
            return f"{path} must be at most {f['max']}"
        return None
    if t == "string":
        if not isinstance(value, str):
            return f"{path} must be text"
        for _, name, _, _ in string.Formatter().parse(value):
            if name is not None and name not in f.get("placeholders", ()):
                return (f"{path}: unknown placeholder {{{name}}}"
                        + (f" (use {', '.join('{' + p + '}' for p in f['placeholders'])})" if f.get("placeholders") else ""))
        return None
    if t == "list":
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            return f"{path} must be a list of text"
        bad = [v for v in value if "item_options" in f and v not in f["item_options"]]
        return f"{path}: {', '.join(bad)} not allowed (use {', '.join(f['item_options'])})" if bad else None
    return None
