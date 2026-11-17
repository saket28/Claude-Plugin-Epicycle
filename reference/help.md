# Epicycle help

A feature goes through the stages **Spec, Build and Release**. You step in at sign-offs and when a task **needs you** (it shows the alert icon and a red border on its card, in whatever stage it stopped, and is listed at the top of the board).

## Commands

| Command | Use it to | Example |
|---|---|---|
| `/epi:help [topic]` | Show this help. Topics: `commands`, `flow`, `review-steps`, `settings` | `/epi:help settings` |
| `/epi:init` | Set up a repo once: writes `.claude/epi.md`, installs the `sdlc` workflow, writes `REVIEW.md`, `.epi/`, and permissions for the agents | `/epi:init` |
| `/epi:install <playbook> [--force]` | Copy a playbook into the repo (`.claude/workflows/<playbook>/`, its agents into `.claude/agents/`); the repo owns the copy | `/epi:install sdlc` |
| `/epi:upgrade <playbook> [--dry-run]` | Merge the plugin's newer playbook into your copy, three ways (workflow.yaml by step and field, other files by line); nothing is written when a file conflicts | `/epi:upgrade sdlc --dry-run` |
| `/epi:intake <text>` | Park a feature in the backlog without starting it | `/epi:intake Add CSV export` |
| `/epi:start [id \| text]` | Start the next backlog item, a backlog card by its number (`/epi:start 6`), a new feature, or continue or retry a task | `/epi:start Add rate limiting` |
| `/epi:status [id] [--check]` | See tasks, stages, tokens, and what needs you. `--check` is a health check | `/epi:status --check` |
| `/epi:approve <id> <approval>` | Sign off the stage that is waiting (`spec` or `build`), add budget (`budget +500k`), `accept` (accept a disagreement or failure as it is; later sign-offs still wait for you), `retry` (try a failed step again) | `/epi:approve 12 spec` |
| `/epi:send-back <id> [--to <step>] "<instructions>"` | Send a task back to an agent of this stage or an earlier one (default: the nearest) with your instructions. In this stage a final round applies them; an earlier stage runs again, then the ones after it | `/epi:send-back 12 --to spec-writer "AC-2 should allow empty input"` |
| `/epi:cancel <id>` | Stop a task and remove its checkout (the record and branch are kept) | `/epi:cancel 12` |
| `/epi:review [names] [--base b]` | Run the named review steps (agents and skills; default all) on the current branch, no task needed, and get one merged report | `/epi:review code a11y` |
| `/epi:dashboard` | Open the local dashboard: board, progress, decisions, tokens, settings, help | `/epi:dashboard` |

The dashboard has buttons for the same decisions (Accept as is, Send back with a *Send to* list, Try again, Approve, and Answer for an agent's questions). The board and the open task refresh by themselves every few seconds, keeping what you have open or typed. A decision you already made is not applied twice (a second click says it was already decided). It cannot run agents, so afterwards run `/epi:start task-<n>` in Claude Code to continue.

## Flow

A task runs through the **stages** of the workflow (`global.workflow.name`, default `sdlc`, installed in your repo at `.claude/workflows/sdlc/workflow.yaml`, which you own and edit; a repo can bring its own workflow in `.claude/workflows/<name>/` too). A stage's phases run in this order: **Plan** (once); the loop **Do → Verify → Review**, where one pass is a **round**; **Finalize** (once the loop passed); and **Finish**, which starts with **your sign-off** (approve to go on, or send it back with instructions). Turn a sign-off off with `enabled: false` on its `signoff` line in `workflow.yaml`.

1. Spec: the spec-writer writes the spec with a short title and testable acceptance criteria. You sign it off before anything is built.
2. Build, the loop: the builder implements it in its own checkout (`<repo>.epi-worktrees/task-<n>`). **Verify** runs your `lint`, `build` and `test` commands, all of them, so one round reports every failure. **Review** runs only when they pass: an independent tester, Claude Code's `code-review`, and `security-review` when the spec's risk is medium or high. Each finding becomes a thread addressed to the builder.
3. In the next round only the builder (if it has an open thread) works; commands run again, and a review step runs again if it has an open thread, is `always_rerun`, or never ran. Review skills are given the spec, their open threads and their previous report, and look only at what changed. The loop ends when nothing blocking is open. When `stage.build.rounds` (default 3) is used up with issues still open, the task needs you. An issue raised in the last round that nobody answered gets one extra round first.
4. Build, Finalize: `open-pr` pushes and opens or updates the pull request, `ci` waits for CI. A failure (a conflict, red CI) goes back to the builder as another round. Then your sign-off: the pull request's own approval on GitHub, `/epi:approve <id> build` locally.
5. Release: rebase if the base moved since your sign-off, run the tests, then `merge` and `cleanup`. A failure here needs you: try again, accept it, or send it back to the builder. (Production monitoring and rollback will go here.)

Whatever an agent leaves uncommitted in the task's checkout is committed by the engine before Verify runs, so the checks, the reviewers and the merge always see it. Reports live in `.epi/task-<n>/<stage>/r<round>/`; the round number counts within the stage and is the one the dashboard shows. When a task needs you, `/epi:status` shows the command that moves it forward, and the dashboard shows the decision with buttons.

## Review steps

A repo adds a step to any phase of a stage by adding a line to its `.claude/workflows/<playbook>/workflow.yaml` (one step per line, in the flow style the file already uses). Most added steps are checks: an agent or skill in **Review**, a command in **Verify**:

```yaml
# .claude/workflows/sdlc/workflow.yaml, in the build stage
    review:
      - { uses: agent/a11y, if: touches-ui }                       # an agent: .claude/agents/a11y.md
    verify:
      - { run: license-check, if: "paths:[package.json]" }         # a command
```
```yaml
# .claude/epi.md
global:
  command: { license-check: "npx license-checker --failOn GPL" }
```

| Kind | Step | Use it for | Cost |
|---|---|---|---|
| Agent | `uses: agent/<name>` with a file `.claude/agents/<name>.md` (a Claude Code subagent: `name`, `description`, `tools`, `model`, then its instructions) | Most custom reviews: UX, marketing, accessibility, architecture | tokens |
| Skill | `uses: skill/<name>` | A review that is already a skill. `code-review` and `security-review` are in the workflow; they apply `REVIEW.md` | tokens |
| Command | `run: <name>` with `global.command.<name>` | Deterministic checks. Exit 0 = PASS, anything else = a blocking finding with the output | none |

Options: `if` (an expression: `always`, `never`, `touches-ui`, `risk >= medium`, `paths:[globs]`, `steps.<id>.outcome == failure`, `steps.<id>.outputs.<name>`, `stages.<stage>.steps.<id>.outputs.<name>`, with `==` `!=` `<` `>`, `and`, `or`, `not`, parentheses), `outputs` (names a step publishes for the steps below it), `env` (run steps: `{ NAME: value }`, `${{ expr }}` allowed in values and in `with`, never in a command), `continue-on-error: true` (advisory: findings are recorded but never block), `with` (inputs for the agent or skill), `always_rerun`, `timeout-minutes` (commands), `id` (to tell two steps with the same name apart), `name` and `doing` (how the step is shown). Leave a step out with `enabled: false` on its line in `workflow.yaml`. Tune an agent with `agent.<name>.model`, `critical_thinking`, `effort`, `writes` and `paths`. Run `/epi:status --check` after editing by hand. Agents may not edit `.claude/epi.md`, `.claude/workflows/**`, `.claude/agents/**`, `.claude/principles/**` or `review-*.md` during a task. A playbook's agents are copied to `.claude/agents/<playbook>-<agent>.md` (Claude Code only finds subagents there); a step `uses: agent/<name>` runs `<playbook>-<name>` when that file exists, else a repo agent `<name>`, else the plugin's.

Agents work in the task's checkout, outside your repo. `/epi:init` allows that folder (`permissions.additionalDirectories` in `.claude/settings.local.json`). Agents run your configured commands as `epi.py run <task> <name>` (their brief gives the exact line), which the plugin allows without a prompt; it runs `global.command.<name>` in the checkout and takes no other arguments.

## Principles

Principles say how the repo does things: architecture, design system, personas, security, testing. They are markdown files in `.claude/principles/`; `/epi:init` creates empty starters. Fill them in, add your own, or edit and remove them in the dashboard: Settings, Global, advanced settings, Principles. Each file's frontmatter has a `summary`, `applies_to` (which agents see it; empty = all) and an optional `when` (for example `touches-ui`). Agents are pointed at the applicable ones in their brief and read what is relevant. Files with no text yet are skipped. Agents can't edit them.

## Critical thinking

Agents can question their instructions before acting, and push back with evidence instead of complying silently. The level is a setting (`global.pipeline.critical_thinking`, default 1); each agent (`agent.<name>.critical_thinking`) can set its own. Higher levels cost more tokens and rounds.

| Level | Name | What the agent does |
|---|---|---|
| 0 | Off | Follows instructions as given. Nothing is added to its brief. |
| 1 | Flag | Raises an objection only when an instruction clearly contradicts the spec, the code or the repo's rules. |
| 2 | Check | Checks instructions against the spec, the code and the rules; flags contradictions and gaps. |
| 3 | Challenge | Questions instructions, specs and earlier findings. Pushes back on weak points, with evidence for each objection. |
| 4 | Sceptical | Assumes there are mistakes until checked. Pushes back early and says what would change its mind. |
| 5 | Adversarial | Challenges every assumption, including the task itself. Proposes a better approach before starting. |

Agents talk in threads, one per issue, shown in the task drawer under each stage in Progress (an issue spans the rounds of its stage) and in `conversation.md`. A review step or the tester raises an issue (blocking, or a note), the builder fixes it or pushes back, and the one who raised it accepts or pushes back again. A plain-code director routes each comment, closes an issue when its raiser accepts, and brings it to you when the two sides still disagree or the rounds run out (each open issue says whether it was found in the last round and never answered, or which side disagrees). You answer with **Accept as is** (`/epi:approve <id> accept`: the work stands; only the issues in dispute close) or **Send back** (`/epi:send-back <id> [--to <step>] "instructions"`: to the builder for a final round, or to an earlier step such as the spec writer, after which the later stages follow). A command that passes again closes the issues it raised. Notes (minor findings) close by themselves when their stage passes. Names in a thread are step ids.

## Settings

Settings live in files, one owner each, and only values that differ from defaults are stored: `.claude/epi.md` holds the repo-wide `global.*` settings; `.claude/workflows/<playbook>/settings.md` holds the playbook's `stage.*` and `agent.*` settings in its frontmatter and the playbook's rules for agents in its body; `learnings.md` next to it holds Learnings and Answers, written by the retro. A setting in the wrong file is an error. Structure (which steps a stage has, sign-offs) is in `workflow.yaml`, not a setting. Edit by hand, or in the dashboard Settings tab (it shows a diff before saving). Every setting is named `<tab>.<section>.<setting>`. The tabs are `global`, `stage` (a section per stage of the workflow) and `agent` (a section per agent or skill). The dashboard shows **Global** (the basic settings first, the rest behind "Show advanced settings"), **Workflow** (stages, their steps and stage and step settings) and **Learnings**. `/epi:status --check` says when the plugin has a newer version of your playbook (`/epi:upgrade`). In the **Workflow** tab you can switch steps and sign-offs on and off, move them up or down within a phase, remove them and add new ones (each change shows its diff before it is saved to `workflow.yaml`, with comments and layout kept), and edit the playbook's **rules** (the body of `settings.md`). When the plugin has a newer playbook, a banner at the top of that tab (*sdlc 0.7.0 → 0.8.0 available*) lets you **Review** the per-file changes and **Upgrade**; a conflict writes nothing and names the files.

- `stage.<id>`: `rounds` (Do, Verify and Review cycles, default 3), `budget_tokens`, `requirements` (extra requirements for the stage's agents, for example sections every spec needs). `stage.build` also has `pr_draft`, `pr_labels`, `pr_reviewers` and `ci_required`; `stage.release` has `strategy`, `message`, `delete_branch` and `cleanup_worktree`.
- `agent.<name>`: `model` (empty = the agent file's own), `critical_thinking`, `effort` and `recheck` (for skills: `recheck: threads`, the default, makes rounds 2 and up one small call that answers the skill's open threads; `full` reviews again), `writes` (`output`, `worktree` or `paths`) and `paths`.
- `global`: `workflow.name`, `pipeline` (`critical_thinking`, `judge_model`, `agent_attempts`, `nits_block`), `learning` (`mode`, `on`: `clean`, `helped`, `done`, `failed`; default `helped` and `failed`, so a task that passed first time costs no retro), `repo` (branches, `branch_name`, folders), `command` (`setup`, `build`, `lint`, `test`, `timeout`, and any command a `run` step names), `human.<role>` (who may approve as a role), `host` (`local`, `github`), `board`, `budget` (including `context_window`, for the context fill each step shows), `dashboard` (`stall_after`: minutes of no activity before a running task shows as stalled; `activity`: how many recent events the activity feed lists, 0 hides it), `wait` (`interval`, `max_hours`), `guardrail` and `principle.list`. Full list: `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/epi.py config`.
