# Email the compliance team when a high-urgency complaint is filed, at most once per 5 minutes (optional: set alert_email).

variable "alert_email" {
  type    = string
  default = ""
}

resource "google_monitoring_notification_channel" "compliance" {
  count        = var.alert_email == "" ? 0 : 1
  display_name = "Compliance team"
  type         = "email"
  labels       = { email_address = var.alert_email }
  depends_on   = [google_project_service.apis]
}

resource "google_monitoring_alert_policy" "escalation" {
  count                 = var.alert_email == "" ? 0 : 1
  display_name          = "High-urgency complaint filed"
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.compliance[0].id]

  conditions {
    display_name = "Escalation ALERT log entry"
    condition_matched_log {
      filter = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"escalation-notifier\" AND severity=ALERT"
    }
  }

  alert_strategy {
    notification_rate_limit { period = "300s" }
    auto_close = "1800s" # each escalation is its own event; don't leave incidents open for days
  }
}
