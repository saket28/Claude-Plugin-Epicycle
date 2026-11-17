---
name: approve
description: Approve a task's spec or merge, raise its budget, accept a disagreement as it is, or retry.
argument-hint: "<task-id> <spec|merge|budget +500k|accept|retry>"
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/epi.py" *)
---

Pass the user's text through stdin, never on the command line: run this with Bash, put the text
between the markers exactly as given, and pick a different marker word if the text contains `EPI_TEXT`.
The marker is quoted, so the shell does not interpret anything in the text.

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/epi.py" approve --stdin <<'EPI_TEXT'
$ARGUMENTS
EPI_TEXT
```

Tell the user the result in one line. If the output says to run /epi:start <task-id> (the task
can now continue), don't ask: continue it now by running the loop below for that task, starting
at its step 1. For an error, just report. `accept` closes only the issues that are in dispute; later
sign-offs (such as the merge) still wait for a person.

In the loop, `<plugin-root>` is `${CLAUDE_PLUGIN_ROOT}` and `<session-id>` is `${CLAUDE_SESSION_ID}`.

!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/epi.py" doc run-loop`
