#!/bin/sh
# Loads the seed data. Runs inside the `seed` compose service (psql client).
# Usage: docker compose --profile seed run --rm seed      (SEED_SCALE=0.1 -> ~1M rows)
set -eu

SCALE="${SEED_SCALE:-1}"

echo "waiting for the API to apply migrations..."
i=0
until psql -qtAX -c "SELECT 1 FROM schema_migrations WHERE version = '002_idempotency.sql'" 2>/dev/null | grep -q 1; do
  i=$((i + 1))
  if [ "$i" -gt 120 ]; then
    echo "schema not ready after 120s; is the api service running?" >&2
    exit 1
  fi
  sleep 1
done

echo "seeding with scale=${SCALE} (this replaces all existing data)"
start=$(date +%s)
psql -v ON_ERROR_STOP=1 -v scale="${SCALE}" -f /db/seed.sql
end=$(date +%s)
echo "seed finished in $((end - start))s"

psql -X -f /db/verify.sql
