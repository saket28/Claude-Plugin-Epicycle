---
name: install
description: Install a playbook (default sdlc) into this repo, where the repo owns and edits its copy.
argument-hint: "<playbook> [--force]"
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/epi.py" *)
---

Pass the user's text through stdin, never on the command line: run this with Bash, put the text
between the markers exactly as given, and pick a different marker word if the text contains `EPI_TEXT`.
The marker is quoted, so the shell does not interpret anything in the text.

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/epi.py" install --stdin <<'EPI_TEXT'
$ARGUMENTS
EPI_TEXT
```

It copies the playbook's `workflow.yaml`, schema and scripts into `.claude/workflows/<playbook>/`, its agents into
`.claude/agents/<playbook>-<agent>.md`, and keeps a pristine copy in `.claude/workflows/<playbook>/.base/` so that
`/epi:upgrade` can merge later. `settings.md` and `learnings.md` are created once and never overwritten.
It refuses when the playbook is installed already; `--force` overwrites the playbook's files (not settings or learnings).

Summarize what was written in a few lines, and tell the user to commit `.claude/`. Point out that the
workflow is theirs to edit (`workflow.yaml`: add a step with a line, turn one off with `enabled: false`;
tuning in `settings.md`).
