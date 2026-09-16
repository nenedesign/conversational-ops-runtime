import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from adapters.adapter_interface import (  # noqa: E402
    CommitCorrectionRequest,
    PrepareCorrectionRequest,
    ReconcileRequest,
    SimulatedPayrollProvider,
    VerifyRequest,
)

# Single shared instance for the process lifetime.
# All routers and the command worker use this instance so that the
# simulated provider's in-memory state (seen keys, resource versions)
# is consistent across the full request lifecycle.
provider = SimulatedPayrollProvider()

__all__ = [
    "provider",
    "CommitCorrectionRequest",
    "PrepareCorrectionRequest",
    "ReconcileRequest",
    "SimulatedPayrollProvider",
    "VerifyRequest",
]
