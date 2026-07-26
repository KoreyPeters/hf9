resource "google_cloud_run_v2_service" "app" {
  name                = "hf-app"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  depends_on = [google_project_service.apis]

  template {
    service_account = google_service_account.app.email

    scaling {
      max_instance_count = 1
      min_instance_count = 0
    }

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

      # The database Litestream restores and replicates. Unset, Django falls
      # back to BASE_DIR/db.sqlite3 — a path inside the image layer, pristine in
      # every new container — while Litestream went on faithfully replicating
      # /data/db.sqlite3, which nothing ever wrote to. Every cold start silently
      # discarded the whole database and rebuilt an empty one, and the replica
      # bucket stayed empty because there was never anything at the path it
      # watched. Plain config rather than a secret: it is a path, and keeping it
      # visible here is what makes the pairing with litestream.yml checkable.
      env {
        name  = "DB_PATH"
        value = "/data/db.sqlite3"
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
        # Litestream replicates from a background process and flushes a final
        # time on shutdown. Throttling the CPU between requests starves both, so
        # the WAL would reach GCS late or not at all — the failure this whole
        # setup exists to prevent. This is what makes scaling to zero safe:
        # the instance keeps its CPU for as long as it is alive, including the
        # shutdown grace period where the last sync happens.
        cpu_idle = false
      }

      startup_probe {
        tcp_socket {
          port = 8080
        }
        initial_delay_seconds = 30
        period_seconds        = 5
        failure_threshold     = 10
      }
    }
  }

  lifecycle {
    ignore_changes = [
      # The image is owned by CI, not by Terraform. cloudbuild.yaml deploys an
      # immutable :$SHORT_SHA tag; var.app_image defaults to :latest. Without
      # this, the two fight — every deploy sets a SHA, every apply reverts it,
      # and whichever ran last decides what is in production. Worse, :latest is
      # mutable, so a later apply could silently ship a different build than the
      # one someone thought they were keeping.
      template[0].containers[0].image,

      # Deploy residue from the same `gcloud run services update`. It stamps the
      # tool's name and version onto the resource and writes an all-zero
      # service-level scaling block; Terraform sets none of them, so it plans to
      # strip all three. Applying that is a treadmill — the next deploy puts them
      # straight back — and none of it is behaviour: the zeros are already the
      # defaults, and this is not template scaling, where max_instance_count = 1
      # holds SQLite to a single writer. That stays under Terraform's control.
      client,
      client_version,
      scaling,
    ]
  }

}

resource "google_cloud_run_v2_job" "migrate" {
  name                = "hf-migrate"
  location            = var.region
  deletion_protection = false

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

  lifecycle {
    ignore_changes = [
      # Same split of ownership as the service above: CI deploys the SHA tag,
      # Terraform owns the rest of the job.
      template[0].template[0].containers[0].image,

      # And the same residue, from `gcloud run jobs update` in the migrate step.
      client,
      client_version,
    ]
  }

}

resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  project  = var.project
  location = var.region
  name     = google_cloud_run_v2_service.app.name
  role     = "roles/run.invoker"
  member   = "allUsers"
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
