"""Three-way merge of a pack's workflow.yaml by structure, not by line.

Line merging reports a clash for edits on neighbouring lines, and workflow.yaml is one step per line. Here a step is
identified by (stage, phase, step id), the sign-off by (stage, "signoff"), and every item is merged field by field:

    changed on one side only -> that side           same field changed differently on both -> a conflict
    added on one side -> kept                       added on both with one id -> merged field by field, empty base
    removed on one side, untouched on the other -> removed; changed on the other -> a conflict

Order within a phase (and of stages and of keys): the plugin's order, with what the repo added inserted after what it
followed in the repo's copy (at the start when it came first). When only the repo reordered the steps both sides
share, the repo's order is the skeleton and the plugin's additions are inserted into it; when both reordered, or
only the plugin did, the plugin's order wins. The text is written again in the shipped style: the plugin's header
comments, one step per line. Comments of the repo's copy that cannot be placed are returned, never dropped silently.
"""

from . import workflow, yamlish

MISSING = object()
PHASE_ORDER = ("name", *workflow.PHASES)


def _show(v):
    return "(absent)" if v is MISSING else yamlish.dump_inline(v)


def _conflict(stage, phase, step, field, b, o, t, what=None):
    where = "/".join(x for x in (stage, phase, step) if x) or "workflow.yaml"
    detail = what or f"field '{field}' changed on both sides (installed: {_show(b)}; repo: {_show(o)}; plugin: {_show(t)})"
    return {"stage": stage, "phase": phase, "step": step, "field": field,
            "base": None if b is MISSING else b, "repo": None if o is MISSING else o,
            "plugin": None if t is MISSING else t, "text": f"{where}: {detail}"}


def insert_order(skeleton, other, keep):
    """`skeleton` (those in `keep`), then what `other` has more, each after the item it followed in `other`."""
    out = [k for k in skeleton if k in keep]
    for i, k in enumerate(other):
        if k in out or k not in keep:
            continue
        pos = 0
        for p in reversed(other[:i]):
            if p in out:
                pos = out.index(p) + 1
                break
        out.insert(pos, k)
    return out


def merge_fields(b, o, t, loc):
    """(merged map, conflicts) of three maps, key by key; a value is compared whole."""
    out, conflicts = {}, []
    for k in dict.fromkeys([*b, *o, *t]):
        bv, ov, tv = b.get(k, MISSING), o.get(k, MISSING), t.get(k, MISSING)
        if ov == tv or tv == bv:
            out[k] = ov
        elif ov == bv:
            out[k] = tv
        else:
            conflicts.append(_conflict(*loc, k, bv, ov, tv))
            out[k] = ov
    keep = {k for k, v in out.items() if v is not MISSING}
    return {k: out[k] for k in insert_order(list(t), list(o), keep) if k in keep}, conflicts


def merge_item(b, o, t, loc):
    """(merged item or MISSING, conflicts) of one item that may be absent on any side."""
    if o == t:
        return o, []
    if b is MISSING:
        if o is MISSING or t is MISSING:
            return (t if o is MISSING else o), []
        b = {}
    if o is MISSING or t is MISSING:
        if (t if o is MISSING else o) == b:
            return MISSING, []
        who = "the repo removed it, the plugin changed it" if o is MISSING else "the plugin removed it, the repo changed it"
        return (b if o is MISSING else o), [_conflict(*loc, None, b, o, t, who)]
    if o == b:
        return t, []
    if t == b:
        return o, []
    if not all(isinstance(x, dict) for x in (b, o, t)):
        return o, [_conflict(*loc, None, b, o, t, f"changed on both sides (installed: {_show(b)}; repo: {_show(o)}; plugin: {_show(t)})")]
    return merge_fields(b, o, t, loc)


def _ids(steps):
    return [workflow.step_id(s) for s in steps]


def merge_phase(b, o, t, stage, phase, notes):
    """The steps of one phase: by step id; order as in the module doc."""
    try:
        bi, oi, ti = _ids(b), _ids(o), _ids(t)
    except (KeyError, AttributeError, IndexError, TypeError):
        return o, [_conflict(stage, phase, None, None, MISSING, MISSING, MISSING, "a step without `uses` or `run` cannot be matched")]
    by = lambda steps, ids: dict(zip(ids, steps))
    bm, om, tm = by(b, bi), by(o, oi), by(t, ti)
    merged, conflicts = {}, []
    for i in dict.fromkeys([*bi, *oi, *ti]):
        v, c = merge_item(bm.get(i, MISSING), om.get(i, MISSING), tm.get(i, MISSING), (stage, phase, i))
        conflicts += c
        if v is not MISSING:
            merged[i] = v
    common = set(bi) & set(oi) & set(ti)
    seq = lambda ids: [i for i in ids if i in common]
    repo_moved, plugin_moved = seq(oi) != seq(bi), seq(ti) != seq(bi)
    where = f"{stage}.{phase}"
    if repo_moved and not plugin_moved:
        order = insert_order(oi, ti, set(merged))
        notes.append(f"{where}: kept the repo's order of the steps; the plugin's new steps are inserted into it")
    else:
        order = insert_order(ti, oi, set(merged))
        if repo_moved:
            notes.append(f"{where}: both sides reordered the steps; the plugin's order is used")
    return [merged[i] for i in order], conflicts


def merge_stage(b, o, t, sid, notes):
    out, conflicts = {}, []
    rest = lambda s: {k: v for k, v in s.items() if k not in workflow.LISTS}
    fields, c = merge_fields(rest(b), rest(o), rest(t), (sid, None, None))
    conflicts += c
    out.update(fields)
    for ph in workflow.LISTS:
        lists = [x.get(ph) for x in (b, o, t)]
        if all(x is None or isinstance(x, list) for x in lists):
            steps, c = merge_phase(*(x or [] for x in lists), sid, ph, notes)
            conflicts += c
            if steps or any(x == [] for x in lists):
                out[ph] = steps
        else:
            v, c = merge_item(*(x if x is not None else MISSING for x in lists), (sid, ph, None))
            conflicts += c
            if v is not MISSING:
                out[ph] = v
    known = [k for k in PHASE_ORDER if k in out]
    return {k: out[k] for k in known + [k for k in out if k not in known]}, conflicts


def merge_stages(b, o, t, notes):
    out, conflicts = {}, []
    for sid in dict.fromkeys([*b, *o, *t]):
        bs, os_, ts = b.get(sid, MISSING), o.get(sid, MISSING), t.get(sid, MISSING)
        if all(isinstance(x, dict) for x in (bs, os_, ts)) or (bs is MISSING and isinstance(os_, dict) and isinstance(ts, dict)):
            v, c = merge_stage({} if bs is MISSING else bs, os_, ts, sid, notes)
        else:
            v, c = merge_item(bs, os_, ts, (sid, None, None))
        conflicts += c
        if v is not MISSING:
            out[sid] = v
    keep = set(out)
    return {k: out[k] for k in insert_order(list(t), list(o), keep)}, conflicts


# ------------------------------------------------------------------ text --

def _block(v, indent):
    pad = " " * indent
    lines = []
    for k, x in v.items():
        if isinstance(x, dict) and x:
            lines += [f"{pad}{k}:", *_block(x, indent + 2)]
        else:
            lines.append(f"{pad}{k}: {yamlish.dump_inline(x)}")
    return lines


def render(wf, header):
    """The workflow in the shipped style: the header, top-level keys, stages with one step per line."""
    lines = list(header)
    lines += _block({k: v for k, v in wf.items() if k != "stages"}, 0)
    lines.append("stages:")
    for sid, st in (wf.get("stages") or {}).items():
        lines.append(f"  {sid}:")
        for k, v in st.items():
            if isinstance(v, list) and k in workflow.LISTS and v:
                lines.append(f"    {k}:")
                lines += [f"      - {yamlish.dump_inline(s)}" for s in v]
            else:
                lines.append(f"    {k}: {yamlish.dump_inline(v)}")
    return "\n".join(lines) + "\n"


def header_of(text):
    out = []
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            break
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return out


def comments_of(text):
    """[(line number, comment text, the next line of YAML)] of a file: whole-line and trailing comments."""
    lines, out = text.splitlines(), []
    for n, raw in enumerate(lines):
        if raw.lstrip().startswith("#"):
            nxt = next((x.strip() for x in lines[n + 1:] if x.strip() and not x.lstrip().startswith("#")), "")
            out.append((n + 1, raw.strip(), nxt))
        else:
            code = yamlish._strip_comment(raw)
            if code != raw.rstrip():
                out.append((n + 1, raw[len(code):].strip(), code.strip()))
    return out


def dropped_comments(base, ours, theirs, header):
    """Comments the merged file does not carry: the repo's own (not in the plugin's or the installed file) and the
    plugin's outside its header."""
    known = {c[1] for c in comments_of(base)} | {c[1] for c in comments_of(theirs)}
    out = [{"source": "repo", "line": n, "comment": c, "near": near}
           for n, c, near in comments_of(ours) if c not in known]
    out += [{"source": "plugin", "line": n, "comment": c, "near": near}
            for n, c, near in comments_of(theirs) if c not in {h.strip() for h in header}]
    return out


def merge(base, ours, theirs):
    """{"text", "conflicts", "dropped_comments", "notes"} for the texts of the installed, the repo's and the plugin's
    workflow.yaml. `text` is None when anything conflicts."""
    try:
        b, o, t = (yamlish.loads(x) for x in (base, ours, theirs))
    except yamlish.YamlError as e:
        return {"text": None, "conflicts": [{"stage": None, "phase": None, "step": None, "field": None,
                                             "text": f"workflow.yaml: {e}"}], "dropped_comments": [], "notes": []}
    notes = []
    stages, conflicts = merge_stages(*(x.get("stages") or {} for x in (b, o, t)), notes)
    top = lambda x: {k: v for k, v in x.items() if k != "stages"}
    fields, c = merge_fields(top(b), top(o), top(t), (None, None, None))
    conflicts += c
    merged = {**fields, "stages": stages}
    if conflicts:
        return {"text": None, "conflicts": conflicts, "dropped_comments": [], "notes": notes}
    header = header_of(theirs)
    text = render(merged, header)
    if yamlish.loads(text) != merged:
        return {"text": None, "dropped_comments": [], "notes": notes, "conflicts": [
            {"stage": None, "phase": None, "step": None, "field": None,
             "text": "workflow.yaml: the merged result does not read back the same; merge it by hand"}]}
    return {"text": text, "conflicts": [], "dropped_comments": dropped_comments(base, ours, theirs, header), "notes": notes}
