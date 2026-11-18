-- Seed: ~10.4M ledger rows over 100,000 users at scale 1 (~1M at scale 0.1).
-- Run via db/seed.sh. Generated entirely inside PostgreSQL with
-- generate_series, so no rows cross the network.
--
-- Distribution (row counts per user, before scaling):
--   u000001 - u000010      150,000 rows each   (1.5M)   "whales"
--   u000011 - u001010        3,000 rows each   (3.0M)
--   u001011 - u100000       20-100 rows each   (~5.9M)
--
-- Rows per user alternate credit, debit, credit, debit... and each debit is
-- half of the credit just before it, so the running balance never goes below
-- zero at any point in a user's history.
--
-- This is a reset: it truncates the ledger. The truncate trigger is switched
-- off only for that one statement, inside this script's own transaction.

\set ON_ERROR_STOP on
\timing on
\if :{?scale}
\else
  \set scale 1
\endif

SET synchronous_commit = off;
SET maintenance_work_mem = '1GB';
SET work_mem = '256MB';
SET max_parallel_maintenance_workers = 4;

BEGIN;
ALTER TABLE ledger DISABLE TRIGGER ledger_no_truncate;
TRUNCATE ledger, accounts, idempotency_keys RESTART IDENTITY;
ALTER TABLE ledger ENABLE TRIGGER ledger_no_truncate;
COMMIT;

-- Bulk load without the secondary index and FK, then rebuild them once.
ALTER TABLE ledger DROP CONSTRAINT IF EXISTS ledger_user_id_fkey;
DROP INDEX IF EXISTS ledger_user_id_id_idx;

INSERT INTO ledger (user_id, type, amount, created_at)
SELECT
    'u' || lpad(u.i::text, 6, '0'),
    CASE WHEN g.k % 2 = 1 THEN 'credit'::ledger_entry_type ELSE 'debit'::ledger_entry_type END,
    CASE WHEN g.k % 2 = 1 THEN b.base ELSE b.base / 2 END,
    -- Spread over the past year, increasing within each user's history.
    now() - interval '365 days' + interval '365 days' * (g.k::float8 / c.n)
FROM generate_series(1, 100000) AS u(i)
CROSS JOIN LATERAL (
    SELECT GREATEST(1, round(:scale * CASE
        WHEN u.i <= 10   THEN 150000
        WHEN u.i <= 1010 THEN 3000
        ELSE 20 + (u.i::bigint * 2654435761 % 81)
    END))::int AS n
) AS c
CROSS JOIN LATERAL generate_series(1, c.n) AS g(k)
-- Credit k=2p-1 and debit k=2p share the same base amount (pair p).
CROSS JOIN LATERAL (
    SELECT 1000 + ((u.i::bigint * 7919 + ((g.k + 1) / 2)::bigint * 104729) % 99000) AS base
) AS b;

-- Materialised balances derived from the ledger, so they agree by construction.
INSERT INTO accounts (user_id, balance, updated_at)
SELECT user_id,
       SUM(CASE WHEN type = 'credit' THEN amount ELSE -amount END),
       max(created_at)
  FROM ledger
 GROUP BY user_id;

CREATE INDEX ledger_user_id_id_idx ON ledger (user_id, id);
ALTER TABLE ledger
    ADD CONSTRAINT ledger_user_id_fkey FOREIGN KEY (user_id) REFERENCES accounts (user_id);

-- Fresh statistics and visibility map (enables index-only scans).
VACUUM (ANALYZE) ledger;
VACUUM (ANALYZE) accounts;
