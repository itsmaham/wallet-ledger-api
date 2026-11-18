-- Optional Idempotency-Key support for POST /credit and POST /debit.
-- A key is scoped to a user. The row is claimed and the response stored in the
-- same transaction as the ledger write, so a replay can never apply twice.
CREATE TABLE idempotency_keys (
    user_id     text COLLATE "C" NOT NULL,
    idem_key    text COLLATE "C" NOT NULL,
    operation   ledger_entry_type NOT NULL,
    amount      bigint NOT NULL,
    status_code smallint,
    response    jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, idem_key)
);
