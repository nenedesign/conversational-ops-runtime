"""
Conversational Operations Runtime — Provider Adapter Interface

Developers implement this Protocol to connect the runtime to a domain provider
(payroll system, booking system, HR platform, etc.).

The runtime wraps every adapter call:
  - Policy gate before prepare
  - Authorization check before commit
  - Idempotency enforcement at dispatch
  - Unknown-state detection after verify
  - Reconciliation trigger on timeout

Developers implement the domain logic. The runtime implements the governance.

Provider capability tiers:
  Tier 1 — No idempotency, no reconciliation:
    High-risk irreversible actions unavailable by default. Requires explicit
    tenant policy with compensating controls.
  Tier 2 — Idempotency only, limited window:
    Automated retry within window. Manual reconciliation after window expires.
  Tier 3 — Full idempotency and reconciliation (recommended):
    Automated reconciliation on unknown outcome. Normal execution path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


# ─────────────────────────────────────────────
# Capability declarations
# ─────────────────────────────────────────────

@dataclass
class ProviderCapabilities:
    """
    Declared at startup. The runtime reads these before any run is created.

    If a workflow requires automated reconciliation and supports_reconcile is
    False, the runtime raises a configuration error before any run is created.

    If a high-risk irreversible action is configured against a Tier 1 provider
    (no idempotency), the runtime rejects the configuration unless the tenant
    has an explicit compensating-controls policy in place.
    """
    provider_id: str
    provider_version: str

    # Tier 3 (recommended): all True
    supports_prepare: bool = True
    supports_verify: bool = True
    supports_reconcile: bool = True

    # Advanced capabilities
    supports_resource_versions: bool = True
    supports_dry_run: bool = False

    # Idempotency window the provider guarantees.
    # The runtime will not retry after this window without reconciling first.
    max_idempotency_window_hours: int = 24

    # "tenant" — key is unique within a tenant
    # "global"  — key is unique across the entire provider
    idempotency_scope: Literal["tenant", "global"] = "tenant"


# ─────────────────────────────────────────────
# Domain types — payroll reference implementation
# ─────────────────────────────────────────────

@dataclass
class Employee:
    employee_id: str
    name: str
    status: str
    contractor_type: str | None
    jurisdiction: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PayrollRecord:
    employee_id: str
    period: str
    gross: float
    currency: str
    classification: str
    resource_version: int
    updated_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PrepareCorrectionRequest:
    employee_id: str
    correction_type: str
    evidence_ids: list[str]
    requested_by: str
    tenant_id: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CorrectionProposal:
    proposal_content: dict[str, Any]       # structured, reviewable
    risk_level: Literal["low", "medium", "high"]
    requires_approval: bool
    evidence_citations: list[str]
    uncertainty_statement: str | None      # required when outcome is uncertain
    resource_version: int                  # version at prepare time for stale detection
    policy_version: str


@dataclass
class CommitCorrectionRequest:
    employee_id: str
    correction_type: str
    approved_content: dict[str, Any]       # the approved proposal content
    approver_id: str
    approved_proposal_version: int
    tenant_id: str


@dataclass
class CommitResult:
    """
    Explicit outcome categories — no generic success/failure.

    committed  — provider received and committed the operation
    rejected   — provider refused the operation (not a runtime failure)
    duplicate  — provider already saw this idempotency key; no second effect
    unknown    — outcome is uncertain (timeout, connection reset, etc.)
                 The runtime sets command.status = 'unknown'. Do not retry
                 automatically. Call reconcile_correction first.
    """
    status: Literal["committed", "rejected", "duplicate", "unknown"]
    provider_reference: str | None = None     # provider transaction ID if committed
    rejection_reason: str | None = None       # if status == "rejected"
    is_duplicate: bool = False                # True when provider already saw key


# ─────────────────────────────────────────────
# Verify and reconcile request types
# ─────────────────────────────────────────────

@dataclass
class VerifyRequest:
    """
    Context for verifying a committed operation.

    Some providers verify by transaction ID; others by idempotency key or
    resource state. Pass all available context — the adapter decides which
    to use.
    """
    idempotency_key: str
    provider_reference: str | None      # downstream_reference from CommitResult
    resource_id: str                    # the affected resource
    expected_effect: dict[str, Any]     # what the commit was supposed to do


@dataclass
class VerifyResult:
    confirmed: bool                     # True = provider confirms the effect happened
    provider_reference: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReconcileRequest:
    """
    Context for resolving an unknown outcome.

    Called when: provider timed out, worker crashed after calling provider,
    or verify returned ambiguous results. Query by idempotency key to determine
    whether the operation actually committed.
    """
    idempotency_key: str
    provider_reference: str | None
    resource_id: str
    attempt_count: int
    last_error: str | None


@dataclass
class ReconciliationResult:
    """
    Resolution of an unknown outcome.

    found_committed   — provider confirms the operation committed
    found_not_present — provider confirms the operation did not commit
                        (safe to retry with the same idempotency key)
    still_unknown     — provider cannot determine the outcome
                        (requires manual intervention)
    """
    status: Literal["found_committed", "found_not_present", "still_unknown"]
    provider_reference: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


# ─────────────────────────────────────────────
# Provider adapter Protocol
# ─────────────────────────────────────────────

class PayrollProvider(Protocol):
    """
    Typed adapter interface for a payroll domain provider.

    Implement this Protocol to connect the governed action runtime to your
    payroll system. The runtime calls these methods at the appropriate stage
    of the action lifecycle:

        read      → get_employee, get_payroll_record
        prepare   → prepare_correction  (Action Service stage — no side effects)
        commit    → commit_correction   (Command Worker — idempotency key required)
        verify    → verify_correction   (Command Worker — after commit)
        reconcile → reconcile_correction (Command Worker — on unknown outcome only)

    Returning CommitResult(status="unknown") signals an uncertain outcome.
    The runtime sets command.status = "unknown" and triggers reconciliation.
    Do not raise an exception for timeouts — return the unknown status.

    Do not perform commits in prepare. prepare must be safe to call multiple
    times without side effects (used for stale approval recalculation).
    """

    def capabilities(self) -> ProviderCapabilities:
        """
        Called at startup. The runtime validates workflow configuration against
        these capabilities before any run is created.
        """
        ...

    async def get_employee(
        self,
        employee_id: str,
        tenant_id: str,
    ) -> Employee:
        """Retrieve authoritative employee record."""
        ...

    async def get_payroll_record(
        self,
        employee_id: str,
        tenant_id: str,
    ) -> PayrollRecord:
        """Retrieve payroll record for the current period."""
        ...

    async def prepare_correction(
        self,
        request: PrepareCorrectionRequest,
    ) -> CorrectionProposal:
        """
        Construct a reviewable correction proposal without committing.

        Safe to call multiple times. Used for:
          - Initial proposal creation
          - Stale approval recalculation (when resource changed during review)
          - Proposal revision after human edits

        Return an uncertainty_statement when the outcome cannot be determined
        with confidence. The runtime surfaces this to the human reviewer.
        """
        ...

    async def commit_correction(
        self,
        request: CommitCorrectionRequest,
        idempotency_key: str,
    ) -> CommitResult:
        """
        Execute the correction as a side effect.

        The idempotency_key is unique per command. If you receive a key you
        have already seen, return CommitResult(status="duplicate") — do not
        apply the effect a second time.

        If the outcome is uncertain (timeout, connection reset), return
        CommitResult(status="unknown"). Do not raise an exception.
        The runtime will call reconcile_correction to resolve the outcome.
        """
        ...

    async def verify_correction(
        self,
        request: VerifyRequest,
    ) -> VerifyResult:
        """
        Confirm the provider received and committed the operation.

        Called immediately after a successful commit response to independently
        confirm the effect. Use provider_reference, idempotency_key, or
        resource state to verify — whichever the provider supports.
        """
        ...

    async def reconcile_correction(
        self,
        request: ReconcileRequest,
    ) -> ReconciliationResult:
        """
        Resolve an unknown outcome by querying the provider.

        Called when command.status is "unknown" — not as part of normal
        execution. Query the provider using the idempotency_key to determine
        whether the operation committed.

        "found_not_present" means it is safe to retry with the same key.
        "still_unknown" means the provider cannot confirm — manual intervention
        is required and the runtime will not retry automatically.
        """
        ...


# ─────────────────────────────────────────────
# Generic adapter base (optional convenience)
# ─────────────────────────────────────────────

class BaseProviderAdapter:
    """
    Optional base class. Provides default capability declarations and
    raises NotImplementedError on unimplemented methods.

    Inherit this class or implement the Protocol directly.
    """

    def capabilities(self) -> ProviderCapabilities:
        raise NotImplementedError(
            f"{type(self).__name__} must implement capabilities()"
        )

    async def get_employee(self, employee_id: str, tenant_id: str) -> Employee:
        raise NotImplementedError

    async def get_payroll_record(self, employee_id: str, tenant_id: str) -> PayrollRecord:
        raise NotImplementedError

    async def prepare_correction(self, request: PrepareCorrectionRequest) -> CorrectionProposal:
        raise NotImplementedError

    async def commit_correction(
        self, request: CommitCorrectionRequest, idempotency_key: str
    ) -> CommitResult:
        raise NotImplementedError

    async def verify_correction(self, request: VerifyRequest) -> VerifyResult:
        raise NotImplementedError

    async def reconcile_correction(self, request: ReconcileRequest) -> ReconciliationResult:
        raise NotImplementedError


# ─────────────────────────────────────────────
# Simulated provider for Phase 1 testing
# ─────────────────────────────────────────────

class SimulatedPayrollProvider(BaseProviderAdapter):
    """
    Failure-aware simulated provider for Phase 1 development and conformance tests.

    Supports:
      - Resource version changes (stale approval detection)
      - Idempotency key tracking (duplicate detection)
      - Configurable timeout injection (unknown outcome path)
      - Unknown outcome simulation

    Not for production use.
    """

    def __init__(self, *, inject_timeout: bool = False, inject_version_change: bool = False):
        self._inject_timeout = inject_timeout
        self._inject_version_change = inject_version_change
        self._seen_keys: dict[str, CommitResult] = {}
        self._resource_version = 1

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id="simulated-payroll",
            provider_version="1.0.0",
            supports_prepare=True,
            supports_verify=True,
            supports_reconcile=True,
            supports_resource_versions=True,
            supports_dry_run=True,
            max_idempotency_window_hours=168,   # 7 days
        )

    async def get_employee(self, employee_id: str, tenant_id: str) -> Employee:
        return Employee(
            employee_id=employee_id,
            name="Carlos Morales",
            status="active",
            contractor_type="autonomous",
            jurisdiction="AR",
        )

    async def get_payroll_record(self, employee_id: str, tenant_id: str) -> PayrollRecord:
        version = self._resource_version + (1 if self._inject_version_change else 0)
        return PayrollRecord(
            employee_id=employee_id,
            period="2026-09",
            gross=4500.00,
            currency="ARS",
            classification="contractor",
            resource_version=version,
            updated_at="2026-09-01T00:00:00Z",
        )

    async def prepare_correction(self, request: PrepareCorrectionRequest) -> CorrectionProposal:
        return CorrectionProposal(
            proposal_content={
                "action": "prepare_case_for_legal_review",
                "employee_id": request.employee_id,
                "correction_type": request.correction_type,
            },
            risk_level="high",
            requires_approval=True,
            evidence_citations=request.evidence_ids,
            uncertainty_statement=(
                "Classification determination requires qualified legal review. "
                "Agent has identified a potential anomaly and assembled evidence. "
                "No legal determination has been made."
            ),
            resource_version=self._resource_version,
            policy_version="arg-payroll-v2.1",
        )

    async def commit_correction(
        self,
        request: CommitCorrectionRequest,
        idempotency_key: str,
    ) -> CommitResult:
        if idempotency_key in self._seen_keys:
            result = self._seen_keys[idempotency_key]
            return CommitResult(
                status=result.status,
                provider_reference=result.provider_reference,
                is_duplicate=True,
            )

        if self._inject_timeout:
            result = CommitResult(status="unknown")
            self._seen_keys[idempotency_key] = result
            return result

        self._resource_version += 1
        result = CommitResult(
            status="committed",
            provider_reference=f"payroll-tx-{idempotency_key[-8:]}",
        )
        self._seen_keys[idempotency_key] = result
        return result

    async def verify_correction(self, request: VerifyRequest) -> VerifyResult:
        result = self._seen_keys.get(request.idempotency_key)
        if result and result.status == "committed":
            return VerifyResult(confirmed=True, provider_reference=result.provider_reference)
        return VerifyResult(confirmed=False)

    async def reconcile_correction(self, request: ReconcileRequest) -> ReconciliationResult:
        result = self._seen_keys.get(request.idempotency_key)
        if result and result.status == "committed":
            return ReconciliationResult(
                status="found_committed",
                provider_reference=result.provider_reference,
            )
        if result is None:
            return ReconciliationResult(status="found_not_present")
        return ReconciliationResult(status="still_unknown")
