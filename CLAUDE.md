# Epicycle plugin: working agreements

A Claude Code plugin (`epi`): a Python stdlib engine in `scripts/engine/` (entry point `scripts/epi.py`), skills in
`skills/`, agents in `agents/`, workflow packs in `workflows/` (the `sdlc` pack), templates in `templates/` (including
the one-file dashboard `templates/dashboard.html`), and in-product help in `reference/`. [README.md](README.md) is the
user-facing doc; keep it and `reference/help.md` in step with any behaviour change.

## How to think here: critical thinking level 3 (Challenge)
Question instructions, specs and earlier findings. Push back on weak points, with evidence for each objection
(a file, a line, a test, a rule in this file). Say what you would change instead, then do the part you can.
Don't invent objections: with no evidence, do the work. The same applies to your own earlier work in a session.

## Settings: make it configurable when a repo could reasonably want to change it
Every setting is `<tab>.<section>.<setting>`, where the tab is `global`, `stage` or `agent`.
Global settings live in the repo's `.claude/epi.md`; stage and agent settings live in
`.claude/workflows/<pack>/settings.md`. When you add behaviour with a hardcoded number, name, template or on/off
choice, expose it:
1. Default in `DEFAULTS` (global), `STAGE_DEFAULTS` or `AGENT_DEFAULTS` in [config.py](scripts/engine/config.py).
   Read it with `cfg.v("tab.section.setting")`.
2. A field in the schema: [epi.schema.json](templates/epi.schema.json) for `global`,
   [settings.schema.json](workflows/sdlc/settings.schema.json) for a pack's `stage` and `agent` settings. Give it a
   type, `options`, `min`/`max`, `placeholders`, `item_options`, and a one-sentence `help`. The schema is the only
   place for allowed values and ranges; `validate()` reads it. Never list an enum twice. Help text never points
   at a doc file by name.
3. Use it in the code, and add a test in `tests/test_settings_behaviour.py` that shows the behaviour changes.
4. If a team would commonly change it, add a row to the settings table in README.md, and mention it in
   `reference/help.md`.
`tests/test_schema.py` fails if the schema and defaults drift. Unknown keys in settings files are errors, never
ignored. Ask of each new constant: would a team plausibly set this differently? If yes, it is a setting.

## Product decisions already made
- No legacy or migration code unless asked. Breaking a settings shape is fine; migrate the user's own
  `.claude/epi.md` and `settings.md` by hand and say so.
- Sign-offs are on or off per step in `workflow.yaml` (`enabled: false` turns one off). There is no conditional gate.
- "Needs you" is a state a task is in, never a stage or column. One alert kind covers decisions, failures and
  errors. Tasks stay in the stage where they stopped.
- Agents push back with evidence (critical thinking 0-5). A checker that keeps a finding the builder disputed
  is a stalemate and goes to a human.
- Principles (`.claude/principles/*.md`) are how the repo does things; briefs list the applicable ones, agents
  read them. Humans write them. Agents never edit principles, reviewers, `.claude/epi.md`, workflows, agents,
  `CLAUDE.md`, `REVIEW.md` or CI files (see `global.guardrail.protected_paths`).
- Learnings and answers are written straight into the pack's `learnings.md` by the retro; leave those edits
  uncommitted.

## Dashboard
Minimal UI text. Edit and delete icons look the same everywhere. Alert icon, not a bell. Cards don't carry
explanations (the side drawer does). Render from the schema, not hardcoded lists. HTML changes need only a
reload; Python changes need a server restart.

## Code
- Briefs are ordered stable-first (stance, principles, answers, learnings, then the task) so they cache.
- `configedit.py` edits one line of a settings file and keeps comments; keep it that way.
- Prefer deleting code over compatibility shims. Keep functions small and tested.

## Verify before saying done
- `python3 -W ignore -m unittest discover -s tests` (all pass; takes about two minutes).
- JS: extract the last `<script>` of `templates/dashboard.html` and `node --check` it. Headless Chrome screenshots
  are flaky here: render functions in node, or hit the API with `curl` (token header + matching Origin).
- Test against a real repo when settings or the dashboard change.

## Working with the user
Concise answers. Commit and push only when told to. Don't `pkill` broadly: it kills the user's running
dashboard; stop only the process you started. Bump the version in both `.claude-plugin/plugin.json` and
`.claude-plugin/marketplace.json` when releasing.
