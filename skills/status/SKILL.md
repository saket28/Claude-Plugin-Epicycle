---
name: status
description: Show tasks, their stage, tokens and anything that needs you. --check runs the health check.
argument-hint: "[task-id] [--check]"
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/epi.py" *)
---

Pass the user's text through stdin, never on the command line: run this with Bash, put the text
between the markers exactly as given, and pick a different marker word if the text contains `EPI_TEXT`.
The marker is quoted, so the shell does not interpret anything in the text.

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/epi.py" status --stdin <<'EPI_TEXT'
$ARGUMENTS
EPI_TEXT
```

Summarize this for the user as a compact table (task, title, status, round, tokens). Call out
anything that needs the user (failed, stalled or waiting for a decision) with the command that moves it forward.
For --check output, list the issues and offer to fix the simple ones. When it says an upgrade of the installed
workflow is available, offer `/epi:upgrade <playbook> --dry-run`.
