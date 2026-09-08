#!/usr/bin/env bash
set -euo pipefail
systemctl --user disable --now riskpilot-smart-scanner.timer 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/riskpilot-smart-scanner.timer" \
      "$HOME/.config/systemd/user/riskpilot-smart-scanner.service"
systemctl --user daemon-reload
echo "RiskPilot smart scanner removed. Existing RiskPilot services/config were not changed."
