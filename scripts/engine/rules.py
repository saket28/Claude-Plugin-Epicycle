"""Expressions: the one small language of `if:` and of `${{ }}` in `env:` and `with:`.

    always | never | touches-ui | risk                    the task's facts (risk compares as low < medium < high)
    paths:[glob, ...]                                       a changed or planned path matches a glob
    steps.<id>.outcome | verdict | outputs.<name>           a step of this stage, this round
    stages.<id>.steps.<id>.outcome | verdict | outputs.<name>   a step of an earlier stage
    'text' "text" 3 true false medium                       literals (a bare word is text)
    == != < <= > >=   and   or   not  !   ( )

No functions. A value that is not there is the empty text, and false when used as a condition. `outcome` is
success | failure | skipped; `verdict` is the step's own (PASS, CHANGES, FAIL ...). A comparison of numbers is
numeric, of risk levels ordered, anything else compares text without regard to case. Text is false when it is
empty, `false` or `0`.
"""

import re

RISK = {"low": 0, "medium": 1, "high": 2}
BUILTINS = ("always", "never", "touches-ui", "risk")
FIELDS = ("outcome", "verdict")
OPS = (">=", "<=", "==", "!=", ">", "<")
TOKEN = re.compile(r"""\s*(?:
    (?P<paths>paths\s*:\s*\[[^\]]*\])
  | (?P<str>"[^"]*"|'[^']*')
  | (?P<num>\d+(?:\.\d+)?(?![\w.-]))
  | (?P<op>>=|<=|==|!=|>|<|!|\(|\))
  | (?P<word>[A-Za-z_][\w.-]*)
)""", re.X)
TEMPLATE = re.compile(r"\$\{\{(.*?)\}\}", re.S)


class RuleError(ValueError):
    pass


def _tokens(text):
    out, i, text = [], 0, text.rstrip()
    while i < len(text):
        m = TOKEN.match(text, i)
        if not m or m.end() == i:
            raise RuleError(f"cannot read `{text[i:].strip()[:30]}` in `{text}`")
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        i = m.end()
    return out


def _ref(word, text):
    """A `steps.` or `stages.` reference as a tuple: ('steps', id, field, name) or ('stages', sid, id, field, name)."""
    parts = word.split(".")
    scope = parts[0]
    if scope == "steps":
        rest, head = parts[2:], parts[1:2]
    else:
        if len(parts) < 4 or parts[2] != "steps":
            raise RuleError(f"`{word}` in `{text}`: write stages.<stage>.steps.<step>.outcome | verdict | outputs.<name>")
        rest, head = parts[4:], [parts[1], parts[3]]
    if len(head) < (1 if scope == "steps" else 2) or not rest or (rest[0] in FIELDS and len(rest) != 1) or \
            (rest[0] == "outputs" and len(rest) != 2) or rest[0] not in (*FIELDS, "outputs"):
        raise RuleError(f"`{word}` in `{text}`: a step has `.outcome`, `.verdict` or `.outputs.<name>`")
    return (scope, *head, rest[0], rest[1] if rest[0] == "outputs" else None)


class _Parser:
    def __init__(self, text):
        self.text, self.toks, self.i = text, _tokens(text), 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def fail(self, what):
        raise RuleError(f"{what} in `{self.text}`")

    def parse(self):
        if not self.toks:
            self.fail("empty condition")
        node = self.or_()
        if self.i < len(self.toks):
            self.fail(f"unexpected `{self.peek()[1]}`")
        return node

    def or_(self):
        node = self.and_()
        while self.peek() == ("word", "or"):
            self.take()
            node = ("or", node, self.and_())
        return node

    def and_(self):
        node = self.not_()
        while self.peek() == ("word", "and"):
            self.take()
            node = ("and", node, self.not_())
        return node

    def not_(self):
        if self.peek() in (("op", "!"), ("word", "not")):
            self.take()
            return ("not", self.not_())
        return self.cmp()

    def cmp(self):
        left = self.atom()
        kind, val = self.peek()
        if kind == "op" and val in OPS:
            self.take()
            right = self.atom()
            for a, b in ((left, right), (right, left)):
                if a == ("builtin", "risk") and b[0] == "word" and b[1] not in RISK:
                    self.fail(f"`{b[1]}` is not a risk level (low, medium or high)")
            return ("cmp", val, left, right)
        if left[0] == "word":
            self.fail(f"unknown term `{left[1]}` (a plain word is only text to compare with)")
        return left

    def atom(self):
        kind, val = self.take()
        if kind == "op" and val == "(":
            node = self.or_()
            if self.take() != ("op", ")"):
                self.fail("a `(` is not closed")
            return node
        if kind == "paths":
            globs = [g.strip().strip("'\"") for g in val[val.index("[") + 1:-1].split(",") if g.strip()]
            if not globs:
                self.fail("paths:[] needs at least one glob")
            return ("paths", globs)
        if kind == "str":
            return ("lit", val[1:-1])
        if kind == "num":
            return ("lit", float(val) if "." in val else int(val))
        if kind == "word":
            if val in ("true", "false"):
                return ("lit", val == "true")
            if val in BUILTINS:
                return ("builtin", val)
            if val.startswith(("steps.", "stages.")) or val in ("steps", "stages"):
                return ("ref", _ref(val, self.text))
            if val in ("and", "or", "not"):
                self.fail(f"`{val}` is missing its operand")
            return ("word", val)
        self.fail("a value is missing" if kind is None else f"unexpected `{val}`")


def parse(text):
    """The parsed expression, or RuleError naming the offending text. `None` is `always`."""
    if text is None:
        return ("builtin", "always")
    if not isinstance(text, str):
        raise RuleError(f"a condition is text, got {text!r}")
    if "${{" in text:
        raise RuleError(f"`{text}`: write the condition without ${{{{ }}}}")
    return _Parser(text).parse()


def _walk(node):
    yield node
    for child in node[2:] if node[0] == "cmp" else node[1:] if node[0] in ("and", "or", "not") else ():
        if isinstance(child, tuple):
            yield from _walk(child)


def refs(text):
    """The step references of an expression (see `_ref`), in the order written."""
    return [n[1] for n in _walk(parse(text)) if n[0] == "ref"]


def templates(text):
    """The expressions written as `${{ expr }}` in a text. RuleError when one is not closed."""
    rest = TEMPLATE.sub("", text)
    if "${{" in rest:
        raise RuleError(f"`{text}`: a `${{{{` is never closed with `}}}}`")
    return [m.strip() for m in TEMPLATE.findall(text)]


def _lookup(ref, ctx):
    scope, *path = ref
    if scope == "steps":
        step = (ctx.get("steps") or {}).get(path[0]) or {}
    else:
        step = (((ctx.get("stages") or {}).get(path[0]) or {}).get("steps") or {}).get(path[1]) or {}
    field, name = path[-2], path[-1]
    got = (step.get("outputs") or {}).get(name) if field == "outputs" else step.get(field)
    return "" if got is None else got


def _value(node, ctx):
    kind = node[0]
    if kind in ("lit", "word"):
        return node[1]
    if kind == "ref":
        return _lookup(node[1], ctx)
    if kind == "builtin":
        name = node[1]
        return {"always": True, "never": False, "touches-ui": bool(ctx.get("touches_ui")),
                "risk": str(ctx.get("risk", "low"))}[name]
    if kind == "paths":
        return any(glob_match(g, p) for g in node[1] for p in ctx.get("paths", []))
    if kind == "not":
        return not _truthy(_value(node[1], ctx))
    if kind == "and":
        return _truthy(_value(node[1], ctx)) and _truthy(_value(node[2], ctx))
    if kind == "or":
        return _truthy(_value(node[1], ctx)) or _truthy(_value(node[2], ctx))
    return _compare(_value(node[2], ctx), node[1], _value(node[3], ctx))


def _truthy(v):
    if isinstance(v, str):
        return v.strip().lower() not in ("", "false", "0")
    return bool(v)


def _text(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return "" if v is None else str(v)


def _compare(have, op, want):
    have, want = _text(have).strip().lower(), _text(want).strip().lower()
    if have in RISK and want in RISK:
        have, want = RISK[have], RISK[want]
    else:
        try:
            have, want = float(have), float(want)
        except ValueError:
            pass
    return {">=": lambda: have >= want, "<=": lambda: have <= want, ">": lambda: have > want,
            "<": lambda: have < want, "==": lambda: have == want, "!=": lambda: have != want}[op]()


def evaluate(text, ctx):
    """Whether the condition holds. ctx: {'risk', 'touches_ui', 'paths': [...], 'steps': {id: {outcome, verdict,
    outputs}}, 'stages': {id: {'steps': {...}}}}."""
    return _truthy(_value(parse(text), ctx))


def interpolate(text, ctx):
    """The text with every `${{ expr }}` replaced by the expression's value."""
    return TEMPLATE.sub(lambda m: _text(_value(parse(m.group(1).strip()), ctx)), text) if isinstance(text, str) else text


_cache = {}


def glob_to_regex(glob):
    if glob in _cache:
        return _cache[glob]
    i, out = 0, []
    while i < len(glob):
        c = glob[i]
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    rx = re.compile("^" + "".join(out) + "$")
    _cache[glob] = rx
    return rx


def glob_match(glob, path):
    if path.startswith("./"):
        path = path[2:]
    rx = glob_to_regex(glob)
    if rx.match(path):
        return True
    # a planned path like "src/api/**" matches a glob like "src/**"
    return "*" in path and glob_to_regex(glob).match(path.replace("**", "x").replace("*", "x")) is not None
