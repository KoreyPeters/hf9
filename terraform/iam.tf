data "google_project" "current" {
  project_id = var.project
}

resource "google_service_account" "app" {
  account_id   = "hf-app"
  display_name = "HF App Runtime"
}

resource "google_service_account" "tasks" {
  account_id   = "hf-tasks"
  display_name = "HF Task Invoker"
}

resource "google_service_account" "cloudbuild" {
  account_id   = "hf-cloudbuild"
  display_name = "HF Cloud Build"
}

resource "google_project_iam_member" "app_secret_accessor" {
  project = var.project
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.app.email}"
}

resource "google_project_iam_member" "app_tasks_enqueuer" {
  project = var.project
  role    = "roles/cloudtasks.enqueuer"
  member  = "serviceAccount:${google_service_account.app.email}"
}

resource "google_project_iam_member" "cloudbuild_ar_writer" {
  project = var.project
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${google_service_account.cloudbuild.email}"
}

resource "google_project_iam_member" "cloudbuild_run_developer" {
  project = var.project
  role    = "roles/run.developer"
  member  = "serviceAccount:${google_service_account.cloudbuild.email}"
}

resource "google_project_iam_member" "cloudbuild_log_writer" {
  project = var.project
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.cloudbuild.email}"
}

resource "google_service_account_iam_member" "cloudbuild_actas_app" {
  service_account_id = google_service_account.app.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.cloudbuild.email}"
}

resource "google_service_account_iam_member" "cloudbuild_agent_token_creator" {
  service_account_id = google_service_account.cloudbuild.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-cloudbuild.iam.gserviceaccount.com"
}
