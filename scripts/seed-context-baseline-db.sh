#!/usr/bin/env bash
# Seed the TEST postgres with a live-DB snapshot for context-engine grading.
#
# plans/substrate-context-engine.md Phase 1: the graded suite never runs
# against the live install (suite runs write benchmark experience into real
# memory). Instead the newest *valid* nightly dump is restored into a
# dedicated database on the test instance — realistic scale, zero
# contamination, perfectly re-seedable so every A/B run starts identical.
#
# Usage:
#   scripts/seed-context-baseline-db.sh [dump-file]
#
# Defaults: newest non-empty dump in ~/.thoth/backups/nightly, restored to
# postgresql://thoth:thoth@localhost:5433/thoth_baseline (the docker-compose
# test cluster). Refuses to touch port 5432 — that's the live instance.
set -euo pipefail

TEST_HOST="${THOTH_BASELINE_PG_HOST:-localhost}"
TEST_PORT="${THOTH_BASELINE_PG_PORT:-5433}"
TEST_USER="${THOTH_BASELINE_PG_USER:-thoth}"
TEST_PASSWORD="${THOTH_BASELINE_PG_PASSWORD:-thoth}"
BASELINE_DB="${THOTH_BASELINE_DB:-thoth_baseline}"
DUMP_DIR="${THOTH_NIGHTLY_DUMP_DIR:-$HOME/.thoth/backups/nightly}"
# Dumps smaller than this are treated as failed-backup artifacts (issue
# #291: a pg_dump that dies after opening its output file leaves 0 bytes).
MIN_DUMP_BYTES=1048576

if [ "$TEST_PORT" = "5432" ]; then
    echo "REFUSING: port 5432 is the LIVE instance. This script only seeds the test cluster." >&2
    exit 1
fi

DUMP="${1:-}"
if [ -z "$DUMP" ]; then
    # Newest dump that passes the size sanity check.
    while IFS= read -r candidate; do
        if [ "$(stat -c%s "$candidate")" -ge "$MIN_DUMP_BYTES" ]; then
            DUMP="$candidate"
            break
        fi
        echo "skipping undersized dump (failed backup?): $candidate" >&2
    done < <(ls -1t "$DUMP_DIR"/thoth-*.dump 2>/dev/null)
fi
if [ -z "${DUMP:-}" ] || [ ! -f "$DUMP" ]; then
    echo "ERROR: no valid dump found in $DUMP_DIR (need >= $MIN_DUMP_BYTES bytes)" >&2
    exit 1
fi
if [ "$(stat -c%s "$DUMP")" -lt "$MIN_DUMP_BYTES" ]; then
    echo "ERROR: $DUMP is undersized ($(stat -c%s "$DUMP") bytes) — refusing to seed from a failed backup" >&2
    exit 1
fi

export PGPASSWORD="$TEST_PASSWORD"
PSQL=(psql -h "$TEST_HOST" -p "$TEST_PORT" -U "$TEST_USER" -d postgres -v ON_ERROR_STOP=1 -q)

echo "Seeding $BASELINE_DB on $TEST_HOST:$TEST_PORT from $(basename "$DUMP") ($(stat -c%s "$DUMP") bytes)"
"${PSQL[@]}" -c "DROP DATABASE IF EXISTS $BASELINE_DB"
"${PSQL[@]}" -c "CREATE DATABASE $BASELINE_DB"
# --no-owner/--no-privileges: the test cluster's roles differ from live.
# Restore errors on missing extensions would be fatal via -e; the test
# image ships pgvector like live, so a clean restore is expected.
pg_restore -h "$TEST_HOST" -p "$TEST_PORT" -U "$TEST_USER" \
    --no-owner --no-privileges -d "$BASELINE_DB" "$DUMP"

echo "--- verification ---"
psql -h "$TEST_HOST" -p "$TEST_PORT" -U "$TEST_USER" -d "$BASELINE_DB" -t -c "
SELECT 'substrate_slices: ' || count(*) FROM substrate_slices
UNION ALL SELECT 'messages: ' || count(*) FROM messages
UNION ALL SELECT 'sessions: ' || count(*) FROM sessions
UNION ALL SELECT 'recall_log: ' || count(*) FROM substrate_recall_log
UNION ALL SELECT 'alembic head: ' || version_num FROM alembic_version;"
echo "Seeded. Point the graded suite at: postgresql://$TEST_USER:***@$TEST_HOST:$TEST_PORT/$BASELINE_DB"
