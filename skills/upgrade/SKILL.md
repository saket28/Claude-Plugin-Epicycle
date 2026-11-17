---
name: upgrade
description: Merge the plugin's newer version of an installed playbook into the repo's copy. --dry-run shows the plan.
argument-hint: "<playbook> [--dry-run]"
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/epi.py" *)
---

Pass the user's text through stdin, never on the command line: run this with Bash, put the text
between the markers exactly as given, and pick a different marker word if the text contains `EPI_TEXT`.
The marker is quoted, so the shell does not interpret anything in the text.

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/epi.py" upgrade --stdin <<'EPI_TEXT'
$ARGUMENTS
EPI_TEXT
```

It merges each file three ways (the pristine copy in `.base/`, the repo's copy, the plugin's current file):
files the repo never changed are replaced, files only the repo changed are kept, and files both changed are
merged. All or nothing: when any file conflicts, nothing is written and the output lists each conflicted file
with its conflict text.

- On success, list what changed per file (`files`) and the new version; tell the user to review `git diff` and commit.
- On conflicts, show the conflicted files and the conflicting hunks. Offer to run `--dry-run` first next time,
  and say how to resolve: edit the repo's copy so it no longer touches the same lines as the plugin (or
  `/epi:install <playbook> --force` to take the plugin's files and lose local edits), then upgrade again.
  Do not edit the workflow files yourself unless the user asks.
