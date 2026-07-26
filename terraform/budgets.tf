# Budget alerting.
#
# Receipt extraction is the first thing HF does that costs money per use rather
# than per month. Cloud Run and storage scale with a bill you can predict; a
# model call scales with player activity, and with anything that goes wrong in
# a loop. These budgets are the backstop for that.
#
# They alert. They do not cap — GCP budgets cannot stop spending, and nothing
# here will halt a runaway on its own. The in-app control is
# MatchConfig.adjudication_candidates, which set to 0 disables Tier 2 without a
# deploy.
#
# Everything below is skipped when var.billing_account is empty, so the rest of
# the configuration still applies for anyone without billing-account access.

locals {
  budgets_enabled   = var.billing_account != ""
  ai_budget_enabled = local.budgets_enabled && var.vertex_billing_service != ""
}

resource "google_monitoring_notification_channel" "budget_email" {
  for_each = local.budgets_enabled ? toset(var.budget_alert_emails) : toset([])

  project      = var.project
  display_name = "Budget alerts: ${each.key}"
  type         = "email"

  labels = {
    email_address = each.key
  }

  depends_on = [google_project_service.apis]
}

# Whole-project spend. The safety net: catches anything unexpected, including
# costs from services that do not exist yet.
resource "google_billing_budget" "project" {
  count = local.budgets_enabled ? 1 : 0

  billing_account = var.billing_account
  display_name    = "HF — all services"

  budget_filter {
    projects               = ["projects/${data.google_project.current.number}"]
    calendar_period        = "MONTH"
    credit_types_treatment = "INCLUDE_ALL_CREDITS"
  }

  amount {
    specified_amount {
      # currency_code is deliberately omitted so the budget inherits the
      # billing account's currency. Setting it to something the account does
      # not use is rejected at apply time.
      units = tostring(var.monthly_budget)
    }
  }

  # Actual spend at 50% and 90% is the routine signal. The forecast rule is the
  # one that matters: it fires when the month is *projected* to overrun, which
  # is days earlier than actual spend crossing the line — and days earlier is
  # the difference between noticing a runaway and paying for it.
  threshold_rules {
    threshold_percent = 0.5
    spend_basis       = "CURRENT_SPEND"
  }
  threshold_rules {
    threshold_percent = 0.9
    spend_basis       = "CURRENT_SPEND"
  }
  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "CURRENT_SPEND"
  }
  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "FORECASTED_SPEND"
  }

  all_updates_rule {
    monitoring_notification_channels = [
      for channel in google_monitoring_notification_channel.budget_email : channel.id
    ]
    # Left false on purpose: billing account admins are notified as well as the
    # channels above. A misconfigured channel then degrades to a quieter alert
    # rather than a silent one.
    disable_default_iam_recipients = false
  }

  depends_on = [google_project_service.apis]
}

# Vertex AI alone. Separated because it is the only cost that tracks player
# activity, so a spike here means something specific — a surge in uploads, a
# retry loop, or adjudication firing far more than expected — whereas a spike
# in the project-wide figure could be anything.
resource "google_billing_budget" "vertex_ai" {
  count = local.ai_budget_enabled ? 1 : 0

  billing_account = var.billing_account
  display_name    = "HF — Vertex AI"

  budget_filter {
    projects               = ["projects/${data.google_project.current.number}"]
    services               = [var.vertex_billing_service]
    calendar_period        = "MONTH"
    credit_types_treatment = "INCLUDE_ALL_CREDITS"
  }

  amount {
    specified_amount {
      units = tostring(var.ai_monthly_budget)
    }
  }

  # Tighter than the project budget. Model spend should be a small, steady
  # fraction of the bill, so the interesting question is whether it is growing
  # faster than usage — which shows up early against a low ceiling.
  threshold_rules {
    threshold_percent = 0.5
    spend_basis       = "CURRENT_SPEND"
  }
  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "CURRENT_SPEND"
  }
  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "FORECASTED_SPEND"
  }

  all_updates_rule {
    monitoring_notification_channels = [
      for channel in google_monitoring_notification_channel.budget_email : channel.id
    ]
    disable_default_iam_recipients = false
  }

  depends_on = [google_project_service.apis]
}
