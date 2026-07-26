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
