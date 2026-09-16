-- Phase 4: add target_run_id to handoffs
-- The source run creates the target run as part of the handoff transaction.
-- This column links the handoff record to the newly created target run.
ALTER TABLE handoffs ADD COLUMN target_run_id TEXT REFERENCES runs(run_id);
