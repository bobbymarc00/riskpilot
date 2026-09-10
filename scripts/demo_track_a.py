from __future__ import annotations
import json
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from spotguard.config import load_settings
from spotguard.market import SpotMarketSnapshot, scaled_synthetic_klines
from spotguard.risk_policy.limits import limits_for
from spotguard.service import SpotGuard

MARKET = SpotMarketSnapshot("BTCUSDT", Decimal("99.90"), Decimal("100"), Decimal("100"), Decimal("5"), Decimal("0.001"), "TRADING", 1)

def main() -> int:
    project = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="riskpilot-track-a-") as temporary:
        root = Path(temporary)
        raw = json.loads((project / "config.example.json").read_text(encoding="utf-8"))
        raw["workspace"] = str(root / "workspace")
        raw["state_dir"] = str(root / "state")
        raw["telegram"]["enabled"] = False
        raw["openclaw"]["command"] = "/bin/false"
        raw["codex"]["command"] = "/bin/false"
        config = root / "config.json"
        config.write_text(json.dumps(raw), encoding="utf-8")
        service = SpotGuard(load_settings(config))
        owner = service.settings.openclaw.telegram_owner_id
        print("RISK PILOT TRACK A - DETERMINISTIC PAPER DEMO")
        print("Temporary ledger: fresh isolated SQLite database")
        scan = service.scan(symbols=["BTCUSDT"], synthetic=True, dry_run=True)
        row = scan["results"][0]
        print("1. Deterministic scan:", "candidate" if row.get("candidate") else "no qualifying candidate", "score", row.get("score"))
        print("2. Agent OS confirmation: SKIPPED in offline demo (optional read-only command documented)")
        with patch("spotguard.service.fetch_spot_snapshot", return_value=MARKET), patch("spotguard.service.fetch_klines", return_value=scaled_synthetic_klines(100)):
            created = service.create_manual_buy_proposal("BTC", Decimal("25"), notify=True, dry_run=True)
            proposal = created["proposal"]
            print("3. PAPER proposal: created for BTCUSDT, 25 USDT")
            token = service.signer.approval_token(proposal["canonical_json"])
            claim = service.claim(proposal["id"], token, owner, owner)
            fill = service.execute_paper(proposal["id"], claim["lease"])
            print("4. Approved simulated fill: completed (NOT A REAL ORDER)")
            position = service.ledger.list_paper_positions(True)[0]
            close = service.create_paper_close_proposal(position["id"], owner, owner, percentage=Decimal("50"), notify=True, dry_run=True)
            command = close["notification"]["payload"]["presentation"]["blocks"][0]["buttons"][0]["action"]["command"]
            _, _, position_id, code = command.split()
            closed = service.approve_paper_close_by_position(position_id, code, owner, owner)
            print("5. Partial close:", closed["close"]["requested_percentage"], "% aggregate position")
            effective_limits = limits_for(
                service.settings,
                "paper",
                service.settings.paper.initial_balance_usdt,
            )[1]
            over_limit_amount = effective_limits.max_entry_notional + Decimal("1")
            try:
                service.create_manual_buy_proposal("ETH", over_limit_amount)
            except Exception as exc:
                if "exceed" not in str(exc).lower() and "limit" not in str(exc).lower():
                    raise
                print(
                    "6. Risk rejection: >"
                    f"{effective_limits.max_entry_notional:f} USDT rejected before fill"
                )
            else:
                raise RuntimeError("expected over-limit request to fail")
        balance = service.ledger.paper_balance()
        with service.ledger.connect() as connection:
            event_count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        ledger_balance = Decimal(balance["free_usdt"]) + Decimal(balance["locked_usdt"])
        print("7. Ledger balance:", format(ledger_balance, "f"), "USDT")
        print("8. Audit events recorded:", event_count)
        print("PASS: temporary state only; Telegram disabled; Binance writes unreachable")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
