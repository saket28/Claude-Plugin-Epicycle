"""Packs: a workflow installed into the repo.

`epi install <pack>` copies the plugin's `workflows/<pack>/` into the repo, which then owns the copy:

    .claude/workflows/<pack>/workflow.yaml, settings.schema.json, scripts/*.py   the pack's files
    .claude/agents/<pack>-<agent>.md                                             the plugin agents the pack uses
    .claude/workflows/<pack>/settings.md, learnings.md                           seeds: written once, never upgraded
    .claude/workflows/<pack>/.base/                                              pristine copy of the pack's files
    .claude/workflows/<pack>/.base/pack.json                                     {"name", "version", "files"}

`epi upgrade <pack>` is a three-way merge per file (base = `.base`, ours = the repo's copy, theirs = the plugin's
current file). workflow.yaml is merged by step and field (wfmerge.py), every other file by line with
`git merge-file`. Nothing is written when any file conflicts.
"""

import difflib
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path

from . import config, workflow, wfmerge, yamlish

SEEDS = ("settings.md", "learnings.md")          # the repo's own: written at install, never upgraded
MARKER = re.compile(r"^(<<<<<<< |>>>>>>> )", re.M)
PACKFILE = "pack.json"


class PackError(ValueError):
    pass


def hash_text(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def pack_root():
    return workflow.WORKFLOWS


def installed_dir(root, pack):
    return config.pack_dir(root, pack)


def base_dir(root, pack):
    return installed_dir(root, pack) / ".base"


def is_installed(root, pack):
    return (installed_dir(root, pack) / "workflow.yaml").exists() and (base_dir(root, pack) / PACKFILE).exists()


def installed_packs(root):
    d = Path(root) / ".claude" / "workflows"
    return sorted(p.parent.name for p in d.glob("*/workflow.yaml")) if d.exists() else []


def target(root, pack, key):
    """Where the pack file `key` lives in the repo."""
    if key.startswith("agents/"):
        return Path(root) / ".claude" / "agents" / f"{pack}-{key[len('agents/'):]}"
    return installed_dir(root, pack) / key


def _agent_text(pack, name, text):
    """A plugin agent as the pack installs it: named `<pack>-<name>`, which is how Claude Code finds it."""
    return re.sub(r"(?m)^name:.*$", f"name: {pack}-{name}", text, count=1) if text.startswith("---") else text


def plugin_files(pack):
    """{key: text} of the plugin's current files for `pack`: the files the repo gets a copy of."""
    src = pack_root() / pack
    if not (src / "workflow.yaml").exists():
        have = sorted(p.parent.name for p in pack_root().glob("*/workflow.yaml"))
        raise PackError(f"no playbook '{pack}' in the plugin (playbooks: {', '.join(have) or 'none'})")
    out = {"workflow.yaml": (src / "workflow.yaml").read_text()}
    if (src / "settings.schema.json").exists():
        out["settings.schema.json"] = (src / "settings.schema.json").read_text()
    for p in sorted((src / "scripts").rglob("*.py")) if (src / "scripts").exists() else []:
        if "__pycache__" not in p.parts:
            out["scripts/" + p.relative_to(src / "scripts").as_posix()] = p.read_text()
    wf = yamlish.loads(out["workflow.yaml"])
    for st in (wf.get("stages") or {}).values():
        for _, s in workflow.stage_steps(st):
            name = workflow.name_of(s) if workflow.kind_of(s) == "agent" else None
            f = config.PLUGIN_ROOT / "agents" / f"{name}.md"
            if name and f.exists():
                out[f"agents/{name}.md"] = _agent_text(pack, name, f.read_text())
    return out


def plugin_version(pack):
    wf = yamlish.loads(plugin_files(pack)["workflow.yaml"])
    if not isinstance(wf.get("version"), str):
        raise PackError(f"playbook '{pack}' has no version in its workflow.yaml")
    return wf["version"]


def _rel(root, p):
    return Path(p).relative_to(root).as_posix()


def _read(p):
    return p.read_text() if p.exists() else None


def read_manifest(root, pack):
    p = base_dir(root, pack) / PACKFILE
    if not p.exists():
        raise PackError(f"playbook '{pack}' is not installed here: run `epi install {pack}`")
    return json.loads(p.read_text())


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _write_base(root, pack, files, version):
    b = base_dir(root, pack)
    for key, text in files.items():
        _write(b / key, text)
    manifest = {"name": pack, "version": version, "files": {k: _rel(root, target(root, pack, k)) for k in sorted(files)}}
    _write(b / PACKFILE, json.dumps(manifest, indent=2) + "\n")


def install(root, pack, force=False):
    """Copy the pack into the repo. Refuses when it is installed already, unless `force`, which overwrites the
    pack's files (not the seeds: settings and learnings are the repo's own)."""
    root = Path(root)
    files = plugin_files(pack)
    if is_installed(root, pack) and not force:
        raise PackError(f"playbook '{pack}' is already installed (use `epi upgrade {pack}` to take the plugin's changes, "
                        "or --force to overwrite your copy)")
    wrote, kept = [], []
    for key, text in files.items():
        _write(target(root, pack, key), text)
        wrote.append(_rel(root, target(root, pack, key)))
    for name in SEEDS:
        seed, dest = pack_root() / pack / name, installed_dir(root, pack) / name
        if dest.exists():
            kept.append(_rel(root, dest))
        elif seed.exists():
            _write(dest, seed.read_text())
            wrote.append(_rel(root, dest))
    version = plugin_version(pack)
    _write_base(root, pack, files, version)
    wrote.append(_rel(root, base_dir(root, pack)) + "/")
    return {"installed": pack, "version": version, "wrote": wrote, "kept": kept}


# ---------------------------------------------------------------- upgrade --

def merge3(ours, base, theirs):
    """(merged text, number of conflicts) by `git merge-file`; the conflict markers name the sides."""
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for n, t in (("ours", ours), ("base", base), ("theirs", theirs)):
            p = Path(d) / n
            p.write_text(t)
            paths.append(str(p))
        r = subprocess.run(["git", "merge-file", "-p", "-L", "repo", "-L", "installed", "-L", "plugin", *paths],
                           capture_output=True, text=True)
    if r.returncode < 0 or r.returncode > 127:
        raise PackError(f"git merge-file failed: {r.stderr.strip()}")
    return r.stdout, r.returncode


def _decide(key, base, ours, theirs):
    """(action, text, extra) for one file; text is what the repo's copy becomes (None = delete it). Actions: same,
    replace, keep, merge, add, remove, conflict (text then holds the conflict markers, or the readable text of the
    conflict; `extra` is the structural merge of workflow.yaml, which carries its conflicts and notes)."""
    if theirs is None:
        return ("remove", None, None) if ours == base else ("keep-removed", ours, None)
    if ours is None:
        if base is None:
            return "add", theirs, None
        return ("same", None, None) if theirs == base else \
            ("conflict", "the repo deleted this file; the plugin changed it\n", None)
    if ours == theirs:
        return "same", ours, None
    if base is None:
        base = ""
    if ours == base:
        return "replace", theirs, None
    if theirs == base:
        return "keep", ours, None
    if key == "workflow.yaml":
        m = wfmerge.merge(base, ours, theirs)
        return ("conflict" if m["conflicts"] else "merge"), m["text"], m
    text, n = merge3(ours, base, theirs)
    return ("conflict" if n else "merge"), text, None


def diff(path, before, after):
    return "".join(difflib.unified_diff((before or "").splitlines(True), (after or "").splitlines(True),
                                        f"a/{path}", f"b/{path}"))


def _commands(root, pack, schema_text):
    """Names of the repo's commands (`run: <name>`): the pack schema's `global.command.*` and the repo's own."""
    names = set()
    try:
        for t in json.loads(schema_text or "{}").get("tabs", []):
            for sec in t.get("sections", []):
                names |= {f["path"].split(".")[2] for f in sec.get("fields", []) if f["path"].startswith("global.command.")}
    except (ValueError, AttributeError):
        pass
    epi, settings, _ = config.read_sources(root)
    try:
        raw = {**yamlish.parse_frontmatter(epi)[0], **yamlish.parse_frontmatter(settings)[0]}
    except yamlish.YamlError:
        raw = {}
    return names | {k for k in (((raw.get("global") or {}).get("command")) or {}) if k not in ("setup", "timeout")}


def _validate(root, pack, text, result):
    """Problems of the merged workflow.yaml as the repo would load it with the other upgraded files: the same checks
    as `workflow.load` (the scripts, commands and agents come from the upgraded set)."""
    try:
        wf = yamlish.loads(text)
    except yamlish.YamlError as e:
        return [str(e)]
    scripts = {p.stem for p in (installed_dir(root, pack) / "scripts").glob("*.py")}
    for key, t in result.items():
        if key.startswith("scripts/") and key.endswith(".py"):
            (scripts.add if t is not None else scripts.discard)(Path(key).stem)
    names = [n.replace("_", "-") for n in scripts if not n.startswith("_")]
    schema_text = result.get("settings.schema.json") or _read(installed_dir(root, pack) / "settings.schema.json")
    errs = workflow.validate(wf, pack, _commands(root, pack, schema_text), script_names=names)
    for sid, st in (wf.get("stages") or {}).items():
        for _, s in workflow.stage_steps(st):
            if workflow.kind_of(s) == "agent":
                n = workflow.name_of(s)
                if not config.agent_file(root, n, pack)[1] and result.get(f"agents/{n}.md") is None:
                    errs.append(f"stage {sid}: agent '{n}' has no file")
    return errs


def upgrade(root, pack, dry_run=False):
    """Merge the plugin's current pack into the repo's copy. All or nothing: any conflict writes nothing."""
    root = Path(root)
    manifest = read_manifest(root, pack)
    theirs = plugin_files(pack)
    new_version = plugin_version(pack)
    plan, conflicts, diffs, result, merged = [], [], {}, {}, None
    for key in sorted(set(manifest["files"]) | set(theirs)):
        dest = target(root, pack, key)
        ours, base = _read(dest), _read(base_dir(root, pack) / key)
        action, text, extra = _decide(key, base, ours, theirs.get(key))
        rel = _rel(root, dest)
        plan.append({"file": rel, "action": action})
        if extra is not None:
            merged = extra
        if action == "conflict":
            found = (extra or {}).get("conflicts") or [{"text": f"{rel}: conflict"}]
            conflicts += [{"file": rel, **c, **({} if extra else {"text": text})} for c in found]
        elif action not in ("same", "keep", "keep-removed"):
            result[key] = text
            diffs[rel] = diff(rel, ours, text)
    wf_text = result.get("workflow.yaml") if "workflow.yaml" in result else _read(target(root, pack, "workflow.yaml"))
    if not conflicts and "workflow.yaml" in result and merged is not None and wf_text:
        errs = _validate(root, pack, wf_text, result)
        if errs:
            rel = _rel(root, target(root, pack, "workflow.yaml"))
            conflicts = [{"file": rel, "field": None, "text": "the merged workflow.yaml is invalid: " + "; ".join(errs)}]
            plan = [{**p, "action": "conflict"} if p["file"] == rel else p for p in plan]
    out = {"pack": pack, "from": manifest["version"], "to": new_version, "dry_run": dry_run, "files": plan}
    removed = [p["file"] for p in plan if p["action"] == "keep-removed"]
    if removed:
        out["removed_from_pack"] = removed          # the pack no longer ships these, and the repo changed them: kept
    if merged and merged["notes"]:
        out["notes"] = merged["notes"]
    if merged and merged["dropped_comments"] and not conflicts:
        out["dropped_comments"] = merged["dropped_comments"]      # comments of the repo's workflow.yaml the merge could not place
    if conflicts:
        return {**out, "upgraded": False, "conflicts": conflicts,
                "note": "nothing was written: resolve by editing your copy, or `epi install --force`, then upgrade again"}
    if dry_run:
        return {**out, "upgraded": False, "diffs": diffs}
    for key, text in result.items():
        if text is None:
            target(root, pack, key).unlink()
        else:
            _write(target(root, pack, key), text)
    keep = {k: t for k, t in theirs.items()}
    for key in set(manifest["files"]) - set(theirs):
        (base_dir(root, pack) / key).unlink(missing_ok=True)
    _write_base(root, pack, keep, new_version)
    return {**out, "upgraded": manifest["version"] != new_version or bool(result), "wrote": sorted(diffs)}


# ------------------------------------------------------------------ check --

def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v))


def check(root, pack):
    """Problems of one installed pack, as messages: an upgrade is available, files with conflict markers."""
    root = Path(root)
    manifest = read_manifest(root, pack)
    issues = []
    try:
        if vtuple(plugin_version(pack)) > vtuple(manifest["version"]):
            issues.append(f"playbook {pack} {manifest['version']} is installed; the plugin has {plugin_version(pack)}: "
                          f"run `epi upgrade {pack}`")
    except PackError:
        pass                                      # a pack of the repo's own, not shipped by the plugin
    own = [target(root, pack, k) for k in manifest["files"]] + [installed_dir(root, pack) / s for s in SEEDS]
    for p in own:
        if p.exists() and MARKER.search(p.read_text()):
            issues.append(f"{_rel(root, p)} has conflict markers: resolve them")
    return issues


def status(root, pack):
    """What the dashboard shows of an installed pack: its version, the plugin's, and when the plugin has a newer
    one the dry-run of the upgrade (per-file actions, diffs, conflicts). None when the pack is not installed."""
    root = Path(root)
    if not is_installed(root, pack):
        return None
    have = read_manifest(root, pack)["version"]
    try:
        new = plugin_version(pack)
    except PackError:
        return {"pack": pack, "installed": have, "available": None, "newer": False}      # the repo's own pack
    out = {"pack": pack, "installed": have, "available": new, "newer": vtuple(new) > vtuple(have)}
    if out["newer"]:
        plan = upgrade(root, pack, dry_run=True)
        out.update({k: plan[k] for k in ("files", "diffs", "conflicts", "removed_from_pack") if k in plan})
    return out
