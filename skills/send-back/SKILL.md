---
name: send-back
description: Send a task back to an agent step of this stage or an earlier one, with your instructions (--to <step>; default the nearest).
argument-hint: "<task-id> [--to <step>] <instructions>"
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/epi.py" *)
---

Pass the user's text through stdin, never on the command line: run this with Bash, put the text
between the markers exactly as given, and pick a different marker word if the text contains `EPI_TEXT`.
The marker is quoted, so the shell does not interpret anything in the text.

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/epi.py" send-back --stdin <<'EPI_TEXT'
$ARGUMENTS
EPI_TEXT
```

Confirm in one line, then continue the task now (the user just gave the feedback, so don't ask)
by running the loop below for that task, starting at its step 1.

In the loop, `<plugin-root>` is `${CLAUDE_PLUGIN_ROOT}` and `<session-id>` is `${CLAUDE_SESSION_ID}`.

!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/epi.py" doc run-loop`
