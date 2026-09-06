from __future__ import annotations
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from spotguard.config import load_settings
from spotguard.live_execution import LiveExecutionAdapter
from spotguard.telegram import candidate_message, proposal_message
from tests.helpers import config_dict

ROOT = Path(__file__).resolve().parents[1]

class RiskPilotBrandTests(unittest.TestCase):
    def test_primary_and_legacy_launchers_share_config_and_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.json"
            path.write_text(json.dumps(config_dict(root)))
            env = {**os.environ, "SPOTGUARD_CONFIG": str(path)}
            a = subprocess.run([str(ROOT/"riskpilot"), "--json", "status"], text=True, capture_output=True, env=env, check=True)
            b = subprocess.run([str(ROOT/"spotguard"), "--json", "status"], text=True, capture_output=True, env=env, check=True)
            self.assertEqual(json.loads(a.stdout)["database"], json.loads(b.stdout)["database"])

    def test_help_and_notifications_use_riskpilot_brand(self):
        result = subprocess.run([str(ROOT/"riskpilot"), "--help"], text=True, capture_output=True, check=True)
        self.assertIn("RiskPilot", result.stdout)
        candidate = {"symbol":"BTCUSDT","interval":"15m","score":80,"price":100.0,"id":"c-1234567890ab","metrics":{"rsi_14":50,"atr_pct":1,"volume_ratio_20":2}}
        self.assertIn("RISKPILOT", candidate_message(candidate)[0])
        proposal = {"mode":"paper","symbol":"BTCUSDT","quote_amount":"25","entry_reference":"100","stop_reference":"99","take_profit_reference":"102","reward_risk":"2","id":"p-1234567890ab","expires_at":"2999-01-01T00:00:00Z","rationale":"test","canonical":{"source":"manual-paper-test"}}
        self.assertIn("RISKPILOT", proposal_message(proposal, "A"*22, "CODE1234")[0])

    def test_live_profile_matches_paper_but_stays_not_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root/"config.json"
            path.write_text(json.dumps(config_dict(root)))
            settings = load_settings(path)
            self.assertEqual(settings.live.max_quote_per_entry_usdt, settings.paper.max_quote_per_entry_usdt)
            self.assertEqual(settings.live.max_active_tranches, settings.paper.max_active_tranches)
            self.assertEqual(settings.live.max_economic_positions, settings.paper.max_economic_positions)
            self.assertEqual(settings.live.max_open_exposure_usdt, settings.paper.max_open_exposure_usdt)
            ready = LiveExecutionAdapter(settings).readiness(connected=True, armed=False)
            self.assertFalse(ready.execution_ready)
            self.assertFalse(ready.live_enabled)
            self.assertFalse(ready.live_armed)

    def test_skill_aliases_and_schema_compatibility(self):
        workflow = (ROOT/"skills/binance-spotguard/references/workflow.md").read_text()
        self.assertIn("`/risk` is not a registered", workflow)
        self.assertIn("/spot", workflow)
        self.assertIn("sg:", workflow)
        self.assertIn("spotguard.live-arm.v1", (ROOT/"src/spotguard/security.py").read_text())

    def test_public_skill_uses_portable_workspace_config_for_analysis(self):
        skill = (ROOT/"skills/binance-spotguard/SKILL.md").read_text()
        command = "${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}/tools/spotguard-agent-os/riskpilot --config ${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}/tools/spotguard-agent-os/config.json --json analyze SYMBOL"
        self.assertIn(command, skill)
        self.assertIsNone(re.search(r"/(?:home|Users)/[^/\s`\"']+", skill))

if __name__ == "__main__":
    unittest.main()
