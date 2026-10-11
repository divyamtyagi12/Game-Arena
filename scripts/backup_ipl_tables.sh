#!/usr/bin/env bash
# IPL Draft: file-based backup with pg_dump (run BEFORE database/migration_ipl_v3.sql).
#   DATABASE_URL="postgresql://postgres:***@db.<ref>.supabase.co:5432/postgres" scripts/backup_ipl_tables.sh [outdir]
# Writes a custom-format dump of all public.ipl_* tables + schema ipl_private and a plain-SQL copy. Never drops or alters anything.
set -euo pipefail
: "${DATABASE_URL:?set DATABASE_URL (Supabase: Project Settings → Database → Connection string)}"
out="${1:-./ipl_backups}"; ts="$(date -u +%Y%m%d_%H%M%S)"; mkdir -p "$out"
tables=$(psql "$DATABASE_URL" -qAtX -c "select string_agg(format('-t public.%I', tablename), ' ') from pg_tables where schemaname='public' and tablename like 'ipl\_%'")
# shellcheck disable=SC2086
pg_dump "$DATABASE_URL" --no-owner --format=custom $tables -n ipl_private -f "$out/ipl_tables_$ts.dump"
# shellcheck disable=SC2086
pg_dump "$DATABASE_URL" --no-owner --data-only --inserts $tables -n ipl_private -f "$out/ipl_tables_$ts.sql"
pg_restore --list "$out/ipl_tables_$ts.dump" >/dev/null   # proves the archive is readable
echo "backup written: $out/ipl_tables_$ts.{dump,sql}"
