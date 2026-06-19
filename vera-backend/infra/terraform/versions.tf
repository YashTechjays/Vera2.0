# Terraform skeleton — structure only, no resources are created yet.
# TODO(vera-2.x): remote state in a GCS bucket with CMEK.

terraform {
  required_version = ">= 1.9"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}
