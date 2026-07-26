variable "project" {
  type    = string
  default = "human-flourishing-4"
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "app_image" {
  type    = string
  default = "us-central1-docker.pkg.dev/human-flourishing-4/hf/hf-app:latest"
}

# ── Budget alerting ───────────────────────────────────────────────────────────
# Budgets live on the billing account, not the project, so whoever runs
# terraform needs roles/billing.costsManager on the billing account itself.
# Project-level permissions are not enough and terraform cannot grant this to
# itself. Leave billing_account empty to skip budgets entirely.

variable "billing_account" {
  type        = string
  description = "Billing account id, e.g. 01ABCD-234567-89EFGH. Find with: gcloud billing accounts list. Empty disables all budget alerts."
  default     = ""
}

variable "budget_alert_emails" {
  type        = list(string)
  description = "Addresses to notify. Billing account admins are always notified as well, so an empty list still alerts someone."
  default     = []
}

variable "monthly_budget" {
  type        = number
  description = "Whole-project monthly budget, in the billing account's own currency."
  default     = 50
}

variable "ai_monthly_budget" {
  type        = number
  description = "Monthly budget for Vertex AI alone. Receipt extraction is the only per-use cost that scales with player activity, so it is worth watching separately from the flat Cloud Run spend."
  default     = 20
}

variable "vertex_billing_service" {
  type        = string
  description = "Billing service id for Vertex AI, e.g. services/XXXX-XXXX-XXXX. Find with: gcloud billing services list --filter=\"displayName~'Vertex'\". Empty skips the AI-specific budget and relies on the project-wide one."
  default     = ""
}

variable "alert_emails" {
  type        = list(string)
  description = "Addresses notified when hf-app returns 5xx. Defaults to budget_alert_emails so one address in tfvars covers both."
  default     = []
}
