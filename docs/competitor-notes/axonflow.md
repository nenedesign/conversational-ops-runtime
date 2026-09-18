# AxonFlow: Research Notes

**Source:** Public repository and documentation review only. Hands-on testing not yet completed.

---

## Overview

AxonFlow positions as an enterprise-grade governed execution runtime for AI agents. Of the products reviewed, it has the broadest feature set and the strongest overlap with the design goals of this runtime.

**Repository:** https://github.com/getaxonflow/axonflow

---

## License: BSL 1.1

AxonFlow uses the **Business Source License 1.1** (BSL 1.1), not a permissive open-source license.

BSL 1.1 is source-available: you can read, fork, and modify the code, but production use in a competing product or service requires a commercial license from the licensor. The license converts to Apache 2.0 after a defined Change Date (typically 4 years).

**Practical implication:** teams building AI agent infrastructure cannot use AxonFlow in production without a commercial agreement if the use case overlaps with AxonFlow's defined "production use" restrictions.

This is the clearest confirmed differentiator from this runtime, which is Apache 2.0 with no such restriction.

---

## Positioning

Enterprise platform. Broad feature set covering policy enforcement, human approval, audit, and agent orchestration. Marketed toward regulated industries.

---

## Capabilities: what is and is not confirmed

From public documentation review only; values not independently verified by running the product.

| Capability | Status |
|-----------|--------|
| Policy gating | Confirmed |
| Human approval workflow | Confirmed |
| Audit trail | Confirmed |
| Managed conversation lifecycle | Confirmed |
| Unknown outcome as first-class durable state | Not evaluated |
| Stale approval detection (recheck before dispatch) | Not evaluated |
| Typed domain provider adapter contract | Not evaluated |
| Immutable proposal versioning | Not evaluated |
| Multi-agent handoffs with scoped context | Not evaluated |
| REST-first (no SDK required) | Not evaluated |

---

## Notes

AxonFlow was identified as the closest overall competitor based on feature scope and positioning. The license difference is confirmed and significant. Whether the specific design goals of this runtime (unknown state, stale detection, typed adapter contract) are present, absent, or handled differently in AxonFlow cannot be determined from documentation alone.

**Priority for hands-on testing:** High.

---

*Research date: 2026-09-18*
