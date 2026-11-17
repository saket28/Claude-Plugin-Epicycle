---
type: agent
engine: builtin:security-review
model: sonnet
at: build
when: "risk>=medium"
blocking: true
---
<!--
Runs Claude Code's built-in /security-review on the job's diff. The rules in REVIEW.md are appended too.
Anything written outside this comment is added as extra instructions for this review.
-->
