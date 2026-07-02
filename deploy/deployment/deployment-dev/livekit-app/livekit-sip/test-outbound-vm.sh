#!/bin/bash
# test-outbound-vm.sh — runs ON vera-livekit-vm via `deploy.sh --test-outbound`.
# Places a test outbound call through the self-hosted server (ws://localhost:7880).
#
# Inputs via env (set by deploy.sh):
#   TEST_NUMBER  (required)  e.g. +919901585111
#   TRUNK_ID     (required)  ST_… (read from secret on the Mac, passed in)
#   ROOM         (default: test-outbound)
#   CLEANUP      (0/1) — if 1, wait for the call to end then delete the room
set -euo pipefail
export PATH="/usr/local/bin:$PATH"

NUMBER="${TEST_NUMBER:?TEST_NUMBER not set}"
TRUNK="${TRUNK_ID:?TRUNK_ID not set}"
ROOM="${ROOM:-test-outbound}"
# Unique per call — LiveKit evicts an existing participant with the same identity,
# so multiple concurrent test calls into one room each need a distinct identity.
IDENTITY="${IDENTITY:-sip-callee-$(date +%s)-${RANDOM}}"

export LIVEKIT_URL=ws://localhost:7880
export LIVEKIT_API_KEY=$(gcloud secrets versions access latest --secret=vera-livekit-selfhost-api-key)
export LIVEKIT_API_SECRET=$(gcloud secrets versions access latest --secret=vera-livekit-selfhost-api-secret)

echo "Trunk: $TRUNK   Room: $ROOM   Dialing: $NUMBER"
lk room create "$ROOM" 2>/dev/null || true
lk sip participant create --trunk "$TRUNK" --call "$NUMBER" --room "$ROOM" --identity "$IDENTITY"
echo "Call placed (identity: $IDENTITY). Participants:"
lk room participants list --room "$ROOM" 2>/dev/null || true

if [[ "${CLEANUP:-0}" == "1" ]]; then
  echo "Cleanup mode: waiting for the callee to answer, then for hangup..."
  joined=0
  for _ in $(seq 1 60); do
    if lk room participants list --room "$ROOM" 2>/dev/null | grep -q sip-callee; then joined=1; break; fi
    sleep 1
  done
  if [[ "$joined" == "1" ]]; then
    while lk room participants list --room "$ROOM" 2>/dev/null | grep -q sip-callee; do sleep 3; done
    lk room delete "$ROOM" 2>/dev/null || true
    echo "Call ended — room '$ROOM' deleted."
  else
    echo "Callee didn't answer within 60s; leaving room to auto-close."
  fi
fi
