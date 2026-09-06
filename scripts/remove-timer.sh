#!/usr/bin/env bash
set -euo pipefail

spotguard_user_units="$HOME/.config/systemd/user"

systemctl --user disable --now spotguard-monitor.timer 2>/dev/null || true
rm -f "$spotguard_user_units/spotguard-monitor.timer" "$spotguard_user_units/spotguard-monitor.service"
systemctl --user daemon-reload

echo "RiskPilot compatibility timer units were removed. Project data and ledger were preserved."
