#!/bin/sh
set -e

litestream restore \
  -if-db-not-exists \
  -if-replica-exists \
  -o /data/db.sqlite3 \
  "gcs://${LITESTREAM_GCS_BUCKET}/hf/db"

python manage.py migrate --noinput
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
