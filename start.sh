#!/bin/sh
set -e

litestream restore \
  -if-db-not-exists \
  -if-replica-exists \
  -o /data/db.sqlite3 \
  "gcs://${LITESTREAM_GCS_BUCKET}/hf/db"

# Migrations run here, at boot, on every cold start — not in the hf-migrate job,
# whose name suggests otherwise. With SQLite on a per-container volume that job
# would migrate a copy it then throws away. See operational-debt.md item 2.
#
# The timeout is here because on 2026-09-01 this command began hanging on about
# half of all cold starts: never completing, never printing, until Cloud Run
# killed the instance at the 80-second startup probe budget and returned 503 to
# whoever was waiting. Two of those were real page loads.
#
# 60 seconds is comfortably past the 6-15s a healthy run takes, so this only
# fires on the pathological case. Two things then happen, both wanted:
#
#   - SIGABRT (not SIGTERM) triggers Python's fault handler, enabled in the
#     Dockerfile, which dumps every thread's stack to stderr. That is the
#     evidence needed to find the cause, and it is why the signal matters.
#   - `set -e` aborts the script, so the container exits and Cloud Run starts a
#     fresh one. A fast crash and retry beats occupying the whole probe budget.
#
# Remove this once the hang is understood and fixed; it is instrumentation
# around a live fault, not a design. plans/startup-hang-and-503s.md
timeout -s ABRT 60 python manage.py migrate --noinput
python manage.py ensure_superuser

# One worker, deliberately. Four of them was the direct cause of repeated OOM
# kills — each is a full Django process carrying Pillow, grpc and the genai
# client, and four baselines plus a receipt image being decoded does not fit.
#
# It is also the only correct setting while the cache is LocMemCache, which is
# per-process: passkey registration stores a WebAuthn challenge on one request
# and reads it back on the next, so with four private caches it only worked when
# both requests happened to land on the same worker. Rate limiting and alert
# throttling were wrong in the same way.
#
# Concurrency is not lost — sync Django views still run in uvicorn's thread pool.
# Revisit only alongside a shared cache; see plans/operational-debt.md item 1.
exec litestream replicate \
  -config /app/litestream.yml \
  -exec "uvicorn hf.asgi:application --host 0.0.0.0 --port 8080 --workers 1"
