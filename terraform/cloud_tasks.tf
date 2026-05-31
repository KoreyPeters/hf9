resource "google_cloud_tasks_queue" "main" {
  name     = "hf-tasks"
  location = var.region
  project  = var.project

  depends_on = [google_project_service.apis]

  rate_limits {
    max_concurrent_dispatches = 10
    max_dispatches_per_second = 100
  }

  retry_config {
    max_attempts  = 5
    min_backoff   = "1s"
    max_backoff   = "300s"
    max_doublings = 5
  }
}
