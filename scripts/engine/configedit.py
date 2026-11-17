"""Edit settings frontmatter in place.

A setting is written to the file of its tab: `global` to .claude/epi.md, `stage` and `agent` to the workflow's
settings.md. Only the changed keys are rewritten; comments, ordering and the markdown body are kept. Values equal
to the default are removed, so the files hold only overrides.
"""

import hashlib
import re

from . import config, yamlish


class EditError(ValueError):
    pass


def file_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _default(path, wf_name):
    return config.default_for(".".join(path), wf_name)


def apply(text, changes, wf_name=None):
    """changes: {"stage.build.rounds": 4, "global.budget.per_task": 800000, "x": None (= remove)}, all for one file."""
    fm, body = yamlish.split_frontmatter(text)
    lines = fm.splitlines()
    raw = yamlish.loads(fm) if fm.strip() else {}
    wf_name = wf_name or changes.get("global.workflow.name") or ((raw.get("global") or {}).get("workflow") or {}).get("name") or config.DEFAULTS["global"]["workflow"]["name"]
    for dotted, value in changes.items():
        path = dotted.split(".")
        if _default(path, wf_name) == value:
            value = None                         # back to default: drop the override
        lines, raw = _set(lines, raw, path, value)
    new_fm = "\n".join(lines).strip("\n")
    yamlish.loads(new_fm)                         # never write something that won't parse
    head = "---\n" + (new_fm + "\n" if new_fm else "") + "---\n"
    return head + ("\n" + body.lstrip("\n") if body.strip() else "")


def _find(lines, key, indent, start=0, end=None):
    end = len(lines) if end is None else end
    rx = re.compile(rf"^{' ' * indent}{re.escape(key)}\s*:(.*)$")
    for i in range(start, end):
        m = rx.match(lines[i])
        if m:
            return i, m.group(1)
    return None, None


def _block_end(lines, i, indent):
    j = i + 1
    while j < len(lines):
        s = lines[j]
        if s.strip() and not s.strip().startswith("#") and (len(s) - len(s.lstrip())) <= indent:
            break
        j += 1
    return j


def _comment(rest):
    m = re.search(r"\s+#.*$", rest)
    return m.group(0) if m and rest.count('"') % 2 == 0 else ""


def _set_block3(lines, raw, path, value):
    """Edit `top: / sub: / key:` in place when all three are written as blocks, so comments on the
    neighbouring lines survive. Returns (lines, raw), or None when the layout isn't that simple."""
    top, sub, key = path
    i, rest = _find(lines, top, 0)
    if i is None or (rest.strip() and not rest.strip().startswith("#")):
        return None
    j, jrest = _find(lines, sub, 2, i + 1, _block_end(lines, i, 0))
    if j is None or (jrest.strip() and not jrest.strip().startswith("#")):
        return None
    jend = _block_end(lines, j, 2)
    k, krest = _find(lines, key, 4, j + 1, jend)
    node = raw.setdefault(top, {}).setdefault(sub, {})
    if value is None:
        if k is not None:
            del lines[k:_block_end(lines, k, 4)]
        node.pop(key, None)
        if not node:                                         # the sub block is empty now: drop it, and its parent if empty
            del lines[j:_block_end(lines, j, 2)]
            raw[top].pop(sub, None)
            if not raw[top]:
                del lines[i:_block_end(lines, i, 0)]
                raw.pop(top)
        return lines, raw
    new = f"    {key}: {yamlish.dump_inline(value)}"
    if k is None:
        lines.insert(jend, new)
    else:
        del lines[k + 1:_block_end(lines, k, 4)]
        lines[k] = new + _comment(krest)
    node[key] = value
    return lines, raw


def _set(lines, raw, path, value):
    top = path[0]
    if len(path) == 3:
        done = _set_block3(lines, raw, path, value)
        if done:
            return done
        cur = dict((raw.get(top) or {}).get(path[1]) or {})        # layout isn't plain blocks: rewrite the section
        if value is None:
            cur.pop(path[2], None)
        else:
            cur[path[2]] = value
        return _set(lines, raw, path[:2], cur or None)
    if len(path) > 3:                                              # inside a setting's own map (adapter ops ...)
        cur = dict(((raw.get(top) or {}).get(path[1]) or {}).get(path[2]) or {})
        node = cur
        for k in path[3:-1]:
            node = node.setdefault(k, {})
        if value is None:
            node.pop(path[-1], None)
        else:
            node[path[-1]] = value
        return _set(lines, raw, path[:3], cur or None)
    i, rest = _find(lines, top, 0)
    if len(path) == 1:
        if value is None:
            if i is not None:
                del lines[i:_block_end(lines, i, 0)]
            raw.pop(top, None)
            return lines, raw
        new = f"{top}: {yamlish.dump_inline(value)}"
        if i is None:
            lines.append(new)
        else:
            del lines[i + 1:_block_end(lines, i, 0)]
            lines[i] = new + _comment(rest)
        raw[top] = value
        return lines, raw
    sub = path[1]
    parent = dict(raw.get(top) or {})
    if value is None:
        parent.pop(sub, None)
    else:
        parent[sub] = value
    if i is not None and rest.strip() and not rest.strip().startswith("#"):
        # inline parent: rewrite the whole line
        lines[i] = (f"{top}: {yamlish.dump_inline(parent)}" + _comment(rest)) if parent else ""
        lines = [l for l in lines if l != ""] if not parent else lines
    elif i is not None:
        end = _block_end(lines, i, 0)
        j, crest = _find(lines, sub, 2, i + 1, end)
        if value is None and j is not None:
            del lines[j:_block_end(lines, j, 2)]
        elif j is not None:
            del lines[j + 1:_block_end(lines, j, 2)]
            lines[j] = f"  {sub}: {yamlish.dump_inline(value)}" + _comment(crest)
        elif value is not None:
            lines.insert(end, f"  {sub}: {yamlish.dump_inline(value)}")
    elif value is not None:
        lines.append(f"{top}: {yamlish.dump_inline(parent)}")
    if parent:
        raw[top] = parent
    else:
        raw.pop(top, None)
        if i is not None:
            k, _ = _find(lines, top, 0)
            if k is not None and not any(l.startswith("  ") for l in lines[k + 1:_block_end(lines, k, 0)]):
                del lines[k:_block_end(lines, k, 0)]
    return lines, raw


def split(changes):
    """The changes by the file they are written to: {"global": {...}, "pack": {...}} (empty ones left out)."""
    out = {"global": {}, "pack": {}}
    for k, v in changes.items():
        tab = k.split(".")[0]
        if tab not in config.PLACES:
            raise EditError(f"unknown setting '{k}': settings are <tab>.<section>.<setting>")
        out["global" if tab == "global" else "pack"][k] = v
    return {f: c for f, c in out.items() if c}


def paths(cfg):
    return {"global": cfg.root / config.EPI_FILE, "pack": config.settings_path(cfg.root, cfg.workflow_name)}


def hashes(cfg):
    """The hash of each settings file: {"global": ..., "pack": ...}."""
    return {f: file_hash(p.read_text() if p.exists() else "") for f, p in paths(cfg).items()}


def plan(cfg, changes):
    """{file key: (path, before, after)} for the files the changes touch."""
    wf_name = changes.get("global.workflow.name") or cfg.workflow_name
    out = {}
    for f, chg in split(changes).items():
        path = paths(cfg)[f]
        before = path.read_text() if path.exists() else "---\n---\n"
        out[f] = (path, before, apply(before, chg, wf_name))
    return out


def save(cfg, changes, expected_hash, source="ui"):
    """Write the changes, each to its file. `expected_hash` is {file key: hash} from `hashes` (or None): a file
    that changed on disk since is refused. Returns the new hashes."""
    done = plan(cfg, changes)
    expected = expected_hash or {}
    for f, (path, before, _) in done.items():
        if expected.get(f) and file_hash(before if path.exists() else "") != expected[f]:
            raise EditError(f"{path.name} changed on disk since you loaded it; reload and try again")
    texts = {f: (p.read_text() if p.exists() else "") for f, p in paths(cfg).items()}
    texts.update({f: new for f, (_, _, new) in done.items()})
    config.from_texts(cfg.root, texts["global"], texts["pack"], cfg.learn_text)      # validate before writing anything
    for path, _, new in done.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(new)
        tmp.replace(path)
    from .store import append_jsonl, now
    append_jsonl(cfg.work_dir / "config-events.jsonl",
                 {"ts": now(), "type": "config_change", "source": source, "keys": sorted(changes)})
    return hashes(cfg)


# ------------------------------------------------------------------ rules --

def set_body(text, body):
    """The settings file with its markdown body replaced; the frontmatter is kept byte for byte."""
    lines = text.splitlines(keepends=True)
    end = 0
    if lines and lines[0].strip() == "---":
        end = next((i + 1 for i in range(1, len(lines)) if lines[i].strip() == "---"), 0)
    head = "".join(lines[:end]) or "---\n---\n"
    body = body.strip("\n")
    return head + ("\n" + body + "\n" if body.strip() else "")


def plan_rules(cfg, body):
    """(path, before, after) for replacing the pack's rules (the body of its settings.md)."""
    p = paths(cfg)["pack"]
    before = p.read_text() if p.exists() else "---\n---\n"
    return p, before, set_body(before, str(body or ""))


def save_rules(cfg, body, expected_hash, source="ui"):
    """Write the rules. Refused when settings.md changed on disk since `expected_hash` (its entry of `hashes`)."""
    p, before, after = plan_rules(cfg, body)
    if expected_hash and file_hash(before if p.exists() else "") != expected_hash:
        raise EditError(f"{p.name} changed on disk since you loaded it; reload and try again")
    config.from_texts(cfg.root, paths(cfg)["global"].read_text() if paths(cfg)["global"].exists() else "", after, cfg.learn_text)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".md.tmp")
    tmp.write_text(after)
    tmp.replace(p)
    from .store import append_jsonl, now
    append_jsonl(cfg.work_dir / "config-events.jsonl", {"ts": now(), "type": "config_change", "source": source, "keys": ["rules"]})
    return hashes(cfg)
