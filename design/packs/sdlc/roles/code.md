---
type: agent
engine: builtin:code-review     # builtin:<name> | agent (the generic reviewer) | run:<command>
model: sonnet
effort: high
at: build                       # the step whose `reviewers` item runs it
when: always
blocking: true
always_rerun: true              # reviews every round, even after a PASS
---
<!--
Runs Claude Code's built-in /code-review on the job's diff, applying the rules in REVIEW.md.
Anything written outside this comment is added as extra instructions for this review.
-->
