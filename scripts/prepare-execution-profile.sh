#!/usr/bin/env bash
set -euo pipefail

project_dir="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
profile_dir="${RISKPILOT_EXECUTION_HOME:-$HOME/.codex-riskpilot-execution}"

if [[ -e "$profile_dir" && ! -d "$profile_dir" ]]; then
  echo "Execution profile path exists but is not a directory: $profile_dir" >&2
  exit 2
fi
mkdir -p "$profile_dir/workspace"
chmod 700 "$profile_dir" "$profile_dir/workspace"
install -m 600 "$project_dir/config.execution.example.toml" "$profile_dir/config.toml"

echo "Prepared protected-live profile template at: $profile_dir/config.toml"
echo "No OAuth login was performed. No MCP server was connected."
echo "Before login, review docs/LIVE_EXECUTION_SETUP.md and the Binance scopes."

