#!/usr/bin/env bash
set -euo pipefail

spotguard_project_dir="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

echo "Direct Binance MCP setup in OpenClaw is not supported by this release." >&2
echo "OpenClaw remains the Telegram router; Codex CLI is the supported Agent OS client." >&2
echo "Run: $spotguard_project_dir/scripts/configure-codex-agent-os.sh" >&2
exit 2
