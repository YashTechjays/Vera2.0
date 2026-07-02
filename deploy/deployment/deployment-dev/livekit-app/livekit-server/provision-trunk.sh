#!/bin/bash
# provision-trunk.sh — run ON vera-livekit-vm via `deploy.sh --provision-trunk`.
#
# Idempotently registers the self-hosted outbound SIP trunk against the LiveKit
# server running locally on this VM. All values are fetched from Secret Manager
# via the VM service account — no credentials touch a laptop.
#
# This VM's SA is READ-ONLY on secrets, so the script PRINTS the resulting trunk
# id; store it yourself (command printed at the end):
#   printf 'ST_xxx' | gcloud secrets versions add vera-livekit-selfhost-sip-trunk-id --data-file=-
set -euo pipefail

TRUNK_NAME="twilio-outbound-selfhost"

# ── lk CLI (install if missing) ───────────────────────────────────────────────
if ! command -v lk &>/dev/null; then
  echo "Installing livekit-cli (lk)..."
  curl -sSL https://get.livekit.io/cli | bash
fi

# ── Secrets (via VM service account) ──────────────────────────────────────────
echo "Fetching secrets from Secret Manager..."
export LIVEKIT_URL=ws://localhost:7880
export LIVEKIT_API_KEY=$(gcloud secrets versions access latest --secret=vera-livekit-selfhost-api-key)
export LIVEKIT_API_SECRET=$(gcloud secrets versions access latest --secret=vera-livekit-selfhost-api-secret)
ADDR=$(gcloud secrets versions access latest --secret=vera-livekit-selfhost-sip-trunk-address)
NUM=$(gcloud secrets versions access latest --secret=vera-livekit-selfhost-sip-trunk-number)
# Optional credential auth — omit if the secrets are empty (e.g. IP-authorized termination)
AUTH_USER=$(gcloud secrets versions access latest --secret=vera-livekit-selfhost-sip-trunk-auth-username 2>/dev/null || true)
AUTH_PASS=$(gcloud secrets versions access latest --secret=vera-livekit-selfhost-sip-trunk-auth-password 2>/dev/null || true)

# ── Replace any existing trunk with this name (so config/auth changes apply) ──
for id in $(lk sip outbound list 2>/dev/null | grep "$TRUNK_NAME" | grep -oE 'ST_[A-Za-z0-9]+'); do
  echo "Deleting existing trunk $id ..."
  lk sip outbound delete "$id" 2>/dev/null || true
done

# ── Create ────────────────────────────────────────────────────────────────────
TRUNK_JSON=$(mktemp)
trap 'rm -f "$TRUNK_JSON"' EXIT
if [[ -n "$AUTH_USER" && -n "$AUTH_PASS" ]]; then
  echo "Including SIP termination credentials (credential auth)."
  printf '{"trunk":{"name":"%s","address":"%s","numbers":["%s"],"auth_username":"%s","auth_password":"%s"}}' \
    "$TRUNK_NAME" "$ADDR" "$NUM" "$AUTH_USER" "$AUTH_PASS" > "$TRUNK_JSON"
else
  echo "No SIP credentials set — creating trunk without auth (IP-authorized termination)."
  printf '{"trunk":{"name":"%s","address":"%s","numbers":["%s"]}}' "$TRUNK_NAME" "$ADDR" "$NUM" > "$TRUNK_JSON"
fi

echo "Creating outbound trunk on ws://localhost:7880 ..."
OUT=$(lk sip outbound create "$TRUNK_JSON")   # if this errors, run `lk sip outbound create --help` for your lk version
echo "$OUT"

ST=$(printf '%s' "$OUT" | grep -oE 'ST_[A-Za-z0-9]+' | head -1)
echo ""
echo "=============================================================="
echo " New self-hosted outbound trunk id: ${ST:-<see output above>}"
echo " Store it (run from your Mac):"
echo "   printf '${ST:-ST_xxx}' | gcloud secrets versions add vera-livekit-selfhost-sip-trunk-id --data-file=-"
echo "=============================================================="
