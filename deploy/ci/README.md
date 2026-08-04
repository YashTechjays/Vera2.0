# CD on Bitbucket Pipelines (dev → test)

This directory holds the runner-side scripts that turn a push to **`dev`** into a deploy of the
three app components (`control-plane`, `agent-worker`, `frontend`) to the GCP **test VM**. It is
the Bitbucket port of the GitHub `.github/workflows/dev.yml` pipeline. Auth is **keyless** —
Bitbucket OIDC exchanged for the deployer service account via Workload Identity Federation; no
JSON key is stored anywhere.

## Flow (defined in `bitbucket-pipelines.yml`, `pipelines.branches.dev`)

```
push to dev
  └─ 1. Full gate (parallel):  backend gate (ruff/mypy + migrate/seed + unit+integration on
     │                          Postgres/Redis)  ·  frontend CI  ·  gitleaks
  └─ 2. Build (parallel, path-filtered): control-plane · agent-worker · frontend
     │      each → Artifact Registry  <region>-docker.pkg.dev/<project>/<repo>/<c>:<sha> + :dev
     │      + Trivy scan
  └─ 3. Deploy (single `deployment: test` step): deploy every component whose :<sha> image was
            built this run, in order control-plane → agent-worker → frontend (last).
```

Why the **full** gate on `dev` (not the fast unit-only lane the other branches use): `dev`
auto-deploys, and a squash-merge can land a tree on `dev` that no PR tested as a whole (base
drift → logical conflict). The DB-backed integration run on the actual `dev` commit is where
that is caught before shipping.

## Scripts

| Script | Role (GitHub equivalent) |
|---|---|
| `bb-gcp-auth.sh` | OIDC → WIF → deployer-SA login (`google-github-actions/auth`) |
| `bb-build-push.sh <component> <context> <dockerfile> [build_args]` | build + push image + Trivy (`_build-image.yml`) |
| `bb-deploy-vm.sh <service> <tag_var> <run_migrations> <run_seed>` | scp assets + IAP-SSH `remote-deploy.sh` (`_deploy-vm.yml`) |
| `bb-deploy-changed.sh` | deploy every component that has a `:<sha>` image, in order (the `dev.yml` deploy DAG) |

The scripts reuse the existing, CI-agnostic deploy assets unchanged: `remote-deploy.sh`,
`render-env.sh`, `secrets.map`, `verify-app-env.sh`, `verify-config.sh`,
`verify-frontend-build-env.sh`, `env-manifest.json`, `docker-compose.dev.yml`.

## Manual deploy (the `workflow_dispatch` equivalent)

**Pipelines → Run pipeline → Branch `dev` → Custom: `deploy-dev-all`.** Builds all three
unconditionally at current `dev` HEAD and deploys them. Use it for the **first** deploy (before
any changeset exists) or a forced full redeploy.

## Configuration — Bitbucket repository variables

Set under **Repo settings → Repository variables** (none are secret — OIDC is keyless):

| Variable | Meaning | Example |
|---|---|---|
| `GCP_WIF_PROVIDER` | WIF provider resource, **bare** — no `//iam.googleapis.com/` scheme (`create-cred-config` adds it; including it doubles the audience and STS rejects the exchange) | `projects/…/providers/bitbucket` |
| `GCP_DEPLOYER_SA` | deployer SA email | `gh-deployer-dev@vera-123456.iam.gserviceaccount.com` |
| `GCP_PROJECT_ID` | test project id | `vera-123456` |
| `GCP_REGION` | region | `us-central1` |
| `AR_REPO` | Artifact Registry repo | `vera-test` |
| `SECRET_PREFIX` | Secret Manager prefix | `vera-test` |
| `VM_NAME` | test VM name | `vera-test-1` |
| `VM_ZONE` | test VM zone | `us-central1-a` |
| `VITE_API_BASE_URL` | frontend build arg | `/api/v1` |

Optional: `TRIVY_EXIT_CODE=1` on the build steps to make the image scan hard-fail (default `0` =
warn, matching the repo's staged scanner rollout).

## Prerequisites (not code — see the sibling handoff/Terraform)

1. **GCP (you, via Terraform):** add a Bitbucket OIDC provider to the existing WIF pool + bind the
   deployer SA to the `dev` branch. See `gcp-wif-bitbucket.tf.example`.
2. **Bitbucket admin:** enable Pipelines, create the **`test`** deployment environment, and set
   the repository variables above. See `bitbucket-admin-handoff.md` (CD section).

## Verify

- **Auth smoke:** temporarily add a step with `oidc: true` running
  `bash deploy/ci/bb-gcp-auth.sh && gcloud auth list && gcloud artifacts repositories list`.
- **Full path:** run `deploy-dev-all`, confirm images in AR
  (`gcloud artifacts docker images list <region>-docker.pkg.dev/<project>/<repo>/control-plane`),
  the deploy step goes green (health-gated `docker compose up --wait`), and the **Deployments**
  dashboard shows a `test` deployment. Then push to `dev` and confirm the auto pipeline runs and
  a frontend-only change deploys only the frontend.
- **Rollback:** re-run a previous green pipeline, or `deploy-dev-all` after checking out an
  earlier `dev` commit (per-SHA image tags are immutable in AR).

## UAT (CI only — build + push, no deploy)

The `uat` branch runs a **CI-only** pipeline (`pipelines.branches.uat`): the full DB-backed gate,
then build the **backend** (control-plane, agent-worker) as images → the **UAT project's** Artifact
Registry, and build the **frontend** as a static Vite bundle → **synced to a GCS bucket** (no image,
no VM). There is **no deploy step** — UAT CD is owned by the infra repo. Auth is the same keyless
OIDC → WIF path.

The frontend keeps `VITE_API_BASE_URL="/api/v1"` (relative). That works on GCS only if the bucket is
fronted by a **GCP HTTPS load balancer that routes `/api/*` to the control-plane** — i.e. the LB
replays what nginx does in the dev image, so the browser stays same-origin (no CORS on PHI APIs).

Because a repository variable holds a single value (already the dev/test project), UAT's GCP config
lives in a parallel **`UAT_`-prefixed** variable set, remapped inline in each uat step onto the
standard names the shared scripts expect (so the dev `GCP_*` variables are never touched):

| Variable | Meaning |
|---|---|
| `UAT_GCP_WIF_PROVIDER` | WIF provider resource for the UAT project — **bare**, no `//iam.googleapis.com/` (see `GCP_WIF_PROVIDER`) |
| `UAT_GCP_DEPLOYER_SA` | UAT builder SA email |
| `UAT_GCP_PROJECT_ID` | UAT project id |
| `UAT_GCP_REGION` | region |
| `UAT_AR_REPO` | UAT Artifact Registry repo (backend images) |
| `UAT_FRONTEND_BUCKET` | GCS bucket name for the static frontend |

The backend image steps set `MOVING_TAG=uat`, so the moving tag (`:uat`) never collides with dev's
`:dev`; `VITE_API_BASE_URL` is reused (relative `/api/v1`). Builds are unconditional (no changeset
filter) so every uat commit produces a complete image set + a fresh frontend.

**Prerequisite (infra repo / Terraform):** in the UAT project — a Bitbucket OIDC provider on a WIF
pool trusting this workspace/repo; a **dedicated** builder SA (not the Terraform-apply SA — least
privilege) that grants **`roles/iam.workloadIdentityUser`** to the app repo's principalSet
(`principalSet://…/workloadIdentityPools/<pool>/attribute.repository/{<app-repo-uuid>}`, so the `uat`
pipeline can impersonate it) and holds **`roles/artifactregistry.writer`** (backend images) **and
`roles/storage.objectAdmin`** on the frontend bucket (the GCS sync prunes stale objects, so it needs
delete); an Artifact Registry repo; and a GCS bucket fronted by an HTTPS LB that routes `/api/*` to
the control-plane. Feed the Terraform outputs into the `UAT_` variables.
