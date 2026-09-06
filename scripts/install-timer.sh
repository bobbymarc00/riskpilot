#!/usr/bin/env bash
set -euo pipefail

spotguard_project_dir="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
spotguard_expected_dir="$HOME/.openclaw/workspace/tools/spotguard-agent-os"
spotguard_user_units="$HOME/.config/systemd/user"

if [ "$spotguard_project_dir" != "$spotguard_expected_dir" ]; then
  echo "timer expects the project at $spotguard_expected_dir" >&2
  echo "current path is $spotguard_project_dir" >&2
  exit 1
fi

mkdir -p "$spotguard_user_units"
install -m 0644 "$spotguard_project_dir/systemd/spotguard-monitor.service" "$spotguard_user_units/spotguard-monitor.service"
install -m 0644 "$spotguard_project_dir/systemd/spotguard-monitor.timer" "$spotguard_user_units/spotguard-monitor.timer"

systemctl --user daemon-reload
systemctl --user enable --now spotguard-monitor.timer
systemctl --user status spotguard-monitor.timer --no-pager
