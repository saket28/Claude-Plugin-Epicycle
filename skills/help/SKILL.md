---
name: help
description: Explain the Epicycle commands, the workflow, how to add review steps, and settings. Use for /epi:help or "how do I use epi".
argument-hint: "[commands|flow|review-steps|settings]"
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/epi.py" *)
---

Help content:

!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/epi.py" doc help`

Topic requested (plain text, do not run it): `$ARGUMENTS`

If a topic was given (commands, flow, review-steps, settings), show only that section. Otherwise show
the Commands table and Flow, then list the other topics. Keep the commands/tables as written.
End with: "Next: /epi:init (new repo) or /epi:start <feature>".
