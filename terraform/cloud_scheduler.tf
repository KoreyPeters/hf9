resource "google_cloud_scheduler_job" "check_deprecations" {
  name             = "hf-check-deprecations"
  project          = var.project
  region           = var.region
  schedule         = "0 * * * *"
  time_zone        = "UTC"
  attempt_deadline = "300s"

  depends_on = [google_project_service.apis]

  http_target {
    uri         = "https://humanflourish.ing/tasks/check-deprecations/"
    http_method = "POST"

    oidc_token {
      service_account_email = google_service_account.tasks.email
      audience              = "https://humanflourish.ing"
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
    uri         = "https://humanflourish.ing/tasks/check-deletions/"
    http_method = "POST"

    oidc_token {
      service_account_email = google_service_account.tasks.email
      audience              = "https://humanflourish.ing"
    }
  }
}

# Safety net for purchase anonymisation. Each purchase schedules its own
# Cloud Task at write time, but a dropped task would leave a player-linked
# basket alive past its retention window — a privacy failure, not a cosmetic
# one. This sweep catches any that were missed. The underlying task is
# idempotent, so overlapping with the per-purchase task is harmless.
resource "google_cloud_scheduler_job" "sweep_purchase_anonymisation" {
  name             = "hf-sweep-purchase-anonymisation"
  project          = var.project
  region           = var.region
  schedule         = "0 3 * * *"
  time_zone        = "UTC"
  attempt_deadline = "300s"

  depends_on = [google_project_service.apis]

  http_target {
    uri         = "https://humanflourish.ing/tasks/sweep-purchase-anonymisation/"
    http_method = "POST"

    oidc_token {
      service_account_email = google_service_account.tasks.email
      audience              = "https://humanflourish.ing"
    }
  }
}

# Daily rating snapshots. Ratings are computed over a rolling twelve-month
# window, so a past value cannot be reconstructed later — the responses behind it
# age out. Recording them as they happen is the only way to show a trend. A
# missed run is a gap in the line, not a correctness problem.
resource "google_cloud_scheduler_job" "snapshot_product_ratings" {
  name             = "hf-snapshot-product-ratings"
  project          = var.project
  region           = var.region
  schedule         = "0 5 * * *"
  time_zone        = "UTC"
  attempt_deadline = "600s"

  depends_on = [google_project_service.apis]

  http_target {
    uri         = "https://humanflourish.ing/tasks/snapshot-product-ratings/"
    http_method = "POST"

    oidc_token {
      service_account_email = google_service_account.tasks.email
      audience              = "https://humanflourish.ing"
    }
  }
}

# Retro-matching. Re-runs the matching cascade over line items already recorded,
# so every alias a player confirms improves receipts read months earlier. This is
# the compounding mechanism, and it is deliberately unhurried — nothing breaks if
# a run is missed, it just happens tomorrow instead.
resource "google_cloud_scheduler_job" "retro_match" {
  name             = "hf-retro-match"
  project          = var.project
  region           = var.region
  schedule         = "30 4 * * *"
  time_zone        = "UTC"
  attempt_deadline = "600s"

  depends_on = [google_project_service.apis]

  http_target {
    uri         = "https://humanflourish.ing/tasks/retro-match/"
    http_method = "POST"

    oidc_token {
      service_account_email = google_service_account.tasks.email
      audience              = "https://humanflourish.ing"
    }
  }
}

# Backstop for receipt image deletion. Images are deleted as soon as extraction
# finishes, so this should normally find nothing. Hourly rather than daily
# because the published commitment is a hard 24 hours: a daily sweep that ran
# just before a deletion was missed could leave an image alive for nearly 48.
resource "google_cloud_scheduler_job" "sweep_receipt_images" {
  name             = "hf-sweep-receipt-images"
  project          = var.project
  region           = var.region
  schedule         = "15 * * * *"
  time_zone        = "UTC"
  attempt_deadline = "300s"

  depends_on = [google_project_service.apis]

  http_target {
    uri         = "https://humanflourish.ing/tasks/sweep-receipt-images/"
    http_method = "POST"

    oidc_token {
      service_account_email = google_service_account.tasks.email
      audience              = "https://humanflourish.ing"
    }
  }
}
