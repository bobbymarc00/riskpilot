#!/usr/bin/env bash
set -euo pipefail

spotguard_project_dir="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
spotguard_mcp_name="binance-mcp-server"
spotguard_mcp_url="https://agent.binance.com/mcp/agentic"

if ! command -v codex >/dev/null 2>&1; then
  echo "Codex CLI is not installed." >&2
  echo "Install it with the official OpenAI command:" >&2
  echo "curl -fsSL https://chatgpt.com/codex/install.sh | sh" >&2
  exit 1
fi

if ! codex login status >/dev/null 2>&1; then
  echo "Codex CLI is not signed in." >&2
  echo "On this headless VPS run: codex login --device-auth" >&2
  echo "Sign in with the ChatGPT account that includes your Codex access." >&2
  exit 2
fi

if spotguard_existing_mcp="$(codex mcp get "$spotguard_mcp_name" --json 2>/dev/null)"; then
  case "$spotguard_existing_mcp" in
    *"$spotguard_mcp_url"*)
      echo "Binance MCP server is already registered in Codex CLI."
      ;;
    *)
      echo "A Codex MCP server named $spotguard_mcp_name already exists with another URL." >&2
      echo "It was not changed. Inspect it with: codex mcp get $spotguard_mcp_name --json" >&2
      exit 3
      ;;
  esac
else
  codex mcp add "$spotguard_mcp_name" \
    --url "$spotguard_mcp_url" \
    --oauth-client-id codex
fi

echo
echo "Starting Binance authorization for the registered Codex client."
echo "For the Track A paper demo, grant Market Data only."
echo "Do not grant Futures, Margin, Transfer, or unrelated products."
echo
codex mcp login "$spotguard_mcp_name"

echo
"$spotguard_project_dir/riskpilot" --json agent-os status
echo
echo "Configuration saved. Test one verified live read with:"
echo "riskpilot --json agent-os market --symbol BTCUSDT"
