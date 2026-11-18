# Pub/Sub -> Cloud Function (gen2) that turns each escalation into an ALERT log entry.

data "archive_file" "escalation" {
  type        = "zip"
  source_dir  = "${path.module}/../functions/escalation"
  output_path = "${path.module}/function.zip"
}

resource "google_storage_bucket" "function_source" {
  name                        = "${var.project_id}-function-source"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  depends_on                  = [google_project_service.apis]
}

resource "google_storage_bucket_object" "escalation" {
  name   = "escalation-${data.archive_file.escalation.output_md5}.zip"
  bucket = google_storage_bucket.function_source.name
  source = data.archive_file.escalation.output_path
}

resource "google_service_account" "function" {
  account_id   = "escalation-notifier"
  display_name = "Escalation notifier (function runtime and trigger)"
}

resource "google_service_account" "function_build" {
  account_id   = "escalation-build"
  display_name = "Escalation notifier (Cloud Build)"
}

resource "google_project_iam_member" "function_build" {
  project = var.project_id
  role    = "roles/cloudbuild.builds.builder"
  member  = google_service_account.function_build.member
}

resource "google_cloudfunctions2_function" "escalation" {
  name     = "escalation-notifier"
  location = var.region

  build_config {
    runtime         = "python313"
    entry_point     = "notify"
    service_account = google_service_account.function_build.id
    source {
      storage_source {
        bucket = google_storage_bucket.function_source.name
        object = google_storage_bucket_object.escalation.name
      }
    }
  }

  service_config {
    max_instance_count    = 1
    available_memory      = "256M"
    service_account_email = google_service_account.function.email
  }

  event_trigger {
    trigger_region        = var.region
    event_type            = "google.cloud.pubsub.topic.v1.messagePublished"
    pubsub_topic          = google_pubsub_topic.escalations.id
    retry_policy          = "RETRY_POLICY_RETRY"
    service_account_email = google_service_account.function.email
  }

  depends_on = [google_project_iam_member.function_build]
}

resource "google_cloud_run_v2_service_iam_member" "function_invoker" {
  name     = google_cloudfunctions2_function.escalation.name
  location = var.region
  role     = "roles/run.invoker"
  member   = google_service_account.function.member
}
