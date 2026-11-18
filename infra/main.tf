terraform {
  required_version = ">= 1.6"
  required_providers {
    google  = { source = "hashicorp/google", version = "~> 8.12" }
    archive = { source = "hashicorp/archive", version = "~> 2.8" }
  }
}

variable "project_id" { type = string }
variable "region" {
  type    = string
  default = "us-central1"
}

provider "google" {
  project = var.project_id
  region  = var.region
  # Bill API quota to this project when applying with user credentials (Dialogflow, Eventarc require it).
  user_project_override = true
  billing_project       = var.project_id
}

resource "google_project_service" "apis" {
  for_each = toset([
    "aiplatform", "apikeys", "artifactregistry", "bigquery", "cloudbuild", "cloudfunctions", "cloudresourcemanager", "iam", "dialogflow", "eventarc",
    "firestore", "iamcredentials", "identitytoolkit", "logging", "monitoring", "pubsub", "run", "serviceusage", "storage", "sts",
  ])
  service            = "${each.key}.googleapis.com"
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "images" {
  repository_id = "complaint-agent"
  location      = var.region
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]
}

# Cases and the case-event stream: the GCP side of the runtime switch.
resource "google_firestore_database" "ops" {
  name                    = "northwind-ops"
  location_id             = var.region
  type                    = "FIRESTORE_NATIVE"
  delete_protection_state = "DELETE_PROTECTION_DISABLED"
  deletion_policy         = "DELETE"
  depends_on              = [google_project_service.apis]
}

resource "google_pubsub_topic" "events" {
  name       = "case-events"
  depends_on = [google_project_service.apis]
}

# Runtime identity: only what the app calls, scoped to the resource where possible.
resource "google_service_account" "app" {
  account_id   = "complaint-agent"
  display_name = "Complaint agent (Cloud Run runtime)"
}

resource "google_project_iam_member" "app" {
  for_each = toset([
    "roles/aiplatform.user", "roles/bigquery.jobUser", "roles/datastore.user", "roles/serviceusage.serviceUsageViewer",
  ])
  project = var.project_id
  role    = each.key
  member  = google_service_account.app.member
}

resource "google_pubsub_topic_iam_member" "app_publish" {
  topic  = google_pubsub_topic.events.id
  role   = "roles/pubsub.publisher"
  member = google_service_account.app.member
}

resource "google_cloud_run_v2_service" "app" {
  name                = "complaint-agent"
  location            = var.region
  deletion_protection = false

  template {
    service_account  = google_service_account.app.email
    session_affinity = true
    scaling { max_instance_count = 1 } # in-memory ADK sessions; also caps spend on a public demo
    containers {
      image = "us-docker.pkg.dev/cloudrun/container/hello" # placeholder; CI deploys the real image
      resources { limits = { cpu = "1", memory = "1Gi" } }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "FIRESTORE_DATABASE"
        value = google_firestore_database.ops.name
      }
      env {
        name  = "EVENTS_TOPIC"
        value = google_pubsub_topic.events.id
      }
      env {
        name  = "EVIDENCE_BUCKET"
        value = google_storage_bucket.evidence.name
      }
      env {
        name  = "WEB_API_KEY" # public browser key, restricted to the sign-in APIs
        value = google_apikeys_key.web.key_string
      }
    }
  }

  lifecycle { ignore_changes = [template[0].containers[0].image, client, client_version] }
  depends_on = [google_project_service.apis]
}

resource "google_cloud_run_v2_service_iam_member" "public" {
  name     = google_cloud_run_v2_service.app.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

output "service_url" { value = google_cloud_run_v2_service.app.uri }
output "image_repo" { value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}" }
