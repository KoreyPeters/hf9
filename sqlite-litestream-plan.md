# SQLite + Litestream Migration Plan

Investigates replacing Cloud SQL (PostgreSQL) with SQLite + Litestream for the HF
application running on Cloud Run. Covers what changes, what the constraints are, and
whether the trade-off is appropriate.

---

## What Litestream Does

Litestream is a standalone process that runs alongside the application and continuously
streams SQLite's Write-Ahead Log (WAL) to object storage — in this case Google Cloud
Storage. On container startup, it restores the database from GCS. On every write commit,
it replicates the WAL frames to GCS within milliseconds.

The result is durable, point-in-time recoverable SQLite without a separate database
server. There is no Litestream server — it is a sidecar binary.

---

## The Critical Constraint

**SQLite does not support concurrent writers from multiple processes.**

Cloud Run scales horizontally by adding container instances. With PostgreSQL, ten
containers can write to the same database. With SQLite, only one container can hold the
write lock at a time — and each container has its own copy of the file, so multiple
running containers would diverge.

**This means Cloud Run must be configured with `--max-instances=1`.**

This is the dominant trade-off: you give up horizontal scaling in exchange for the
elimination of the Cloud SQL bill. At pre-launch and early-stage traffic, a single
container running Uvicorn with multiple async workers is entirely sufficient. The
constraint becomes binding only when the app outgrows a single container's capacity.

SQLite with WAL mode handles concurrent readers without any locking at all, and a single
writer with millisecond-level write times. For a read-heavy app like HF (browsing ratings,
viewing elections), this is an excellent fit.

---

## PostgreSQL Feature Audit

This codebase uses no PostgreSQL-specific ORM fields. All fields in use are fully portable:

| Feature | Used | SQLite compatible |
|---|---|---|
| `CharField`, `TextField`, `BooleanField` | ✅ | ✅ |
| `DecimalField` | ✅ | ✅ (stored as text, Django handles it) |
| `DateTimeField`, `DateField` | ✅ | ✅ |
| `BinaryField` (PasskeyCredential) | ✅ | ✅ |
| `GenericForeignKey` (ContentType) | ✅ | ✅ |
| `JSONField` | ✅ | ✅ (Django 3.1+) |
| `ArrayField` | ✗ not used | N/A |
| `HStoreField` | ✗ not used | N/A |
| Raw SQL / `cursor()` | ✗ not used | N/A |

One code change is required (see below): `select_for_update()` in `polium/task_views.py`
must be wrapped in `atomic()` to be correct on SQLite's WAL locking model.

---

## What Changes

### §1 — Django database settings

`hf/settings/base.py` — replace the PostgreSQL `DATABASES` block:

```python
# Before
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config("DB_NAME", default="hf"),
        "USER": config("DB_USER", default="hf"),
        "PASSWORD": config("DB_PASSWORD", default="hf"),
        "HOST": config("DB_HOST", default="localhost"),
        "PORT": config("DB_PORT", default="5432"),
    }
}

# After
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": config("DB_PATH", default=BASE_DIR / "db.sqlite3"),
        "OPTIONS": {
            "init_command": (
                "PRAGMA journal_mode=WAL; "
                "PRAGMA synchronous=NORMAL; "
                "PRAGMA foreign_keys=ON; "
                "PRAGMA cache_size=-32000; "
                "PRAGMA temp_store=MEMORY; "
                "PRAGMA mmap_size=134217728;"
            ),
            "timeout": 20,
        },
    }
}
```

`journal_mode=WAL` is required — it enables concurrent reads during writes and is the
mode Litestream expects. `synchronous=NORMAL` is safe with Litestream because Litestream
provides its own durability guarantee via GCS replication; the default `FULL` mode adds
unnecessary fsync overhead.

`timeout=20` instructs SQLite to wait up to 20 seconds for a write lock before raising
a `OperationalError`. This handles momentary lock contention under async concurrency
within the single container.

In production (`hf/settings/prod.py`), `DB_PATH` should resolve to `/data/db.sqlite3`
(a volume mount or the container's local disk where Litestream writes).

### §2 — `select_for_update()` must be wrapped in `atomic()`

`polium/task_views.py` uses `select_for_update()` without an enclosing `atomic()` block.
On SQLite, `select_for_update()` is only valid inside an explicit transaction; Django will
raise a `TransactionManagementError` in auto-commit mode.

Wrap the task body in `atomic()`:

```python
from django.db.transaction import atomic

@task("update-candidate-rating")
def update_candidate_rating(candidate_id: int) -> None:
    from .models import Candidate
    with atomic():
        candidate = Candidate.objects.select_for_update().get(pk=candidate_id)
        ...
```

This is correct behaviour on all backends, not just SQLite — the fix should be made
regardless of which database is in use.

### §3 — Remove psycopg, add nothing

Remove `psycopg[binary]` from `pyproject.toml` dependencies. Django's SQLite backend
is built into Python's standard library — no additional driver package is needed.

### §4 — Litestream configuration

Add a `litestream.yml` at the project root:

```yaml
dbs:
  - path: /data/db.sqlite3
    replicas:
      - type: gcs
        bucket: ${LITESTREAM_GCS_BUCKET}
        path: hf/db
```

The GCS bucket is separate from the static/media bucket. Litestream authenticates using
the container's service account (the same one already used for Cloud Tasks and GCS media
storage — no new credentials needed if the service account has `storage.objectAdmin` on
the bucket).

### §5 — Container startup

Litestream manages its own startup via the `-exec` flag — it launches the application as
a subprocess and shuts down cleanly when the application exits.

A `start.sh` script at the project root:

```bash
#!/bin/sh
set -e

# Restore the database from GCS if it doesn't already exist locally
litestream restore \
  -if-db-not-exists \
  -if-replica-exists \
  -o /data/db.sqlite3 \
  "gcs://${LITESTREAM_GCS_BUCKET}/hf/db"

# Start Litestream with the app as its managed subprocess
exec litestream replicate \
  -config /app/litestream.yml \
  -exec "uvicorn hf.asgi:application --host 0.0.0.0 --port 8080 --workers 4"
```

### §6 — SQLite-specific enhancements

The `init_command` in §1 already sets WAL and synchronous modes. The four additional
PRAGMAs above address correctness and performance:

**`PRAGMA foreign_keys=ON`** — SQLite does not enforce foreign key constraints by
default. Without this, deleting a parent row leaves orphaned children silently. Django's
test runner enables foreign keys automatically, but production does not unless you
add it here. This is the most important correctness setting beyond WAL mode.

**`PRAGMA cache_size=-32000`** — Sets the in-memory page cache to 32MB (negative values
are interpreted as kilobytes). The default is 2MB. A larger cache means hot pages stay
in memory across multiple reads, reducing disk I/O significantly for a read-heavy app.
With `--memory=1Gi` on Cloud Run, 32MB is a small fraction of available RAM.

**`PRAGMA temp_store=MEMORY`** — Stores temporary tables and indices in memory rather
than writing them to disk. Temporary tables are created by complex queries with ORDER BY,
GROUP BY, and subqueries. Keeping them in RAM avoids unnecessary disk writes.

**`PRAGMA mmap_size=134217728`** — Enables 128MB of memory-mapped I/O. SQLite reads
pages from the file directly via the OS memory map rather than through `read()` system
calls. For a read-heavy app this is a measurable throughput improvement.

A `Dockerfile` referencing this:

```dockerfile
FROM python:3.14-slim

# Install Litestream
ARG LITESTREAM_VERSION=0.3.13
ADD https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-v${LITESTREAM_VERSION}-linux-amd64.tar.gz /tmp/
RUN tar -C /usr/local/bin -xzf /tmp/litestream-*.tar.gz && rm /tmp/litestream-*.tar.gz

WORKDIR /app
COPY . .

RUN pip install uv && uv sync --no-dev
RUN python manage.py collectstatic --noinput

RUN mkdir -p /data
VOLUME /data

CMD ["./start.sh"]
```

### §7 — Cloud Run service configuration

```
--max-instances=1          # REQUIRED — single-writer constraint
--memory=1Gi               # SQLite loads pages into memory; more RAM = faster
--set-env-vars DJANGO_SETTINGS_MODULE=hf.settings.prod
--set-env-vars LITESTREAM_GCS_BUCKET=hf-litestream-backup
--set-env-vars DB_PATH=/data/db.sqlite3
```

---

## What Does Not Change

- All Django app code
- All migrations (they re-run against SQLite cleanly — no PostgreSQL-specific DDL was used)
- Redis (still needed for caching and rate limiting)
- Static files (already on GCS)
- Media files (already on GCS)
- Cloud Tasks
- All environment variables except the DB connection block

---

## Cost Comparison

| | Cloud SQL (PostgreSQL) | SQLite + Litestream |
|---|---|---|
| Database server | ~$50–100/month (db-f1-micro) | $0 |
| GCS replication bucket | $0 | ~$0.02/GB/month |
| Cold start penalty | None | Seconds (Litestream restore on first request) |
| Horizontal scaling | Yes, unlimited | No — max-instances=1 |

For an early-stage app with low traffic, the saving is real and the scaling constraint
is not binding.

---

## Operational Differences

**Backup and recovery:** Litestream continuously replicates WAL frames to GCS. Point-in-time
recovery is built in — you can restore to any second in the replication window using
`litestream restore -timestamp`. This is equivalent to or better than Cloud SQL's
automated backups.

**Monitoring:** Litestream exposes Prometheus metrics. Add a Cloud Monitoring scrape
job to watch replication lag. If lag exceeds a few seconds, something is wrong.

**Database inspection:** SQLite files can be downloaded directly from GCS and opened
with any SQLite tool (DB Browser, DBeaver, `sqlite3` CLI). No VPC peering, no Cloud SQL
proxy, no credentials beyond GCS access.

**Schema migrations:** `manage.py migrate` runs on startup before Litestream's `-exec`
launches Uvicorn, or as a separate Cloud Run Job step before deploying the new revision.

---

## When Not to Use This

- Traffic requires more than one container (horizontal scaling)
- Write throughput is very high (hundreds of writes/second sustained)
- Multi-region deployment is required (SQLite is single-region by nature)
- You need PostgreSQL-specific features (full-text search, JSONB operators, etc.) in future

HF is currently read-heavy and single-region. SQLite + Litestream is appropriate for
the current stage. The migration back to PostgreSQL (or forward to a distributed SQLite
like Turso/LiteFS) is straightforward — Django's ORM abstracts the backend.

---

## File Summary

| File | Change |
|---|---|
| `hf/settings/base.py` | Replace PostgreSQL `DATABASES` with SQLite config |
| `hf/settings/prod.py` | Remove PostgreSQL env var references |
| `polium/task_views.py` | Wrap `select_for_update()` in `atomic()` |
| `pyproject.toml` | Remove `psycopg[binary]`; add nothing (stdlib SQLite) |
| `litestream.yml` | New — Litestream replication config |
| `start.sh` | New — container startup: restore then replicate+serve |
| `Dockerfile` | New — installs Litestream, sets up `/data` volume |

---

## Sequencing

1. ✅ **Code changes** — wrap `select_for_update()` in `atomic()`; update settings; remove psycopg
2. ✅ **Local verification** — 72/72 tests passing against SQLite
3. ✅ **Litestream setup** — `litestream.yml` written; GCS bucket creation is a deploy-time step
4. ✅ **Dockerfile + start.sh** — written; container build is a deploy-time step
5. **Cloud Run deploy** — deploy with `--max-instances=1`, cut over DNS
6. **Monitor** — watch Litestream replication lag for the first 24 hours
