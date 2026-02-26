#!/bin/bash
set -euo pipefail

# Only run in Claude Code on the web remote sessions
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

echo '{"async": true, "asyncTimeout": 120000}'

cd "$CLAUDE_PROJECT_DIR"
# Install dependencies; ignore errors if already provided by system packages
pip install -r requirements.txt --quiet --break-system-packages 2>/dev/null \
  || pip install -r requirements.txt --quiet 2>/dev/null \
  || true

# Verify Flask is importable
python3 -c "import flask" && echo "[hook] flask OK"
