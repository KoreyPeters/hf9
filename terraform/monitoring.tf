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
      # The shortest window the metric supports. A single 500 is worth knowing
      # about on a site this size; the throttle that matters is on the Django
      # side, where the volume actually is.
      duration = "60s"

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
