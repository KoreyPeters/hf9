output "cloud_run_url" {
  value = google_cloud_run_v2_service.app.uri
}

output "artifact_registry_url" {
  value = "${var.region}-docker.pkg.dev/${var.project}/${google_artifact_registry_repository.hf.repository_id}"
}

output "wif_provider" {
  value       = google_iam_workload_identity_pool_provider.github.name
  description = "WIF provider resource name — use in GitHub Actions workflows if added later"
}
