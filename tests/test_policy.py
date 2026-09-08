from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from dataclasses import replace

from spotguard.config import load_settings
from spotguard.policy import PolicyError, build_proposal, validate_claim
from spotguard.service import SpotGuard
from spotguard.util import isoformat, utcnow

from tests.helpers import write_config


class PolicyTests(unittest.TestCase):
    def _candidate(self, root: Path):
        settings = load_settings(write_config(root))
        service = SpotGuard(settings)
        scan = service.scan(symbols=["BTCUSDT"], synthetic=True)
        candidate = scan["results"][0]["candidate"]
        self.assertIsNotNone(candidate)
        return settings, candidate

    def test_builds_policy_owned_references(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings, candidate = self._candidate(Path(directory))
            proposal = build_proposal(
                settings,
                candidate,
                Decimal(str(candidate["price"])) * Decimal("0.999"),
                Decimal(str(candidate["price"])),
                Decimal("6"),
                "Trend and spread passed the paper review.",
            )
            self.assertEqual(proposal["product"], "SPOT")
            self.assertEqual(proposal["side"], "BUY")
            self.assertEqual(proposal["quote_amount"], "6")
            self.assertGreaterEqual(Decimal(proposal["reward_risk"]), Decimal("2"))

    def test_rejects_oversized_quote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings, candidate = self._candidate(Path(directory))
            with self.assertRaisesRegex(PolicyError, "quote amount"):
                build_proposal(
                    settings,
                    candidate,
                    Decimal(str(candidate["price"])) * Decimal("0.999"),
                    Decimal(str(candidate["price"])),
                    Decimal("101"),
                    "Oversized request.",
                )

    def test_rejects_stale_price_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings, candidate = self._candidate(Path(directory))
            stale = Decimal(str(candidate["price"])) * Decimal("1.02")
            with self.assertRaisesRegex(PolicyError, "drifted"):
                build_proposal(
                    settings,
                    candidate,
                    stale * Decimal("0.999"),
                    stale,
                    Decimal("6"),
                    "Stale price.",
                )

    def test_rejects_expired_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings, candidate = self._candidate(Path(directory))
            candidate["expires_at"] = isoformat(utcnow() - timedelta(seconds=1))
            with self.assertRaisesRegex(PolicyError, "expired"):
                build_proposal(
                    settings,
                    candidate,
                    Decimal(str(candidate["price"])) * Decimal("0.999"),
                    Decimal(str(candidate["price"])),
                    Decimal("6"),
                    "Expired candidate.",
                )

    def test_rejects_wide_order_book_spread(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings, candidate = self._candidate(Path(directory))
            ask = Decimal(str(candidate["price"]))
            bid = ask * Decimal("0.99")
            with self.assertRaisesRegex(PolicyError, "spread"):
                build_proposal(settings, candidate, bid, ask, Decimal("6"), "Wide spread.")

    def test_low_price_paper_bracket_is_canonical_after_serialization(self) -> None:
        """VET-scale references must remain approvable after eight-place storage."""
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(write_config(Path(directory)))
            settings = replace(settings, market=replace(settings.market, symbols=("VETUSDT",)))
            candidate = {
                "id": "c-0123456789ab", "status": "ACTIVE", "side": "BUY",
                "symbol": "VETUSDT", "price": "0.007956",
                "expires_at": isoformat(utcnow() + timedelta(minutes=5)),
                # This produces the formerly failing independently-truncated bracket.
                "metrics": {"atr_14": "0.0000984695"},
            }
            proposal = build_proposal(
                settings, candidate, Decimal("0.00795"), Decimal("0.007956"),
                Decimal("6"), "Low-price PAPER bracket regression.",
            )
            canonical = proposal["canonical"]
            entry = Decimal(canonical["entry_reference"])
            stop = Decimal(canonical["stop_reference"])
            target = Decimal(canonical["take_profit_reference"])
            ratio = Decimal(canonical["reward_risk"])
            self.assertEqual(canonical["take_profit_reference"], "0.00825142")
            self.assertEqual((target - entry) / (entry - stop), ratio)
            self.assertGreaterEqual(ratio, settings.risk.min_reward_risk)
            proposal["status"] = "PENDING"
            validate_claim(settings, proposal, Decimal("0"))

    def test_high_price_paper_bracket_values_are_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(write_config(Path(directory)))
            candidate = {
                "id": "c-0123456789ab", "status": "ACTIVE", "side": "BUY",
                "symbol": "BTCUSDT", "price": "100",
                "expires_at": isoformat(utcnow() + timedelta(minutes=5)),
                "metrics": {"atr_14": "1"},
            }
            proposal = build_proposal(
                settings, candidate, Decimal("99.9"), Decimal("100"), Decimal("6"),
                "High-price PAPER bracket regression.",
            )
            self.assertEqual(
                {key: proposal["canonical"][key] for key in
                 ("entry_reference", "stop_reference", "take_profit_reference", "reward_risk")},
                {"entry_reference": "100", "stop_reference": "98.5",
                 "take_profit_reference": "103", "reward_risk": "2"},
            )


if __name__ == "__main__":
    unittest.main()
