from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .config import Settings
from .presentation import compact_number, translate, number


class TelegramError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeliveryResult:
    delivered: bool
    transport: str
    payload: dict[str, Any]
    output: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "delivered": self.delivered,
            "transport": self.transport,
            "payload": self.payload,
            "output": self.output,
        }


def _callback(value: str) -> str:
    if len(value.encode("utf-8")) > 64:
        raise TelegramError("callback value exceeds Telegram's 64-byte limit")
    return value


def _command(value: str) -> str:
    """Validate an OpenClaw native-command button before delivery."""
    if not value.startswith("/"):
        raise TelegramError("button command must be an exact slash command")
    if len(("tgcmd:" + value).encode("utf-8")) > 64:
        raise TelegramError("command action exceeds Telegram's 64-byte callback limit")
    return value


def candidate_message(candidate: dict[str, Any], locale: str = "en") -> tuple[str, list[dict[str, str]]]:
    text = translate("candidate.body", locale, symbol=candidate["symbol"], interval=candidate["interval"],
                     score=number(candidate["score"], locale, 0), price=number(candidate["price"], locale),
                     identifier=candidate["id"])
    return text, [{
    "label": translate("button.review", locale),
    # Presentation callbacks are dispatched by OpenClaw's native interactive
    # handler.  Keep the candidate id as the only callback payload.
    "command": _command(f"/binance_spotguard review {candidate['id']}"),
    "style": "primary",
}]


def _fixed(value: Any) -> str:
    return format(Decimal(str(value)), "f")


def _display_fields(values: dict[str, Any], locale: str) -> dict[str, Any]:
    # Symbols, identifiers and command tokens must never be number-formatted.
    result = dict(values)
    for key, value in values.items():
        if key.endswith(("_id", "_at", "_asset")) or key in {"id", "symbol", "schema", "mode", "nonce", "selector"}:
            continue
        try:
            amount = Decimal(str(value))
            if amount.is_finite():
                if key.endswith(("percentage", "count")):
                    places = 2
                elif "quantity" in key or key in {"fee_estimate_base"}:
                    places = 8
                elif any(term in key for term in ("price", "entry", "stop", "target", "bid", "ask", "cost")):
                    places = 8
                else:
                    places = 4
                result[key] = compact_number(amount, locale, places)
        except (ValueError, ArithmeticError):
            pass
    return result


def proposal_message(proposal: dict[str, Any], token: str, confirmation_code: str | None = None,
                     locale: str = "en") -> tuple[str, list[dict[str, str]]]:
    canonical = proposal["canonical"]
    if canonical.get("source") == "manual-live-set-protection":
        text=("🛡️ RISKPILOT TP/SL RESTORE PROPOSAL\n⚠️ LIVE — REAL FUNDS\n\n"
              f"Protect {canonical['quantity']} {proposal['symbol'][:-len(canonical['quote_asset'])]}\n"
              f"Stop loss: {canonical['stop_reference']}\nTake profit: {canonical['take_profit_reference']}\n"
              f"ID: {proposal['id']}\nExpires: {proposal['expires_at']}\n\n"
              "Approval submits a Spot SELL OCO only; it does not sell the asset now.")
        return text,[{"label":translate("button.approve_live",locale),"command":_command(f"/binance_spotguard live-approve {proposal['id']}"),"style":"danger"},{"label":translate("button.reject_live",locale),"command":_command(f"/binance_spotguard live-reject {proposal['id']}"),"style":"secondary"}]
    if canonical.get("source") == "manual-live-partial-exit":
        remaining = Decimal(str(canonical["remaining_quantity"]))
        if remaining == 0:
            text=("🛡️ RISKPILOT FULL EXIT PROPOSAL\n⚠️ LIVE — REAL FUNDS\n\n"+f"{proposal['symbol']}: sell 100% ({canonical['sell_quantity']})\n"+"Remaining: 0\n"+f"ID: {proposal['id']}\nExpires: {proposal['expires_at']}\n\nApproval cancels OCO and sells the protected quantity. No TP/SL will be re-armed.")
        else:
            text=("🛡️ RISKPILOT PARTIAL EXIT PROPOSAL\n⚠️ LIVE — REAL FUNDS\n\n"+f"{proposal['symbol']}: sell {canonical['percentage']}% ({canonical['sell_quantity']})\n"+f"Remaining: {canonical['remaining_quantity']} with existing TP/SL {canonical['stop_reference']} / {canonical['take_profit_reference']}\n"+f"ID: {proposal['id']}\nExpires: {proposal['expires_at']}\n\nApproval cancels OCO, sells, then re-arms the unchanged TP/SL.")
        return text,[{"label":translate("button.approve_live",locale),"command":_command(f"/binance_spotguard live-approve {proposal['id']}"),"style":"danger"},{"label":translate("button.reject_live",locale),"command":_command(f"/binance_spotguard live-reject {proposal['id']}"),"style":"secondary"}]
    if canonical.get("source") == "manual-live-cancel-protection":
        text = ("🛡️ RISKPILOT PROTECTION-CANCEL PROPOSAL\n⚠️ LIVE — REAL FUNDS\n\n"
                f"Cancel active Spot OCO protection for {proposal['symbol']}\n"
                f"Order list ID: {canonical['order_list_id']}\nID: {proposal['id']}\nExpires: {proposal['expires_at']}\n\n"
                "Approval is required. Approving removes the active TP/SL; it does not sell the asset.")
        return text, [{"label": translate("button.approve_live", locale), "command": _command(f"/binance_spotguard live-approve {proposal['id']}"), "style": "danger"},
                      {"label": translate("button.reject_live", locale), "command": _command(f"/binance_spotguard live-reject {proposal['id']}"), "style": "secondary"}]
    if canonical.get("source") == "manual-live-close":
        text = ("🛡️ RISKPILOT TRADE PROPOSAL\n⚠️ LIVE — REAL FUNDS\n\n"
                f"Spot MARKET SELL — close free {canonical['base_asset']} balance\n"
                f"Pair: {proposal['symbol']}\nQuantity: {canonical['quantity']} {canonical['base_asset']}\n"
                f"Estimated reference: {canonical['estimated_quote_amount']} {canonical['quote_asset']}\n"
                f"ID: {proposal['id']}\nExpires: {proposal['expires_at']}\n\n"
                "Approval is required. Approving submits this real-money Spot market close; it cannot be undone.")
        return text, [
            {"label": translate("button.approve_live", locale),
             "command": _command(f"/binance_spotguard live-approve {proposal['id']}"), "style": "danger"},
            {"label": translate("button.reject_live", locale),
             "command": _command(f"/binance_spotguard live-reject {proposal['id']}"), "style": "secondary"},
        ]
    mode = "proposal.mode.live" if proposal["mode"] == "live" else (
        "proposal.mode.manual" if canonical.get("source") == "manual-paper-test" else "proposal.mode.paper")
    text = translate("proposal.body", locale,
        title=translate("proposal.paper_buy.title", locale), mode=translate(mode, locale),
        # New proposals carry these immutable fields in canonical.  The
        # proposal-level fallback preserves rendering of legacy PAPER records
        # created before those fields were stored there.
        order_type=canonical.get("order_type", proposal.get("order_type", "MARKET")),
        side=canonical.get("side", proposal.get("side", "BUY")), symbol=proposal["symbol"],
        amount=compact_number(proposal["quote_amount"], locale, 4),
        entry=compact_number(proposal["entry_reference"], locale, 8), stop=compact_number(proposal["stop_reference"], locale, 8),
        target=compact_number(proposal["take_profit_reference"], locale, 8), ratio=compact_number(proposal["reward_risk"], locale, 2),
        identifier=proposal["id"], expiry=proposal["expires_at"])
    if proposal["mode"] == "paper" and canonical.get("scale_in"):
        fields = _display_fields(canonical, locale)
        fields["quantity"] = compact_number(Decimal(canonical["existing_quantity"]) + Decimal(canonical["new_proposed_tranche_quantity"]), locale, 8)
        text += "\n\n" + translate("proposal.scale_in", locale, **fields)
    if proposal["mode"] == "live":
        text += "\n\n" + translate("proposal.live.confirmation", locale)
        return text, [
            {"label": translate("button.approve_live", locale),
             "command": _command(f"/binance_spotguard live-approve {proposal['id']}"), "style": "danger"},
            {"label": translate("button.reject_live", locale),
             "command": _command(f"/binance_spotguard live-reject {proposal['id']}"), "style": "secondary"},
        ]
    if proposal["mode"] != "paper":
        raise TelegramError("proposal mode is invalid")
    if not confirmation_code:
        raise TelegramError("PAPER command controls require a confirmation code")
    text += "\n\n" + translate("proposal.fallback", locale, identifier=proposal["id"], code=confirmation_code)
    return text, [
        {"label": translate("button.approve_paper", locale), "command": _command(
            f"/binance_spotguard paper-approve {proposal['id']} {confirmation_code}"), "style": "success"},
        {"label": translate("button.reject_paper", locale), "command": _command(
            f"/binance_spotguard paper-reject {proposal['id']} {confirmation_code}"), "style": "danger"},
    ]


def paper_close_message(position: dict[str, Any], close: dict[str, Any], token: str, code: str,
                        locale: str = "en") -> tuple[str, list[dict[str, str]]]:
    fields = _display_fields(position, locale)
    text = translate("proposal.close.body", locale, **fields,
                     identifier=close["id"], expiry=close["expires_at"], code=code)
    return text, [
        {"label": translate("button.approve_close", locale), "command": _command(
            f"/binance_spotguard close-approve {position['position_id']} {code}"), "style": "danger"},
        {"label": translate("button.reject_close", locale), "command": _command(
            f"/binance_spotguard close-reject {position['position_id']} {code}"), "style": "secondary"},
    ]


class OpenClawMessenger:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _available_flag(self) -> str | None:
        command = self.settings.openclaw.command
        executable = command if shutil.which(command) else None
        if executable is None and not command.startswith("/"):
            return None
        try:
            result = subprocess.run(
                [command, "message", "send", "--help"],
                text=True,
                capture_output=True,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        help_text = f"{result.stdout}\n{result.stderr}"
        if "--presentation" in help_text:
            return "presentation"
        if "--buttons" in help_text:
            return "buttons"
        return "text"

    @staticmethod
    def _presentation(buttons: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "blocks": [
                {
                    "type": "buttons",
                    "buttons": [
                        {
                            "label": item["label"],
                            "action": ({"type": "command", "command": item["command"]}
                                       if "command" in item else {"type": "callback", "value": item["value"]}),
                            "style": item["style"],
                        }
                        for item in buttons
                    ],
                }
            ]
        }

    @staticmethod
    def _legacy_buttons(buttons: list[dict[str, str]]) -> list[list[dict[str, str]]]:
        return [[{"text": item["label"], "callback_data": (
            item["command"] if "command" in item else item["value"])} for item in buttons]]

    def send_text(self, message: str, dry_run: bool = False) -> DeliveryResult:
        payload = {"channel": self.settings.telegram.channel, "target": self.settings.telegram.chat_id, "message": message}
        if dry_run:
            return DeliveryResult(False, "dry-run", payload)
        if not self.settings.telegram.enabled:
            return DeliveryResult(False, "disabled", payload)
        command = [self.settings.openclaw.command, "message", "send", "--channel", self.settings.telegram.channel,
                   "--target", self.settings.telegram.chat_id, "--message", message]
        if self.settings.telegram.account:
            command.extend(["--account", self.settings.telegram.account])
        command.append("--json")
        result = subprocess.run(command, text=True, capture_output=True, timeout=20, check=False)
        if result.returncode != 0:
            raise TelegramError(f"OpenClaw Telegram delivery failed: {(result.stderr or result.stdout).strip()[-500:]}")
        return DeliveryResult(True, "text", payload, output=result.stdout.strip() or None)

    def send(self, message: str, buttons: list[dict[str, str]], dry_run: bool = False) -> DeliveryResult:
        presentation = self._presentation(buttons)
        payload = {
            "channel": self.settings.telegram.channel,
            "target": self.settings.telegram.chat_id,
            "message": message,
            "presentation": presentation,
        }
        if dry_run:
            return DeliveryResult(False, "dry-run", payload)
        if not self.settings.telegram.enabled:
            return DeliveryResult(False, "disabled", payload)
        capability = self._available_flag()
        if capability is None:
            raise TelegramError("OpenClaw message CLI is unavailable")
        command = [
            self.settings.openclaw.command,
            "message",
            "send",
            "--channel",
            self.settings.telegram.channel,
            "--target",
            self.settings.telegram.chat_id,
            "--message",
            message,
        ]
        if self.settings.telegram.account:
            command.extend(["--account", self.settings.telegram.account])
        if capability == "presentation":
            command.extend(["--presentation", json.dumps(presentation, separators=(",", ":"))])
        elif capability == "buttons":
            command.extend(["--buttons", json.dumps(self._legacy_buttons(buttons), separators=(",", ":"))])
        else:
            raise TelegramError(
                "this OpenClaw build cannot send inline buttons; update OpenClaw before enabling approvals"
            )
        command.append("--json")
        try:
            result = subprocess.run(command, text=True, capture_output=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TelegramError(f"OpenClaw Telegram delivery failed: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-500:]
            raise TelegramError(f"OpenClaw Telegram delivery failed: {detail}")
        return DeliveryResult(True, capability, payload, output=result.stdout.strip() or None)
