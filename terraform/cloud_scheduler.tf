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

# Reads receipts still waiting. Covers two cases with one sweep: uploads that
# waited out an emergency stop, and uploads whose original task was dropped.
# While the stop is on it does nothing, since processing returns early, so it is
# safe to leave running throughout an incident.
#
# Was every fifteen minutes, and that was costing about $45 a month. The service
# runs with `cpu_idle = false`, so Cloud Run bills CPU for an instance's whole
# lifetime rather than only while it is serving; an idle instance lingers for
# roughly fifteen minutes. A job on a fifteen-minute cron therefore kept the
# container alive permanently — measured over three days, every single hour had
# requests, with only about ten cold starts a day. Effectively a keepalive with
# a bill attached.
#
# Hourly, and deliberately at :05 so it shares one wake window with
# `check_deprecations` at :00 and `sweep_receipt_images` at :10. Three jobs in
# one fifteen-minute window costs one instance lifetime; three spread across the
# hour costs three.
#
# What the delay costs: a receipt whose Cloud Task was genuinely dropped now
# waits up to an hour rather than fifteen minutes. That case is already rare —
# Cloud Tasks retries five times before anything reaches this sweep — and the
# other case, a receipt waiting out an emergency stop, was never going to be
# resolved in fifteen minutes anyway.
resource "google_cloud_scheduler_job" "sweep_pending_receipts" {
  name             = "hf-sweep-pending-receipts"
  project          = var.project
  region           = var.region
  schedule         = "5 * * * *"
  time_zone        = "UTC"
  attempt_deadline = "900s"

  depends_on = [google_project_service.apis]

  http_target {
    uri         = "https://humanflourish.ing/tasks/sweep-pending-receipts/"
    http_method = "POST"

    oidc_token {
      service_account_email = google_service_account.tasks.email
      audience              = "https://humanflourish.ing"
    }
  }
}

# Convergence metrics. Recorded daily rather than derived on demand, because
# the claim they exist to test is that the system improves without curation —
# and a rate computed once says nothing about whether it is moving.
resource "google_cloud_scheduler_job" "snapshot_metrics" {
  name             = "hf-snapshot-metrics"
  project          = var.project
  region           = var.region
  schedule         = "30 5 * * *"
  time_zone        = "UTC"
  attempt_deadline = "600s"

  depends_on = [google_project_service.apis]

  http_target {
    uri         = "https://humanflourish.ing/tasks/snapshot-metrics/"
    http_method = "POST"

    oidc_token {
      service_account_email = google_service_account.tasks.email
      audience              = "https://humanflourish.ing"
    }
  }
}

# Which products are worth interrupting players about. Runs after the rating
# snapshot, since one of the signals is a rating having moved sharply and that
# comparison needs the day's snapshot to exist. Manual admin flags survive it.
resource "google_cloud_scheduler_job" "recompute_hotness" {
  name             = "hf-recompute-hotness"
  project          = var.project
  region           = var.region
  schedule         = "0 6 * * *"
  time_zone        = "UTC"
  attempt_deadline = "600s"

  depends_on = [google_project_service.apis]

  http_target {
    uri         = "https://humanflourish.ing/tasks/recompute-hotness/"
    http_method = "POST"

    oidc_token {
      service_account_email = google_service_account.tasks.email
      audience              = "https://humanflourish.ing"
    }
  }
}

# Action centre emails. Weekly, not daily — the task itself enforces at most one
# per player per week, but running it daily would mean a player who became
# eligible on a Tuesday waited a day for no reason while adding six pointless
# sweeps. Routine items never qualify for an email at all.
resource "google_cloud_scheduler_job" "action_centre_emails" {
  name             = "hf-action-centre-emails"
  project          = var.project
  region           = var.region
  schedule         = "0 15 * * 2"
  time_zone        = "UTC"
  attempt_deadline = "1800s"

  depends_on = [google_project_service.apis]

  http_target {
    uri         = "https://humanflourish.ing/tasks/send-action-centre-emails/"
    http_method = "POST"

    oidc_token {
      service_account_email = google_service_account.tasks.email
      audience              = "https://humanflourish.ing"
    }
  }
}

# Daily rating snapshots, for products and stores alike. Ratings are computed
# over a rolling twelve-month window, so a past value cannot be reconstructed
# later — the responses behind it age out. Recording them as they happen is the
# only way to show a trend. A missed run is a gap in the line, not a correctness
# problem.
#
# One job covering both subjects rather than two: same cadence, same reason, and
# two schedules would be two things to keep in step.
resource "google_cloud_scheduler_job" "snapshot_ratings" {
  name             = "hf-snapshot-ratings"
  project          = var.project
  region           = var.region
  schedule         = "0 5 * * *"
  time_zone        = "UTC"
  attempt_deadline = "600s"

  depends_on = [google_project_service.apis]

  http_target {
    uri         = "https://humanflourish.ing/tasks/snapshot-ratings/"
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
#
# Moved from :15 to :10 so it lands in the same wake window as the other two
# hourly jobs — see the note on `sweep_pending_receipts` for why that is worth
# money. Nothing about the 24-hour commitment depends on which minute it runs.
resource "google_cloud_scheduler_job" "sweep_receipt_images" {
  name             = "hf-sweep-receipt-images"
  project          = var.project
  region           = var.region
  schedule         = "10 * * * *"
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
