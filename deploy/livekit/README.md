# Self-hosted LiveKit — test environment operator deploy

Starts the self-hosted LiveKit stack (server + SIP + Egress) on the dedicated test
VMs that Terraform provisions (`infra/terraform-test/livekit.tf`). LiveKit is **not**
part of the app CI/CD — you run these scripts by hand.

**Public `wss://` endpoint.** The server VM runs Caddy, which provisions a Let's Encrypt
cert for `test.livekit.veratechsolutions.ai` and terminates TLS on `:443`, so browser
participants can join. The agent-worker (in-VPC), outbound phone calls (Twilio SIP), and
call recording (Egress) reach the server directly on the private IP at `ws://…:7880`.

## Prerequisites (on your laptop)

- `gcloud` installed and logged in: `gcloud auth login`
- IAM on your account: IAP tunnel access + SSH to the VMs + Secret Manager access.
- The Terraform in `infra/terraform-test/` has been applied (VMs, dedicated LiveKit
  Redis, secrets, firewall, recordings bucket exist).

The scripts SSH into each VM over IAP and run the start script **as root there**; each
start script fetches its own secrets from Secret Manager. Nothing sensitive lands on
your laptop.

## Config

Defaults target test; override via env if needed:

| Var | Default |
|-----|---------|
| `PROJECT_ID` | `innate-watch-497101-k4` |
| `ZONE` | `us-east1-b` |
| `LIVEKIT_VM` | `vera-test-livekit-vm` |
| `SIP_VM` | `vera-test-sip-vm` |
| `EGRESS_VM` | `vera-test-egress-vm` |
| `SECRET_PREFIX` | `vera-test` (used inside the start scripts) |

## Order of operations

```bash
# 1. Terraform-generated secrets already exist (api-key/-secret/-url + redis auth).
#    Populate the test-owned Twilio secrets with dev's SAME values (outbound-only):
gcloud secrets versions add vera-test-twilio-account-sid              --data-file=- <<< "..."
gcloud secrets versions add vera-test-twilio-auth-token              --data-file=- <<< "..."
gcloud secrets versions add vera-test-livekit-sip-trunk-address      --data-file=- <<< "...pstn.twilio.com"
gcloud secrets versions add vera-test-livekit-sip-trunk-number       --data-file=- <<< "+1..."
gcloud secrets versions add vera-test-livekit-sip-trunk-auth-username --data-file=- <<< "..."
gcloud secrets versions add vera-test-livekit-sip-trunk-auth-password --data-file=- <<< "..."

# 2. Start the server first.
./deploy-livekit.sh --server

# 3. Register test's outbound trunk (prints ST_…) and store it.
#    provision-trunk.sh creates the trunk with transport=TLS + media_encryption=REQUIRE
#    to match Twilio Secure Trunking (see "Twilio Secure Trunking" below).
./deploy-livekit.sh --provision-trunk
gcloud secrets versions add vera-test-livekit-sip-trunk-id --data-file=- <<< "ST_..."
#    Then add to deploy/secrets.map:  livekit-sip-trunk-id = VERA_LIVEKIT_SIP_TRUNK_ID

# 4. Start SIP + Egress.
./deploy-livekit.sh --sip --egress          # or --all for server+sip+egress

# 5. Redeploy the app (push to dev) so control-plane/worker pick up
#    ws://<livekit-internal-ip>:7880 from Secret Manager.

# Optional PSTN smoke test:
./deploy-livekit.sh --test-outbound +15551234567
```

## Updating / re-running

Safe to re-run any flag — the start scripts stop the old container and start fresh.
To change server/SIP/egress config, edit the matching `start.sh` and re-run its flag.

## Twilio Secure Trunking (TLS + SRTP) — required for outbound

The Twilio Elastic SIP Trunk has **Secure Trunking** enabled (TLS signaling on `:5061`
+ SRTP media). The LiveKit **outbound trunk** must therefore be created with
`transport = SIP_TRANSPORT_TLS` and `media_encryption = SIP_MEDIA_ENCRYPT_REQUIRE`
(`provision-trunk.sh` does this). If the trunk is left on the default `AUTO`/`DISABLE`,
LiveKit sends the INVITE over plain UDP, Twilio silently drops it, and the call fails
with `SIP invite failed … upstream-no-response`.

Fix an existing wrong trunk in place (keeps the same `ST_…` id — no secret/app change):
```bash
gcloud compute ssh vera-test-livekit-vm --tunnel-through-iap --zone us-east1-b
export LIVEKIT_URL=ws://localhost:7880
export LIVEKIT_API_KEY=$(gcloud secrets versions access latest --secret=vera-test-livekit-api-key)
export LIVEKIT_API_SECRET=$(gcloud secrets versions access latest --secret=vera-test-livekit-api-secret)
lk sip outbound update --id ST_xxxx --transport SIP_TRANSPORT_TLS --media-enc SIP_MEDIA_ENCRYPT_REQUIRE
lk sip outbound list          # verify Transport=TLS, Encryption=REQUIRE
```

## Egress notes

- **Config is passed via the `EGRESS_CONFIG_BODY` env var**, not a mounted file: the
  `livekit/egress` image runs as a **non-root** user (it sandboxes headless Chrome) and
  cannot read a root-owned `0600` config file — a file mount crash-loops with
  `open /etc/livekit/egress.yaml: permission denied`.
- **CPU sizing:** Room Composite egress (Chrome-based, even for audio-only calls) needs
  **~4 vCPU**. On `e2-standard-2` (2 vCPU) egress starts and reaches `service ready`, but
  logs `not enough cpu for some egress types` and will **reject Room Composite jobs**. Set
  `egress_machine_type = "e2-standard-4"` and `terraform apply` if recordings use Room
  Composite. Lightweight audio **track** egress is fine on 2 vCPU.
- Recording only happens when the **control-plane calls the Egress API** for a room; the
  bucket (`vera-test-recordings-<project>`) + egress SA `storage.objectAdmin` are already
  provisioned by `recordings.tf`.

## Troubleshooting

- **Outbound call fails, `upstream-no-response`** → trunk not on TLS/SRTP; see "Twilio
  Secure Trunking" above. Check `sudo docker logs livekit-sip` on `vera-test-sip-vm`.
- **`livekit-egress` keeps restarting, `permission denied`** → old file-mount config; re-run
  `./deploy-livekit.sh --egress` (current script uses `EGRESS_CONFIG_BODY`).
- **Recording job rejected / no egress available** → egress CPU too low; upsize to
  `e2-standard-4` (see "Egress notes").

## Browser wss:// endpoint

The server VM has a static external IP, Caddy provisions TLS for
`test.livekit.veratechsolutions.ai`, the public WebRTC ports are open, and the LiveKit
URL secret is split (public `wss://` → control-plane `VERA_LIVEKIT_URL`, internal `ws://`
→ worker `LIVEKIT_URL`). Point the domain's A record at the reserved
`livekit_static_ip` **before** `./deploy-livekit.sh --server`, or Caddy's Let's Encrypt
challenge (HTTP-01 on :80) fails.
