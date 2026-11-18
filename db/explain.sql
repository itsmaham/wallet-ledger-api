-- Query plans for the three request paths, against a seeded whale user
-- (u000001, 150,000 ledger rows). Writes run inside a transaction that is
-- rolled back, so nothing is changed.
-- Run: docker compose exec db psql -U ledger -d ledger -f /db/explain.sql

\echo '== get_balance =='
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
SELECT balance FROM accounts WHERE user_id = 'u000001';

\echo '== credit =='
BEGIN;
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
WITH acc AS (
    INSERT INTO accounts AS a (user_id, balance)
    VALUES ('u000001', 10000)
    ON CONFLICT (user_id) DO UPDATE
        SET balance = a.balance + EXCLUDED.balance,
            updated_at = now()
    RETURNING a.balance
), entry AS (
    INSERT INTO ledger (user_id, type, amount)
    SELECT 'u000001', 'credit'::ledger_entry_type, 10000 FROM acc
)
SELECT balance FROM acc;
ROLLBACK;

\echo '== debit =='
BEGIN;
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
WITH acc AS (
    UPDATE accounts
       SET balance = balance - 4000,
           updated_at = now()
     WHERE user_id = 'u000001'
       AND balance >= 4000
    RETURNING balance
), entry AS (
    INSERT INTO ledger (user_id, type, amount)
    SELECT 'u000001', 'debit'::ledger_entry_type, 4000 FROM acc
)
SELECT balance FROM acc;
ROLLBACK;
