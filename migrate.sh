#!/bin/sh
set -e

litestream restore \
  -if-db-not-exists \
  -if-replica-exists \
  -o /data/db.sqlite3 \
  "gcs://${LITESTREAM_GCS_BUCKET}/hf/db"

python manage.py collectstatic --noinput

exec litestream replicate \
  -config /app/litestream.yml \
  -exec "python manage.py migrate --noinput"
