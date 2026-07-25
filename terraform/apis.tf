locals {
  apis = toset([
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudtasks.googleapis.com",
    "cloudscheduler.googleapis.com",
    "storage.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "compute.googleapis.com",
    # Receipt extraction and Tier 2 adjudication call Gemini on Vertex AI.
    "aiplatform.googleapis.com",
    # Budget alerts and the notification channels they fire through.
    "billingbudgets.googleapis.com",
    "monitoring.googleapis.com",
  ])
}

resource "google_project_service" "apis" {
  for_each           = local.apis
  project            = var.project
  service            = each.key
  disable_on_destroy = false
}
