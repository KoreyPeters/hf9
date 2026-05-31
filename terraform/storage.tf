resource "google_storage_bucket" "litestream" {
  name                        = "hf-litestream-${var.project}"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      age = 30
    }
  }
}

resource "google_storage_bucket" "assets" {
  name                        = "hf-assets-${var.project}"
  location                    = var.region
  uniform_bucket_level_access = true

  cors {
    origin          = ["https://humanflourish.ing"]
    method          = ["GET", "HEAD"]
    response_header = ["Content-Type"]
    max_age_seconds = 3600
  }
}

resource "google_storage_bucket_iam_member" "assets_public" {
  bucket = google_storage_bucket.assets.name
  role   = "roles/storage.objectViewer"
  member = "allUsers"
}

resource "google_storage_bucket_iam_member" "app_litestream" {
  bucket = google_storage_bucket.litestream.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.app.email}"
}

resource "google_storage_bucket_iam_member" "app_assets" {
  bucket = google_storage_bucket.assets.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.app.email}"
}
