from dataclasses import dataclass
from typing import Literal


@dataclass
class PolicyDecision:
    risk_level: Literal["low", "medium", "high"]
    requires_approval: bool
    outcome: Literal["approved", "requires_approval", "rejected"]


# Phase 1: hardcoded policy matching spec/tool_contracts/payroll.json
_POLICY: dict[str, PolicyDecision] = {
    "investigate_payroll_anomaly": PolicyDecision(
        risk_level="low",
        requires_approval=False,
        outcome="approved",
    ),
    "prepare_classification_correction": PolicyDecision(
        risk_level="high",
        requires_approval=True,
        outcome="requires_approval",
    ),
    "flag_for_legal_review": PolicyDecision(
        risk_level="medium",
        requires_approval=True,
        outcome="requires_approval",
    ),
}


def evaluate_policy(tool_name: str) -> PolicyDecision:
    return _POLICY.get(
        tool_name,
        PolicyDecision(risk_level="high", requires_approval=False, outcome="rejected"),
    )
