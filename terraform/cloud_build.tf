resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github-pool"
  display_name              = "GitHub"
  project                   = var.project

  depends_on = [google_project_service.apis]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-provider"
  project                            = var.project

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }

  attribute_condition = "assertion.repository == 'KoreyPeters/hf9'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "wif_cloudbuild" {
  service_account_id = google_service_account.cloudbuild.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/KoreyPeters/hf9"
}

resource "google_cloudbuild_trigger" "main_push" {
  name            = "hf-main-push"
  location        = "global"
  project         = var.project
  service_account = google_service_account.cloudbuild.id
  filename        = "cloudbuild.yaml"

  depends_on = [google_project_service.apis]

  github {
    owner = "KoreyPeters"
    name  = "hf9"
    push {
      branch = "^main$"
    }
  }

  substitutions = {
    _IMAGE  = "us-central1-docker.pkg.dev/${var.project}/hf/hf-app"
    _REGION = var.region
  }
}
