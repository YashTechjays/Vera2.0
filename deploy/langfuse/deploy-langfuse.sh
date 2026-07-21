#!/usr/bin/env bash
# Operator deploy script for the self-hosted Langfuse stack in the TEST environment.
#
# Run from your laptop (needs gcloud + IAP tunnel access). It SSHes into the Langfuse
# VM over IAP and runs start-langfuse.sh AS ROOT there; that script fetches its own
# secrets from Secret Manager (nothing sensitive touches your laptop) and brings up the
# langfuse-web + worker + clickhouse + redis + minio stack. Idempotent — re-run to
# restart/upgrade; ClickHouse + MinIO persist on the VM's dedicated data disk.
#
# Langfuse has no public endpoint. To reach the UI after deploy, open an IAP tunnel:
#   gcloud compute start-iap-tunnel vera-test-langfuse-vm 3000 \
#     --local-host-port=localhost:3000 --zone "$ZONE" --project "$PROJECT_ID"
# then browse http://localhost:3000 (login admin@vera.test / vera-test-langfuse-init-user-password).
#
# Usage:
#   ./deploy-langfuse.sh --deploy      # start/restart the Langfuse stack
#
# Config (override via env): PROJECT_ID, ZONE, LANGFUSE_VM.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PROJECT_ID="${PROJECT_ID:-innate-watch-497101-k4}"
ZONE="${ZONE:-us-east1-b}"
LANGFUSE_VM="${LANGFUSE_VM:-vera-test-langfuse-vm}"

# Run a local script on a remote VM as root, over the IAP tunnel.
ssh_run() {
  local vm="$1" script="$2"
  echo "→ ${vm}: $(basename "$script")"
  gcloud compute ssh "$vm" \
    --tunnel-through-iap --zone "$ZONE" --project "$PROJECT_ID" \
    --command "sudo bash -s" < "$script"
}

do_deploy=false

[[ $# -eq 0 ]] && { grep -E '^#( |$)' "$0" | sed -E 's/^# ?//'; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --deploy) do_deploy=true ;;
    *) echo "unknown flag: $1" >&2; exit 1 ;;
  esac
  shift
done

$do_deploy && ssh_run "$LANGFUSE_VM" "$SCRIPT_DIR/start-langfuse.sh"

echo "Done."
