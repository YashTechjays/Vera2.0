# frontend — Deploy to GCS

React 19 SPA — `vera-frontend/`

## Requirements

- Node.js 22+
- npm
- `gsutil` (Google Cloud SDK)
- Authenticated to GCP (`gcloud auth login`)

## Step 1 — Build

`VITE_API_BASE_URL` is the only variable needed. It must be set before building — it gets baked into the JS bundle at compile time and cannot be changed after.

```bash
cd vera-frontend
npm ci
VITE_API_BASE_URL=https://your-backend-url/api/v1 npm run build
```

Output lands in `vera-frontend/dist/`.

## Step 2 — Upload to GCS

Two separate syncs are needed because JS/CSS assets and HTML files require different cache headers.

```bash
# JS and CSS — cache forever (Vite content-hashes the filenames, so they're safe to cache long-term)
gsutil -m rsync -r -c \
  -x ".*\.html$" \
  -h "Cache-Control:public, max-age=31536000, immutable" \
  dist/assets/ gs://YOUR_BUCKET/assets/

# HTML — never cache (index.html must always be fresh so the browser gets the latest asset manifest)
gsutil -m rsync -r -c \
  -x ".*assets/.*" \
  -h "Cache-Control:no-cache, no-store" \
  dist/ gs://YOUR_BUCKET/
```

Replace `YOUR_BUCKET` with your bucket name.

## Step 3 — Invalidate CDN Cache (if using Cloud CDN)

JS/CSS filenames change on every build (content-hashed), so no CDN invalidation needed for them. Only HTML needs to be invalidated:

```bash
gcloud compute url-maps invalidate-cdn-cache YOUR_URL_MAP \
  --path "/*.html" \
  --path "/" \
  --global \
  --async
```

Replace `YOUR_URL_MAP` with your Cloud CDN URL map name. Skip this step if not using Cloud CDN.

## Redeploying

Same steps — run build again with the same (or updated) `VITE_API_BASE_URL`, then re-sync. The `gsutil rsync -c` flag only uploads files that have changed.
