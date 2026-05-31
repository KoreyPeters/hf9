resource "google_artifact_registry_repository" "hf" {
  repository_id = "hf"
  location      = var.region
  format        = "DOCKER"

  depends_on = [google_project_service.apis]

  cleanup_policies {
    id     = "keep-10-tagged"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }

  cleanup_policies {
    id     = "delete-untagged"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "86400s"
    }
  }

  cleanup_policy_dry_run = false
}
