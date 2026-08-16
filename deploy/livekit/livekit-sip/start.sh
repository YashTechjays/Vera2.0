#!/bin/bash
# Runs ON vera-test-sip-vm (as root, via deploy-livekit.sh over IAP).
# Starts the LiveKit SIP bridge (Twilio ↔ LiveKit, outbound). It reaches the SFU
# over the LiveKit VM's private IP (ws://…:7880, in-VPC) and coordinates over the
# dedicated LiveKit Redis. Twilio termination uses the trunk registered by
# provision-trunk.sh. Secrets are fetched at runtime from Secret Manager.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
SECRET_PREFIX="${SECRET_PREFIX:-vera-test}"

if ! command -v docker &>/dev/null; then
  apt-get update -y
  apt-get install -y --no-install-recommends docker.io gettext-base curl
  systemctl enable docker
fi
systemctl start docker || true

echo "Fetching secrets from Secret Manager (${SECRET_PREFIX}-*)..."
export LIVEKIT_API_KEY LIVEKIT_API_SECRET REDIS_AUTH REDIS_HOST LIVEKIT_SERVER_IP
LIVEKIT_API_KEY=$(gcloud secrets versions access latest --secret="${SECRET_PREFIX}-livekit-api-key")
LIVEKIT_API_SECRET=$(gcloud secrets versions access latest --secret="${SECRET_PREFIX}-livekit-api-secret")
REDIS_AUTH=$(gcloud secrets versions access latest --secret="${SECRET_PREFIX}-livekit-redis-auth-string")
REDIS_HOST=$(curl -sf "http://metadata.google.internal/computeMetadata/v1/instance/attributes/redis-host" -H "Metadata-Flavor: Google")
LIVEKIT_SERVER_IP=$(curl -sf "http://metadata.google.internal/computeMetadata/v1/instance/attributes/livekit-server-ip" -H "Metadata-Flavor: Google")

mkdir -p /etc/livekit
chmod 700 /etc/livekit

# ── TLS cert for SIPS (Let's Encrypt, HTTP-01) ───────────────────────────────
# Twilio Secure Trunking sends in-dialog requests — notably the BYE when the far
# end hangs up — over TLS. Without a TLS listener livekit-sip reads the handshake
# as plaintext SIP, drops it, and the call only ends on the 15s media timeout.
# Twilio validates the cert, so it must be CA-signed; self-signed is rejected.
# --standalone binds :80 for the challenge (nothing else on this VM uses it; the
# sip-http-acme firewall rule opens it). Idempotent: certbot no-ops if the cert is
# still valid, so this is safe on every redeploy.
SIP_TLS_DOMAIN="${SIP_TLS_DOMAIN:-test.sip.veratechsolutions.ai}"
if ! command -v certbot &>/dev/null; then
  apt-get update -y
  apt-get install -y --no-install-recommends certbot
fi

# Deploy hook — the renewal half of the story. certbot's systemd timer runs
# `certbot renew` twice daily and renews inside 30 days of expiry, but livekit-sip
# reads cert_file/key_file once at startup (startTLS() takes a pre-built tls.Config;
# there is no GetCertificate callback or file watcher), so a renewed file on disk is
# NOT picked up live. Without this hook the cert silently expires from the service's
# point of view and Twilio starts rejecting the TLS connection — back to 15s hangups,
# ~90 days after deploy, with nothing in the deploy logs to explain it.
#
# certbot persists --deploy-hook into /etc/letsencrypt/renewal/<domain>.conf, so it
# runs on every future renewal, not just this invocation. It only fires when a cert
# is actually renewed, never on a no-op check.
#
# NOTE: `docker restart` drops calls in progress. Renewal fires at a randomised time
# inside the 30-day window, so in a production environment this wants a drain or a
# maintenance window rather than an unannounced restart.
install -d -m 755 /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/livekit-sip.sh <<'HOOK'
#!/bin/bash
# Installed by livekit-sip start.sh. Runs as root, only after a successful renewal.
# $RENEWED_LINEAGE is set by certbot to /etc/letsencrypt/live/<domain>.
set -euo pipefail
[ -n "${RENEWED_LINEAGE:-}" ] || exit 0

install -d -m 700 /etc/livekit/tls
# -L dereferences: /etc/letsencrypt/live/* are symlinks into ../archive, which the
# container cannot follow through its read-only mount.
cp -L "$RENEWED_LINEAGE/fullchain.pem" /etc/livekit/tls/fullchain.pem
cp -L "$RENEWED_LINEAGE/privkey.pem"   /etc/livekit/tls/privkey.pem
chmod 644 /etc/livekit/tls/fullchain.pem
chmod 640 /etc/livekit/tls/privkey.pem

# Tolerate the container being absent: this hook also fires on first issuance,
# which happens before the container is started further down in start.sh.
if docker inspect livekit-sip >/dev/null 2>&1; then
  echo "livekit-sip: cert renewed, restarting to load it"
  docker restart livekit-sip
else
  echo "livekit-sip: cert renewed, container not present — start.sh will pick it up"
fi
HOOK
chmod 755 /etc/letsencrypt/renewal-hooks/deploy/livekit-sip.sh

certbot certonly --standalone --non-interactive --agree-tos \
  --register-unsafely-without-email \
  --keep-until-expiring \
  --deploy-hook /etc/letsencrypt/renewal-hooks/deploy/livekit-sip.sh \
  -d "${SIP_TLS_DOMAIN}"

# The timer is what actually drives renewal. The Debian package enables it, but a
# minimal image or a disabled unit would mean the cert never renews and nothing
# reports it — so assert it rather than assume.
systemctl enable --now certbot.timer 2>/dev/null || \
  echo "WARNING: could not enable certbot.timer — cert renewal will NOT be automatic"

# First-run copy: on initial issuance the hook has run, but on a redeploy where the
# cert is still valid certbot no-ops and the hook does not fire, so place the files
# unconditionally here too. Both paths are idempotent.
install -d -m 700 /etc/livekit/tls
cp -L "/etc/letsencrypt/live/${SIP_TLS_DOMAIN}/fullchain.pem" /etc/livekit/tls/fullchain.pem
cp -L "/etc/letsencrypt/live/${SIP_TLS_DOMAIN}/privkey.pem"   /etc/livekit/tls/privkey.pem
chmod 644 /etc/livekit/tls/fullchain.pem
chmod 640 /etc/livekit/tls/privkey.pem

export SIP_TLS_DOMAIN

envsubst '${LIVEKIT_SERVER_IP} ${LIVEKIT_API_KEY} ${LIVEKIT_API_SECRET} ${REDIS_HOST} ${REDIS_AUTH} ${SIP_TLS_DOMAIN}' << 'YAML_TEMPLATE' > /etc/livekit/sip.yaml
sip_port: 5060
rtp_port: 10000
rtp_port_end: 20000

# sip_hostname is only used when the transport is TLS — getContactURI() in
# livekit/sip keeps the raw signaling IP for udp/tcp and substitutes this name
# for TLS, so the Contact Twilio dials back becomes
#   <sip:…@${SIP_TLS_DOMAIN}:5061;transport=tls>
# and matches the cert. Both this and the tls block are required: with only the
# tls block the Contact carries an IP and cert validation fails; with only the
# hostname the port falls back to sip_port (5060).
sip_hostname: ${SIP_TLS_DOMAIN}
tls:
  port: 5061
  certs:
    - cert_file: /etc/livekit/tls/fullchain.pem
      key_file: /etc/livekit/tls/privkey.pem

api_key: ${LIVEKIT_API_KEY}
api_secret: ${LIVEKIT_API_SECRET}
ws_url: ws://${LIVEKIT_SERVER_IP}:7880
use_external_ip: true

redis:
  address: ${REDIS_HOST}:6379
  password: ${REDIS_AUTH}

logging:
  level: info
YAML_TEMPLATE

chmod 600 /etc/livekit/sip.yaml

docker stop livekit-sip 2>/dev/null || true
docker rm   livekit-sip 2>/dev/null || true

echo "Starting livekit-sip..."
docker run -d \
  --name livekit-sip \
  --restart unless-stopped \
  --log-driver gcplogs \
  --network host \
  -v "/etc/livekit/sip.yaml:/sip/config.yaml" \
  -v "/etc/livekit/tls:/etc/livekit/tls:ro" \
  livekit/sip:latest

echo "Done. livekit-sip status:"
docker ps --filter name=livekit-sip
