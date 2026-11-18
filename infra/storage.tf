# Customer documents (statements, letters) attached to cases. Private; only the app's service account reads or
# writes them, and the API checks the caller owns the case or is staff before serving a file.
resource "google_storage_bucket" "evidence" {
  name                        = "${var.project_id}-case-evidence"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true
  lifecycle_rule {
    condition { age = 90 } # demo data; a bank would follow its record-retention schedule
    action { type = "Delete" }
  }
  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket_iam_member" "app_evidence" {
  bucket = google_storage_bucket.evidence.name
  role   = "roles/storage.objectUser"
  member = google_service_account.app.member
}
