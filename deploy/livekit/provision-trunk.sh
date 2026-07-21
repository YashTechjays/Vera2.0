#!/bin/bash
# Runs ON vera-test-livekit-vm (as root, via deploy-livekit.sh over IAP).
# One-off: registers test's OUTBOUND SIP trunk inside test's own LiveKit server and
# prints the resulting ST_… id. This is a LiveKit-side object (stored in test's
# dedicated LiveKit Redis) — it must be created per-environment; it costs nothing on
# Twilio and points at the SAME outbound Twilio path as dev.
#
# After it prints ST_…, store it:
#   gcloud secrets versions add vera-test-livekit-sip-trunk-id --data-file=- <<< "ST_…"
# then add `livekit-sip-trunk-id = VERA_LIVEKIT_SIP_TRUNK_ID` to deploy/secrets.map.
set -euo pipefail

SECRET_PREFIX="${SECRET_PREFIX:-vera-test}"

# ── LiveKit CLI (idempotent install) ──────────────────────────────────────────
if ! command -v lk &>/dev/null; then
  echo "Installing livekit-cli (lk)..."
  curl -sSL https://get.livekit.io/cli | bash
fi

# ── Connection + trunk details from Secret Manager ────────────────────────────
export LIVEKIT_URL="ws://localhost:7880"
LIVEKIT_API_KEY=$(gcloud secrets versions access latest --secret="${SECRET_PREFIX}-livekit-api-key")
LIVEKIT_API_SECRET=$(gcloud secrets versions access latest --secret="${SECRET_PREFIX}-livekit-api-secret")
export LIVEKIT_API_KEY LIVEKIT_API_SECRET

TRUNK_ADDRESS=$(gcloud secrets versions access latest --secret="${SECRET_PREFIX}-livekit-sip-trunk-address")
TRUNK_NUMBER=$(gcloud secrets versions access latest --secret="${SECRET_PREFIX}-livekit-sip-trunk-number")
TRUNK_USER=$(gcloud secrets versions access latest --secret="${SECRET_PREFIX}-livekit-sip-trunk-auth-username")
TRUNK_PASS=$(gcloud secrets versions access latest --secret="${SECRET_PREFIX}-livekit-sip-trunk-auth-password")

# ── Create the outbound trunk ─────────────────────────────────────────────────
REQ="$(mktemp)"
trap 'rm -f "$REQ"' EXIT
# transport TLS + media_encryption REQUIRE match Twilio Secure Trunking (TLS
# signaling on :5061 + SRTP media). Without these the INVITE goes out over plain
# UDP and Twilio silently drops it ("upstream-no-response"). Mirrors dev's trunk.
cat > "$REQ" <<JSON
{
  "trunk": {
    "name": "${SECRET_PREFIX}-outbound",
    "address": "${TRUNK_ADDRESS}",
    "transport": "SIP_TRANSPORT_TLS",
    "numbers": ["${TRUNK_NUMBER}"],
    "auth_username": "${TRUNK_USER}",
    "auth_password": "${TRUNK_PASS}",
    "media_encryption": "SIP_MEDIA_ENCRYPT_REQUIRE"
  }
}
JSON

echo "Creating outbound trunk in test's LiveKit..."
OUT="$(lk sip outbound create "$REQ")"
echo "$OUT"

TRUNK_ID="$(printf '%s\n' "$OUT" | grep -oE 'ST_[A-Za-z0-9]+' | head -n1)"
echo
if [[ -n "$TRUNK_ID" ]]; then
  echo "==> Outbound trunk id: ${TRUNK_ID}"
  echo "    Store it:  gcloud secrets versions add ${SECRET_PREFIX}-livekit-sip-trunk-id --data-file=- <<< \"${TRUNK_ID}\""
else
  echo "!! Could not parse ST_… from the output above — copy the trunk id manually." >&2
fi
