# Connecting Vera to LiveKit Cloud

This guide covers switching the control-plane and agent worker from the
self-hosted `vera-livekit-vm` to LiveKit Cloud.

## What You Need

From the [LiveKit Cloud dashboard](https://cloud.livekit.io):
- **Project URL** — `wss://your-project.livekit.cloud`
- **API Key** — starts with `API`
- **API Secret**

---

## Step 1 — Add LiveKit Cloud Secrets to Secret Manager

Run these from your Mac once:

```bash
PROJECT=innate-watch-497101-k4

# Add the LiveKit Cloud URL (new secret)
echo -n "wss://your-project.livekit.cloud" | \
  gcloud secrets create vera-livekit-url \
    --data-file=- \
    --project=$PROJECT

# Update API key and secret with LiveKit Cloud credentials
# (replaces the self-hosted values)
echo -n "<your-cloud-api-key>" | \
  gcloud secrets versions add vera-livekit-api-key \
    --data-file=- \
    --project=$PROJECT

echo -n "<your-cloud-api-secret>" | \
  gcloud secrets versions add vera-livekit-api-secret \
    --data-file=- \
    --project=$PROJECT
```

---

## Step 2 — Update Start Scripts

### `start-control-plane.sh`

In the secrets fetch block, add:
```bash
LIVEKIT_URL=$(gcloud secrets versions access latest --secret=vera-livekit-url)
```

Change the `docker run` line from:
```bash
-e VERA_LIVEKIT_URL="ws://${LIVEKIT_INTERNAL_IP}:7880" \
```
To:
```bash
-e VERA_LIVEKIT_URL="$LIVEKIT_URL" \
```

### `start-worker.sh`

In the secrets fetch block, add:
```bash
LIVEKIT_URL=$(gcloud secrets versions access latest --secret=vera-livekit-url)
```

Change the `docker run` line from:
```bash
-e LIVEKIT_URL="ws://${LIVEKIT_INTERNAL_IP}:7880" \
```
To:
```bash
-e LIVEKIT_URL="$LIVEKIT_URL" \
```

---

## Step 3 — Redeploy

```bash
cd deployment
./deploy.sh --control-plane --worker --skip-build
```

`--skip-build` is fine here — no code changed, only secrets and config.

---

## Step 4 — Verify

Check the control-plane picked up the LiveKit Cloud URL:
```bash
gcloud compute ssh vera-control-plane-vm --tunnel-through-iap \
  --zone=us-central1-a --project=innate-watch-497101-k4 -- \
  "sudo docker inspect vera-control-plane | grep VERA_LIVEKIT_URL"
```

Check the worker is connecting:
```bash
gcloud compute ssh vera-worker-vm --tunnel-through-iap \
  --zone=us-central1-a --project=innate-watch-497101-k4 -- \
  "sudo docker logs vera-worker --tail=30"
```

A healthy worker log shows:
```
Connected to LiveKit server
Registered worker ...
```

---

## Notes

- The self-hosted `vera-livekit-vm` and `vera-sip-vm` are **not affected** by this change —
  they still run independently. If you no longer need them, stop them manually or deprovision
  via Terraform.
- LiveKit Cloud uses `wss://` (TLS). The self-hosted setup used `ws://` (plain). Make sure
  the URL you paste starts with `wss://`.
- The `vera-livekit-api-key` and `vera-livekit-api-secret` secrets are now shared between
  the control-plane, worker, livekit-server, and livekit-sip. If you switch to LiveKit Cloud
  for control-plane/worker only, the self-hosted LiveKit services will also pick up the new
  credentials on their next restart — keep that in mind.
