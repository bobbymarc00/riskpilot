#!/usr/bin/env bash
set -euo pipefail

spotguard_project_dir="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
spotguard_workspace="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
spotguard_config="$spotguard_project_dir/config.json"
spotguard_skill_source="$spotguard_project_dir/skills/binance-spotguard"
spotguard_extension_source="$spotguard_project_dir/extensions/riskpilot-direct-review"
spotguard_extension_target="$spotguard_workspace/.openclaw/extensions/riskpilot-direct-review"
spotguard_bin_target="$HOME/.local/bin/spotguard"
riskpilot_bin_target="$HOME/.local/bin/riskpilot"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required" >&2
  exit 1
fi

mkdir -p "$spotguard_workspace/skills" "$spotguard_workspace/state" "$HOME/.local/bin"

if [ ! -f "$spotguard_config" ]; then
  spotguard_init_args=(--config "$spotguard_config" init --workspace "$spotguard_workspace")
  if [ -n "${SPOTGUARD_TELEGRAM_CHAT_ID:-}" ]; then
    spotguard_init_args+=(--chat-id "$SPOTGUARD_TELEGRAM_CHAT_ID")
  fi
  "$spotguard_project_dir/spotguard" "${spotguard_init_args[@]}"
fi
chmod 600 "$spotguard_config"
chmod +x "$spotguard_project_dir/riskpilot" "$spotguard_project_dir/spotguard"
"$spotguard_project_dir/riskpilot" --json check >/dev/null

if ! command -v openclaw >/dev/null 2>&1; then
  echo "OpenClaw CLI is required to install the RiskPilot skill." >&2
  exit 1
fi
# OpenClaw's workspace root is a containment boundary: a symlink to this
# nested repository is skipped unless the operator widens global trust.  Its
# supported local installer copies the reviewed skill into workspace/skills.
(cd "$spotguard_workspace" && openclaw skills install --force "$spotguard_skill_source")
openclaw skills info binance-spotguard --json >/dev/null

# The deterministic Telegram extension is canonical in this repository.  Copy
# only its reviewed runtime entrypoint and manifests; it has no dependencies,
# state, credentials, or OpenClaw global configuration to install.
mkdir -p "$spotguard_extension_target"
install -m 0644 "$spotguard_extension_source/index.js" "$spotguard_extension_target/index.js"
install -m 0644 "$spotguard_extension_source/openclaw.plugin.json" "$spotguard_extension_target/openclaw.plugin.json"
install -m 0644 "$spotguard_extension_source/package.json" "$spotguard_extension_target/package.json"

if [ -L "$riskpilot_bin_target" ]; then
  riskpilot_existing_bin="$(readlink -f "$riskpilot_bin_target")"
  if [ "$riskpilot_existing_bin" != "$spotguard_project_dir/riskpilot" ]; then
    echo "command path already points elsewhere: $riskpilot_bin_target" >&2
    exit 1
  fi
elif [ -e "$riskpilot_bin_target" ]; then
  echo "command path already exists and was not changed: $riskpilot_bin_target" >&2
  exit 1
else
  ln -s "$spotguard_project_dir/riskpilot" "$riskpilot_bin_target"
fi

if [ -L "$spotguard_bin_target" ]; then
  spotguard_existing_bin="$(readlink -f "$spotguard_bin_target")"
  if [ "$spotguard_existing_bin" != "$spotguard_project_dir/spotguard" ]; then
    echo "command symlink already points elsewhere: $spotguard_bin_target" >&2
    exit 1
  fi
elif [ -e "$spotguard_bin_target" ]; then
  echo "command path already exists and was not changed: $spotguard_bin_target" >&2
  exit 1
else
  ln -s "$spotguard_project_dir/spotguard" "$spotguard_bin_target"
fi

echo "RiskPilot installed in PAPER mode."
echo "Config: $spotguard_config"
echo "Skill:  $spotguard_workspace/skills/binance-spotguard (OpenClaw-managed local install)"
echo "Extension: $spotguard_extension_target (restart or reload OpenClaw to activate)"
echo "Next:   $spotguard_project_dir/scripts/configure-codex-agent-os.sh"
