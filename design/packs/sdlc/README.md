# sdlc playbook (design draft)

```
pack.yaml               the flow: steps and their items
roles/                  agents, humans and reviewers, one md file each
settings.schema.json    what a repo can tune, with defaults
```

## A step

```
plan  ->  act  ->  review  ->  finalize  ->  next step
           ^         |
           +- round -+   until review passes, at most step.<id>.rounds times
```

| Substep | Runs | If it has no items |
|---|---|---|
| `plan` | once, before the first act | no plan |
| `act` | round 1: every item; later rounds: only items with an open thread | review-only step; a failed review must `reopen` or go to a human |
| `review` | in order; later rounds: only items with an open thread, plus `always_rerun` | the step passes |
| `finalize` | once, after review passes | nothing |

A **round** is one act followed by the review list. Review findings become threads addressed to the
act items. Act items fix them or push back, and the reviewer accepts or not. When `rounds` runs out with
issues still open, a person decides.

A review item with `stop_on_fail` ends the round as soon as it fails, because later reviews would be
wasted. It defaults to `true` for commands and `false` for everything else, so all agent and human
findings reach the act items together.

## Items

| Written as | Is | md file |
|---|---|---|
| `builder` | a role: agent or human | `roles/builder.md` |
| `reviewers` | every role whose `at` is this step and whose `when` matches | each reviewer's own file |
| `{ run: lint }` | a command from settings; an empty command passes | none |
| `{ kind: merge }` | playbook code (`open-pr`, `ci`, `merge`, `cleanup`) | none |
| `{ role: maintainer, via: host }` | a role with options; `via: host` takes the approval from the PR | the role's |

Item options: `stop_on_fail: true|false` (review), `on_fail: retry|act|reopen|human` (finalize).

## Step fields

| Field | Means | Default |
|---|---|---|
| `plan`, `act`, `review`, `finalize` | lists of items | empty |
| `referee` | rules on disputes about the plan | the last plan item, else a human |
| `reopen` | where findings go when no act item can fix them | none: a human |

## Finalize

Finalize runs after approval, so it can't go back into review without losing that approval.
Irreversible actions go here and nowhere else. Each finalize item must be safe to run twice. When one
fails, `on_fail` decides:

- `retry`: run it again, then a human (the default)
- `act`: run this step's act again. The approval is thrown away, because what was approved is no longer
  what would land. Example: the base branch moved, so the merge was refused.
- `reopen`: go to the step's `reopen` target
- `human`: stop

## What a repo tunes (settings)

| Setting | Means |
|---|---|
| `step.<id>.rounds` | act + review cycles before a person decides |
| `step.<id>.skip` | items to leave out, such as `[maintainer]` for no merge approval or `[tester]` |
| `step.<id>.reopens` | times this step may send a job back to its `reopen` target |
| `role.<id>.*` | model, critical thinking, members, write scope (the role file holds the defaults) |
| `step.merge.merge.*`, `step.merge.pr.*` | merge strategy, message, PR options |

## Where today's code goes

| Today | Now |
|---|---|
| `queued` + setup command | the workspace (engine) |
| `spec`, spec reviewers, spec gate | step `spec`; a spec gate is `maintainer` in its review list |
| Questions | spec-writer `asks: maintainer`: questions are threads to a human role |
| `build`, `verify`, `test`, `review` | step `build`: one act, five review items |
| Exchanges + rounds (two counters) | `rounds` (one counter); later rounds rerun only items with open threads |
| Spec-writer ruling | `referee: spec-writer` |
| `merge`: PR, CI, rule check, approval, rebase, verify, merge | step `merge`: act `open-pr`, review list, finalize `merge` |
| Gates, `approve`, `rework` | a human item in a review list; rework is its CHANGES |
| Hardcoded `code` re-review | `always_rerun: true` in `roles/code.md` |

## Open

- The rule check (protected paths, no open issues) runs inside the `merge` kind for now. Should it be a
  review item of its own?
- One `rounds` counter drops today's "full re-review after new issues" pass. Add `final_full_review: true`
  if that's missed.
- Board columns are steps, so Build covers what were four columns. Cards show the current item.
