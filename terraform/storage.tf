resource "google_storage_bucket" "litestream" {
  name                        = "hf-litestream-${var.project}"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  # Every Litestream delete makes the object noncurrent rather than removing it,
  # so this bucket holds roughly three times what a plain listing shows —
  # measured 2026-09-05 at 489 live objects / 24.5 MiB against 2,098 total
  # versions / 70.4 MiB. Objects Litestream deleted weeks ago are still here
  # until the rule below reaches them.
  #
  # Kept for that reason as much as for rollback: it is a second line of defence
  # against the deletion accident described under the lifecycle rule.
  versioning {
    enabled = true
  }

  # **This is the primary pruning mechanism for the replica**, as of 2026-09-05.
  # Not a backstop — the thing that actually removes old generations.
  #
  # It was always here, but Litestream's own retention pruned more aggressively
  # so this rule never got to act on anything current. That retention check was
  # costing about $1.03 CAD/day in LIST operations, so it was moved to a 24-hour
  # interval that never fires inside a container's ~26-minute life
  # (litestream.yml, plans/litestream-retention-cost.md). Pruning is now here.
  #
  # **30 days must stay generous, and the reason is not storage.** A lifecycle
  # rule runs whether or not the application does. Litestream's retention is
  # self-limiting — it can only prune while a container is alive, so an idle app
  # cannot delete its own backup. This rule has no such limit. Tune it down to
  # 7 days "to save space", leave the app idle for eight, and GCS deletes the
  # entire replica; `start.sh` runs `litestream restore -if-replica-exists`,
  # which would then start from an empty database rather than fail loudly.
  #
  # At 30 days that is not a plausible scenario. It becomes one the moment
  # somebody shortens this without reading the above.
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
