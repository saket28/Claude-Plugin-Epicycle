---
name: review
description: Run the named review steps (default all) on the current branch and write one consolidated report. No task needed.
argument-hint: "[reviewer-names...] [--base <branch>]"
disable-model-invocation: true
allowed-tools: Read, Write, Bash, Agent, Skill
---

Review the current branch with the reviewers named in `$ARGUMENTS` (a plain-text list; none named = every agent and skill review step of the workflow, including the ones the repo's `workflow.yaml` adds), then produce one report.

Get the plan with Bash, passing the arguments through stdin (the marker is quoted, so the shell does not
interpret them; leave the lines empty when none were given):

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/epi.py" review-plan --stdin <<'EPI_TEXT'
$ARGUMENTS
EPI_TEXT
```

If the plan is an `error`, show it and stop. If `files` is empty, say there is nothing to review
and stop. Otherwise start all reviewers **in parallel** (one message, several calls). Each writes
`<report_dir>/review-<name>.md` in the reviewer format
(`verdict: PASS|CHANGES`, `## Important`, `## Nit`). Never modify the code under review.

By `kind`:
- `skill`, `target` `code-review`: run the `code-review` skill on `<base>...HEAD` (REVIEW.md applies when `has_review_md`).
- `skill`, `target` `security-review`: run the `security-review` skill (same REVIEW.md rule). Any other skill: run it by name.
- `agent`: spawn the project agent `target` (`.claude/agents/<target>.md`). Its prompt: the diff command `git diff <base>...HEAD`, and the `output` path. No spec exists, so judge the change on its own merits.

For built-in skills, convert their findings into the reviewer format with a severity of
Important (would break behaviour, lose or leak data, or break a rule in REVIEW.md) or Nit.
If a reviewer fails to run, record `verdict: ERROR` with the reason; do not stop the others.

Then write `<report_dir>/report.md`:

1. Header: branch vs base, number of files, date, and an overall verdict. **BLOCKED** if any
   `blocking` reviewer says CHANGES or ERROR, else **CLEAR** (note advisory findings).
2. A summary table: reviewer, kind, verdict, Important count, Nit count, blocking?
3. **Important findings**, merged across reviewers. Combine duplicates (same place, same issue)
   and name every reviewer that raised them. Order by risk. Each: `path:line`, the problem, why it
   matters, how to fix.
4. **Nits**, grouped by file, briefly.
5. Anything in `skipped`, and why.

Finish by showing the user the verdict, the table, the top Important findings (max 5), and the
report path. Do not commit anything and do not fix the findings unless asked.
