from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spotguard.cli import main
from spotguard.presentation import render
from spotguard.service import SpotGuard
from tests.helpers import write_config


def skipped_review() -> dict:
    return {
        "market_review": {"decision": "APPROVE", "reason": "fresh candle verified"},
        "review_decision": "APPROVE",
        "proposal": None,
        "proposal_mode": "live",
        "proposal_status": "SKIPPED_NOT_EXECUTION_READY",
        "proposal_blockers": ["decimal_transport_verified"],
    }


class ReviewPresentationTests(unittest.TestCase):
    def test_none_proposal_never_crashes_renderer(self) -> None:
        text = render(skipped_review(), "en", "agent-os")
        self.assertIn("AI REVIEW completed: APPROVE", text)

    def test_skipped_live_proposal_keeps_successful_review_and_safe_blocker(self) -> None:
        text = render(skipped_review(), "en", "agent-os")
        self.assertIn("fresh candle verified", text)
        self.assertIn("LIVE proposal was not created", text)
        self.assertIn("decimal_transport_verified", text)

    def test_real_proposal_presentation_is_unchanged(self) -> None:
        self.assertEqual(
            render({"proposal": {"id": "p-0123456789ab", "mode": "live"}}, "en"),
            "RiskPilot · LIVE proposal created: p-0123456789ab. Approval is required before real Spot execution.",
        )

    def test_absent_proposal_key_keeps_generic_completion(self) -> None:
        self.assertEqual(render({}, "en"), "RiskPilot · Request completed.")

    def test_top_level_execution_summary_remains_preferred(self) -> None:
        summary = {
            "simulated": True, "symbol": "TOPUSDT", "net_base_quantity": "1",
            "average_fill_price": "2", "actual_paper_spend": "2", "simulated_fee": "0.01",
            "stop_loss": "1", "take_profit": "3", "paper_order_id": "o-top", "position_id": "pos-top",
        }
        proposal_summary = {**summary, "symbol": "PROPOSALUSDT"}
        text = render({"execution_summary": summary, "proposal": {"execution_summary": proposal_summary}}, "en")
        self.assertIn("TOPUSDT", text)
        self.assertNotIn("PROPOSALUSDT", text)

    def test_json_cli_serializes_skipped_review_with_null_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = write_config(Path(directory))
            output = io.StringIO()
            with patch.object(SpotGuard, "review_candidate_with_agent_os", return_value=skipped_review()), \
                    contextlib.redirect_stdout(output):
                code = main(["--config", str(config), "--json", "agent-os", "review", "--candidate", "c-f02470ba55f6"])
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertIsNone(payload["proposal"])
        self.assertIn("LIVE proposal was not created", payload["presentation"]["text"])
