from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from spotguard.cli import main
from spotguard.config import load_settings
from spotguard.intent import normalize_trade_intent
from spotguard.security import SecurityError
from spotguard.service import SpotGuard
from spotguard.telegram import TelegramError
from tests.helpers import config_dict, write_config


OWNER = "123456789"


class CliTradeIntentTests(unittest.TestCase):
    def _invoke(self, config: Path, *, sender: str = OWNER, chat: str = OWNER) -> tuple[int, dict]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main([
                "--config", str(config), "--json", "trade-intent",
                "--text", "sell all BTC", "--sender-id", sender, "--chat-id", chat,
            ])
        return code, json.loads(output.getvalue())

    def test_correct_owner_and_chat_reach_mocked_live_exit_at_100_percent(self) -> None:
        """The CLI authenticates before this mock; no proposal or order is made."""
        with tempfile.TemporaryDirectory() as directory:
            config = write_config(Path(directory))
            with patch.object(
                SpotGuard, "create_live_partial_exit_proposal",
                return_value={"proposal": {"id": "p-fixture", "status": "PENDING", "mode": "live"}},
            ) as create:
                code, payload = self._invoke(config)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["intent"], {
            "action": "close", "symbol": "BTCUSDT", "percentage": "100",
            "close_selector": "all", "mode": "live", "buy_deferred": False, "message": None,
        })
        create.assert_called_once_with("BTCUSDT", Decimal("100"), notify=False, dry_run=False)

    def test_wrong_owner_or_chat_fails_before_proposal_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = write_config(Path(directory))
            with patch.object(SpotGuard, "create_live_partial_exit_proposal", side_effect=AssertionError("must not create")):
                owner_code, owner_payload = self._invoke(config, sender="999999999")
                chat_code, chat_payload = self._invoke(config, chat="-999999999")
        self.assertEqual((owner_code, owner_payload["type"]), (2, "SecurityError"))
        self.assertEqual((chat_code, chat_payload["type"]), (2, "SecurityError"))

    def test_sell_all_sol_normalizes_to_protected_live_exit(self) -> None:
        intent = normalize_trade_intent("sell all SOL", ("SOLUSDT",))
        self.assertEqual(intent["action"], "close")
        self.assertEqual(intent["mode"], "live")
        self.assertEqual(intent["close_selector"], "all")
        self.assertEqual(intent["percentage"], "100")

    def test_full_arm_with_readiness_blocker_fails_before_any_order_or_proposal_work(self) -> None:
        """This is the production failure branch, exercised with no Binance calls."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = config_dict(root)
            raw["live"]["enabled"] = True
            config = root / "config.json"
            config.write_text(json.dumps(raw), encoding="utf-8")
            service = SpotGuard(load_settings(config))
            service.live_arm.status = Mock(return_value=SimpleNamespace(armed=True, scope="FULL"))
            service.live_status = Mock(return_value={"execution_ready": False})
            service.live_executor.read_open_spot_orders = Mock(side_effect=AssertionError("must not read orders"))
            with self.assertRaisesRegex(SecurityError, "readiness checks have not all passed"):
                service.create_live_partial_exit_proposal("BTC", Decimal("100"))
            service.live_executor.read_open_spot_orders.assert_not_called()

    def test_notification_failure_has_telegram_error_type_not_security_type(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = write_config(Path(directory))
            with patch.object(SpotGuard, "create_live_partial_exit_proposal", side_effect=TelegramError("fixture notify unavailable")):
                code, payload = self._invoke(config)
        self.assertEqual(code, 2)
        self.assertEqual(payload["type"], "TelegramError")


if __name__ == "__main__":
    unittest.main()
