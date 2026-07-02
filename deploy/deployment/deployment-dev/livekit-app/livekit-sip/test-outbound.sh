#!/bin/bash
# test-outbound.sh — place a test outbound call through the SELF-HOSTED LiveKit
# server + SIP trunk. Run from your Mac (needs `lk` + `gcloud`).
#
# Opens an IAP tunnel to the self-hosted server's internal :7880, pulls the
# self-host creds + trunk id from Secret Manager, creates a room, and dials.
#
# Usage:
#   ./test-outbound.sh +15551234567 [room-name]
#
# Set CLEANUP=1 to wait for the call to end and delete the room afterward:
#   CLEANUP=1 ./test-outbound.sh +15551234567
# (Without it, LiveKit auto-closes the empty room after empty_timeout, ~5 min.)
set -euo pipefail

NUMBER="${1:-}"
ROOM="${2:-test-outbound}"
ZONE="us-central1-a"
LIVEKIT_VM="vera-livekit-vm"
# Unique per call so multiple calls into one room don't evict each other (DUPLICATE_IDENTITY)
IDENTITY="${IDENTITY:-sip-callee-$(date +%s)-${RANDOM}}"

if [[ -z "$NUMBER" ]]; then
  echo "Usage: $0 +1<number> [room-name]" >&2
  exit 1
fi
command -v lk     >/dev/null || { echo "lk CLI not found on PATH" >&2; exit 1; }
command -v gcloud >/dev/null || { echo "gcloud not found on PATH" >&2; exit 1; }

PROJECT=$(gcloud config get-value project 2>/dev/null)

# ── IAP tunnel to the self-hosted server (torn down on exit) ──────────────────
echo "Opening IAP tunnel to ${LIVEKIT_VM}:7880 ..."
gcloud compute start-iap-tunnel "$LIVEKIT_VM" 7880 \
  --local-host-port=localhost:7880 --zone="$ZONE" --project="$PROJECT" &
TUNNEL_PID=$!
trap 'kill "$TUNNEL_PID" 2>/dev/null || true' EXIT

# wait for the tunnel to accept connections
for _ in $(seq 1 30); do
  nc -z localhost 7880 2>/dev/null && break
  sleep 1
done
nc -z localhost 7880 2>/dev/null || { echo "Tunnel did not come up on :7880" >&2; exit 1; }

# ── Self-host creds + trunk id (via your gcloud identity) ─────────────────────
export LIVEKIT_URL=ws://localhost:7880
export LIVEKIT_API_KEY=$(gcloud secrets versions access latest --secret=vera-livekit-selfhost-api-key)
export LIVEKIT_API_SECRET=$(gcloud secrets versions access latest --secret=vera-livekit-selfhost-api-secret)
TRUNK=$(gcloud secrets versions access latest --secret=vera-livekit-selfhost-sip-trunk-id)

echo "Trunk: $TRUNK   Room: $ROOM   Dialing: $NUMBER"
echo "(Tip: watch SIP logs →  gcloud compute ssh ${LIVEKIT_VM/livekit/sip} --tunnel-through-iap --zone=$ZONE --command='sudo docker logs -f livekit-sip')"

# ── Create room + dial (lk flags vary by version — see `lk <cmd> --help`) ─────
lk room create "$ROOM" 2>/dev/null || true
lk sip participant create --trunk "$TRUNK" --call "$NUMBER" --room "$ROOM" --identity "$IDENTITY"

echo ""
echo "Call placed. Check who joined:  lk room participants list --room $ROOM"

if [[ "${CLEANUP:-0}" == "1" ]]; then
  echo "Cleanup mode: waiting for the callee to answer, then for hangup..."
  joined=0
  for _ in $(seq 1 60); do
    if lk room participants list --room "$ROOM" 2>/dev/null | grep -q sip-callee; then joined=1; break; fi
    sleep 1
  done
  if [[ "$joined" == "1" ]]; then
    # callee answered — wait until they leave (call ends), then delete the room
    while lk room participants list --room "$ROOM" 2>/dev/null | grep -q sip-callee; do sleep 3; done
    lk room delete "$ROOM" 2>/dev/null || true
    echo "Call ended — room '$ROOM' deleted."
  else
    echo "Callee didn't answer within 60s; leaving room to auto-close (lk room delete $ROOM to remove now)."
  fi
else
  echo "(Room auto-closes once empty — server empty_timeout, ~5 min. Or: lk room delete $ROOM)"
fi
