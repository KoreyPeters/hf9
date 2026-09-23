# Every job here carries the same `retry_config`, added 2026-09-01.
#
# It was missing entirely, which meant `retry_count` defaulted to zero: one
# non-2xx response and the run was simply abandoned. That is survivable for the
# hourly jobs, which get another go in an hour, and not survivable for a weekly
# one.
#
# It bit on 2026-09-01. `hf-action-centre-emails` fires `0 15 * * 2` — once a
# week. It fired into a cold start that never finished booting, took a 503 at
# 15:01:58, and that was the whole of it. No retry, no alert on the missing work
# itself, and the next attempt is seven days later. See
# plans/startup-hang-and-503s.md.
#
# Retrying is safe for all ten. The sweeps say so in their own docstrings, the
# snapshots are keyed `update_or_create` on today's date, hotness and retro-match
# are recomputations, and `send_action_centre_emails` records that it sent
# *before* sending precisely so a retry cannot double-mail.
#
# The 30s floor is chosen against the observed failure: a cold start that fails
# takes about 85 seconds to do so. A 5-second backoff would just hit the same
# instance still trying to boot. 30s doubling to a 300s ceiling gives it room.
resource "google_cloud_scheduler_job" "check_deprecations" {
  name    = "hf-check-deprecations"
  project = var.project
  region  = var.region

  # :15, not :00. Five other jobs run at minute zero — check_deletions (02:00),
  # sweep_purchase_anonymisation (03:00), snapshot_ratings (05:00),
  # recompute_hotness (06:00) and action_centre_emails (Tue 15:00) — and with
  # `maxScale = 1` the second job to arrive at a cold instance has nowhere to
  # queue and takes a 429.
  #
  # This job was the one that always lost, because it is the only one present at
  # all five collision points: five of the six 429s in the 30 days to
  # 2026-09-22 were this job. Moving it alone closes every one of them.
  #
  # :15 keeps it inside the same hourly wake window as sweep_pending_receipts
  # (:05) and sweep_receipt_images (:10) — a 10-minute span, exactly what :00,
  # :05, :10 spanned before — so the clustering below still holds. Do not move
  # this to a minute already in use without re-reading that reasoning.
  schedule         = "15 * * * *"
  time_zone        = "UTC"
  attempt_deadline = "300s"

  depends_on = [google_project_service.apis]

  # Retries, because without them a single transient failure silently drops the
  # work. See the note at the top of this file.
  retry_config {
    retry_count          = 3
    min_backoff_duration = "30s"
    max_backoff_duration = "300s"
  }

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

  # Retries, because without them a single transient failure silently drops the
  # work. See the note at the top of this file.
  retry_config {
    retry_count          = 3
    min_backoff_duration = "30s"
    max_backoff_duration = "300s"
  }

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

  # Retries, because without them a single transient failure silently drops the
  # work. See the note at the top of this file.
  retry_config {
    retry_count          = 3
    min_backoff_duration = "30s"
    max_backoff_duration = "300s"
  }

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
# Was every fifteen minutes, and that was costing about $45 a month. At the time
# the service ran with `cpu_idle = false`, so Cloud Run billed CPU for an
# instance's whole lifetime rather than only while it was serving; an idle
# instance lingers for roughly fifteen minutes. A job on a fifteen-minute cron
# therefore kept the container alive permanently — measured over three days,
# every single hour had requests, with only about ten cold starts a day.
# Effectively a keepalive with a bill attached.
#
# **That is history, not current configuration.** `cpu_idle = true` since
# 2026-08-08 (terraform/cloud_run.tf), so idle time is no longer billed and the
# "one instance lifetime" arithmetic above no longer applies. This comment
# claimed otherwise until 2026-09-23.
#
# Hourly, and deliberately at :05 so it shares one wake window with
# `sweep_receipt_images` at :10 and `check_deprecations` at :15. **The clustering
# is still right, for a different reason:** what costs money now is the cold
# start itself — startup CPU, plus a LIST per Litestream generation during
# restore — so three jobs sharing one wake window still costs one cold start
# where three spread across the hour would cost three.
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

  # Retries, because without them a single transient failure silently drops the
  # work. See the note at the top of this file.
  retry_config {
    retry_count          = 3
    min_backoff_duration = "30s"
    max_backoff_duration = "300s"
  }

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

  # Retries, because without them a single transient failure silently drops the
  # work. See the note at the top of this file.
  retry_config {
    retry_count          = 3
    min_backoff_duration = "30s"
    max_backoff_duration = "300s"
  }

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

  # Retries, because without them a single transient failure silently drops the
  # work. See the note at the top of this file.
  retry_config {
    retry_count          = 3
    min_backoff_duration = "30s"
    max_backoff_duration = "300s"
  }

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

  # Retries, because without them a single transient failure silently drops the
  # work. See the note at the top of this file.
  retry_config {
    retry_count          = 3
    min_backoff_duration = "30s"
    max_backoff_duration = "300s"
  }

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

  # Retries, because without them a single transient failure silently drops the
  # work. See the note at the top of this file.
  retry_config {
    retry_count          = 3
    min_backoff_duration = "30s"
    max_backoff_duration = "300s"
  }

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

  # Retries, because without them a single transient failure silently drops the
  # work. See the note at the top of this file.
  retry_config {
    retry_count          = 3
    min_backoff_duration = "30s"
    max_backoff_duration = "300s"
  }

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

  # Retries, because without them a single transient failure silently drops the
  # work. See the note at the top of this file.
  retry_config {
    retry_count          = 3
    min_backoff_duration = "30s"
    max_backoff_duration = "300s"
  }

  http_target {
    uri         = "https://humanflourish.ing/tasks/sweep-receipt-images/"
    http_method = "POST"

    oidc_token {
      service_account_email = google_service_account.tasks.email
      audience              = "https://humanflourish.ing"
    }
  }
}

# Keeps the Litestream replica to a bounded number of generations.
#
# One generation is created per container start and nothing else removes them:
# `retention-check-interval` in litestream.yml is deliberately longer than a
# container lives, so the in-process check never fires. Left alone they reached
# 758 by 2026-09-22 and put 40 seconds into the front of every cold start,
# because `litestream restore` inspects every generation to find the newest.
#
# Daily is enough: at ~25-33 new generations a day against a keep count of 50,
# a missed run costs a slightly slower boot and nothing else. See
# plans/cron-collision-and-boot-regression.md and core/replica.py.
#
# 20:40 UTC, chosen to sit away from everything else — well clear of the hourly
# cluster at :05/:10/:15 and of the daily jobs, which run between 02:00 and
# 06:00. A prune racing a restore would not corrupt anything (it never touches
# the newest generations) but it would make both slower.
resource "google_cloud_scheduler_job" "prune_generations" {
  name             = "hf-prune-generations"
  project          = var.project
  region           = var.region
  schedule         = "40 20 * * *"
  time_zone        = "UTC"
  attempt_deadline = "600s"

  depends_on = [google_project_service.apis]

  # Retries, because without them a single transient failure silently drops the
  # work. See the note at the top of this file.
  retry_config {
    retry_count          = 3
    min_backoff_duration = "30s"
    max_backoff_duration = "300s"
  }

  http_target {
    uri         = "https://humanflourish.ing/tasks/prune-generations/"
    http_method = "POST"

    oidc_token {
      service_account_email = google_service_account.tasks.email
      audience              = "https://humanflourish.ing"
    }
  }
}
