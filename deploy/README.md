# Vera CI/CD — dev environment

Build + deploy of the three components to the **single VM** on every push to the
`dev` branch. (The `dev` branch targets the GCP **`test`** environment — its
secrets, AR repo, and VM are the `vera-test` resources; the app still runs with
`VERA_ENV=dev`.) Keyless throughout: **Workload Identity Federation** (no SA JSON
key), **OS Login over an IAP tunnel** (no public SSH, no stored SSH keys), and the
VM's **attached service account** pulls from Artifact Registry and reads Secret
Manager (no registry password, no secrets on the box at rest except a rendered
0600 env file).

```
push to dev ─▶ <component>-dev.yml
                 ├─ ci     → _ci-*.yml          → lint / typecheck / test
                 ├─ verify →                       env preflight, fail-closed (before build)
                 ├─ build  → _build-image.yml  → push <sha> + dev to Artifact Registry
                 └─ deploy → _deploy-vm.yml     → scp compose+scripts, ssh over IAP,
                                                   render env from Secret Manager,
                                                   verify app env → pull →
                                                   (control-plane) migrate-if-pending →
                                                   up -d --wait (health-gated)
```

The load balancer does **path-based routing**: `/api/*` → control-plane `:8000`,
`/*` → frontend `:80`. The browser sees one origin, so `VITE_API_BASE_URL=/api/v1`
with no CORS.

| Component | Trigger workflow | Build context | Dockerfile |
|---|---|---|---|
| frontend | `frontend-dev.yml` | `vera-frontend` | `vera-frontend/Dockerfile` (nginx SPA) |
| control-plane | `control-plane-dev.yml` | `vera-backend` | `vera-backend/docker/control_plane.Dockerfile` |
| agent-worker | `worker-dev.yml` | `vera-backend` | `vera-backend/docker/agent_worker.Dockerfile` |

Reusable building blocks: `_ci-backend.yml`, `_ci-frontend.yml`, `_build-image.yml`,
`_deploy-vm.yml`. Shipped to the VM each deploy (a subset of the files in this directory):
`docker-compose.dev.yml`, `remote-deploy.sh`, `render-env.sh`, `secrets.map`, `verify-app-env.sh`.
The env benchmark `env-manifest.json` and the CI-only checks (`verify-config.sh`,
`verify-frontend-build-env.sh`) stay in CI — they're never shipped to the VM.

---

## Guardrails (what keeps a bad deploy from going live)

- **One deploy at a time.** All deploys share a single concurrency group (`deploy-vm-<env>`,
  `cancel-in-progress: false`) so two pushes never mutate the VM at once. `ci`/`build` use
  per-component groups with `cancel-in-progress: true`, so a burst of pushes collapses to the
  latest without stacking runs or wasting minutes. A running **deploy** is never cancelled.
- **Env verification (two-phase, fail-closed).** The required env per component is the single
  source of truth in **`env-manifest.json`** (benchmarked from the proven manual deploy).
  - *Before build (CI):* `verify-config.sh` asserts every backend-required var is supplied by
    `secrets.map` (+ render-env built-ins); `verify-frontend-build-env.sh` asserts the frontend
    build vars are non-empty. A missing/unwired var → **red, build never starts.**
  - *On the VM (after render):* `verify-app-env.sh` asserts every required var is present &
    non-empty in the rendered `app.env` → **red before the container starts.** The VM never parses
    JSON — CI resolves the list (jq) and passes it down.
- **Migrations run backward-safe.** Control-plane deploys migrate only when the DB is **behind
  head** (`alembic current` check on the app role, no downtime if already current). When a
  migration is pending, the old container is **stopped first** so the previous code never serves
  against the new schema, and `alembic upgrade head` runs on a **privileged** connection (see the
  `migration-database-url` secret in §1) because migrations seed NULL-tenant platform rows under
  FORCE RLS the app role can't write.
- **Health-gated restart.** The new container starts with `docker compose up -d --wait`; if it
  isn't healthy within the timeout the deploy goes **red** (and dumps logs) — no green-but-down.
- **Disk hygiene.** Each deploy prunes unused images older than 7 days (old `:<sha>` tags), so the
  single VM's disk doesn't fill.

---

## 1. Required GCP resources (provisioned by Terraform)

Infra is managed with Terraform outside this repo — the pipeline only *assumes* the
following exists. This is the contract, not a runbook; no `gcloud` steps here.

**Workload Identity Federation (keyless GitHub → GCP)**
- A WIF **pool** + **OIDC provider** with issuer `https://token.actions.githubusercontent.com`.
- Attribute mapping `google.subject=assertion.sub`, `attribute.repository=assertion.repository`.
- Attribute condition locking to this repo: `assertion.repository == 'YashTechjays/Vera2.0'`.

**Deployer service account** (the identity the workflow impersonates)
- Project roles: `roles/artifactregistry.writer` (push images), `roles/iap.tunnelResourceAccessor`
  (SSH via IAP), `roles/compute.osLogin`, `roles/compute.viewer`.
- A `roles/iam.workloadIdentityUser` binding for the repo's WIF principalSet
  (`principalSet://…/attribute.repository/YashTechjays/Vera2.0`) so the pool can impersonate it.

**The dev/test VM**
- **OS Login** enabled (`enable-oslogin=TRUE`) — SSH identity via IAM, no static keys.
- A **firewall rule** allowing only the IAP range `35.235.240.0/20` to `tcp:22`; **no**
  `0.0.0.0/0` SSH rule.
- **Docker engine + compose plugin + `gcloud`** installed (startup script / image).
- `/opt/vera` owned by the OS-Login deploy user, and that user in the `docker` group
  (so the pipeline can write/run without sudo).

**The VM's attached service account** (the app's ADC + secret access)
- `roles/artifactregistry.reader` (pull images), `roles/secretmanager.secretAccessor`
  (so `render-env.sh` reads the `vera-test-*` secrets), `roles/cloudkms.cryptoKeyEncrypterDecrypter`
  (MFA envelope encryption), `roles/aiplatform.user` (Vertex AI).

**Artifact Registry** — a Docker repo (the `vera-test` repo; already exists).

**Secret Manager** — the `vera-test-*` entries (already exist), consumed via `secrets.map` and
rendered into `app.env`. **Plus one secret that is deliberately NOT in `secrets.map`:**
`vera-test-migration-database-url` — a **postgres-superuser (`BYPASSRLS`)** connection string used
only by the migration one-off (`alembic upgrade head`), because migrations seed NULL-tenant
platform rows under FORCE RLS that the app role cannot write. It must never reach `app.env` / the
running container, so `remote-deploy.sh` fetches it directly at deploy time. Create it once, and
ensure that role has `BYPASSRLS` on the Cloud SQL instance (`ALTER ROLE … BYPASSRLS`). If it's
missing, control-plane migrations fail **red** (fail-closed) before the container is touched.

### Database (Cloud SQL) — one-time setup

Run as the **`postgres`** admin on the **`vera_db`** database (Cloud SQL Studio). All SQL is
idempotent — safe on an existing DB. Migrations build everything else (tables, RLS,
`vera_definer_owner`, functions, seed); you only do the steps below. `vera_user` = the app role in
`<SECRET_PREFIX>-database-url`.

1. **Create the DB** — `vera_db` (skip if it exists).

2. **Admin can bypass RLS:**
   ```sql
   ALTER ROLE postgres BYPASSRLS;
   ```

3. **App user + grants** (must NOT be superuser/BYPASSRLS, so RLS applies):
   ```sql
   -- create only on a brand-new DB (skip if vera_user already exists):
   CREATE ROLE vera_user LOGIN PASSWORD '<strong-password>';

   -- always safe to run — access to current AND future migration objects:
   GRANT USAGE ON SCHEMA public TO vera_user;
   GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA public TO vera_user;
   GRANT USAGE, SELECT               ON ALL SEQUENCES IN SCHEMA public TO vera_user;
   GRANT EXECUTE                     ON ALL FUNCTIONS IN SCHEMA public TO vera_user;
   ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
     GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES    TO vera_user;
   ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
     GRANT USAGE, SELECT               ON SEQUENCES TO vera_user;
   ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
     GRANT EXECUTE                     ON FUNCTIONS TO vera_user;
   ```

4. **Extensions** — none to do; migrations run `CREATE EXTENSION IF NOT EXISTS` for `vector`,
   `pg_trgm`, `pgcrypto`. Only pre-enable them in the console if your instance blocks `CREATE EXTENSION`.

5. **Two secrets** (both → `vera_db`; format `postgresql+asyncpg://<user>:<pass>@<host>:5432/vera_db`):
   - `<SECRET_PREFIX>-database-url` → user **`vera_user`** (running app; RLS-bound)
   - `<SECRET_PREFIX>-migration-database-url` → user **`postgres`** (migrations only; BYPASSRLS)

Then the first control-plane deploy runs `alembic upgrade head` and builds the rest.

No hand-placed files or shell exports are needed on the VM: the compose file, `remote-deploy.sh`,
`render-env.sh`, and `secrets.map` are shipped by the pipeline each run, and the container env
(`/opt/vera/app.env`) is **rendered from Secret Manager** on every deploy.

---

## 2. GitHub repository configuration

Auth is **keyless** — no SA JSON keys or SSH keys anywhere. All values are stored as
**repo-level** Actions secrets/variables (Settings → Secrets and variables → Actions), and are
**prefixed by branch** (`DEV_*` for the `dev` pipeline). The branch-specific caller workflow
(`*-dev.yml`) reads its `DEV_*` values and passes them into the shared reusables.

> **Why repo-level + branch-prefixed:** GitHub **Environments** (with their own secrets/variables
> and deployment-branch rules) require a paid plan on **private** repos. On Free, a repo-level name
> holds one value — so to keep per-branch config in one repo we prefix names with the branch. See
> **Adding another branch** below, and the **branch-gate note** for the security model.

### A. Repo Secrets (→ **Secrets**)

| Secret | Example / notes |
|---|---|
| `DEV_WIF_PROVIDER` | the WIF provider's **full resource name** (see below) |
| `DEV_DEPLOYER_SA` | `gh-deployer-dev@vera-123456.iam.gserviceaccount.com` — the SA the workflow impersonates via WIF. |

**Getting `DEV_WIF_PROVIDER` right** (a wrong value gives `auth failed … invalid value for "audience" … should be the full resource name`):

- **Format** — the provider's *full resource name*, nothing else:
  ```
  projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/<POOL_ID>/providers/<PROVIDER_ID>
  ```
- **Dummy example:**
  ```
  projects/123456789012/locations/global/workloadIdentityPools/github-pool/providers/github
  ```
- **Fetch the exact value** (copy its output verbatim):
  ```bash
  gcloud iam workload-identity-pools providers describe <PROVIDER_ID> \
    --location=global --workload-identity-pool=<POOL_ID> --project=<PROJECT_ID> \
    --format='value(name)'
  ```
- **Gotchas** (each produces the "invalid audience" error):
  - use the project **NUMBER**, not the project ID;
  - **no** `https://` or `//iam.googleapis.com/` prefix;
  - **no** surrounding quotes, **no** trailing space/newline;
  - it must be a **Secret** (not a Variable), named exactly `DEV_WIF_PROVIDER`.

### B. Repo Variables (→ **Variables**)

| Variable | Example / notes |
|---|---|
| `DEV_GCP_PROJECT_ID` | `vera-123456` |
| `DEV_VITE_API_BASE_URL` | `/api/v1` (same-origin, relative; LB path-routes `/api/*`). **Must be non-empty** — the frontend `verify-env` job fails the build otherwise. |
| `DEV_GCP_REGION` | `us-central1` |
| `DEV_AR_REPO` | `vera-test` (the Artifact Registry repo) |
| `DEV_SECRET_PREFIX` | `vera-test` (Secret Manager names are `<prefix>-<basename>`) |
| `DEV_VM_NAME` | `vera-test-1` |
| `DEV_VM_ZONE` | `us-central1-a` |

### Adding another branch (e.g. `main`/`staging`)

Because config is branch-prefixed and passed into the reusables, a new branch is self-contained:
1. Create a `<BRANCH>_*` set of the same **2 secrets + 7 variables** at repo level.
2. Add `*-<branch>.yml` caller pipelines (copy the `*-dev.yml` ones) that trigger on that branch and
   pass the `<BRANCH>_*` values into `_build-image.yml` / `_deploy-vm.yml`.
3. Bind a WIF principal for that branch's ref in GCP (see the branch-gate note).
The shared reusables (`_build-image.yml`, `_deploy-vm.yml`) need no change.

### Branch-gate note — enforced in GCP (already configured)

A GitHub Environment would have restricted deploys to the `dev` branch. Without it, repo secrets are
visible to any workflow run in the repo (our deploy workflows only *trigger* on `push: [dev]`, but
that's a trigger, not an access control). The branch → identity lock therefore lives in **GCP** and
is **already configured**: the WIF binding/condition scopes `DEPLOYER_SA` to `refs/heads/dev`, so a
run on any other branch cannot obtain deploy credentials even though it can read the secret's *name*.
**No additional action needed** — it's the intended control, just enforced in GCP rather than by a
GitHub Environment.

> **Prod later:** either upgrade the plan to use Environments (with Required-reviewers as the manual
> gate) or run prod from a separate repo/branch with its own repo-level `DEPLOYER_SA` bound to that
> ref in GCP; add `workflow_dispatch` for a manual trigger.

---

## 3. Verify (end to end)

1. **Auth smoke:** re-run any workflow; the `Authenticate to Google Cloud` step
   should succeed with no JSON key present.
2. **Env preflight (fail-closed):** the `verify` job runs before `build`. Temporarily
   drop a mapped var from `secrets.map` (or unset the frontend `VITE_API_BASE_URL`
   variable) → the pipeline goes **red before building**, naming the missing var. Restore.
3. **Build isolation:** push a trivial change under one component's path; only
   that component's pipeline runs. Confirm `…/<component>:<sha>` and `:dev` appear
   in Artifact Registry.
4. **Deploy:** the `deploy` job connects via `--tunnel-through-iap` (VM has no
   public SSH). On the VM, `docker compose ps` shows the service updated, and
   `/opt/vera/app.env` exists (0600) with the rendered secrets. `verify-app-env.sh`
   passed (all required vars present) before the container started.
5. **Migrations:** with a pending migration, the control-plane deploy logs
   "Pending migration; stopping control-plane…", runs `alembic upgrade head` on the
   **privileged** connection (from `migration-database-url`), and succeeds; with no
   pending migration it logs "DB already at head" and skips (no downtime). `app.env`
   still holds the **app-role** URL — the superuser URL is never persisted.
6. **Health gate:** a healthy deploy is green; point a service at a broken image → the
   deploy goes **red** at `up -d --wait` with the container logs dumped.
7. **Serving:** `curl http://<dev-lb>/healthz` → `{"status":"ok"}` (LB routes it to
   the control-plane); the frontend loads and its `/api/v1/...` calls succeed via
   the LB; the worker log shows LiveKit registration (`agent_name="vera-agent"`).

---

## 4. Later: `main` / prod (4 VMs, manual)

Reuse `_build-image.yml` + `_deploy-vm.yml`. Add `on: workflow_dispatch` (with an
image-SHA input) and an `environment: production` gate (Required reviewers on
GitHub Team/Enterprise; otherwise dispatch-only is the manual control), then fan
the deploy across the 4 VMs per the chosen component split. Add a `AR_REPO_PROD`
variable and prod `DEV_VM_*` equivalents when that lands.
