# Alerting on server errors.
#
# This is the second of two layers, and the two exist because neither is
# sufficient alone. Django mails a full traceback from inside the request, which
# is what you actually need to fix a bug — but it can only report errors it
# survives to report. A container that fails to boot, is OOM-killed, or is
# rejected by Cloud Run before Django ever sees the request sends nothing at
# all, and those are precisely the failures where nobody notices for a day.
#
# So this watches from outside, at the platform's own count of 5xx responses.
# It is deliberately dumb: no traceback, no detail, just "the site is returning
# errors, go and look". The email from Django, if one arrives, is the useful one.

locals {
  # Falls back to the budget list so a single address in tfvars covers both
  # without having to be repeated.
  alert_emails = length(var.alert_emails) > 0 ? var.alert_emails : var.budget_alert_emails
}

resource "google_monitoring_notification_channel" "errors" {
  for_each = toset(local.alert_emails)

  project      = var.project
  display_name = "Server errors: ${each.key}"
  type         = "email"

  labels = {
    email_address = each.key
  }

  depends_on = [google_project_service.apis]
}

# The third layer, and the one the other two structurally cannot provide.
#
# An OOM kill is a SIGKILL from the platform, so Django never runs and never
# mails. And the 5xx policy below infers container death from 5xx *responses* —
# which is only true when the container dies mid-request. Three OOM kills on
# 2026-07-26 produced no 5xx at all, because the container died between
# requests, so neither layer said anything and the only trace was a log line
# nobody was reading.
#
# So this watches the log line itself. Cloud Run emits it on every OOM kill
# regardless of what the process was doing at the time.
resource "google_logging_metric" "oom_kills" {
  project = var.project
  name    = "hf-app-oom-kills"
  filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=\"hf-app\"",
    "textPayload:\"Memory limit of\"",
  ])

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "oom_kill" {
  count = length(local.alert_emails) > 0 ? 1 : 0

  project      = var.project
  display_name = "hf-app killed for exceeding its memory limit"
  combiner     = "OR"

  documentation {
    content = trimspace(<<-EOT
      hf-app exceeded its memory limit and was killed.

      Nothing else will tell you: the process is SIGKILLed, so Django cannot
      mail a traceback, and an OOM between requests produces no 5xx for the
      response-code alert to catch.

      Check what was in flight. A receipt upload decoding a large image is the
      expected spike; a steady climb with no uploads is a leak. Raising the
      memory limit is in terraform/cloud_run.tf, but confirm which of the two it
      is first -- a leak will simply take longer to reach a higher ceiling.
    EOT
    )
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "memory limit exceeded"

    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND metric.type = \"logging.googleapis.com/user/${google_logging_metric.oom_kills.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  alert_strategy {
    auto_close = "1800s"
  }

  notification_channels = [
    for channel in google_monitoring_notification_channel.errors : channel.id
  ]
}

# The fourth layer, for a failure the other three are all blind to.
#
# A scheduled job that never reaches the application is invisible to every alert
# above. Django is never entered, so no traceback is mailed. The container is
# never even woken, so there is no OOM and no 5xx. Cloud Scheduler records the
# failure in its own log and nothing was reading it.
#
# Not hypothetical: hf-snapshot-product-ratings returned 404 every morning from
# 2026-08-09 to 2026-08-31 -- twenty-three consecutive failures, found by hand
# while looking for something else. See plans/scheduler-url-drift.md.
#
# Note that a 404 is not a 5xx, so the response-code policy below could not have
# caught it however it was tuned. This watches Cloud Scheduler's own verdict on
# whether the attempt succeeded, which is the only signal that exists when the
# request never lands.
resource "google_logging_metric" "scheduler_failures" {
  project = var.project
  name    = "hf-scheduler-failures"

  # Every failure mode in one filter, deliberately. Cloud Scheduler logs an
  # ERROR whatever the cause -- 404 from a renamed path, 503 from an instance
  # that would not start, a deadline exceeded on a slow task -- and all of them
  # mean the same thing operationally: work that was supposed to happen did not.
  filter = join(" AND ", [
    "resource.type=\"cloud_scheduler_job\"",
    "severity>=ERROR",
  ])

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

# Applying this from scratch fails the first time, and that is expected rather
# than a fault in the config:
#
#   Error 404: Cannot find metric(s) that match type =
#   "logging.googleapis.com/user/hf-scheduler-failures"
#
# A newly created log-based metric takes a few minutes to become visible to
# Monitoring, and Terraform's dependency graph only knows the metric resource
# exists, not that the metrics API has caught up. Wait five minutes and apply
# again. Left as a documented retry rather than a `time_sleep` resource, which
# would pull in another provider to paper over a once-per-lifetime delay.
resource "google_monitoring_alert_policy" "scheduler_failure" {
  count = length(local.alert_emails) > 0 ? 1 : 0

  project      = var.project
  display_name = "A scheduled job failed"
  combiner     = "OR"

  documentation {
    content = trimspace(<<-EOT
      A Cloud Scheduler job did not reach the application successfully.

      The incident names the job in `resource.label.job_id`. Read its verdict
      with:

          gcloud logging read \
            'resource.type=cloud_scheduler_job AND severity>=ERROR' \
            --limit=5 --format=json

      `jsonPayload.status` is the useful field. NOT_FOUND means the job's URI no
      longer matches a path in hf/task_urls.py -- check whether a rename shipped
      in the application without a matching `terraform apply`. UNAVAILABLE
      usually means the instance failed to start rather than anything wrong with
      the job itself.

      Do not assume one failed run is harmless. Several of these tasks record
      state that cannot be reconstructed later -- snapshot-ratings is the clearest
      case, since a rating computed over a rolling window is gone once the day
      passes.
    EOT
    )
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "scheduler job attempt failed"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"cloud_scheduler_job\"",
        "metric.type = \"logging.googleapis.com/user/${google_logging_metric.scheduler_failures.name}\"",
      ])

      comparison      = "COMPARISON_GT"
      threshold_value = 0
      # Zero for the same reason as the 5xx policy: a duration requires the
      # condition to stay true for that long, and a job that fires once a day
      # produces a single point. Anything above zero here would mean the alert
      # only fires for failures that repeat within the dwell time, which is
      # exactly the daily failure this was built for.
      duration = "0s"

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_SUM"

        # One incident per job rather than one for "the scheduler". Ten jobs
        # share this policy, and folding them together would let a second job
        # start failing silently while an incident for the first is still open.
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.label.job_id"]
      }

      trigger {
        count = 1
      }
    }
  }

  alert_strategy {
    auto_close = "1800s"
  }

  notification_channels = [
    for channel in google_monitoring_notification_channel.errors : channel.id
  ]
}

resource "google_monitoring_alert_policy" "http_5xx" {
  count = length(local.alert_emails) > 0 ? 1 : 0

  project      = var.project
  display_name = "hf-app returning 5xx"
  combiner     = "OR"

  documentation {
    content = trimspace(<<-EOT
      hf-app returned at least one 5xx response.

      Check for an email from Django with the traceback — that will name the
      view. If none arrived, the failure happened before or below Django
      (container boot, OOM, or the platform rejecting the request), so go
      straight to the Cloud Run revision logs.
    EOT
    )
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "5xx responses from hf-app"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"cloud_run_revision\"",
        "resource.label.service_name = \"hf-app\"",
        "metric.type = \"run.googleapis.com/request_count\"",
        "metric.label.response_code_class = \"5xx\"",
      ])

      comparison      = "COMPARISON_GT"
      threshold_value = 0
      # Zero, not 60s. The intent has always been that a single 500 is worth
      # knowing about on a site this size, but a duration requires the condition
      # to *stay* true for that long — which an isolated error never does, so
      # the policy quietly wanted sustained failure while its comment claimed
      # otherwise. The alignment period below is the window; this is the dwell
      # time, and for "tell me about any of them" it has to be zero.
      duration = "0s"

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  # Without this an incident stays open until acknowledged by hand, and the next
  # error is folded into it silently rather than sending a fresh alert.
  alert_strategy {
    auto_close = "1800s"
  }

  notification_channels = [
    for channel in google_monitoring_notification_channel.errors : channel.id
  ]
}
