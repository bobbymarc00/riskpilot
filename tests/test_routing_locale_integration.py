from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spotguard.cli import main
from spotguard.config import load_settings
from spotguard.db import Ledger
from spotguard.intent import normalize_paper_intent
from spotguard.presentation import render
from tests.helpers import write_config
from tests.test_localization import OWNER, analysis


ROOT = Path(__file__).resolve().parents[1]
INSTALLED_SKILL = Path.home() / ".openclaw/workspace/skills/binance-spotguard/SKILL.md"
WORKSPACE_AGENTS = Path.home() / ".openclaw/workspace/AGENTS.md"


class RoutingLocaleIntegrationTests(unittest.TestCase):
    def test_english_comparison_overrides_persisted_indonesian_locale(self):
        with tempfile.TemporaryDirectory() as directory:
            config = write_config(Path(directory))
            settings = load_settings(config)
            Ledger(settings.database_path, settings.paper.initial_balance_usdt).remember_chat_locale(OWNER, "id")
            output = io.StringIO()
            with patch("spotguard.service.SpotGuard._analyze_one",
                       side_effect=lambda symbol, amount: analysis(symbol)), \
                    contextlib.redirect_stdout(output):
                code = main([
                    "--config", str(config), "--json", "--utterance",
                    "compare BTC and BNB", "compare", "BTC", "BNB",
                ])
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["presentation"]["locale"], "en")
        canonical = {key: value for key, value in payload.items() if key != "presentation"}
        self.assertEqual(payload["presentation"]["text"], render(canonical, "en", "compare"))
        self.assertNotEqual(payload["presentation"]["text"], render(canonical, "id", "compare"))

    def test_skill_forwards_original_comparison_utterance_in_source_and_install(self):
        command = "--json --utterance ORIGINAL_MESSAGE compare SYMBOL..."
        paths = [ROOT / "skills/binance-spotguard/SKILL.md"]
        if INSTALLED_SKILL.exists():
            paths.append(INSTALLED_SKILL)
        for path in paths:
            with self.subTest(path=path):
                text = path.read_text(encoding="utf-8")
                self.assertIn(command, text)
                self.assertIn("Pass `ORIGINAL_MESSAGE` unchanged as one argv item", text)

    def test_fresh_session_routes_recognized_status_but_preserves_explicit_platforms(self):
        source_skill = (ROOT / "skills/binance-spotguard/SKILL.md").read_text(encoding="utf-8")
        installed_skill = INSTALLED_SKILL.read_text(encoding="utf-8") if INSTALLED_SKILL.exists() else source_skill
        recognized = {
            "show balance": "balance",
            "show positions": "positions",
            "show balance and positions": "status",
            "cek saldo": "balance",
            "cek posisi": "positions",
            "cek saldo dan posisi": "status",
        }
        for utterance, action in recognized.items():
            with self.subTest(utterance=utterance):
                self.assertEqual(normalize_paper_intent(utterance, ("BTCUSDT",))["action"], action)
        for utterance in ("Technocore balance", "Technocore saldo"):
            with self.subTest(utterance=utterance):
                self.assertNotIn(
                    normalize_paper_intent(utterance, ("BTCUSDT",))["action"],
                    {"balance", "positions", "status"},
                )
        if WORKSPACE_AGENTS.exists():
            agents = WORKSPACE_AGENTS.read_text(encoding="utf-8")
            self.assertIn("routes directly to RiskPilot even in a fresh session after `/new`", agents)
            self.assertIn("do not ask which account or platform", agents)
            self.assertIn("An explicitly named unrelated platform or account always takes precedence", agents)
        for text in (source_skill, installed_skill):
            self.assertIn("After `/new`", text)
            self.assertIn("routes directly through `trade-intent`", text)
            self.assertIn("Explicitly named unrelated platforms remain outside this skill", text)

    def test_top_radar_intent_and_cli_are_read_only_local_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            config = write_config(Path(directory))
            settings = load_settings(config)
            state = settings.state_dir / "smart-scanner"
            state.mkdir(parents=True)
            (state / "top-radar.json").write_text(json.dumps({
                "generated_at": "2026-09-09T00:00:00+00:00", "active_count": 48,
                "used_weight_1m": 116,
                "rows": [{
                    "symbol": "VETUSDT", "lane": "MOMENTUM", "potential_score": 84.5,
                    "label": "POTENSI_TINGGI", "configured": True, "native_score": 80.0,
                    "native_interval": "15m", "candidate_eligible": True,
                    "core_score": 76.0, "momentum_score": 92.0, "hype_score": 71.0,
                    "ret_5m_pct": 0.4, "ret_15m_pct": 1.2, "spread_pct": 0.02,
                }],
            }), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["--config", str(config), "--json", "--locale", "id", "radar"])
        self.assertEqual(code, 0, output.getvalue())
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["top_radar"][0]["symbol"], "VETUSDT")
        self.assertIn("bukan kandidat", payload["presentation"]["text"])
        self.assertEqual(normalize_paper_intent("radar potensi", ("BTCUSDT",))["action"], "radar")


if __name__ == "__main__":
    unittest.main()
