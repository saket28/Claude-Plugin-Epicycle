---
name: init
description: Set up Epicycle in this repository (.claude/epi.md, the sdlc playbook, REVIEW.md, .epi/, permissions for the agents).
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/epi.py" *)
---

Set up this repository for Epicycle.

Detected so far:
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/epi.py" init --detect || true`

1. Show the user what was detected (language, commands, host, tester paths) in a short list.
2. Ask, in ONE AskUserQuestion call, only about what's missing or uncertain: the test, lint and
   build commands; whether a person signs off specs and changes before they merge (default yes for both; no means `enabled: false` on that stage's `signoff` in the installed `.claude/workflows/<playbook>/workflow.yaml`, which you edit after init); and, if the host is `github`
   but `gh` is missing, whether to install it (`brew install gh` on macOS, `winget install
   GitHub.cli` on Windows, the distro package on Linux). If they install it, ask them to run
   `! gh auth login` themselves. If they decline, the host becomes `local`.
3. Write the settings that differ from the defaults (only those) as JSON to a temp file, for
   example `{"global": {"command": {"setup": "...", "build": "...", "lint": "...", "test": "..."}}, "agent": {"tester": {"paths": [...]}}}` (the detected `global` and `agent` objects are already in this shape; a declined `gh` adds `"host": {"adapter": "local"}` under `global`), and run
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/epi.py" init --write <file>`. `global` goes to `.claude/epi.md`; `agent` and `stage` go to the installed playbook's `settings.md`. Init installs the default playbook (like `/epi:install`) when none is installed. It also creates empty starter principles in `.claude/principles/` (architecture, design-system, personas, security, testing) and lists them under `global.principle.list`; tell the user to fill them in, or offer to draft the architecture one from what you can see in the repo. Record each answer the user gave under `## Answers` in `.claude/workflows/<playbook>/learnings.md`
   as `- [A-xxxx] all · Question? Answer. (init)`.
4. Tell the user that `init` also wrote `.claude/settings.local.json` (not committed): it lets agents work in the task
   worktree directory (`permissions.additionalDirectories`), which Claude Code applies after the folder trust prompt.
   Agents run the configured commands through `epi.py run`, which the plugin allows without a prompt. `learnings.md.lock` and `.claude/settings.local.json` are added to `.gitignore`.
5. Tell the user to commit `.gitignore`, `REVIEW.md`, `.claude/epi.md`, `.claude/workflows/`, `.claude/agents/` and `.claude/principles/` before the first task: a merge needs a clean main
   checkout (`learnings.md` may stay uncommitted; the retro edits it).
6. Offer to turn on Claude Code's Bash sandbox in the user's settings, explaining that it limits
   where agents' shell commands can write. Only change settings if they agree.
7. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/epi.py" status --check` and report the result. Finish with: "Next: /epi:intake or /epi:start".
