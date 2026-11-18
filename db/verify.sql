-- Reconciliation checks. Every count in "problems" must be 0.
-- Run: docker compose exec db psql -U ledger -d ledger -f /db/verify.sql

\echo '== size =='
SELECT (SELECT count(*) FROM ledger)   AS ledger_rows,
       (SELECT count(*) FROM accounts) AS users,
       (SELECT count(*) FROM (SELECT user_id FROM ledger GROUP BY user_id
                               HAVING count(*) >= 100000) w) AS users_with_100k_plus_rows;

\echo '== problems (all must be 0) =='
WITH ledger_balance AS (
    SELECT user_id, SUM(CASE WHEN type = 'credit' THEN amount ELSE -amount END) AS balance
      FROM ledger
     GROUP BY user_id
)
SELECT
    -- balance in accounts differs from credits minus debits in the ledger,
    -- or a user exists on one side only
    count(*) FILTER (WHERE a.user_id IS NULL OR l.user_id IS NULL
                        OR a.balance <> l.balance)          AS balance_mismatches,
    count(*) FILTER (WHERE a.balance < 0)                   AS negative_balances
  FROM accounts a
  FULL JOIN ledger_balance l ON l.user_id = a.user_id;

-- Running balance in id order. Ledger ids are assigned while the account row
-- lock is held, so per-user id order is the order writes were applied.
SELECT count(*) AS negative_running_balance_points
  FROM (SELECT SUM(CASE WHEN type = 'credit' THEN amount ELSE -amount END)
                   OVER (PARTITION BY user_id ORDER BY id) AS running
          FROM ledger) r
 WHERE running < 0;
