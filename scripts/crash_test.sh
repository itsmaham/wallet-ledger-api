#!/usr/bin/env bash
# Atomicity under a crash: run write load, SIGKILL PostgreSQL mid-flight,
# bring it back, then reconcile every balance against the ledger.
# Requests in flight during the crash get 503s (outcome unknown to the client);
# what matters is that the data afterwards is consistent.
#
# Usage (from the repo root, stack already up and seeded):  ./scripts/crash_test.sh
set -euo pipefail

echo "starting 30s of load in the background..."
DURATION=30s LABEL=crash VUS=50 docker compose --profile loadtest run --rm loadtest >/dev/null 2>&1 &
load_pid=$!

sleep 10
echo "killing postgres with SIGKILL..."
docker compose kill -s SIGKILL db
sleep 2
echo "restarting postgres (crash recovery replays the WAL)..."
docker compose start db
until docker compose exec -T db pg_isready -U ledger -d ledger >/dev/null 2>&1; do sleep 1; done

wait "$load_pid" || true
echo "reconciling..."
docker compose exec -T db psql -U ledger -d ledger -f /db/verify.sql
