#!/bin/sh
set -eu

for migration in /syco23-migrations/*.sql; do
  printf '[proof-db] applying %s\n' "$(basename "$migration")"
  psql \
    --set ON_ERROR_STOP=1 \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --file "$migration"
done
