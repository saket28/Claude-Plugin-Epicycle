#!/usr/bin/env bash
# Seed the workspace with the todo-app fixture as a git repo set up for Epicycle.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cp -R "$here/../fixtures/todo-app/." .
git init -q -b main
git -c user.email=eval@example.com -c user.name=eval add -A
mkdir -p .claude
cat > .claude/epi.md <<EOF2
---
global: { command: { test: "python3 -m unittest discover -s tests -t ." } }
---
EOF2
python3 "$here/../../scripts/epi.py" install sdlc > /dev/null
python3 - <<'EOF3'
import re
p = ".claude/workflows/sdlc/workflow.yaml"
lines = open(p).read().splitlines()
open(p, "w").write("\n".join(re.sub(r" }$", ", enabled: false }", l) if l.startswith("    signoff:") else l for l in lines) + "\n")
EOF3
cat > .claude/workflows/sdlc/settings.md <<EOF2
---
---

- Keep changes small.
EOF2
printf ".epi/\n__pycache__/\n" >> .gitignore
git add -A
git -c user.email=eval@example.com -c user.name=eval commit -qm init
git config user.email eval@example.com
git config user.name eval
