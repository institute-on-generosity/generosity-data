#!/usr/bin/env bash
# Apply db/migrations/*.sql in order, once each.
set -euo pipefail
cd "$(dirname "$0")"
DB="${DATABASE_URL:-postgresql:///nombot}"
psql "$DB" -v ON_ERROR_STOP=1 -qc "CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
for f in migrations/*.sql; do
  name="$(basename "$f")"
  if [ "$(psql "$DB" -Atc "SELECT 1 FROM schema_migrations WHERE name = '$name'")" = "1" ]; then
    echo "skip   $name"; continue
  fi
  psql "$DB" -v ON_ERROR_STOP=1 -q --single-transaction -f "$f" \
    -c "INSERT INTO schema_migrations (name) VALUES ('$name')"
  echo "apply  $name"
done
