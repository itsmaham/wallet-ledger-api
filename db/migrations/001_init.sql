-- Core schema: the immutable ledger (source of truth) and one materialised
-- balance row per user, updated in the same transaction as each ledger insert.

CREATE TYPE ledger_entry_type AS ENUM ('credit', 'debit');

-- One row per user. Holds the current balance so get_balance is a single
-- primary-key lookup, whatever the size of the ledger.
--   * COLLATE "C": user ids are ASCII-only, so byte-wise comparison is correct
--     and cheaper than locale-aware collation in the btree.
--   * fillfactor 80: leaves room on each page so balance updates are HOT
--     (no indexed column changes), which avoids index churn on hot rows.
CREATE TABLE accounts (
    user_id    text COLLATE "C" PRIMARY KEY,
    balance    bigint NOT NULL CONSTRAINT accounts_balance_non_negative CHECK (balance >= 0),
    updated_at timestamptz NOT NULL DEFAULT now()
) WITH (
    fillfactor = 80,
    autovacuum_vacuum_scale_factor = 0.01,
    autovacuum_analyze_scale_factor = 0.02
);

-- Append-only ledger. Never updated or deleted (enforced by triggers below).
CREATE TABLE ledger (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    text COLLATE "C" NOT NULL
               CONSTRAINT ledger_user_id_fkey REFERENCES accounts (user_id),
    type       ledger_entry_type NOT NULL,
    amount     bigint NOT NULL CONSTRAINT ledger_amount_positive CHECK (amount > 0),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Per-user history in insertion order: serves reconciliation / statements and
-- the FK's ON DELETE check on accounts. Not used by the hot request paths.
CREATE INDEX ledger_user_id_id_idx ON ledger (user_id, id);

CREATE FUNCTION ledger_reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'ledger rows are immutable: % is not allowed', TG_OP
        USING ERRCODE = 'feature_not_supported';
END
$$;

CREATE TRIGGER ledger_no_update_delete
    BEFORE UPDATE OR DELETE ON ledger
    FOR EACH ROW EXECUTE FUNCTION ledger_reject_mutation();

CREATE TRIGGER ledger_no_truncate
    BEFORE TRUNCATE ON ledger
    FOR EACH STATEMENT EXECUTE FUNCTION ledger_reject_mutation();
