# Artifact formats

Every artifact starts with YAML frontmatter (a restricted YAML subset: scalars, lists, maps, inline `{}`, `#` comments).

| File | Required frontmatter |
|---|---|
| `spec.md` (the spec-writer's report, published) | `title` (a short title, at most 60 characters), `risk` (low/medium/high), `touches_ui`, `change_type` (feat/fix/chore), `breaking`, `paths`, `open_questions` |
| `<stage>/r<number>/<step id>.md` of a Do agent | none required (the builder writes `round`) |
| any agent's report | one field per output the step declares in `outputs` (the brief names them), plain text on one line |
| `<stage>/r<number>/outputs.json` | written by the engine: `{step id: {outcome, verdict, outputs, reused?}}` |
| `<stage>/r<number>/<step id>.md` of a run step | `verdict` (PASS/FAIL), written by the engine |
| `<stage>/r<number>/<step id>.md` of a test or review step | `verdict` (PASS/CHANGES; the tester PASS/FAIL), `important`, `nit` |
| `decision.md` | `decision` (DONE), `accept` |

Sections every agent may add: `## Questions`, `## Learning candidates`.
Agents talk to each other in a `## Comments` section, one line per comment:
`<id|new> -> <to>, <type>[, <severity>]: <text>` (indented lines are detail). The leading `- ` is optional. A comment without a severity is a nit, and a review whose verdict is PASS cannot raise blocking issues. A line that looks like a comment but does not parse is logged as an error and shown to you.
- `type`: `feedback` (raise an issue; severity `blocking` or `nit`), `pushback`, `fixed` (builder), `accept`.
- `to`: a step id of the stage (`builder`, `tester`, `code-review` ...). Unknown names are redirected to the stage's first Do step.
- New issues use `new`; the director numbers them `I1`, `I2` … and keeps them in `threads.jsonl` (readable copy: `conversation.md`).
- The raiser's `accept` closes an issue; a nit pushback closes it; the raiser repeating a pushback is a disagreement that needs you; a checker that says nothing about an issue addressed to it closes it as "no reply".
- Skill reviewers (`code-review`, ...) end their reply with a JSON block `{"findings": [...], "replies": [...]}`; `replies` answer the reviewer's open threads (`accept` or `pushback`). A skill whose step declares `outputs` adds `"outputs": {"name": "text"}`; the engine writes those as fields of the step's report (when converting a raw reply by hand, put them in the frontmatter).
