#!/usr/bin/env bash
# Operator deploy script for the self-hosted LiveKit stack in the TEST environment.
#
# Run from your laptop (needs gcloud + IAP tunnel access). It SSHes into each VM
# over IAP and runs the matching start script AS ROOT there; the start script fetches
# its own secrets from Secret Manager (nothing sensitive touches your laptop).
#
# The server exposes a public wss:// endpoint (Caddy + Let's Encrypt on the
# livekit-domain), so browser participants can join; in-VPC clients use ws://…:7880.
#
# Usage:
#   ./deploy-livekit.sh --server                 # start LiveKit server (do this first)
#   ./deploy-livekit.sh --provision-trunk        # register test's outbound trunk → prints ST_…
#   ./deploy-livekit.sh --sip                    # start the Twilio SIP bridge
#   ./deploy-livekit.sh --egress                 # start the recorder
#   ./deploy-livekit.sh --all                    # server + sip + egress (not trunk/test)
#   ./deploy-livekit.sh --test-outbound +1555... # place a test PSTN call
#
# Config (override via env): PROJECT_ID, ZONE, LIVEKIT_VM, SIP_VM, EGRESS_VM.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PROJECT_ID="${PROJECT_ID:-innate-watch-497101-k4}"
ZONE="${ZONE:-us-east1-b}"
LIVEKIT_VM="${LIVEKIT_VM:-vera-test-livekit-vm}"
SIP_VM="${SIP_VM:-vera-test-sip-vm}"
EGRESS_VM="${EGRESS_VM:-vera-test-egress-vm}"

# Run a local script on a remote VM as root, over the IAP tunnel. Optional 3rd arg
# is a shell prelude (e.g. env exports) prepended before the script body.
ssh_run() {
  local vm="$1" script="$2" prelude="${3:-}"
  echo "→ ${vm}: $(basename "$script")"
  # shellcheck disable=SC2086
  gcloud compute ssh "$vm" \
    --tunnel-through-iap --zone "$ZONE" --project "$PROJECT_ID" \
    --command "sudo bash -s" < <(printf '%s\n' "$prelude"; cat "$script")
}

do_server=false do_sip=false do_egress=false do_trunk=false do_test=false
test_dest=""

[[ $# -eq 0 ]] && { grep -E '^#( |$)' "$0" | sed -E 's/^# ?//'; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --server) do_server=true ;;
    --sip) do_sip=true ;;
    --egress) do_egress=true ;;
    --all) do_server=true; do_sip=true; do_egress=true ;;
    --provision-trunk) do_trunk=true ;;
    --test-outbound) do_test=true; test_dest="${2:-}"; shift ;;
    *) echo "unknown flag: $1" >&2; exit 1 ;;
  esac
  shift
done

# Order matters: server up first; sip/egress/trunk all depend on it.
$do_server && ssh_run "$LIVEKIT_VM" "$SCRIPT_DIR/livekit-server/start.sh"
$do_trunk  && ssh_run "$LIVEKIT_VM" "$SCRIPT_DIR/provision-trunk.sh"
$do_sip    && ssh_run "$SIP_VM"     "$SCRIPT_DIR/livekit-sip/start.sh"
$do_egress && ssh_run "$EGRESS_VM"  "$SCRIPT_DIR/livekit-egress/start.sh"

if $do_test; then
  [[ -n "$test_dest" ]] || { echo "--test-outbound needs a destination number, e.g. +15551234567" >&2; exit 1; }
  ssh_run "$LIVEKIT_VM" "$SCRIPT_DIR/test-outbound.sh" "export TEST_DEST='${test_dest}'"
fi

echo "Done."
