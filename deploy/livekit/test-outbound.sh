#!/bin/bash
# Runs ON vera-test-livekit-vm (as root, via deploy-livekit.sh --test-outbound +1…).
# Smoke test: places an outbound PSTN call through test's SIP trunk into a fresh room.
# Needs TEST_DEST (E.164 destination) exported by the caller.
set -euo pipefail

SECRET_PREFIX="${SECRET_PREFIX:-vera-test}"
: "${TEST_DEST:?TEST_DEST (destination number, e.g. +15551234567) must be set}"

if ! command -v lk &>/dev/null; then
  curl -sSL https://get.livekit.io/cli | bash
fi

export LIVEKIT_URL="ws://localhost:7880"
LIVEKIT_API_KEY=$(gcloud secrets versions access latest --secret="${SECRET_PREFIX}-livekit-api-key")
LIVEKIT_API_SECRET=$(gcloud secrets versions access latest --secret="${SECRET_PREFIX}-livekit-api-secret")
export LIVEKIT_API_KEY LIVEKIT_API_SECRET

TRUNK_ID=$(gcloud secrets versions access latest --secret="${SECRET_PREFIX}-livekit-sip-trunk-id")
ROOM="sip-test-$(date +%s)"

REQ="$(mktemp)"
trap 'rm -f "$REQ"' EXIT
cat > "$REQ" <<JSON
{
  "sip_trunk_id": "${TRUNK_ID}",
  "sip_call_to": "${TEST_DEST}",
  "room_name": "${ROOM}",
  "participant_identity": "sip-test",
  "participant_name": "SIP Test Call"
}
JSON

echo "Dialing ${TEST_DEST} via ${TRUNK_ID} into room ${ROOM}..."
lk sip participant create "$REQ"
