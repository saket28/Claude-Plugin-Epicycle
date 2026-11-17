# Review instructions

Used by Claude Code's code review: the managed Code Review service on pull requests, and the
sdlc plugin's built-in `code-review` step.

## What Important means here
Important means a correctness bug, a security problem, a violation of the spec, broken tests, or
a change that would lose or leak data or block a rollback. Docs and docstrings, naming, style,
refactoring, missing extra tests and hardening for inputs the spec does not mention are Nit, unless
the spec or this file requires them.

## Cap the nits
Report at most five Nits per review.

## Do not report
- Anything CI already enforces: lint, formatting, type errors
- Generated files and lockfiles

## Always check
- New behaviour has a test
