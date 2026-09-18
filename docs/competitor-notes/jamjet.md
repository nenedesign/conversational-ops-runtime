# JamJet — Research Notes

**Source:** Public documentation review only. Hands-on testing not yet completed.

---

## Overview

JamJet describes itself as "an action-control plane for AI agents." Of the products reviewed, it has the most similar framing to this runtime and the same Apache 2.0 license. It is the highest-priority product for hands-on evaluation.

**Site:** https://jamjet.dev

---

## License: Apache 2.0

JamJet is Apache 2.0. No production use restrictions. Same licensing position as this runtime.

---

## Positioning

Action-control plane. The framing closely overlaps with this runtime's "sits between the AI model and business systems" positioning. Self-hosted. Targets teams building AI agents that need a policy and approval layer before side effects occur.

---

## Capabilities — what is and is not confirmed

From public documentation review only.

| Capability | Status |
|-----------|--------|
| Policy gating | Confirmed |
| Human approval workflow | Confirmed |
| Audit trail | Not evaluated |
| Managed conversation lifecycle | Not evaluated |
| Unknown outcome as first-class durable state | Not evaluated |
| Stale approval detection (recheck before dispatch) | Not evaluated |
| Typed domain provider adapter contract | Not evaluated |
| Immutable proposal versioning | Not evaluated |
| Multi-agent handoffs with scoped context | Not evaluated |
| REST-first (no SDK required) | Not evaluated |

---

## Notes

JamJet is the closest competitor from a licensing and positioning standpoint. The overlap in language ("action-control plane" vs "action boundary") suggests similar problem framing. Whether the specific mechanisms are equivalent — particularly unknown outcome handling, stale detection, and the typed adapter contract — is the core evaluation question.

JamJet appears to be at an earlier stage than AxonFlow based on documentation completeness. This may mean smaller feature surface, or simply less documentation.

**Priority for hands-on testing:** Highest. Apache 2.0, similar framing, most direct comparison.

---

*Research date: 2026-09-18*
