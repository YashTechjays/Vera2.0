# Vera 2.0 infrastructure — SKELETON. Modules are placeholders; nothing is
# provisioned from this configuration yet.
#
# Target shape (all CMEK-encrypted via the kms module):
#   network      VPC, private services access, serverless/NAT egress
#   kms          keyring + per-service crypto keys (CMEK everywhere)
#   cloudsql     Postgres 17 + pgvector, private IP, app + migration DB roles
#                (app role WITHOUT bypassrls — RLS must bind it; see ADR/RLS tests)
#   memorystore  Redis (permission cache, short-TTL state)
#   secrets      Secret Manager entries (LiveKit keys, Langfuse keys, DB creds)
#   gke          Autopilot cluster, workload identity for both deployments:
#                control_plane (FastAPI) and agent_worker (GCP service
#                principal; no human RBAC)
#
# TODO(vera-2.x): fill the modules; add langfuse self-hosting (ADR-0003),
# artifact registry, and the deploy pipeline.

module "network" {
  source = "./modules/network"
}

module "kms" {
  source = "./modules/kms"
}

module "cloudsql" {
  source = "./modules/cloudsql"
}

module "memorystore" {
  source = "./modules/memorystore"
}

module "secrets" {
  source = "./modules/secrets"
}

module "gke" {
  source = "./modules/gke"
}
