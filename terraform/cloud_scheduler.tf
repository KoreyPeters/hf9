resource "google_cloud_scheduler_job" "check_deprecations" {
  name             = "hf-check-deprecations"
  project          = var.project
  region           = var.region
  schedule         = "0 * * * *"
  time_zone        = "UTC"
  attempt_deadline = "300s"

  depends_on = [google_project_service.apis]

  http_target {
    uri         = "${google_cloud_run_v2_service.app.uri}/tasks/check-deprecations/"
    http_method = "POST"

    oidc_token {
      service_account_email = google_service_account.tasks.email
      audience              = google_cloud_run_v2_service.app.uri
    }
  }
}

resource "google_cloud_scheduler_job" "check_deletions" {
  name             = "hf-check-deletions"
  project          = var.project
  region           = var.region
  schedule         = "0 2 * * *"
  time_zone        = "UTC"
  attempt_deadline = "300s"

  depends_on = [google_project_service.apis]

  http_target {
    uri         = "${google_cloud_run_v2_service.app.uri}/tasks/check-deletions/"
    http_method = "POST"

    oidc_token {
      service_account_email = google_service_account.tasks.email
      audience              = google_cloud_run_v2_service.app.uri
    }
  }
}
