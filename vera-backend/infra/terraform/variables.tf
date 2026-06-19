variable "project_id" {
  description = "GCP project hosting all Vera resources"
  type        = string
}

variable "region" {
  description = "Primary region (HIPAA: keep data + compute co-resident)"
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "dev | staging | prod"
  type        = string
}
