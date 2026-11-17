---
name: intake
description: Add a feature to the backlog.
argument-hint: "<feature description>"
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/epi.py" *)
---

Pass the user's text through stdin, never on the command line: run this with Bash, put the text
between the markers exactly as given, and pick a different marker word if the text contains `EPI_TEXT`.
The marker is quoted, so the shell does not interpret anything in the text.

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/epi.py" intake --stdin <<'EPI_TEXT'
$ARGUMENTS
EPI_TEXT
```

Confirm in one line what was added. Don't do anything else.
