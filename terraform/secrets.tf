locals {
  secret_ids = toset([
    "SECRET_KEY",
    "SQID_SALT_CANDIDATE",
    "SQID_SALT_ELECTION",
    "SQID_SALT_PLAYER",
    "SQID_SALT_JURISDICTION",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "APPLE_CLIENT_ID",
    "APPLE_CLIENT_SECRET",
    "APPLE_KEY_ID",
    "APPLE_PRIVATE_KEY",
    "MAILGUN_API_KEY",
    "MAILGUN_SENDER_DOMAIN",
    "WEBAUTHN_RP_ID",
    "WEBAUTHN_ORIGIN",
    "LITESTREAM_GCS_BUCKET",
    "GCS_BUCKET_NAME",
    "ALLOWED_HOSTS",
    "TASK_BASE_URL",
    "TASK_SERVICE_ACCOUNT",
  ])
}

data "google_secret_manager_secret" "all" {
  for_each  = local.secret_ids
  project   = var.project
  secret_id = each.key
}
