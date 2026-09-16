#!/usr/bin/env bash
# Conversational Operations Runtime — end-to-end curl walkthrough
# Prerequisites: runtime running at localhost:8080, API key set in API_KEY
#
# This example walks through the primary governed-action loop:
#   start run → send message → await approval → approve → inspect audit
#
# The golden event sequence for this flow is in spec/golden_sequence.json

set -euo pipefail

BASE="http://localhost:8080"
KEY="${API_KEY:-your-api-key-here}"
AUTH="Authorization: Bearer $KEY"

echo "=== 1. Start an agent run ==="
# Tenant is derived from the API key — do not supply tenant_id
RUN=$(curl -s -X POST "$BASE/v1/runs" \
  -H "$AUTH" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: run-example-$(date +%s)" \
  -d '{"agent_id": "payroll-detective"}')
echo "$RUN" | python3 -m json.tool
RUN_ID=$(echo "$RUN" | python3 -c "import sys,json; print(json.load(sys.stdin)['run_id'])")
echo "Run ID: $RUN_ID"


echo ""
echo "=== 2. Send a message — expect 202 awaiting_approval ==="
MSG=$(curl -s -X POST "$BASE/v1/runs/$RUN_ID/messages" \
  -H "$AUTH" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: msg-$RUN_ID-001" \
  -d '{"content": "Investigate the payroll anomaly for EMP-4412."}')
echo "$MSG" | python3 -m json.tool
STATUS=$(echo "$MSG" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
echo "Run status: $STATUS"


echo ""
echo "=== 3. Get the pending approval ==="
APPROVAL_ID=$(echo "$MSG" | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print(d['pending_approval']['approval_id'])")
APPROVAL=$(curl -s "$BASE/v1/approvals/$APPROVAL_ID" -H "$AUTH")
echo "$APPROVAL" | python3 -m json.tool
VERSION=$(echo "$APPROVAL" | python3 -c \
  "import sys,json; print(json.load(sys.stdin)['current_proposal_version'])")
echo "Proposal version: $VERSION"


echo ""
echo "=== 4. Claim the approval (prevent simultaneous review) ==="
curl -s -X POST "$BASE/v1/approvals/$APPROVAL_ID/claim" \
  -H "$AUTH" \
  -H "Idempotency-Key: claim-$APPROVAL_ID" \
  | python3 -m json.tool


echo ""
echo "=== 5. Approve the proposal ==="
curl -s -X POST "$BASE/v1/approvals/$APPROVAL_ID/approve" \
  -H "$AUTH" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: approve-$APPROVAL_ID-v$VERSION" \
  -d "{\"proposal_version\": $VERSION, \"approver_note\": \"Reviewed cited records.\"}" \
  | python3 -m json.tool


echo ""
echo "=== 6. Get command state ==="
# Wait a moment for the worker to dispatch
sleep 2
COMMANDS=$(curl -s "$BASE/v1/commands?run_id=$RUN_ID" -H "$AUTH")
echo "$COMMANDS" | python3 -m json.tool
CMD_ID=$(echo "$COMMANDS" | python3 -c \
  "import sys,json; print(json.load(sys.stdin)['commands'][0]['command_id'])")
echo "Command ID: $CMD_ID"


echo ""
echo "=== 7. Inspect the full audit event log ==="
curl -s "$BASE/v1/runs/$RUN_ID/events?limit=50" -H "$AUTH" | python3 -m json.tool


echo ""
echo "=== 8. Replay in inspect mode — no side effects ==="
curl -s -X POST "$BASE/v1/runs/$RUN_ID/replay" \
  -H "$AUTH" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: replay-$RUN_ID-inspect-001" \
  -d '{"mode": "inspect"}' \
  | python3 -m json.tool


echo ""
echo "=== 9. Simulate unknown outcome (failure injection) ==="
# Trigger the reconciliation path by injecting a timeout at the provider
# In the test harness, set X-Simulate-Failure: timeout on the commit call
# then inspect the command status:
curl -s "$BASE/v1/commands/$CMD_ID" -H "$AUTH" | python3 -m json.tool

echo ""
echo "Done. Golden event sequence expected:"
cat "$(dirname "$0")/../golden_sequence.json" \
  | python3 -c "import sys,json; [print(' ', e['event']) for e in json.load(sys.stdin)['sequence']]"
