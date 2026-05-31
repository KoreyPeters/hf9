# Terraform + Cloud Build Deployment Plan

## Goals

1. All GCP infrastructure declared in Terraform — no click-ops, no manual state
2. Push to `main` on GitHub → Cloud Build → Docker image → Cloud Run deploy, fully automated
3. Zero long-lived credentials anywhere; GitHub → GCP trust established via Workload Identity Federation
4. Secrets live only in Secret Manager; no plaintext env vars containing sensitive values
5. Idempotent — the pipeline can run on every push, rollback is one redeploy

---

## Known Values

| Variable | Value |
|---|---|
| GCP project ID | `human-flourishing-4` |
| GitHub repo | `KoreyPeters/hf9` |
| Region | `us-central1` |
| Domain | `humanflourish.ing` |
| Environment | Production only (single env) |

---

## Constraints Specific to This Stack

### SQLite + Litestream = max-instances: 1

Cloud Run can spin up multiple container instances. SQLite is a local file; two concurrent instances both replicating to GCS will corrupt the database. The Cloud Run service **must** be pinned to `max-instances: 1`.

This is already the effective behaviour (single replica with 4 in-process Uvicorn workers). It must be explicitly enforced in Terraform so a future autoscaling change doesn't silently break data integrity.

If traffic ever demands horizontal scale, the migration path is PostgreSQL (Cloud SQL). That's a future project decision, not a current constraint on the Terraform design.

### No Redis

There is no cache requirement right now. Cloud Memorystore and its associated VPC are excluded from this plan. `hf/settings/base.py` currently points `CACHES` at Redis; `hf/settings/prod.py` must override this **before the first deploy** (see Prerequisite Code Change below).

### Migrations

`start.sh` does not run `manage.py migrate`. Django migrations must be run before the new revision receives traffic. The pipeline runs migrations as a one-off **Cloud Run Job** after the image is pushed but before traffic is cut over to the new revision.

---

## Prerequisite Code Change (before Terraform)

Add to `hf/settings/prod.py`:

```python
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
```

`USE_X_FORWARDED_HOST` and `SECURE_PROXY_SSL_HEADER` are required because the load balancer (added for Cloud Armor) terminates TLS before traffic reaches Cloud Run. Without these, Django sees all requests as HTTP and `SECURE_SSL_REDIRECT` bounces them in a loop.

---

## Infrastructure Inventory

Every resource below will be Terraform-managed.

| Resource | GCP Service | Notes |
|---|---|---|
| Container registry | Artifact Registry | `us-central1-docker.pkg.dev/human-flourishing-4/hf` |
| App container | Cloud Run (v2) | `hf-app`, max 1 instance, port 8080, internal-and-LB ingress only |
| Migration runner | Cloud Run Job | Same image, runs `migrate` on deploy |
| Database backup | Cloud Storage bucket | `hf-litestream-human-flourishing-4` — Litestream WAL replication |
| Static / media files | Cloud Storage bucket | `hf-assets-human-flourishing-4` — served via GCS public URL |
| Background tasks | Cloud Tasks queue | `hf-tasks` queue, OIDC-authenticated |
| Scheduled tasks | Cloud Scheduler | Lifecycle deprecation/deletion jobs |
| Secrets | Secret Manager | All sensitive config (see list below) |
| Load balancer | Global HTTP(S) LB | Required for Cloud Armor + custom domain + managed SSL |
| WAF / DDoS | Cloud Armor | Security policy on the load balancer; start permissive, tighten when public |
| CI/CD triggers | Cloud Build | GitHub push to `main` → build → deploy |
| Workload Identity | IAM / WIF | Cloud Build uses federated identity, not a key file |

### Secret Manager secrets to provision

```
SECRET_KEY
SQID_SALT_CANDIDATE
SQID_SALT_ELECTION
SQID_SALT_PLAYER
SQID_SALT_JURISDICTION
GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET
APPLE_CLIENT_ID
APPLE_CLIENT_SECRET
APPLE_KEY_ID
APPLE_PRIVATE_KEY
MAILGUN_API_KEY
MAILGUN_SENDER_DOMAIN
WEBAUTHN_RP_ID                 (value = humanflourish.ing)
WEBAUTHN_ORIGIN                (value = https://humanflourish.ing)
LITESTREAM_GCS_BUCKET          (value = hf-litestream-human-flourishing-4)
GCS_BUCKET_NAME                (value = hf-assets-human-flourishing-4)
ALLOWED_HOSTS                  (value = humanflourish.ing)
TASK_BASE_URL                  (value = https://humanflourish.ing)
TASK_SERVICE_ACCOUNT
```

Terraform provisions the secret **resources** and their IAM bindings. Secret **values** are set out-of-band (manually via `gcloud secrets versions add` or a one-time bootstrap script) — Terraform does not manage secret values.

---

## Repository Structure

```
hf9/
├── cloudbuild.yaml          # CI/CD pipeline definition
├── Dockerfile
├── start.sh
├── terraform/
│   ├── backend.tf           # GCS remote state
│   ├── providers.tf         # google, google-beta, required versions
│   ├── variables.tf         # project, region, image tag, etc.
│   ├── outputs.tf           # Cloud Run URL, load balancer IP, Artifact Registry path
│   ├── apis.tf              # google_project_service enables
│   ├── iam.tf               # Service accounts + IAM bindings
│   ├── secrets.tf           # Secret Manager resources
│   ├── storage.tf           # GCS buckets
│   ├── artifact_registry.tf
│   ├── cloud_run.tf         # Cloud Run service + Job
│   ├── cloud_tasks.tf       # Task queue
│   ├── cloud_scheduler.tf   # Scheduled jobs
│   ├── load_balancer.tf     # Global LB, Serverless NEG, managed SSL cert, Cloud Armor
│   └── cloud_build.tf       # Build trigger + WIF
```

Flat layout, no modules — the surface area is small enough that modules would add indirection without value.

---

## Service Accounts and IAM (Least Privilege)

Three service accounts:

### `hf-app@human-flourishing-4.iam.gserviceaccount.com`
The runtime identity for the Cloud Run service and Cloud Run Job.

| Role | Why |
|---|---|
| `roles/secretmanager.secretAccessor` | Read runtime secrets |
| `roles/storage.objectAdmin` on `hf-litestream-*` bucket | Litestream WAL read/write |
| `roles/storage.objectAdmin` on `hf-assets-*` bucket | Static file writes (collectstatic) |
| `roles/cloudtasks.enqueuer` | Enqueue background tasks |
| `roles/run.invoker` on Cloud Run service | Task endpoint self-invocation |

### `hf-tasks@human-flourishing-4.iam.gserviceaccount.com`
The OIDC principal attached to Cloud Tasks HTTP targets and Cloud Scheduler jobs. The `@task` decorator verifies its token.

| Role | Why |
|---|---|
| `roles/run.invoker` on Cloud Run service | Cloud Tasks and Cloud Scheduler call `/tasks/*` endpoints |

### `hf-cloudbuild@human-flourishing-4.iam.gserviceaccount.com`
The Cloud Build service account (replaces the default `@cloudbuild.gserviceaccount.com`).

| Role | Why |
|---|---|
| `roles/artifactregistry.writer` | Push Docker images |
| `roles/run.developer` | Deploy new Cloud Run revisions |
| `roles/iam.serviceAccountUser` on `hf-app` SA | Act-as when deploying Cloud Run |
| `roles/run.invoker` on Cloud Run service | Invoke migration Job |

---

## Terraform State

Remote state in GCS. The bucket is the one resource created manually before `terraform init` (chicken-and-egg — you can't Terraform a Terraform state bucket).

```hcl
# terraform/backend.tf
terraform {
  backend "gcs" {
    bucket = "hf-tfstate-human-flourishing-4"
    prefix = "terraform/state"
  }
}
```

Bootstrap once:
```bash
gcloud storage buckets create gs://hf-tfstate-human-flourishing-4 \
  --uniform-bucket-level-access \
  --public-access-prevention
```

State locking is provided natively by the GCS backend.

---

## GCP APIs to Enable

Terraform manages these via `google_project_service`:

```
run.googleapis.com
artifactregistry.googleapis.com
cloudbuild.googleapis.com
secretmanager.googleapis.com
cloudtasks.googleapis.com
cloudscheduler.googleapis.com
storage.googleapis.com
iam.googleapis.com
iamcredentials.googleapis.com    # Workload Identity Federation
compute.googleapis.com           # Load balancer, Cloud Armor
```

---

## Cloud Build Pipeline (`cloudbuild.yaml`)

The pipeline runs on every push to `main`. Steps:

```
1. build    — docker build -t IMAGE:$SHORT_SHA .
2. push     — docker push IMAGE:$SHORT_SHA + IMAGE:latest
3. migrate  — gcloud run jobs update hf-migrate --image IMAGE:$SHORT_SHA
              gcloud run jobs execute hf-migrate --wait
4. deploy   — gcloud run services update hf-app --image IMAGE:$SHORT_SHA --no-traffic
5. smoke    — curl -f https://humanflourish.ing/
6. cutover  — gcloud run services update-traffic hf-app --to-latest
```

Splitting deploy and cutover lets the migration job complete against the new schema before any request hits the new code. If the migration job fails, the pipeline stops and traffic stays on the old revision.

### `cloudbuild.yaml` skeleton

```yaml
steps:
  - id: build
    name: gcr.io/cloud-builders/docker
    args: [build, -t, $_IMAGE:$SHORT_SHA, -t, $_IMAGE:latest, .]

  - id: push
    name: gcr.io/cloud-builders/docker
    args: [push, --all-tags, $_IMAGE]
    waitFor: [build]

  - id: migrate
    name: gcr.io/google.com/cloudsdktool/cloud-sdk
    entrypoint: bash
    args:
      - -c
      - |
        gcloud run jobs update hf-migrate \
          --image $_IMAGE:$SHORT_SHA \
          --region $_REGION \
          --service-account hf-app@$PROJECT_ID.iam.gserviceaccount.com
        gcloud run jobs execute hf-migrate --region $_REGION --wait
    waitFor: [push]

  - id: deploy
    name: gcr.io/google.com/cloudsdktool/cloud-sdk
    args:
      - run
      - services
      - update
      - hf-app
      - --image=$_IMAGE:$SHORT_SHA
      - --region=$_REGION
      - --no-traffic
    waitFor: [migrate]

  - id: smoke
    name: gcr.io/cloud-builders/curl
    args: [-f, https://humanflourish.ing/]
    waitFor: [deploy]

  - id: cutover
    name: gcr.io/google.com/cloudsdktool/cloud-sdk
    args: [run, services, update-traffic, hf-app, --to-latest, --region=$_REGION]
    waitFor: [smoke]

substitutions:
  _IMAGE: us-central1-docker.pkg.dev/human-flourishing-4/hf/hf-app
  _REGION: us-central1

options:
  serviceAccount: projects/human-flourishing-4/serviceAccounts/hf-cloudbuild@human-flourishing-4.iam.gserviceaccount.com
  logging: CLOUD_LOGGING_ONLY
```

### Cloud Run Job for migrations

The migration runner is a separate Cloud Run Job (`hf-migrate`) using the same image, overriding the command:

```
command: ["python", "manage.py", "migrate", "--noinput"]
```

It mounts the same secrets as the app and uses the `hf-app` service account. It runs to completion (exit 0 = success) before the new revision goes live.

---

## Workload Identity Federation (no key files)

Cloud Build authenticates to GCP using Workload Identity Federation. This means:
- No service account JSON key file is ever created or stored
- GitHub's OIDC token is exchanged for a short-lived GCP credential at build time
- If the GitHub repo is compromised, rotating WIF is instant; there is no leaked key to chase down

Terraform provisions:
```hcl
resource "google_iam_workload_identity_pool" "github" { ... }

resource "google_iam_workload_identity_pool_provider" "github" {
  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }
  attribute_condition = "assertion.repository == 'KoreyPeters/hf9'"
  oidc { issuer_uri = "https://token.actions.githubusercontent.com" }
}

resource "google_service_account_iam_member" "wif_cloudbuild" {
  service_account_id = google_service_account.cloudbuild.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${pool.name}/attribute.repository/KoreyPeters/hf9"
}
```

---

## Cloud Run Service (Terraform)

Key settings that must be explicit:

```hcl
resource "google_cloud_run_v2_service" "app" {
  name     = "hf-app"
  location = var.region

  # Restrict to load balancer traffic only — prevents Cloud Armor bypass via direct Cloud Run URL
  ingress = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"

  template {
    max_instance_count = 1       # SQLite constraint — must not increase without migrating to Cloud SQL
    min_instance_count = 0       # Scale to zero when idle (cost)

    service_account = google_service_account.app.email

    volumes {
      name = "data"
      empty_dir { medium = "MEMORY"; size_limit = "256Mi" }
    }

    containers {
      image = "us-central1-docker.pkg.dev/human-flourishing-4/hf/hf-app:latest"
      ports { container_port = 8080 }

      volume_mounts { name = "data"; mount_path = "/data" }

      env { name = "DJANGO_SETTINGS_MODULE"; value = "hf.settings.prod" }

      # All sensitive config comes from Secret Manager
      dynamic "env" {
        for_each = local.secret_env_vars
        content {
          name = env.value.name
          value_source {
            secret_key_ref {
              secret  = env.value.secret
              version = "latest"
            }
          }
        }
      }

      resources {
        limits   = { cpu = "1", memory = "512Mi" }
        cpu_idle = true  # CPU only allocated during request handling
      }

      startup_probe {
        http_get { path = "/" }
        initial_delay_seconds = 30
        period_seconds        = 5
        failure_threshold     = 10
      }
    }
  }
}
```

**`INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER`** ensures all traffic must pass through the Cloud Armor policy on the load balancer. The direct `*.run.app` URL will return 403 — this is intentional.

**Volume note:** `/data` is an in-memory `emptyDir`. Litestream restores the SQLite DB from GCS at startup, then replicates writes back continuously. If the instance restarts, the next instance restores from GCS. Data is never lost as long as Litestream is replicating.

**Cold start note:** `min_instance_count = 0` means the service scales to zero when idle. Every cold start runs `litestream restore` before Uvicorn starts — add download time from GCS to the startup budget. The startup probe is set to `initial_delay_seconds = 30, failure_threshold = 10, period_seconds = 5` (80 seconds total) to accommodate this. If the database grows large and restores start timing out, increase `initial_delay_seconds` first.

---

## Load Balancer + Cloud Armor (`load_balancer.tf`)

The load balancer serves two purposes at once: it's the attachment point for the Cloud Armor WAF policy, and it's required to use a custom domain with a Google-managed SSL certificate.

**Architecture:**
```
Internet → Global Forwarding Rule (reserved IP)
         → HTTPS Target Proxy (managed SSL cert for humanflourish.ing)
         → URL Map
         → Backend Service (Cloud Armor policy attached here)
         → Serverless NEG → Cloud Run hf-app
```

```hcl
# Reserved external IP — set this as your DNS A record
resource "google_compute_global_address" "default" {
  name = "hf-lb-ip"
}

# Managed SSL certificate — Google auto-provisions and renews
resource "google_compute_managed_ssl_certificate" "default" {
  name = "hf-ssl"
  managed { domains = ["humanflourish.ing"] }
}

# Serverless NEG — bridges the load balancer to Cloud Run
resource "google_compute_region_network_endpoint_group" "cloudrun" {
  name                  = "hf-neg"
  network_endpoint_type = "SERVERLESS"
  region                = var.region
  cloud_run { service = google_cloud_run_v2_service.app.name }
}

# Cloud Armor security policy
resource "google_compute_security_policy" "default" {
  name = "hf-armor"

  # Start permissive — add WAF rules when the app is public-facing
  rule {
    action   = "allow"
    priority = 2147483647
    match {
      versioned_expr = "SRC_IPS_V1"
      config { src_ip_ranges = ["*"] }
    }
    description = "Default allow — tighten before public launch"
  }
}

resource "google_compute_backend_service" "default" {
  name                  = "hf-backend"
  protocol              = "HTTPS"
  security_policy       = google_compute_security_policy.default.id
  load_balancing_scheme = "EXTERNAL_MANAGED"

  backend {
    group = google_compute_region_network_endpoint_group.cloudrun.id
  }
}
```

**DNS:** After `terraform apply`, output the reserved IP and create an A record:
```
humanflourish.ing.  →  <hf-lb-ip>
```

**SSL certificate provisioning** takes up to 60 minutes after the DNS record is live. The certificate will not be issued until the domain resolves to the load balancer IP.

**Before public launch**, replace the default-allow Cloud Armor rule with:
- Google's pre-configured WAF rule sets (`evaluatePreconfiguredExpr("xss-stable")`, `sqli-stable`, etc.)
- Rate limiting per IP to protect against survey-stuffing
- Geo-blocking if the player base is initially regional

---

## Cloud Tasks Queue

```hcl
resource "google_cloud_tasks_queue" "main" {
  name     = "hf-tasks"
  location = var.region

  rate_limits {
    max_concurrent_dispatches = 10
    max_dispatches_per_second = 100
  }

  retry_config {
    max_attempts  = 5
    min_backoff   = "1s"
    max_backoff   = "300s"
    max_doublings = 5
  }
}
```

---

## Cloud Scheduler Jobs

Lifecycle deprecation and deletion checks. Example:

```hcl
resource "google_cloud_scheduler_job" "deprecation_check" {
  name     = "hf-deprecation-check"
  schedule = "0 * * * *"   # every hour
  region   = var.region

  http_target {
    uri         = "https://humanflourish.ing/tasks/lifecycle/deprecation-check/"
    http_method = "POST"

    oidc_token {
      service_account_email = google_service_account.tasks.email
      audience              = "https://humanflourish.ing"
    }
  }
}
```

One job per `@task`-decorated function that needs scheduled invocation. Add scheduler jobs as task handlers are built. Note: the OIDC audience is the canonical domain, not the Cloud Run URL, because Cloud Armor is in front.

---

## GCS Buckets

```hcl
# Litestream backup — private, versioned
resource "google_storage_bucket" "litestream" {
  name                        = "hf-litestream-human-flourishing-4"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning { enabled = true }

  lifecycle_rule {
    action { type = "Delete" }
    condition { age = 30 }   # keep 30 days of WAL history
  }
}

# Static files — public read
resource "google_storage_bucket" "assets" {
  name                        = "hf-assets-human-flourishing-4"
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
```

---

## Implementation Order

Run these phases in sequence. Each phase leaves the system in a working state.

### Phase 0 — Bootstrap (manual, once)
1. Make the prerequisite code change (`prod.py` CACHES + proxy headers) and merge to `main`
2. Create the GCP project `human-flourishing-4` and enable billing
3. Create the Terraform state bucket: `gs://hf-tfstate-human-flourishing-4`
4. Create the initial `hf-cloudbuild` SA with `roles/owner` temporarily (tightened in Phase 1)
5. Run `terraform init`

### Phase 1 — Core infrastructure
Provision: APIs, Artifact Registry, GCS buckets, Secret Manager secrets (empty), service accounts with correct IAM.

Verify: `terraform apply` is clean.

### Phase 2 — First manual deploy
Build and push the image manually to confirm it works before wiring CI:
```bash
docker build -t us-central1-docker.pkg.dev/human-flourishing-4/hf/hf-app:init .
docker push us-central1-docker.pkg.dev/human-flourishing-4/hf/hf-app:init
```
Populate all Secret Manager values.
Deploy the Cloud Run service and Job via Terraform with the `:init` tag.
Run migration job manually: `gcloud run jobs execute hf-migrate --wait`.
Confirm the app serves via the `*.run.app` URL (still accessible before ingress restriction is applied).

### Phase 3 — Load balancer, Cloud Armor, custom domain
Provision: load balancer, Serverless NEG, managed SSL cert, Cloud Armor policy.
Set Cloud Run ingress to `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER`.
Create DNS A record for `humanflourish.ing` → reserved IP.
Wait for SSL certificate to provision (up to 60 minutes after DNS propagates).
Verify `https://humanflourish.ing/` serves the app.

### Phase 4 — Cloud Build pipeline
Provision: Cloud Build trigger, WIF pool and provider, `hf-cloudbuild` SA bindings.
Add `cloudbuild.yaml` to the repo.
Push a test commit to `main` and watch the pipeline run end-to-end.
Remove the temporary `roles/owner` from `hf-cloudbuild` SA.

### Phase 5 — Cloud Tasks and Scheduler
Provision: task queue, scheduler jobs.
Verify a scheduler job hits the task endpoint and returns 200.

### Phase 6 — Tighten Cloud Armor
Before announcing the public launch, replace the default-allow Cloud Armor rule with Google's managed WAF rule sets and add rate limiting.
Audit IAM with `gcloud projects get-iam-policy human-flourishing-4 --format=json`.

---

## Rollback

Cloud Run keeps previous revisions. To roll back:

```bash
# List revisions
gcloud run revisions list --service hf-app --region us-central1

# Send 100% traffic to a specific previous revision
gcloud run services update-traffic hf-app \
  --to-revisions hf-app-PREVIOUS-REVISION=100 \
  --region us-central1
```

This is instant (seconds). It does not roll back migrations — if a migration is irreversible (column drops, etc.), a separate rollback migration must exist before the forward migration is merged.

---

## Best Practices Checklist

- [ ] Prerequisite code change merged before first deploy (`prod.py` CACHES + proxy headers)
- [ ] Terraform state in GCS with state locking (built-in for GCS backend)
- [ ] No service account key files — Workload Identity Federation for Cloud Build
- [ ] All runtime secrets in Secret Manager, accessed as env vars at Cloud Run startup
- [ ] `max-instances: 1` on Cloud Run enforced in Terraform (SQLite constraint)
- [ ] Cloud Run ingress set to `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER` (Cloud Armor cannot be bypassed)
- [ ] Cloud Armor policy attached to backend service before public launch
- [ ] Cloud Build service account is not the default `@cloudbuild` SA — dedicated SA with least-privilege roles
- [ ] Separate service accounts for app runtime, task invocation, and CI/CD
- [ ] Artifact Registry with image retention policy (keep last 10 tagged images)
- [ ] GCS Litestream bucket: versioning on, public access prevention enforced, 30-day lifecycle rule
- [ ] Django migrations run as an isolated Cloud Run Job before traffic cutover
- [ ] `cloudbuild.yaml` smoke test step between deploy and traffic cutover
- [ ] Cloud Scheduler and Cloud Tasks jobs use OIDC tokens verified by `_verify_oidc()` in `core/tasks.py`
- [ ] OIDC audience set to `https://humanflourish.ing` (the canonical domain, not the Cloud Run URL)
- [ ] `DJANGO_SETTINGS_MODULE=hf.settings.prod` set as a plain (non-secret) env var
- [ ] `SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE` all True in prod settings (already done)

---

## Implementation Todo

Each task is marked `[ ]` to be checked off as work is completed. Phases must be done in order; tasks within a phase can be parallelised.

---

### Phase 0 — Prerequisite Code Change

- [x] `hf/settings/prod.py` — add `CACHES` override to `LocMemCache` (replaces Redis dependency)
- [x] `hf/settings/prod.py` — add `USE_X_FORWARDED_HOST = True`
- [x] `hf/settings/prod.py` — add `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")`
- [x] `hf/settings/prod.py` — fix `DEFAULT_FROM_EMAIL` domain to `humanflourish.ing`
- [x] `migrate.sh` — created; runs litestream restore → collectstatic → litestream replicate -exec migrate
- [x] `Dockerfile` — added chmod, build-arg SECRET_KEY placeholder, explicit DJANGO_SETTINGS_MODULE for collectstatic
- [x] `.dockerignore` — created; excludes .env, .venv, local_only/, terraform/

---

### Phase 1 — Core Terraform Infrastructure

**Scaffolding**
- [x] Create `terraform/` directory at repo root
- [x] `terraform/backend.tf` — GCS remote state (`hf-tfstate-human-flourishing-4`, prefix `terraform/state`)
- [x] `terraform/providers.tf` — `hashicorp/google` `~> 6.0` and `hashicorp/google-beta` `~> 6.0`; `required_version = ">= 1.9"`
- [x] `terraform/variables.tf` — declare `project` (default `human-flourishing-4`), `region` (default `us-central1`), `app_image` (used for first manual deploy)
- [x] `terraform/outputs.tf` — all outputs (cloud_run_url, load_balancer_ip, artifact_registry_url, wif_provider)

**APIs (`terraform/apis.tf`)**
- [x] Enable `run.googleapis.com`
- [x] Enable `artifactregistry.googleapis.com`
- [x] Enable `cloudbuild.googleapis.com`
- [x] Enable `secretmanager.googleapis.com`
- [x] Enable `cloudtasks.googleapis.com`
- [x] Enable `cloudscheduler.googleapis.com`
- [x] Enable `storage.googleapis.com`
- [x] Enable `iam.googleapis.com`
- [x] Enable `iamcredentials.googleapis.com` (Workload Identity Federation)
- [x] Enable `compute.googleapis.com` (load balancer + Cloud Armor)
- [x] Set `disable_on_destroy = false` on all API resources

**Service accounts (`terraform/iam.tf`)**
- [x] Create SA `hf-app` (display name "HF App Runtime")
- [x] Create SA `hf-tasks` (display name "HF Task Invoker")
- [x] Create SA `hf-cloudbuild` (display name "HF Cloud Build")

**IAM bindings (`terraform/iam.tf` / `terraform/storage.tf` / `terraform/cloud_run.tf`)**
- [x] `hf-app` → `roles/secretmanager.secretAccessor` (project-level)
- [x] `hf-app` → `roles/cloudtasks.enqueuer` (project-level)
- [x] `hf-app` → `roles/storage.objectAdmin` on litestream bucket
- [x] `hf-app` → `roles/storage.objectAdmin` on assets bucket
- [x] `hf-app` → `roles/run.invoker` on Cloud Run service
- [x] `hf-tasks` → `roles/run.invoker` on Cloud Run service
- [x] `hf-cloudbuild` → `roles/artifactregistry.writer` (project-level)
- [x] `hf-cloudbuild` → `roles/run.developer` (project-level)
- [x] `hf-cloudbuild` → `roles/logging.logWriter` (project-level; added — required for Cloud Build custom SA)
- [x] `hf-cloudbuild` → `roles/iam.serviceAccountUser` on `hf-app` SA
- [x] `hf-cloudbuild` → `roles/run.invoker` on Cloud Run service
- [x] Cloud Build service agent → `roles/iam.serviceAccountTokenCreator` on `hf-cloudbuild` SA (required for custom SA in triggers)

**Secret Manager (`terraform/secrets.tf`)**
- [x] All 20 secrets referenced via `data "google_secret_manager_secret"` with `for_each` (secrets already exist in GCP; `data` sources used instead of `resource` blocks)
- [x] `hf-app` SA access covered by project-level `secretAccessor` binding in iam.tf

**GCS Buckets (`terraform/storage.tf`)**
- [x] Litestream bucket `hf-litestream-human-flourishing-4`: private, `uniform_bucket_level_access`, `public_access_prevention = "enforced"`, versioning on, lifecycle rule delete objects older than 30 days
- [x] Assets bucket `hf-assets-human-flourishing-4`: `uniform_bucket_level_access`, CORS for `https://humanflourish.ing` (GET/HEAD, Content-Type, 3600s max-age)
- [x] `allUsers` → `roles/storage.objectViewer` on assets bucket

**Artifact Registry (`terraform/artifact_registry.tf`)**
- [x] Create Docker repository `hf` in `us-central1`, format `DOCKER`
- [x] Cleanup policy: keep last 10 tagged images; delete untagged after 1 day
- [x] Output: `artifact_registry_url` in outputs.tf

---

### Phase 2 — Cloud Run

**Cloud Run service + Job (`terraform/cloud_run.tf`)**
- [x] `local.secret_ids` toset reused from secrets.tf for the dynamic `env` block (env var name = secret name for all 20 secrets)
- [x] Cloud Run v2 service `hf-app`:
  - `ingress = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"`
  - `max_instance_count = 1`, `min_instance_count = 0` (scale to zero when idle; cold start latency accepted)
  - Service account: `hf-app`
  - In-memory `emptyDir` volume mounted at `/data` (256Mi)
  - Env var `DJANGO_SETTINGS_MODULE = "hf.settings.prod"` (plain, non-secret)
  - All 20 secrets injected as env vars via `secret_key_ref`, version `latest`
  - Resources: 1 CPU, 512Mi memory, `cpu_idle = true`
  - Startup probe: HTTP GET `/`, initial delay 30s, period 5s, failure threshold 10 (covers Litestream restore on cold start)
  - Image: `var.app_image`
- [x] Cloud Run Job `hf-migrate`:
  - Command: `["/app/migrate.sh"]` (runs litestream restore + collectstatic + litestream replicate -exec migrate)
  - Service account: `hf-app`; same secrets; task count 1, max retries 1, timeout 600s
- [x] Output: Cloud Run service URL in outputs.tf

---

### Phase 3 — Load Balancer, Cloud Armor, Custom Domain

**All resources in `terraform/load_balancer.tf`**
- [x] `google_compute_global_address` `hf-lb-ip` — reserved static external IP
- [x] `google_compute_managed_ssl_certificate` `hf-ssl` — domain `humanflourish.ing`
- [x] `google_compute_region_network_endpoint_group` `hf-neg` — SERVERLESS type, Cloud Run service `hf-app`, region `us-central1`
- [x] `google_compute_backend_service` `hf-backend`: protocol HTTPS, scheme EXTERNAL_MANAGED, Cloud Armor attached, logging at 1.0 sample rate
- [x] `google_compute_security_policy` `hf-armor`: default-allow rule, description notes tightening required before public launch
- [x] `google_compute_url_map` `hf-urlmap` — default service: `hf-backend`
- [x] `google_compute_url_map` `hf-http-redirect` — redirect all HTTP to HTTPS (301)
- [x] `google_compute_target_https_proxy` `hf-https-proxy` — URL map + SSL cert
- [x] `google_compute_target_http_proxy` `hf-http-proxy` — HTTP→HTTPS redirect URL map
- [x] `google_compute_global_forwarding_rule` `hf-https` — port 443, IP `hf-lb-ip`, target: HTTPS proxy
- [x] `google_compute_global_forwarding_rule` `hf-http` — port 80, IP `hf-lb-ip`, target: HTTP proxy
- [x] Output: `load_balancer_ip` in outputs.tf

---

### Phase 4 — Cloud Build Pipeline

**Workload Identity + trigger (`terraform/cloud_build.tf`)**
- [x] `google_iam_workload_identity_pool` `github` — pool ID `github-pool`
- [x] `google_iam_workload_identity_pool_provider` `github` — provider ID `github-provider`, OIDC issuer, attribute mapping, attribute condition for `KoreyPeters/hf9`
- [x] `google_service_account_iam_member` binding `hf-cloudbuild` SA as `roles/iam.workloadIdentityUser` for the WIF pool principal set
- [x] `google_cloudbuild_trigger` `hf-main-push`: GitHub `KoreyPeters/hf9`, branch `^main$`, filename `cloudbuild.yaml`, SA `hf-cloudbuild`, substitutions `_IMAGE` + `_REGION`
- [x] Output: `wif_provider` in outputs.tf

**Pipeline definition (`cloudbuild.yaml` at repo root)**
- [x] Step `build` — docker build with `$SHORT_SHA` and `latest` tags
- [x] Step `push` — `docker push --all-tags` (waitFor: build)
- [x] Step `migrate` — jobs update then execute --wait (waitFor: push)
- [x] Step `deploy` — services update --no-traffic (waitFor: migrate)
- [x] Step `smoke` — curl --silent --fail --max-time 30 --retry 3 --retry-delay 10 (waitFor: deploy)
- [x] Step `cutover` — update-traffic --to-latest (waitFor: smoke)
- [x] Substitutions: `_IMAGE`, `_REGION`
- [x] Options: `serviceAccount` = hf-cloudbuild full resource name, `logging: CLOUD_LOGGING_ONLY`

---

### Phase 5 — Cloud Tasks and Scheduler

**Task queue (`terraform/cloud_tasks.tf`)**
- [x] Queue `hf-tasks` in `us-central1`
- [x] Rate limits: `max_concurrent_dispatches = 10`, `max_dispatches_per_second = 100`
- [x] Retry config: `max_attempts = 5`, `min_backoff = "1s"`, `max_backoff = "300s"`, `max_doublings = 5`

**Scheduler jobs (`terraform/cloud_scheduler.tf`)**
- [x] Job `hf-check-deprecations`: schedule `"0 * * * *"` (every hour), POST to `https://humanflourish.ing/tasks/check-deprecations/`, OIDC token with SA `hf-tasks`, audience `https://humanflourish.ing`
- [x] Job `hf-check-deletions`: schedule `"0 2 * * *"` (daily at 02:00 UTC), POST to `https://humanflourish.ing/tasks/check-deletions/`, OIDC token with SA `hf-tasks`, audience `https://humanflourish.ing`
- [x] Both jobs: `attempt_deadline = "300s"`, `time_zone = "UTC"`

---

### Phase 6 — Outputs and Final Wiring

**`terraform/outputs.tf`** (consolidate all outputs)
- [x] `cloud_run_url` — service URL
- [x] `load_balancer_ip` — reserved IP (annotate: set as DNS A record for `humanflourish.ing`)
- [x] `artifact_registry_url` — full repository path
- [x] `wif_provider` — WIF provider resource name
