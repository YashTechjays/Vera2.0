#!/usr/bin/env bash
# Runs FROM your laptop or the jumpserver (NOT part of CI/CD). Ships superadmin_seed.sh to
# the app VM over an IAP tunnel and runs it there to seed platform operator #1 — the first
# SUPER_ADMIN. The super-admin email/password come from Secret Manager (the on-VM script fetches
# them); nothing sensitive is typed here.
#
#   superadmin_runme.sh [--project ID] [--vm NAME] [--zone ZONE] \
#                       [--prefix PREFIX] [--sa SA_EMAIL] [--check] [--yes]
#
# Each value: an explicit flag wins, else it is auto-detected from gcloud (env vars VERA_PROJECT
# / VERA_VM / VERA_ZONE / VERA_SECRET_PREFIX / VERA_DEPLOYER_SA are accepted too). Resolved
# values are printed and confirmed BEFORE anything is copied to the VM — a wrong value aborts
# without touching the box. First run prints a one-time otpauth:// MFA URI; scan it immediately
# (re-runs are a safe no-op). Requires a deployed app VM (app.env present) and the two secrets
# "${PREFIX}-superadmin-email" / "${PREFIX}-superadmin-password" to exist in Secret Manager.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PROJECT="${VERA_PROJECT:-}"
VM="${VERA_VM:-}"
ZONE="${VERA_ZONE:-}"
SECRET_PREFIX="${VERA_SECRET_PREFIX:-vera-test}"
DEPLOYER_SA="${VERA_DEPLOYER_SA:-}"
CHECK_ONLY=false
ASSUME_YES=false

die() { echo "error: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --vm)      VM="$2"; shift 2 ;;
    --zone)    ZONE="$2"; shift 2 ;;
    --prefix)  SECRET_PREFIX="$2"; shift 2 ;;
    --sa)      DEPLOYER_SA="$2"; shift 2 ;;
    --check)   CHECK_ONLY=true; shift ;;
    --yes)     ASSUME_YES=true; shift ;;
    -h|--help) grep '^#' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)         die "unknown argument: $1" ;;
  esac
done

# Resolve PROJECT from gcloud config if not supplied.
if [ -z "$PROJECT" ]; then
  PROJECT="$(gcloud config get-value project 2>/dev/null || true)"
  [ -n "$PROJECT" ] || die "no project: pass --project or run 'gcloud config set project <ID>'"
fi

# Resolve the VM name+zone: an explicit --vm wins, else auto-detect the (single) test VM.
if [ -n "$VM" ]; then
  VM_SRC="arg"
else
  VM_SRC="auto-detected"
  matches="$(gcloud compute instances list --project "$PROJECT" \
    --filter="name~'^${SECRET_PREFIX}'" --format="value(name,zone)")"
  count="$(printf '%s' "$matches" | grep -c . || true)"
  case "$count" in
    0) die "no VM matched name~'^${SECRET_PREFIX}' in $PROJECT — pass --vm and --zone" ;;
    1) read -r VM ZONE <<<"$matches" ;;
    *) die "$count VMs matched name~'^${SECRET_PREFIX}' in $PROJECT — pass --vm and --zone to disambiguate" ;;
  esac
fi
[ -n "$ZONE" ] || die "no zone for VM '$VM' — pass --zone"

# Preflight: show what will run and confirm before touching the VM.
cat >&2 <<EOF

Bootstrap preflight — verify before continuing:
  PROJECT       = $PROJECT
  VM / ZONE     = $VM / $ZONE   ($VM_SRC)
  SECRET_PREFIX = $SECRET_PREFIX
  IMPERSONATE   = ${DEPLOYER_SA:-none}

Will, over an IAP tunnel:
  1. scp $SCRIPT_DIR/superadmin_seed.sh -> $VM:/opt/vera/
  2. ssh $VM -- bash /opt/vera/superadmin_seed.sh $SECRET_PREFIX $PROJECT
EOF

if [ "$CHECK_ONLY" = true ]; then
  echo "--check: nothing copied or executed." >&2
  exit 0
fi

if [ "$ASSUME_YES" != true ]; then
  read -r -p "These values correct — proceed? [y/N] " reply
  case "$reply" in
    y|Y|yes|YES) ;;
    *) echo "aborted — nothing copied to the VM." >&2; exit 1 ;;
  esac
fi

# Impersonation is optional but recommended: superadmin_seed.sh runs `docker compose` on the
# VM, so the SSH identity must be in the VM's docker group (the provisioned deploy SA is).
GCLOUD_COMMON=(--tunnel-through-iap --zone "$ZONE" --project "$PROJECT")
[ -n "$DEPLOYER_SA" ] && GCLOUD_COMMON+=(--impersonate-service-account="$DEPLOYER_SA")

echo "Copying superadmin_seed.sh to $VM:/opt/vera/ ..." >&2
gcloud compute scp "${GCLOUD_COMMON[@]}" "$SCRIPT_DIR/superadmin_seed.sh" "$VM:/opt/vera/"

echo "Running bootstrap on $VM ..." >&2
gcloud compute ssh "$VM" "${GCLOUD_COMMON[@]}" \
  --command "bash /opt/vera/superadmin_seed.sh '$SECRET_PREFIX' '$PROJECT'"

cat >&2 <<'EOF'

Done. If this was the first run, scan the otpauth:// line above into your authenticator app NOW
— it is shown only once. Re-running prints "platform operator already exists — no-op".
EOF
