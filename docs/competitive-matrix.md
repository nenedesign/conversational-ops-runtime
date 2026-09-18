# Competitive Matrix

Feature comparison based on public documentation and repository review. Hands-on testing has not yet been completed for any of these products. See the [Status section](../README.md#status) in the main README for context.

**Legend:** `✓` Confirmed from public docs or code · `–` Not documented in public sources · `?` Not evaluated · `Planned` On the roadmap, not yet built

---

## Closest comparisons

| Feature | This runtime | AxonFlow | JamJet | Tandem |
|---------|:-----------:|:--------:|:------:|:------:|
| **License** | Apache 2.0 | BSL 1.1 | Apache 2.0 | Proprietary |
| **Policy gating** | ✓ | ✓ | ✓ | ✓ |
| **Human approval workflow** | ✓ | ✓ | ✓ | ✓ |
| **Append-only audit trail** | ✓ | ✓ | – | – |
| **Self-hosted** | ✓ | ✓ | ✓ | – |
| **Managed conversation lifecycle** | ✓ | ✓ | ? | ? |
| **Unknown outcome as a first-class durable state** | ✓ | – | – | – |
| **Stale approval detection (recheck before dispatch)** | ✓ | – | – | – |
| **Typed domain provider adapter contract** | ✓ | – | – | – |
| **Immutable proposal versioning** | ✓ | – | – | – |
| **Multi-agent handoffs with scoped context packages** | ✓ | – | – | – |
| **REST-first protocol (no SDK required)** | ✓ | – | – | – |
| **Docker Compose packaging** | Planned | ✓ | ✓ | ? |
| **OpenTelemetry traces** | Planned | ✓ | ? | ? |
| **Second domain adapter** | Planned | ? | ? | ? |

---

## Complementary tools (not direct competitors)

| Product | Relationship |
|---------|-------------|
| **LangGraph** | Agent orchestration framework — sits above the action boundary, not beside it |
| **Temporal** | Durable workflow engine — handles retries and state machines, not agent governance |

LangGraph and Temporal are better understood as integration targets. An agent built on LangGraph could submit tool calls to this runtime's action boundary. Temporal could back the command worker dispatch loop.

---

## Key observations

**License differentiation is real.** AxonFlow uses BSL 1.1 (Business Source License). Under BSL 1.1, the code is source-available but production use in a competing product or service requires a commercial license. Apache 2.0 carries no such restriction.

**Unknown outcome and stale detection are not documented elsewhere.** From documentation review, no comparable product explicitly documents `unknown` as a durable command state with a separate reconciliation path, or describes rechecking provider resource version immediately before dispatch. This may mean the feature is absent, undocumented, or handled differently. Hands-on testing is needed to confirm.

**JamJet is the closest Apache 2.0 alternative.** Self-described as "an action-control plane for AI agents" — framing and scope overlap significantly. The licensing position is identical. Hands-on evaluation of JamJet is the highest-priority competitive test.

**Three roadmap items close known gaps.** Docker Compose packaging and OpenTelemetry traces are confirmed in AxonFlow — adding them brings deployment and observability to parity. The second domain adapter is the most strategically important: it validates that the typed provider contract is genuinely domain-agnostic, not payroll-specific.

---

*Last updated: 2026-09-18. Based on public repository and documentation review only. See [`docs/competitor-notes/`](competitor-notes/) for per-product research notes.*
