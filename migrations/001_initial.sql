-- Conversational Operations Runtime — Initial Schema
-- Migration: 001_initial
--
-- Ownership:
--   Agent API owns:       tenants, users, runs, run_events
--   Action Service owns:  tool_calls, proposals, proposal_versions, approvals,
--                         commands, command_attempts, outbox, policies,
--                         policy_versions, policy_decisions, reconciliation_tasks
--   Shared append-only:   audit_events, evidence_snapshots, handoffs,
--                         knowledge_documents
--
-- Rules for shared append-only tables:
--   - Any service may INSERT rows
--   - No service may UPDATE or DELETE rows owned by another service
--   - Database assigns sequence numbers (never application-assigned for ordering)
--   - Every audit_event row requires: producer, causation_id, correlation_id

BEGIN;

-- ─────────────────────────────────────────────
-- AGENT API TABLES
-- ─────────────────────────────────────────────

CREATE TABLE tenants (
    tenant_id       TEXT        PRIMARY KEY,
    name            TEXT        NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active', 'suspended', 'deleted')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE users (
    user_id         TEXT        PRIMARY KEY,
    tenant_id       TEXT        NOT NULL REFERENCES tenants(tenant_id),
    email           TEXT,
    roles           TEXT[]      NOT NULL DEFAULT '{}',
    scopes          TEXT[]      NOT NULL DEFAULT '{}',
    status          TEXT        NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active', 'suspended', 'deleted')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_users_tenant ON users(tenant_id);

CREATE TABLE runs (
    run_id              TEXT        PRIMARY KEY,
    tenant_id           TEXT        NOT NULL REFERENCES tenants(tenant_id),
    conversation_id     TEXT,
    status              TEXT        NOT NULL DEFAULT 'created'
                                    CHECK (status IN (
                                        'created', 'running', 'awaiting_user',
                                        'awaiting_approval', 'awaiting_external',
                                        'escalated', 'completed', 'failed', 'cancelled'
                                    )),
    state_version       INTEGER     NOT NULL DEFAULT 0,
    agent_id            TEXT        NOT NULL,
    agent_version       TEXT,
    model_provider      TEXT,
    model_id            TEXT,
    prompt_version      TEXT,
    tool_schema_version TEXT,
    pending_approval_id TEXT,       -- FK added after approvals table created
    metadata            JSONB       NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ
);

CREATE INDEX idx_runs_tenant           ON runs(tenant_id);
CREATE INDEX idx_runs_status           ON runs(status);
CREATE INDEX idx_runs_tenant_status    ON runs(tenant_id, status);
CREATE INDEX idx_runs_agent_id         ON runs(agent_id);

-- Append-only event log for a run.
-- state_version tracks run state at the time of the event.
-- sequence is assigned by the database trigger, never by application code.
CREATE TABLE run_events (
    event_id        TEXT        PRIMARY KEY,
    run_id          TEXT        NOT NULL REFERENCES runs(run_id),
    tenant_id       TEXT        NOT NULL,
    sequence        BIGINT      NOT NULL,   -- per-run monotonic; set by trigger
    run_version     INTEGER,
    type            TEXT        NOT NULL,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    causation_id    TEXT,                   -- event or request that caused this
    correlation_id  TEXT        NOT NULL,   -- always the run_id
    producer        TEXT        NOT NULL
                                CHECK (producer IN (
                                    'agent-api', 'action-service',
                                    'command-worker', 'system'
                                )),
    data            JSONB       NOT NULL DEFAULT '{}'
);

-- Enforce append-only: no updates or deletes
CREATE RULE run_events_no_update AS ON UPDATE TO run_events DO INSTEAD NOTHING;
CREATE RULE run_events_no_delete AS ON DELETE TO run_events DO INSTEAD NOTHING;

CREATE INDEX idx_run_events_run_id    ON run_events(run_id, sequence);
CREATE INDEX idx_run_events_type      ON run_events(run_id, type);
CREATE INDEX idx_run_events_cursor    ON run_events(event_id);

-- Per-run sequence counter for run_events
CREATE TABLE run_event_sequences (
    run_id          TEXT        PRIMARY KEY REFERENCES runs(run_id),
    last_sequence   BIGINT      NOT NULL DEFAULT 0
);

-- ─────────────────────────────────────────────
-- ACTION SERVICE TABLES
-- ─────────────────────────────────────────────

-- Records the model's tool request before forwarding to the Action Service
CREATE TABLE tool_calls (
    tool_call_id    TEXT        PRIMARY KEY,
    run_id          TEXT        NOT NULL REFERENCES runs(run_id),
    tenant_id       TEXT        NOT NULL,
    tool_name       TEXT        NOT NULL,
    arguments       JSONB       NOT NULL DEFAULT '{}',
    status          TEXT        NOT NULL DEFAULT 'pending'
                                CHECK (status IN (
                                    'pending', 'completed', 'rejected', 'proposal_created'
                                )),
    result          JSONB,
    proposal_id     TEXT,       -- FK added after proposals table created
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX idx_tool_calls_run ON tool_calls(run_id);

CREATE TABLE proposals (
    proposal_id         TEXT        PRIMARY KEY,
    run_id              TEXT        NOT NULL REFERENCES runs(run_id),
    tenant_id           TEXT        NOT NULL,
    tool_call_id        TEXT        NOT NULL REFERENCES tool_calls(tool_call_id),
    status              TEXT        NOT NULL DEFAULT 'drafted'
                                    CHECK (status IN (
                                        'drafted', 'awaiting_approval',
                                        'approved', 'rejected', 'expired'
                                    )),
    current_version     INTEGER     NOT NULL DEFAULT 1,
    approved_version    INTEGER,    -- set only when status = 'approved'
    approval_id         TEXT,       -- FK added after approvals table created
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_proposals_run        ON proposals(run_id);
CREATE INDEX idx_proposals_status     ON proposals(status);

-- Each row is an immutable snapshot of a proposal at one point in time.
-- Prior versions are never mutated. A command references the exact approved version.
CREATE TABLE proposal_versions (
    version_id              TEXT        PRIMARY KEY,
    proposal_id             TEXT        NOT NULL REFERENCES proposals(proposal_id),
    run_id                  TEXT        NOT NULL,
    tenant_id               TEXT        NOT NULL,
    version                 INTEGER     NOT NULL,
    content                 JSONB       NOT NULL,       -- domain-specific proposal content
    evidence_ids            TEXT[]      NOT NULL DEFAULT '{}',
    policy_version          TEXT,
    uncertainty_statement   TEXT,
    created_by              TEXT        NOT NULL,       -- actor_id
    created_by_type         TEXT        NOT NULL
                                        CHECK (created_by_type IN ('agent', 'user', 'system')),
    reason                  TEXT,       -- initial | human_revision | stale_recalculation
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (proposal_id, version)
);

-- Enforce immutability: no updates or deletes
CREATE RULE proposal_versions_no_update AS ON UPDATE TO proposal_versions DO INSTEAD NOTHING;
CREATE RULE proposal_versions_no_delete AS ON DELETE TO proposal_versions DO INSTEAD NOTHING;

CREATE INDEX idx_proposal_versions_proposal ON proposal_versions(proposal_id, version);

CREATE TABLE approvals (
    approval_id                 TEXT        PRIMARY KEY,
    proposal_id                 TEXT        NOT NULL REFERENCES proposals(proposal_id),
    run_id                      TEXT        NOT NULL,
    tenant_id                   TEXT        NOT NULL,
    current_proposal_version    INTEGER     NOT NULL DEFAULT 1,
    status                      TEXT        NOT NULL DEFAULT 'pending'
                                            CHECK (status IN (
                                                'pending', 'claimed', 'revised',
                                                'approved', 'rejected', 'released', 'expired'
                                            )),
    expires_at                  TIMESTAMPTZ,
    assigned_to_type            TEXT        CHECK (assigned_to_type IN ('role', 'user')),
    assigned_to_id              TEXT,
    claimed_by                  TEXT,
    claimed_at                  TIMESTAMPTZ,
    approver_id                 TEXT,
    approver_note               TEXT,
    decided_proposal_version    INTEGER,    -- which version was approved/rejected
    resulting_command_id        TEXT,       -- FK added after commands table created
    decided_at                  TIMESTAMPTZ,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_approvals_run        ON approvals(run_id);
CREATE INDEX idx_approvals_tenant     ON approvals(tenant_id, status);
CREATE INDEX idx_approvals_status     ON approvals(status);
CREATE INDEX idx_approvals_assigned   ON approvals(assigned_to_id) WHERE status = 'pending';

-- Commands are created only after authorization.passed.
-- A command represents an authorized, dispatchable side effect.
CREATE TABLE commands (
    command_id              TEXT        PRIMARY KEY,
    run_id                  TEXT        NOT NULL REFERENCES runs(run_id),
    tenant_id               TEXT        NOT NULL,
    tool_call_id            TEXT        REFERENCES tool_calls(tool_call_id),
    proposal_id             TEXT        REFERENCES proposals(proposal_id),
    proposal_version        INTEGER     NOT NULL,   -- exact approved version
    approval_id             TEXT        REFERENCES approvals(approval_id),
    actor_id                TEXT        NOT NULL,
    tool_name               TEXT        NOT NULL,
    validated_arguments     JSONB       NOT NULL DEFAULT '{}',
    policy_decision_id      TEXT,
    idempotency_key         TEXT        NOT NULL UNIQUE,
    resource_version        INTEGER,
    expected_state_hash     TEXT,
    status                  TEXT        NOT NULL DEFAULT 'created'
                                        CHECK (status IN (
                                            'created', 'authorized', 'dispatched',
                                            'succeeded', 'failed', 'unknown',
                                            'reconciliation_required'
                                        )),
    downstream_reference    TEXT,       -- provider transaction ID, set on succeeded
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    authorized_at           TIMESTAMPTZ,
    dispatched_at           TIMESTAMPTZ,
    completed_at            TIMESTAMPTZ
);

CREATE INDEX idx_commands_run         ON commands(run_id);
CREATE INDEX idx_commands_status      ON commands(status);
CREATE INDEX idx_commands_idempotency ON commands(idempotency_key);
CREATE INDEX idx_commands_tenant      ON commands(tenant_id, status);

CREATE TABLE command_attempts (
    attempt_id          TEXT        PRIMARY KEY,
    command_id          TEXT        NOT NULL REFERENCES commands(command_id),
    attempt_number      INTEGER     NOT NULL,
    worker_id           TEXT,
    status              TEXT        NOT NULL DEFAULT 'in_progress'
                                    CHECK (status IN (
                                        'in_progress', 'succeeded', 'failed', 'unknown'
                                    )),
    provider_reference  TEXT,
    error               TEXT,
    request_payload     JSONB,
    response_payload    JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ,
    UNIQUE (command_id, attempt_number)
);

CREATE INDEX idx_attempts_command ON command_attempts(command_id);

-- Outbox table — written in the same transaction as the command record.
-- Workers claim events with leases. If a worker crashes, lease expiry allows
-- another worker to reclaim. The command enters 'unknown' if outcome is uncertain.
CREATE TABLE outbox (
    event_id            TEXT        PRIMARY KEY,
    command_id          TEXT        NOT NULL REFERENCES commands(command_id),
    status              TEXT        NOT NULL DEFAULT 'pending'
                                    CHECK (status IN (
                                        'pending', 'claimed', 'dispatched',
                                        'completed', 'reconcile_required'
                                    )),
    claimed_at          TIMESTAMPTZ,
    lease_expires_at    TIMESTAMPTZ,
    worker_id           TEXT,
    attempt_number      INTEGER     NOT NULL DEFAULT 0,
    last_error          TEXT,
    next_attempt_at     TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_outbox_status      ON outbox(status, next_attempt_at)
    WHERE status IN ('pending', 'reconcile_required');
CREATE INDEX idx_outbox_lease       ON outbox(lease_expires_at)
    WHERE status = 'claimed';

CREATE TABLE policies (
    policy_id       TEXT        PRIMARY KEY,
    tenant_id       TEXT        NOT NULL,
    domain          TEXT        NOT NULL,
    name            TEXT        NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'active',
    current_version TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE policy_versions (
    version_id      TEXT        PRIMARY KEY,
    policy_id       TEXT        NOT NULL REFERENCES policies(policy_id),
    version         TEXT        NOT NULL,
    rules           JSONB       NOT NULL DEFAULT '{}',
    effective_from  TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (policy_id, version)
);

-- Every deterministic policy evaluation produces a retrievable record.
-- Referenced by policy_decision_id on commands and approvals.
CREATE TABLE policy_decisions (
    decision_id         TEXT        PRIMARY KEY,
    policy_id           TEXT        REFERENCES policies(policy_id),
    policy_version      TEXT        NOT NULL,
    tenant_id           TEXT        NOT NULL,
    actor_id            TEXT,
    action              TEXT        NOT NULL,   -- e.g. payroll:correction:prepare
    result              TEXT        NOT NULL    CHECK (result IN ('allowed', 'denied', 'approval_required')),
    rule_matched        TEXT,
    evaluated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    context             JSONB       NOT NULL DEFAULT '{}'   -- amount, jurisdiction, etc.
);

CREATE INDEX idx_policy_decisions_tenant ON policy_decisions(tenant_id, evaluated_at);

CREATE TABLE reconciliation_tasks (
    task_id             TEXT        PRIMARY KEY,
    command_id          TEXT        NOT NULL REFERENCES commands(command_id),
    tenant_id           TEXT        NOT NULL,
    status              TEXT        NOT NULL DEFAULT 'pending'
                                    CHECK (status IN (
                                        'pending', 'in_progress', 'resolved', 'failed'
                                    )),
    resolution          TEXT        CHECK (resolution IN ('succeeded', 'failed', 'unknown')),
    idempotency_key     TEXT        NOT NULL,
    provider_reference  TEXT,
    attempts            INTEGER     NOT NULL DEFAULT 0,
    last_error          TEXT,
    next_attempt_at     TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at         TIMESTAMPTZ
);

CREATE INDEX idx_reconciliation_status ON reconciliation_tasks(status, next_attempt_at);

-- ─────────────────────────────────────────────
-- SHARED APPEND-ONLY TABLES
-- Rules: any service may INSERT; no service may UPDATE or DELETE
-- ─────────────────────────────────────────────

-- Full lifecycle audit events. Ordering is per run (via correlation_id + sequence),
-- not global. The database assigns sequence from run_event_sequences.
CREATE TABLE audit_events (
    event_id        TEXT        PRIMARY KEY,
    tenant_id       TEXT        NOT NULL,
    run_id          TEXT,       -- null for tenant-level events
    sequence        BIGINT,     -- per-run sequence assigned at insert
    type            TEXT        NOT NULL,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    causation_id    TEXT        NOT NULL,
    correlation_id  TEXT        NOT NULL,  -- always the run_id when run-scoped
    producer        TEXT        NOT NULL
                                CHECK (producer IN (
                                    'agent-api', 'action-service',
                                    'command-worker', 'system'
                                )),
    data            JSONB       NOT NULL DEFAULT '{}'
);

CREATE RULE audit_events_no_update AS ON UPDATE TO audit_events DO INSTEAD NOTHING;
CREATE RULE audit_events_no_delete AS ON DELETE TO audit_events DO INSTEAD NOTHING;

CREATE INDEX idx_audit_events_run    ON audit_events(run_id, sequence);
CREATE INDEX idx_audit_events_tenant ON audit_events(tenant_id, occurred_at);
CREATE INDEX idx_audit_events_type   ON audit_events(type);

-- Evidence is stored at retrieval time — not as a document ID reference.
-- If the source document is later updated, the original decision record is unchanged.
CREATE TABLE evidence_snapshots (
    evidence_id         TEXT        PRIMARY KEY,
    tenant_id           TEXT        NOT NULL,
    source_type         TEXT        NOT NULL,
    source_uri          TEXT        NOT NULL,
    source_version      TEXT,
    jurisdiction        TEXT,
    retrieved_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    content_hash        TEXT        NOT NULL,
    excerpt             TEXT,
    locator             TEXT,
    trust_level         TEXT        NOT NULL
                                    CHECK (trust_level IN (
                                        'authoritative', 'informational', 'unverified'
                                    )),
    content_type        TEXT        NOT NULL DEFAULT 'text/plain',
    redaction_profile   TEXT,
    used_by_model       BOOLEAN     NOT NULL DEFAULT FALSE,
    used_in_proposal    BOOLEAN     NOT NULL DEFAULT FALSE,
    used_in_decision    BOOLEAN     NOT NULL DEFAULT FALSE,
    retention_policy_id TEXT,
    expires_at          TIMESTAMPTZ,
    deletion_status     TEXT        NOT NULL DEFAULT 'active'
                                    CHECK (deletion_status IN ('active', 'scheduled', 'deleted')),
    legal_hold          BOOLEAN     NOT NULL DEFAULT FALSE
);

CREATE RULE evidence_snapshots_no_delete AS ON DELETE TO evidence_snapshots DO INSTEAD NOTHING;

CREATE INDEX idx_evidence_run       ON evidence_snapshots(tenant_id, retrieved_at);
CREATE INDEX idx_evidence_retention ON evidence_snapshots(expires_at)
    WHERE deletion_status = 'active' AND legal_hold = FALSE;

CREATE TABLE handoffs (
    handoff_id              TEXT        PRIMARY KEY,
    run_id                  TEXT        NOT NULL REFERENCES runs(run_id),
    tenant_id               TEXT        NOT NULL,
    from_agent              TEXT        NOT NULL,
    to_agent                TEXT        NOT NULL,
    handoff_type            TEXT        NOT NULL
                                        CHECK (handoff_type IN (
                                            'internal_handoff',
                                            'user_visible_handoff',
                                            'human_escalation'
                                        )),
    reason                  TEXT,
    required_scopes         TEXT[]      NOT NULL DEFAULT '{}',
    user_visible            BOOLEAN     NOT NULL DEFAULT TRUE,
    requires_user_consent   BOOLEAN     NOT NULL DEFAULT FALSE,
    status                  TEXT        NOT NULL DEFAULT 'initiated'
                                        CHECK (status IN (
                                            'initiated', 'active', 'completed', 'failed'
                                        )),
    context_package         JSONB       NOT NULL DEFAULT '{}',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at            TIMESTAMPTZ
);

CREATE INDEX idx_handoffs_run ON handoffs(run_id);

CREATE TABLE knowledge_documents (
    document_id     TEXT        PRIMARY KEY,
    tenant_id       TEXT        NOT NULL,
    domain          TEXT        NOT NULL,
    source_type     TEXT        NOT NULL,
    source_uri      TEXT        NOT NULL,
    content_hash    TEXT        NOT NULL,
    embedding_model TEXT,
    jurisdiction    TEXT,
    effective_from  TIMESTAMPTZ,
    effective_to    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_knowledge_domain ON knowledge_documents(tenant_id, domain);

-- ─────────────────────────────────────────────
-- DEFERRED FOREIGN KEYS AND CROSS-TABLE REFS
-- ─────────────────────────────────────────────

ALTER TABLE runs ADD CONSTRAINT fk_runs_pending_approval
    FOREIGN KEY (pending_approval_id) REFERENCES approvals(approval_id)
    DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE tool_calls ADD CONSTRAINT fk_tool_calls_proposal
    FOREIGN KEY (proposal_id) REFERENCES proposals(proposal_id)
    DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE proposals ADD CONSTRAINT fk_proposals_approval
    FOREIGN KEY (approval_id) REFERENCES approvals(approval_id)
    DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE approvals ADD CONSTRAINT fk_approvals_command
    FOREIGN KEY (resulting_command_id) REFERENCES commands(command_id)
    DEFERRABLE INITIALLY DEFERRED;

-- ─────────────────────────────────────────────
-- IDEMPOTENCY KEY STORE
-- ─────────────────────────────────────────────

CREATE TABLE idempotency_keys (
    key_hash        TEXT        PRIMARY KEY,   -- SHA-256 of method+path+tenant+body
    idempotency_key TEXT        NOT NULL,
    tenant_id       TEXT        NOT NULL,
    method          TEXT        NOT NULL,
    path            TEXT        NOT NULL,
    request_id      TEXT        NOT NULL,
    response_status INTEGER     NOT NULL,
    response_body   JSONB       NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL   -- 24h for API; longer for commands
);

CREATE INDEX idx_idempotency_expires ON idempotency_keys(expires_at);

COMMIT;
