# IBV sheet → backend upload — local dev setup

Quick steps to upload an IBV form from the Google Sheet to your local backend.

## 1. Backend up
```bash
just up && just migrate && just seed   # Postgres/Redis + IBV schema + sample tenant/admin
```

## 2. Set the KMS key (one-time, in `.env`)
The app won't start without it. Generate once and paste into `.env` line `LOCAL_KMS_MASTER_KEY=`:
```bash
python -c "import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

## 3. Run the API
```bash
just api          # serves on http://127.0.0.1:8000  (loads .env)
```

## 4. Expose it to the internet (pinggy)
The Sheet runs in Google's cloud, so it needs a public URL. In a separate terminal:
```bash
ssh -p 443 -R0:localhost:8000 a.pinggy.io
```
Copy the hostname it prints, e.g. `rnxyz-1-2-3-4.a.free.pinggy.io` (no `https://`, no trailing slash).

## 5. Get an `intake:write` API key
As the seeded admin, `POST /api/v1/api-keys` with `{"name":"sheet","scope":"intake:write"}` and an
`Idempotency-Key` header — copy the one-time `token` (`vk_…`). It stays valid across restarts.

## 6. Get the form IDs (these change after a re-seed / `just check`)
```sql
SELECT fs.id AS form_type_id, sv.id AS schema_version_id
FROM form_schema fs
JOIN schema_version sv ON sv.schema_id = fs.id
WHERE fs.insurance_type = 'infertility_treatment' AND sv.status = 'published';
```
Run via: `docker compose exec postgres psql -U vera -d vera -c "<query>"`

## 7. Configure the Apps Script
**Project Settings → Script properties** (and set sheet cell **BB6 = `LOCAL`**):
- `SC_LOCAL_HOST` = the pinggy hostname from step 4
- `EXTERNAL_LOCAL_API_KEY` = the `vk_…` token from step 5

**In `sendDataToExternalSystem`**, set the body + endpoint:
```javascript
const finalpayload = {
  "form_type_id":      "<form_type_id from step 6>",
  "schema_version_id": "<schema_version_id from step 6>",
  "intake_payload":    dataToSend
};
const API_ENDPOINT = `https://${HOST}/api/v1/patient-forms`;
```

## 8. Upload
Fill the required cells, run the upload menu item → expect **✅ 200**. Verify:
```bash
docker compose exec postgres psql -U vera -d vera -c \
"select id, status, patient_name from patient_form order by created_at desc limit 1;"
```

## Gotchas
- **`unknown schema version` (404):** the IDs from step 6 are stale — `just check` wipes/re-seeds the
  schema. Re-run step 6 and update the Sheet.
- **pinggy URL changes** on each restart (free tier) → re-paste `SC_LOCAL_HOST`.
- The `vk_…` key is **machine auth** for intake only; the dispute/display endpoints use a logged-in
  user session instead.
