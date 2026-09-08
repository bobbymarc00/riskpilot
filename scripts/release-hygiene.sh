#!/usr/bin/env bash
set -euo pipefail
# Reject sensitive/runtime artefacts from tracked or staged publication candidates.
patterns='(^|/)(config\.json([./_-]|$)|state|backups|\.codex[^/]*|\.openclaw[^/]*)(/|$)|(^|/)(approval\.key|.*\.(db|sqlite|sqlite3)(-wal|-shm)?|.*\.(pem|key|token|secret|credentials)|.*oauth.*|.*\.(log|zip|tar|tgz|tmp|bak|backup|trace)|__pycache__|build|dist|.*\.egg-info|tmp)(/|$)|(^|/).*\.py[co]$'
files=$(git ls-files -co --exclude-standard; git diff --cached --name-only)
if printf '%s\n' "$files" | grep -Eiq "$patterns"; then
  printf '%s\n' 'release hygiene failed: sensitive or generated publication candidate found' >&2
  exit 1
fi

# Prevent obsolete pre-LIVE wording from contradicting the authenticated transport.
if git grep -n -E 'No write transpor[t] is installed in this release|Hard boundary for a futur[e] exact Binance protected Spot write' -- . ':!scripts/release-hygiene.sh'; then
  printf '%s\n' 'release hygiene failed: obsolete protected-live wording found' >&2
  exit 1
fi
