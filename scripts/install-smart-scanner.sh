#!/usr/bin/env bash
set -euo pipefail

project_dir="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
expected_dir="$HOME/.openclaw/workspace/tools/spotguard-agent-os"
user_units="$HOME/.config/systemd/user"

if [ "$project_dir" != "$expected_dir" ]; then
  echo "Smart scanner installer expects the project at:" >&2
  echo "  $expected_dir" >&2
  echo "Current path:" >&2
  echo "  $project_dir" >&2
  exit 1
fi

if [ ! -f "$project_dir/config.json" ]; then
  echo "config.json not found; existing RiskPilot must be configured first" >&2
  exit 1
fi

if systemctl --user is-active --quiet spotguard-monitor.timer 2>/dev/null; then
  echo "Refusing to enable Smart Scanner while spotguard-monitor.timer is active." >&2
  echo "Stop the old scheduled scanner first to prevent overlapping Binance requests:" >&2
  echo "  systemctl --user disable --now spotguard-monitor.timer" >&2
  exit 1
fi

if [ ! -f "$project_dir/smart-scanner.json" ]; then
  cp "$project_dir/smart-scanner.example.json" "$project_dir/smart-scanner.json"
  chmod 600 "$project_dir/smart-scanner.json"
  echo "Created smart-scanner.json from the safe example."
fi

chmod +x "$project_dir/scripts/riskpilot-smart-scanner.py"
mkdir -p "$user_units"
install -m 0644 "$project_dir/systemd/riskpilot-smart-scanner.service" "$user_units/riskpilot-smart-scanner.service"
install -m 0644 "$project_dir/systemd/riskpilot-smart-scanner.timer" "$user_units/riskpilot-smart-scanner.timer"

# The old scanner is intentionally not enabled or modified here.
systemctl --user daemon-reload
systemctl --user enable --now riskpilot-smart-scanner.timer

echo
echo "Smart scanner timer enabled. Existing spotguard-monitor.timer was NOT changed."
systemctl --user status riskpilot-smart-scanner.timer --no-pager || true
