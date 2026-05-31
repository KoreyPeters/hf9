resource "google_cloud_run_v2_service" "app" {
  name     = "hf-app"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"

  depends_on = [google_project_service.apis]

  template {
    max_instance_count = 1
    min_instance_count = 0
    service_account    = google_service_account.app.email

    volumes {
      name = "data"
      empty_dir {
        medium     = "MEMORY"
        size_limit = "256Mi"
      }
    }

    containers {
      image = var.app_image

      ports {
        container_port = 8080
      }

      volume_mounts {
        name       = "data"
        mount_path = "/data"
      }

      env {
        name  = "DJANGO_SETTINGS_MODULE"
        value = "hf.settings.prod"
      }

      dynamic "env" {
        for_each = local.secret_ids
        content {
          name = env.value
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle = true
      }

      startup_probe {
        http_get {
          path = "/"
        }
        initial_delay_seconds = 30
        period_seconds        = 5
        failure_threshold     = 10
      }
    }
  }
}

resource "google_cloud_run_v2_job" "migrate" {
  name     = "hf-migrate"
  location = var.region

  depends_on = [google_project_service.apis]

  template {
    task_count = 1

    template {
      max_retries     = 1
      timeout         = "600s"
      service_account = google_service_account.app.email

      volumes {
        name = "data"
        empty_dir {
          medium     = "MEMORY"
          size_limit = "256Mi"
        }
      }

      containers {
        image   = var.app_image
        command = ["/app/migrate.sh"]

        volume_mounts {
          name       = "data"
          mount_path = "/data"
        }

        env {
          name  = "DJANGO_SETTINGS_MODULE"
          value = "hf.settings.prod"
        }

        dynamic "env" {
          for_each = local.secret_ids
          content {
            name = env.value
            value_source {
              secret_key_ref {
                secret  = env.value
                version = "latest"
              }
            }
          }
        }
      }
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "app_invoker" {
  project  = var.project
  location = var.region
  name     = google_cloud_run_v2_service.app.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.app.email}"
}

resource "google_cloud_run_v2_service_iam_member" "tasks_invoker" {
  project  = var.project
  location = var.region
  name     = google_cloud_run_v2_service.app.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.tasks.email}"
}

resource "google_cloud_run_v2_service_iam_member" "cloudbuild_invoker" {
  project  = var.project
  location = var.region
  name     = google_cloud_run_v2_service.app.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.cloudbuild.email}"
}
