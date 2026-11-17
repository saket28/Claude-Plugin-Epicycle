# The orchestrator loop

The workflow logic lives in the engine (`scripts/engine/machine.py`), driven by the workflow's `workflow.yaml`. The orchestrator only repeats:

1. `epi.py next <task> --session <id>` → one JSON action.
2. Do that action (spawn agents in parallel, answer questions, convert a review, start a
   background waiter), and nothing else.
3. Back to 1, until the action is `wait`, `done` or `failed`.

The `start` skill contains the exact instructions.
