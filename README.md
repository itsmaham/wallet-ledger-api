# Wallet Ledger API

An HTTP API for a money ledger: `POST /credit`, `POST /debit`, `GET /balance/{user_id}`.
Python (Starlette + asyncpg on uvicorn) over PostgreSQL 16.

## How to run

```bash
docker compose up -d --build                         # API on http://localhost:8080
docker compose --profile seed run --rm seed          # ~10.4M ledger rows, 100k users (resets data)
docker compose --profile test run --rm tests         # contract + concurrency tests
docker compose --profile loadtest run --rm loadtest  # k6, 100 clients, p50/p99 per endpoint
```

The token is `API_TOKEN` (default `recruitment-test-token` in `docker-compose.yml`; override with
`API_TOKEN=... docker compose up`). Other useful commands:

```bash
docker compose exec db psql -U ledger -d ledger -f /db/verify.sql   # reconcile balances vs ledger
docker compose exec db psql -U ledger -d ledger -f /db/explain.sql  # query plans
SEED_SCALE=0.1 docker compose --profile seed run --rm seed          # ~1M rows instead of 10M
./scripts/crash_test.sh                                             # SIGKILL postgres under load, then reconcile
```

```bash
curl -s -H "Authorization: Bearer recruitment-test-token" -H "Content-Type: application/json" \
     -d '{"user_id":"u123","amount":10000}' http://localhost:8080/credit
```

PostgreSQL is also published on `127.0.0.1:55432` (user/password/db `ledger`).

## Layout

| Path | What |
|---|---|
| `app/main.py` | routes, error mapping, startup (migrations, pool) |
| `app/ledger.py` | the three operations and their SQL, retry loop, idempotency |
| `app/auth.py` | bearer-token middleware (constant-time compare, runs before routing) |
| `app/validation.py` | input rules |
| `app/limiter.py` | per-user in-flight write limit (hot-user isolation) |
| `db/migrations/` | schema, indexes, immutability triggers; applied on API startup |
| `db/seed.sql`, `db/seed.sh` | seed (in-database `generate_series`) |
| `db/verify.sql` | reconciliation queries |
| `db/explain.sql` | `EXPLAIN ANALYZE` for the three request paths |
| `tests/` | pytest suite run against the live stack |
| `loadtest/k6.js` | load test; results written to `loadtest/results/` |

## Design

### Schema

```
accounts(user_id text PK, balance bigint CHECK (balance >= 0), updated_at)
ledger(id bigint identity PK, user_id → accounts, type enum('credit','debit'),
       amount bigint CHECK (amount > 0), created_at timestamptz default now())
index ledger(user_id, id)
```

* **The ledger is the source of truth; `accounts.balance` is a materialised running total**
  updated in the *same statement / transaction* as the ledger insert. So
  `balance == Σcredits − Σdebits` holds after every commit, and `get_balance` is a primary-key
  lookup whose cost does not depend on ledger size.
* Ledger rows are immutable: `BEFORE UPDATE OR DELETE` and `BEFORE TRUNCATE` triggers raise.
* `CHECK (balance >= 0)` makes a negative balance impossible even if application code were wrong.
* `user_id` uses `COLLATE "C"` (ids are ASCII; byte comparison is correct and faster).
* `accounts` has `fillfactor=80` so balance updates are HOT updates (no index writes), and
  aggressive autovacuum settings because its rows are rewritten constantly.
* `ledger(user_id, id)` serves per-user history and the reconciliation queries; the request paths
  do not read the ledger at all.

### The write path (one round trip, one statement)

Credit:

```sql
WITH acc AS (
    INSERT INTO accounts AS a (user_id, balance) VALUES ($1, $2)
    ON CONFLICT (user_id) DO UPDATE SET balance = a.balance + EXCLUDED.balance, updated_at = now()
    RETURNING a.balance
), entry AS (
    INSERT INTO ledger (user_id, type, amount) SELECT $1, 'credit', $2 FROM acc
)
SELECT balance FROM acc;
```

Debit:

```sql
WITH acc AS (
    UPDATE accounts SET balance = balance - $2, updated_at = now()
     WHERE user_id = $1 AND balance >= $2
    RETURNING balance
), entry AS (
    INSERT INTO ledger (user_id, type, amount) SELECT $1, 'debit', $2 FROM acc
)
SELECT balance FROM acc;
```

If the debit returns no row, nothing was written; a follow-up PK read tells `404 user_not_found`
apart from `409 insufficient_funds`.

### Why it is safe under concurrency

* **Isolation level: READ COMMITTED** (PostgreSQL's default) plus the **row lock** on the user's
  `accounts` row taken by the `UPDATE` / `ON CONFLICT DO UPDATE`.
* Concurrent writes to the same user queue on that lock. When a waiting `UPDATE` gets the lock,
  PostgreSQL re-evaluates `balance >= $2` against the newly committed row (READ COMMITTED's
  re-check), so two debits of 60 on a balance of 100 give exactly one `200` and one `409`, and
  `balance = balance + $2` can never lose an update.
* `ON CONFLICT DO UPDATE` makes "create the user on first credit" race-free: 200 concurrent first
  credits create one row.
* The ledger insert selects `FROM acc`, so it runs only after the lock is held (and not at all if
  the debit guard failed). Ledger ids are therefore handed out in the order each user's writes are
  applied, which is what makes the running-balance check in `verify.sql` meaningful.
* Each statement locks exactly **one** row, so these transactions cannot deadlock. A retry loop for
  `40001` / `40P01` (with jittered backoff) exists as a safety net; such errors roll the transaction
  back, so a retry cannot double-apply.
* **Atomicity:** the ledger row and the balance change are one statement, one transaction. A crash
  before commit leaves neither; after commit (`synchronous_commit=on`) both survive.
  `scripts/crash_test.sh` SIGKILLs PostgreSQL under load and then reconciles.
* **Read-your-writes:** every request goes to the one primary; a `200` is sent only after commit, so
  any later read sees it. Reads take no locks and are never blocked by writers (MVCC).
* **No silent failures:** expected outcomes are 400/401/404/409. If the database is unreachable or a
  connection dies mid-request the API answers `503 service_unavailable` (for a write in flight, the
  outcome is unknown to the client: that is what `Idempotency-Key` is for), never a wrong balance.

### Hot-user isolation

Writes for one user serialise on one row, so throughput for a single user is bounded by commit
latency. The danger is that 100 requests for that user each hold a pool connection while waiting
for the lock, starving every other user. `app/limiter.py` caps in-flight writes per user per worker
(`PER_USER_WRITE_CONCURRENCY=2`); extra requests for the hot user wait in memory, *before* taking a
connection, so the pool stays available to everyone else. Reads are not limited (they never wait
on locks).

### Alternatives considered and rejected

| Choice | Rejected alternative | Why rejected |
|---|---|---|
| Materialised balance in `accounts` | `SUM()` over the user's ledger rows per request | Scans a user's whole history (150k rows for the biggest users); the brief rules it out. |
| | Periodic snapshots + sum of rows since the snapshot | Bounded but still variable cost, plus a snapshot job and more failure modes. |
| | `balance_after` column on each ledger row | Still needs a per-user lock to read the latest row; same cost, more storage, harder to check. |
| READ COMMITTED + row lock | SERIALIZABLE | Hot-user traffic would produce many serialization failures and retries → worse p99 for no extra safety here. |
| | `SELECT … FOR UPDATE` then `UPDATE` then `INSERT` in an explicit transaction | Same locking, but 4–5 round trips instead of 1; the CTE is the same logic in one statement. |
| | Advisory locks per user | Extra mechanism to get right; the row lock already exists for free. |
| | Optimistic concurrency (version column + retry) | Under a hot user most attempts conflict and retry → latency spikes. |
| Writes in the database | In-memory balances / queue per user in the app | Breaks as soon as there are several API instances, and loses atomicity with the ledger. |
| `text` user_id in ledger | Surrogate `bigint` account id | Smaller rows and index, but adds a lookup per request and makes the reviewers' SQL less direct. Worth it at a larger scale. |
| Python/Starlette | FastAPI | FastAPI's validation returns 422 with its own shape; the brief needs 400 with a custom body, and Starlette alone has less per-request overhead. |

### Other details

* Auth is ASGI middleware that runs before routing and before any database access; the token is
  read from `API_TOKEN` (the app refuses to start without it), compared with `hmac.compare_digest`,
  never logged (access logs are off) and never echoed. The scheme is case-insensitive per RFC 7235.
* Amount validation uses `type(x) is int`: Python's `bool` is an `int` subclass, so `true` must be
  rejected explicitly; `1.0` and `1e3` decode as floats and are rejected.
* Migrations run on API startup under a PostgreSQL advisory lock, so `docker compose up` is the
  only step and several workers do not race.
* Optional extras: `Idempotency-Key` on credit/debit (stored in `idempotency_keys` in the same
  transaction as the write; a concurrent duplicate waits on the key's row and then replays the stored
  response; reusing a key with a different body gives `422 idempotency_key_reused`), `GET /health`
  (also authenticated, as the brief says every endpoint is), graceful shutdown (uvicorn drains
  in-flight requests on SIGTERM; the pool is closed in the lifespan hook).

## Seed

`db/seed.sql` generates everything inside PostgreSQL with `generate_series`, loads the ledger with
the FK and secondary index dropped, derives `accounts` from the ledger with one `GROUP BY`, rebuilds
the index and FK, then `VACUUM ANALYZE`.

| Users | Rows each | Total |
|---|---|---|
| `u000001`–`u000010` | 150,000 | 1.5M |
| `u000011`–`u001010` | 3,000 | 3.0M |
| `u001011`–`u100000` | 20–100 | ~5.9M |

Each user's rows alternate credit, debit, credit… and every debit is half the credit just before it,
so no balance is ever negative at any point in history. `accounts` is computed from the ledger, so
it agrees by construction; `seed.sh` runs `verify.sql` at the end to prove it.

**Seed time:** _TODO: fill in from `seed.sh` output ("seed finished in Ns") and the machine spec._

## Query plans

_TODO: paste the output of `docker compose exec db psql -U ledger -d ledger -f /db/explain.sql`
at 10M rows._ Expected shape:

* `get_balance`: `Index Scan using accounts_pkey on accounts` — one index probe, a few buffers.
* `credit`: `Insert on accounts` with `Conflict Resolution: UPDATE` via `accounts_pkey`, then
  `Insert on ledger` (appends at the end of the heap; maintains `ledger_pkey` and
  `ledger_user_id_id_idx`, and the FK check is a PK probe on `accounts`).
* `debit`: `Update on accounts` → `Index Scan using accounts_pkey`, then `Insert on ledger` as above.

None of them read the ledger, so their cost does not grow with the table (only the two btree
inserts, which are O(log n)).

## Load test results

`loadtest/k6.js`: 100 virtual users for 60s of mixed traffic (50% balance, 30% credit, 20% debit
over random seeded users), then 60s with 50 users on one hot user and 50 on everyone else.
404/409 count as expected.

_TODO: paste `loadtest/results/1M.md` and `loadtest/results/10M.md`:_

```bash
SEED_SCALE=0.1 docker compose --profile seed run --rm seed
LABEL=1M docker compose --profile loadtest run --rm loadtest
docker compose --profile seed run --rm seed
LABEL=10M docker compose --profile loadtest run --rm loadtest
docker compose exec db psql -U ledger -d ledger -f /db/verify.sql   # all problem counts must be 0
```

## Tests

`tests/test_contract.py`: auth on every endpoint (missing, wrong scheme, wrong/near-miss token,
before validation, on unknown routes), every input rule (floats, `true`, strings, 0, negatives,
over the max, huge integers, bad user ids, malformed / non-object JSON), 404 / 409 with nothing
written, response shapes, ledger immutability, DB-level non-negative check, idempotency.

`tests/test_concurrency.py`, one test per rule in the brief:

1. two racing debits of 60 on 100 → exactly one 200 and one 409 (25 rounds); 60 debits of 100 on
   1,000 → exactly 10 succeed;
2. 1,000 concurrent credits of 100 → +100,000, and the 1,000 returned balances are all distinct;
   200 concurrent *first* credits create the user once;
3. mixed random traffic over 20 users → only 200/409, balances = ledger sums, running balance never
   negative (atomicity under a real crash: `scripts/crash_test.sh`);
4. read-your-writes, sequential and concurrent;
5. a hot user hammered by 80 clients while other users' p95 latency is measured against a baseline;
6. no 500s anywhere (asserted in every concurrent test).

## Known limits

* **At 10× load, what breaks first:** the single hot row. All writes for one user serialise on one
  row lock, so one user's throughput is capped at roughly 1 / (commit latency) — a few hundred to
  a few thousand writes/s depending on disk fsync. Next is WAL fsync for overall write volume
  (mitigations: group commit via `commit_delay`, faster disks), then CPU on the Python workers.
* **Several API instances:** correctness is unchanged — it lives entirely in the database (row
  locks, constraints, one statement per write). What changes: the per-user limiter becomes
  per-instance (the cap is instances × workers × 2 connections per hot user), connection count
  multiplies so PgBouncer in transaction mode would be needed (asyncpg then needs
  `statement_cache_size=0` or PgBouncer ≥ 1.21 prepared-statement support), and migrations are
  already safe because of the advisory lock.
* `idempotency_keys` grows forever; it needs a TTL cleanup job.
* The seed resets data (it truncates the ledger with the immutability trigger switched off for that
  one statement). It is a setup tool, not something to run against live data.

## With more time

* Partition `ledger` by time (or hash of user) to keep indexes and vacuum manageable well past 10M.
* A surrogate `bigint` account id to shrink the ledger and its index.
* For extreme hot users: batch their writes in the app (one transaction applying N queued
  operations), or split a hot wallet into sub-balances.
* PgBouncer, metrics (latency histograms, lock waits), and a nightly reconciliation job that alerts
  on any mismatch.
* Read replicas for `get_balance` traffic, with a read-your-writes token (LSN) so a client never
  reads a replica that is behind its own write.
