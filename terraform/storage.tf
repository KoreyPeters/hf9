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

# Receipt images. A separate bucket from `assets` specifically because that one
# is public — it serves static files, and `allUsers` has objectViewer on it. A
# receipt uploaded there would be readable by anyone who guessed the path, and
# the paths are guessable: Django only randomises a filename on collision, so
# the first `receipt.jpg` of a month sits at a predictable URL. Nothing reads
# these over HTTP — extraction fetches them server-side and they are never
# rendered in a template — so the bucket can be sealed outright.
resource "google_storage_bucket" "media" {
  name                        = "hf-media-${var.project}"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  # Belt and braces behind the published 24-hour deletion commitment. The app
  # deletes each image as soon as extraction finishes and an hourly sweep backs
  # that up, so this should never be the thing that acts. It is here for the
  # case where both fail: a storage-level guarantee does not depend on our code
  # being correct. Two days rather than one because the lifecycle runs on its
  # own schedule and must not race a fresh upload.
  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      age = 2
    }
  }
}

resource "google_storage_bucket_iam_member" "app_media" {
  bucket = google_storage_bucket.media.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.app.email}"
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
