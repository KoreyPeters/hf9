#!/bin/sh
set -e

litestream restore \
  -if-db-not-exists \
  -if-replica-exists \
  -o /data/db.sqlite3 \
  "gcs://${LITESTREAM_GCS_BUCKET}/hf/db"

python manage.py migrate --noinput
python manage.py ensure_superuser

exec litestream replicate \
  -config /app/litestream.yml \
  -exec "uvicorn hf.asgi:application --host 0.0.0.0 --port 8080 --workers 4"
