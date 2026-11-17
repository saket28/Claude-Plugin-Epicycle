---
name: start
description: Start or continue a task and run it through spec, build and merge.
argument-hint: "[task-id | backlog-id | feature description]"
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/epi.py" *)
---

Start or continue a task.

Pass the user's text through stdin, never on the command line: run this with Bash, put the text
between the markers exactly as given, and pick a different marker word if the text contains `EPI_TEXT`.
The marker is quoted, so the shell does not interpret anything in the text.

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/epi.py" start --stdin <<'EPI_TEXT'
$ARGUMENTS
EPI_TEXT
```

Then run the loop:

In the loop, `<plugin-root>` is `${CLAUDE_PLUGIN_ROOT}` and `<session-id>` is `${CLAUDE_SESSION_ID}`.

!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/epi.py" doc run-loop`
