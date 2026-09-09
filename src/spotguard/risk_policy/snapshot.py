from __future__ import annotations

from typing import Any, Mapping

from ..config import Settings
from .evaluator import EvaluationResult, PolicyContext
from .limits import policy_descriptor, policy_fingerprint
from .sizing import SizingDecision


POLICY_SNAPSHOT_SCHEMA = "riskpilot.policy-snapshot.v2"
SUPPORTED_POLICY_SNAPSHOT_SCHEMAS = {
    "riskpilot.policy-snapshot.v1",
    POLICY_SNAPSHOT_SCHEMA,
}


def build_policy_snapshot(
    settings: Settings,
    mode: str,
    context: PolicyContext,
    evaluation: EvaluationResult,
    sizing: SizingDecision | None = None,
    *,
    calculated_quantity: Any = None,
) -> dict[str, Any]:
    """Return canonical JSON-ready policy evidence to embed in a proposal."""

    result = {
        "schema": POLICY_SNAPSHOT_SCHEMA,
        "policy_version": settings.sizing_policy.schema_version,
        "captured_at": context.equity.observed_at,
        "policy_fingerprint": policy_fingerprint(settings, mode),
        "policy_config": policy_descriptor(settings, mode),
        "equity_snapshot": context.equity.to_dict(),
        "effective_limits": context.effective_limits.to_dict(),
        "usage": context.usage.to_dict(),
        "evaluation": evaluation.to_dict(),
        "sizing": sizing.to_dict() if sizing is not None else None,
        "proposal_state": {
            "equity_at_proposal": format(context.equity.equity, "f"),
            "free_balance_at_proposal": format(context.equity.free_quote, "f"),
            "exposure_at_proposal": format(context.usage.open_exposure, "f"),
            "open_risk_at_proposal": format(
                context.usage.aggregate_open_risk, "f"
            ),
            "daily_loss_at_proposal": format(
                context.usage.daily_realized_loss, "f"
            ),
            "weekly_loss_at_proposal": format(
                context.usage.weekly_realized_loss, "f"
            ),
            "calculated_quantity": (
                str(calculated_quantity) if calculated_quantity is not None else None
            ),
        },
        # Phase 2 can add an R:R policy here without changing capital/limit APIs.
        "extensions": {},
    }
    return result


def validate_policy_snapshot(
    stored: Any,
    settings: Settings,
    mode: str,
) -> None:
    if not isinstance(stored, Mapping):
        raise ValueError(
            "APPROVAL_INVALID: proposal is missing an immutable policy snapshot"
        )
    if stored.get("schema") not in SUPPORTED_POLICY_SNAPSHOT_SCHEMAS:
        raise ValueError("APPROVAL_INVALID: proposal policy snapshot schema is unsupported")
    if stored.get("policy_fingerprint") != policy_fingerprint(settings, mode):
        raise ValueError(
            "ACCOUNT_STATE_CHANGED: policy configuration changed; create a new proposal"
        )
    evaluation = stored.get("evaluation")
    if not isinstance(evaluation, Mapping) or evaluation.get("accepted") is not True:
        raise ValueError(
            "APPROVAL_INVALID: proposal policy snapshot was not accepted at creation"
        )
