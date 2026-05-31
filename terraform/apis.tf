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
  ])
}

resource "google_project_service" "apis" {
  for_each           = local.apis
  project            = var.project
  service            = each.key
  disable_on_destroy = false
}
