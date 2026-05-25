# Phase 11 Todo — GCP Infrastructure

Phase 11 provisions the full production environment and produces a running Cloud Run deployment. All work is infrastructure and configuration; there are no Django model or migration changes. Two small code changes are required before the first Docker build can succeed.

---

#### 11.0 — Code changes required before first build

Two settings gaps would cause the Docker build or runtime to fail if not addressed first.

- [ ] Add `STATIC_ROOT = BASE_DIR / "staticfiles"` to `hf/settings/base.py` (adjacent to `STATIC_URL`):
  - The Dockerfile (§11.2) runs `collectstatic` at build time using dev settings; without `STATIC_ROOT`, Django raises `ImproperlyConfigured` and the build fails
  - In prod, `STORAGES` overrides the backend to GCS, so this path is only used locally and during the Docker build
- [ ] Add `staticfiles/` to `.gitignore`:
  - The Docker build writes collected files to `staticfiles/`; the directory must not be committed

---

#### 11.1 — Prerequisites

All subsequent steps depend on these being in place. These are one-time manual steps, not automated.

- [ ] Install and authenticate `gcloud` CLI: `gcloud auth login`
- [ ] Set the active project: `gcloud config set project PROJECT_ID`
- [ ] Enable all required APIs in one command:
  ```
  gcloud services enable \
    run.googleapis.com \
    sqladmin.googleapis.com \
    redis.googleapis.com \
    tasks.googleapis.com \
    cloudscheduler.googleapis.com \
    secretmanager.googleapis.com \
    artifactregistry.googleapis.com \
    servicenetworking.googleapis.com \
    storage.googleapis.com
  ```

---

#### 11.2 — Dockerfile and .dockerignore

- [ ] Create `Dockerfile` at project root:
  ```dockerfile
  FROM python:3.12-slim
  WORKDIR /app
  COPY pyproject.toml uv.lock ./
  RUN pip install uv && uv sync --frozen
  COPY . .
  RUN python manage.py collectstatic --noinput
  EXPOSE 8000
  CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "hf.asgi:application"]
  ```
  - `collectstatic` runs with dev settings (default), which writes files to the `staticfiles/` directory inside the image — this is the local fallback copy. The GCS upload for prod happens as a separate post-deploy step (§11.10).
  - `DJANGO_SETTINGS_MODULE` is not set here; Cloud Run sets it to `hf.settings.prod` at runtime via `--set-env-vars` (§11.11)

- [ ] Create `.dockerignore` at project root to keep the build context small and exclude secrets:
  ```
  .env
  .git/
  .venv/
  __pycache__/
  *.pyc
  *.pyo
  staticfiles/
  .idea/
  .python-version
  .ruff_cache/
  ```

---

#### 11.3 — Service accounts

Two service accounts are needed. They must exist before the IAM bindings in later steps can be created.

- [ ] Create the Cloud Run service account (runs the Django app):
  ```
  gcloud iam service-accounts create hf-web \
    --display-name "HF Web Service"
  ```
- [ ] Create the Cloud Tasks / Cloud Scheduler service account (attaches OIDC tokens to task requests):
  ```
  gcloud iam service-accounts create hf-tasks \
    --display-name "HF Tasks Identity"
  ```

---

#### 11.4 — Secret Manager

Create one secret per sensitive value. Do not put secrets in env vars directly in Cloud Run configuration — use Secret Manager references so secrets are never visible in the console or CLI history.

- [ ] Create each secret (replace each `<value>` with the real secret):
  ```
  echo -n "<value>" | gcloud secrets create hf-secret-key --data-file=-
  echo -n "<value>" | gcloud secrets create hf-db-password --data-file=-
  echo -n "<value>" | gcloud secrets create hf-sqid-candidate --data-file=-
  echo -n "<value>" | gcloud secrets create hf-sqid-election --data-file=-
  echo -n "<value>" | gcloud secrets create hf-sqid-player --data-file=-
  echo -n "<value>" | gcloud secrets create hf-sqid-jurisdiction --data-file=-
  echo -n "<value>" | gcloud secrets create hf-mailgun-key --data-file=-
  ```
- [ ] Grant `hf-web` SA access to each secret:
  ```
  for SECRET in hf-secret-key hf-db-password hf-sqid-candidate \
    hf-sqid-election hf-sqid-player hf-sqid-jurisdiction hf-mailgun-key; do
    gcloud secrets add-iam-policy-binding $SECRET \
      --member serviceAccount:hf-web@PROJECT_ID.iam.gserviceaccount.com \
      --role roles/secretmanager.secretAccessor
  done
  ```

---

#### 11.5 — Cloud SQL (PostgreSQL)

- [ ] Create the Cloud SQL instance with private IP only (no public IP — Cloud Run connects via Direct VPC Egress):
  ```
  gcloud sql instances create hf-db \
    --database-version POSTGRES_16 \
    --tier db-g1-small \
    --region us-central1 \
    --network default \
    --no-assign-ip
  ```
- [ ] Create the application database:
  ```
  gcloud sql databases create hf --instance hf-db
  ```
- [ ] Create the application user:
  ```
  gcloud sql users create hf --instance hf-db --password <password>
  ```
  Use the same password stored in `hf-db-password` secret.
- [ ] Note the private IP of the instance (shown in console or `gcloud sql instances describe hf-db --format "value(ipAddresses[0].ipAddress)"`) — this becomes `DB_HOST` in Cloud Run env vars (§11.11)

---

#### 11.6 — Cloud Memorystore (Redis)

- [ ] Create the Redis instance:
  ```
  gcloud redis instances create hf-redis \
    --size 1 \
    --region us-central1 \
    --redis-version redis_7_0 \
    --network default
  ```
- [ ] Note the Redis IP (`gcloud redis instances describe hf-redis --region us-central1 --format "value(host)"`) — this becomes `REDIS_URL=redis://<ip>:6379/0` in Cloud Run env vars (§11.11)

> **Cost note:** Cloud Memorystore minimum is 1 GB (~$35/month). If budget is a constraint at Stage 1, a Redis instance on a Compute Engine e2-micro is a viable alternative (~$6/month managed manually). Migrate to Memorystore when reliability matters.

---

#### 11.7 — Cloud Storage

- [ ] Create the static files bucket:
  ```
  gcloud storage buckets create gs://hf-static \
    --location us-central1 \
    --uniform-bucket-level-access
  ```
- [ ] Make it publicly readable (static files are public assets):
  ```
  gcloud storage buckets add-iam-policy-binding gs://hf-static \
    --member allUsers \
    --role roles/storage.objectViewer
  ```
- [ ] Grant `hf-web` SA write access so Cloud Run can upload files from `collectstatic`:
  ```
  gcloud storage buckets add-iam-policy-binding gs://hf-static \
    --member serviceAccount:hf-web@PROJECT_ID.iam.gserviceaccount.com \
    --role roles/storage.objectAdmin
  ```

---

#### 11.8 — Artifact Registry

- [ ] Create a Docker repository to store the container image:
  ```
  gcloud artifacts repositories create hf \
    --repository-format docker \
    --location us-central1
  ```
- [ ] Authenticate Docker to push to Artifact Registry:
  ```
  gcloud auth configure-docker us-central1-docker.pkg.dev
  ```

---

#### 11.9 — Cloud Tasks queue

- [ ] Create the task queue:
  ```
  gcloud tasks queues create hf-tasks \
    --location us-central1 \
    --max-concurrent-dispatches 100 \
    --max-dispatches-per-second 500
  ```
- [ ] Grant `hf-web` SA permission to enqueue tasks:
  ```
  gcloud tasks queues add-iam-policy-binding hf-tasks \
    --location us-central1 \
    --member serviceAccount:hf-web@PROJECT_ID.iam.gserviceaccount.com \
    --role roles/cloudtasks.enqueuer
  ```
- [ ] Grant `hf-tasks` SA permission to invoke the Cloud Run service (so its OIDC tokens are accepted by the `_verify_oidc` check in `core/tasks.py`). Defer this step until after Cloud Run is deployed (§11.11), since the service must exist for the IAM binding:
  ```
  gcloud run services add-iam-policy-binding hf-web \
    --region us-central1 \
    --member serviceAccount:hf-tasks@PROJECT_ID.iam.gserviceaccount.com \
    --role roles/run.invoker
  ```

---

#### 11.10 — Build and push Docker image

- [ ] Build the image (run from the project root):
  ```
  docker build -t us-central1-docker.pkg.dev/PROJECT_ID/hf/hf:latest .
  ```
- [ ] Push to Artifact Registry:
  ```
  docker push us-central1-docker.pkg.dev/PROJECT_ID/hf/hf:latest
  ```

---

#### 11.11 — Deploy Cloud Run

The Cloud Run service is deployed in two passes:
- **First deploy**: without `TASK_BASE_URL` (its value is the Cloud Run URL, which isn't known yet). This gives us the URL.
- **Update**: set `TASK_BASE_URL` and configure Cloud Scheduler (§11.12) using the known URL.

- [ ] Deploy the service (first pass — `TASK_BASE_URL` set to a placeholder; substitute real values for all `<...>` tokens):
  ```
  gcloud run deploy hf-web \
    --image us-central1-docker.pkg.dev/PROJECT_ID/hf/hf:latest \
    --region us-central1 \
    --platform managed \
    --allow-unauthenticated \
    --timeout 3600 \
    --min-instances 1 \
    --max-instances 10 \
    --concurrency 1000 \
    --memory 4Gi \
    --cpu 2 \
    --service-account hf-web@PROJECT_ID.iam.gserviceaccount.com \
    --vpc-egress all-traffic \
    --network default \
    --subnet default \
    --set-env-vars "DJANGO_SETTINGS_MODULE=hf.settings.prod,\
  DB_NAME=hf,DB_USER=hf,DB_HOST=<cloud-sql-private-ip>,DB_PORT=5432,\
  REDIS_URL=redis://<memorystore-ip>:6379/0,\
  GCP_PROJECT=PROJECT_ID,GCP_REGION=us-central1,\
  CLOUD_TASKS_QUEUE=hf-tasks,\
  TASK_BASE_URL=https://placeholder.run.app,\
  TASK_SERVICE_ACCOUNT=hf-tasks@PROJECT_ID.iam.gserviceaccount.com,\
  GCS_BUCKET_NAME=hf-static,\
  MAILGUN_SENDER_DOMAIN=<domain>,\
  ALLOWED_HOSTS=<cloud-run-domain>,\
  DEFAULT_FROM_EMAIL=noreply@humanflourishing.org" \
    --set-secrets "SECRET_KEY=hf-secret-key:latest,\
  DB_PASSWORD=hf-db-password:latest,\
  SQID_SALT_CANDIDATE=hf-sqid-candidate:latest,\
  SQID_SALT_ELECTION=hf-sqid-election:latest,\
  SQID_SALT_PLAYER=hf-sqid-player:latest,\
  SQID_SALT_JURISDICTION=hf-sqid-jurisdiction:latest,\
  MAILGUN_API_KEY=hf-mailgun-key:latest"
  ```
  Critical flags:
  - `--timeout 3600` — maximum Cloud Run request timeout; SSE connections are capped at one hour by the platform
  - `--min-instances 1` — prevents scale-to-zero from terminating all open SSE connections
  - `--concurrency 1000` — Daphne's async event loop handles many concurrent connections in a single process
  - `--vpc-egress all-traffic` — routes outbound traffic through the VPC to reach Cloud SQL and Memorystore on private IPs

- [ ] Note the Cloud Run URL from the deploy output (format: `https://hf-web-<hash>-uc.a.run.app`)
- [ ] Add Cloud Run invoker binding for `hf-tasks` SA (from §11.9, now possible):
  ```
  gcloud run services add-iam-policy-binding hf-web \
    --region us-central1 \
    --member serviceAccount:hf-tasks@PROJECT_ID.iam.gserviceaccount.com \
    --role roles/run.invoker
  ```
- [ ] Update Cloud Run service with the real `TASK_BASE_URL`:
  ```
  gcloud run services update hf-web \
    --region us-central1 \
    --update-env-vars TASK_BASE_URL=https://hf-web-<hash>-uc.a.run.app
  ```
  `TASK_BASE_URL` must exactly match the Cloud Run URL — `_verify_oidc()` in `core/tasks.py` uses it as the expected OIDC token audience.

---

#### 11.12 — Collectstatic (upload to GCS)

This step runs `collectstatic` with prod settings to upload static assets to the GCS bucket. It must run after Cloud SQL and Cloud Run are up (the app needs to be able to import settings cleanly) and after GCS bucket permissions are in place (§11.7). Run locally with application default credentials or in CI with a service account key.

- [ ] Authenticate locally for GCS access: `gcloud auth application-default login`
- [ ] Run collectstatic with prod settings (substitute real values):
  ```
  DJANGO_SETTINGS_MODULE=hf.settings.prod \
  GCS_BUCKET_NAME=hf-static \
  SECRET_KEY=<any-value> \
  DB_NAME=hf DB_USER=hf DB_PASSWORD=<password> DB_HOST=localhost DB_PORT=5432 \
  SQID_SALT_CANDIDATE=<value> SQID_SALT_ELECTION=<value> \
  SQID_SALT_PLAYER=<value> SQID_SALT_JURISDICTION=<value> \
  MAILGUN_API_KEY=unused MAILGUN_SENDER_DOMAIN=unused \
  uv run python manage.py collectstatic --noinput
  ```

---

#### 11.13 — Cloud Scheduler

Cloud Scheduler must be configured after Cloud Run is deployed and `TASK_BASE_URL` is known. The `--oidc-token-audience` must match `TASK_BASE_URL` exactly (same value used by `_verify_oidc()`).

- [ ] Create the deprecation check job (every hour):
  ```
  gcloud scheduler jobs create http hf-check-deprecations \
    --location us-central1 \
    --schedule "0 * * * *" \
    --uri "https://hf-web-<hash>-uc.a.run.app/tasks/check-deprecations/" \
    --http-method POST \
    --message-body '{}' \
    --oidc-service-account-email hf-tasks@PROJECT_ID.iam.gserviceaccount.com \
    --oidc-token-audience "https://hf-web-<hash>-uc.a.run.app"
  ```
- [ ] Create the deletion check job (daily at midnight UTC):
  ```
  gcloud scheduler jobs create http hf-check-deletions \
    --location us-central1 \
    --schedule "0 0 * * *" \
    --uri "https://hf-web-<hash>-uc.a.run.app/tasks/check-deletions/" \
    --http-method POST \
    --message-body '{}' \
    --oidc-service-account-email hf-tasks@PROJECT_ID.iam.gserviceaccount.com \
    --oidc-token-audience "https://hf-web-<hash>-uc.a.run.app"
  ```

---

#### 11.14 — Custom domain and SSL

- [ ] Create the domain mapping (replace `humanflourishing.org` with the actual domain):
  ```
  gcloud run domain-mappings create \
    --service hf-web \
    --domain humanflourishing.org \
    --region us-central1
  ```
- [ ] Add the DNS records shown in the command output to the domain's DNS provider (typically CNAME or A records)
- [ ] Wait for Google-managed SSL certificate provisioning — this can take up to 24 hours and requires DNS records to propagate first
- [ ] Update `ALLOWED_HOSTS` in Cloud Run to include the custom domain:
  ```
  gcloud run services update hf-web \
    --region us-central1 \
    --update-env-vars ALLOWED_HOSTS=humanflourishing.org,www.humanflourishing.org
  ```

---

#### Phase 11 complete when
- [ ] `hf/settings/base.py` has `STATIC_ROOT = BASE_DIR / "staticfiles"` and `.gitignore` has `staticfiles/`
- [ ] `Dockerfile` and `.dockerignore` exist at the project root
- [ ] All GCP APIs are enabled
- [ ] `hf-web` and `hf-tasks` service accounts exist
- [ ] All seven secrets exist in Secret Manager and `hf-web` SA has `secretAccessor` on each
- [ ] Cloud SQL instance `hf-db` exists with database `hf` and user `hf`
- [ ] Cloud Memorystore instance `hf-redis` exists
- [ ] Cloud Storage bucket `gs://hf-static` exists, is public, and `hf-web` SA has `objectAdmin`
- [ ] Artifact Registry repository `hf` exists in `us-central1`
- [ ] Cloud Tasks queue `hf-tasks` exists with correct IAM bindings on both SAs
- [ ] Docker image is built and pushed to Artifact Registry
- [ ] Static files are uploaded to GCS via `collectstatic` with prod settings
- [ ] Cloud Run service `hf-web` is deployed with all env vars, secrets, VPC egress, and `TASK_BASE_URL` set to the real Cloud Run URL
- [ ] Cloud Scheduler has two jobs targeting the deployed Cloud Run URL
- [ ] Custom domain is mapped and DNS records are in place
