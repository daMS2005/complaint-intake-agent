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
    "aiplatform", "artifactregistry", "cloudbuild", "cloudfunctions", "dialogflow", "eventarc",
    "iamcredentials", "logging", "monitoring", "pubsub", "run", "serviceusage", "storage", "sts",
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

# Case files and the escalation queue: the GCP side of the runtime switch.
resource "google_storage_bucket" "cases" {
  name                        = "${var.project_id}-complaint-cases"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  depends_on                  = [google_project_service.apis]
}

resource "google_pubsub_topic" "escalations" {
  name       = "complaint-escalations"
  depends_on = [google_project_service.apis]
}

# Runtime identity: only what the app calls, scoped to the resource where possible.
resource "google_service_account" "app" {
  account_id   = "complaint-agent"
  display_name = "Complaint agent (Cloud Run runtime)"
}

resource "google_project_iam_member" "app" {
  for_each = toset(["roles/aiplatform.user", "roles/serviceusage.serviceUsageViewer"])
  project  = var.project_id
  role     = each.key
  member   = google_service_account.app.member
}

resource "google_storage_bucket_iam_member" "app_cases" {
  bucket = google_storage_bucket.cases.name
  role   = "roles/storage.objectUser"
  member = google_service_account.app.member
}

resource "google_pubsub_topic_iam_member" "app_publish" {
  topic  = google_pubsub_topic.escalations.id
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
        name  = "CASES_BUCKET"
        value = google_storage_bucket.cases.name
      }
      env {
        name  = "ESCALATION_TOPIC"
        value = google_pubsub_topic.escalations.id
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
