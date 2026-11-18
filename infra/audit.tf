# Audit trail: every case event (filed, escalated, status_changed) lands in BigQuery through a Pub/Sub BigQuery
# subscription, with no code in between. Compliance can query who changed what, and when.
data "google_project" "this" {}

resource "google_bigquery_dataset" "audit" {
  dataset_id                 = "northwind_audit"
  location                   = "US"
  delete_contents_on_destroy = true
  depends_on                 = [google_project_service.apis]
}

resource "google_bigquery_table" "case_events" {
  dataset_id          = google_bigquery_dataset.audit.dataset_id
  table_id            = "case_events"
  deletion_protection = false
  time_partitioning { type = "DAY" }
  schema = jsonencode([
    { name = "subscription_name", type = "STRING" },
    { name = "message_id", type = "STRING" },
    { name = "publish_time", type = "TIMESTAMP" },
    { name = "data", type = "STRING" },
    { name = "attributes", type = "STRING" },
  ])
}

resource "google_bigquery_dataset_iam_member" "pubsub_writes_audit" {
  dataset_id = google_bigquery_dataset.audit.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_subscription" "audit" {
  name  = "case-events-to-bigquery"
  topic = google_pubsub_topic.events.id
  bigquery_config {
    table          = "${google_bigquery_table.case_events.project}.${google_bigquery_table.case_events.dataset_id}.${google_bigquery_table.case_events.table_id}"
    write_metadata = true
  }
  depends_on = [google_bigquery_dataset_iam_member.pubsub_writes_audit]
}
