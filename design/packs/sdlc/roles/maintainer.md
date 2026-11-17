---
type: human
members: []                     # empty = whoever runs the CLI or opens the dashboard (routing, not auth)
can: [review, answer, override, cancel, retry, budget]   # review = PASS, or CHANGES with feedback
writes: []                      # no artifacts edited by hand; CHANGES feedback is the way to change work
notify: none                    # none | dashboard | command:<shell template>
---
Shown in the side drawer when a job waits on you.

- Reviewing a spec (when the spec step includes you): read spec.md. PASS, or CHANGES with what should change.
- Questions: answer only what the spec-writer couldn't find in the code, Answers or CLAUDE.md.
- Rounds used up: read the conversation. Override sides with the builder; CHANGES is your ruling and the
  final round.
- Reviewing the merge: on GitHub this is the PR's approval; locally, /<fw>:review <job> pass.
