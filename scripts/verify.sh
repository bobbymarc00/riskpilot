#!/usr/bin/env bash
set -euo pipefail
project_dir="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$project_dir"
export PYTHONPATH="$project_dir/src${PYTHONPATH:+:$PYTHONPATH}"
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests scripts/demo_track_a.py
python3 -c 'from spotguard.config import load_settings; load_settings("config.example.json", create_state=False)'
python3 -c 'import json; from pathlib import Path; [json.loads(p.read_text()) for p in Path("schemas").glob("*.json")]'
find scripts -type f -name "*.sh" -exec bash -n {} +
if command -v shellcheck >/dev/null 2>&1; then shellcheck scripts/*.sh; fi
"$project_dir/riskpilot" --help >/dev/null
"$project_dir/spotguard" --help >/dev/null
