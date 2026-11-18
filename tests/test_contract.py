"""API contract: auth, input validation, status codes, response shapes."""

import json

import pytest

from conftest import API_TOKEN, account_balance, ledger_balance, new_user

ENDPOINTS = [("POST", "/credit"), ("POST", "/debit"), ("GET", "/balance/someone"), ("GET", "/health")]


# ---- authentication ---------------------------------------------------------

@pytest.mark.parametrize("method,path", ENDPOINTS)
@pytest.mark.parametrize("header", [
    None,
    "",
    "Bearer",
    f"Basic {API_TOKEN}",
    f"Token {API_TOKEN}",
    "Bearer wrong-token",
    f"Bearer {API_TOKEN}x",
    f"Bearer {API_TOKEN[:-1]}",
    f"Bearer  {API_TOKEN}",
])
async def test_rejects_missing_or_bad_token(anon, method, path, header):
    headers = {} if header is None else {"Authorization": header}
    body = {"user_id": "u1", "amount": 1} if method == "POST" else None
    r = await anon.request(method, path, json=body, headers=headers)
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"
    assert API_TOKEN not in r.text


async def test_auth_runs_before_validation(anon):
    r = await anon.post("/credit", content=b"not json", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


async def test_auth_on_unknown_route(anon):
    r = await anon.get("/nope")
    assert r.status_code == 401


async def test_scheme_is_case_insensitive(anon):
    r = await anon.get("/health", headers={"Authorization": f"bearer {API_TOKEN}"})
    assert r.status_code == 200


# ---- happy path -------------------------------------------------------------

async def test_credit_debit_balance_flow(client, db, uid):
    r = await client.post("/credit", json={"user_id": uid, "amount": 10000})
    assert r.status_code == 200
    assert r.json() == {"user_id": uid, "balance": 10000}

    r = await client.post("/credit", json={"user_id": uid, "amount": 15000})
    assert r.json() == {"user_id": uid, "balance": 25000}

    r = await client.post("/debit", json={"user_id": uid, "amount": 4000})
    assert r.status_code == 200
    assert r.json() == {"user_id": uid, "balance": 21000}

    r = await client.get(f"/balance/{uid}")
    assert r.status_code == 200
    assert r.json() == {"user_id": uid, "balance": 21000}
    assert r.headers["content-type"].startswith("application/json")

    rows = await db.fetch("SELECT type::text, amount FROM ledger WHERE user_id = $1 ORDER BY id", uid)
    assert [(r["type"], r["amount"]) for r in rows] == [("credit", 10000), ("credit", 15000), ("debit", 4000)]
    assert await ledger_balance(db, uid) == 21000 == await account_balance(db, uid)


async def test_debit_to_exactly_zero(client, uid):
    await client.post("/credit", json={"user_id": uid, "amount": 500})
    r = await client.post("/debit", json={"user_id": uid, "amount": 500})
    assert r.status_code == 200 and r.json()["balance"] == 0


async def test_amount_bounds_accepted(client, uid):
    assert (await client.post("/credit", json={"user_id": uid, "amount": 1})).status_code == 200
    r = await client.post("/credit", json={"user_id": uid, "amount": 1_000_000_000})
    assert r.status_code == 200 and r.json()["balance"] == 1_000_000_001


async def test_unknown_fields_ignored(client, uid):
    r = await client.post("/credit", json={"user_id": uid, "amount": 5, "currency": "KES", "note": [1]})
    assert r.status_code == 200 and r.json() == {"user_id": uid, "balance": 5}


async def test_user_id_charset_and_length(client):
    for user_id in ["a", "A-Z_az-09", "x" * 64]:
        r = await client.post("/credit", json={"user_id": user_id, "amount": 1})
        assert r.status_code == 200, user_id


# ---- not found / insufficient funds -------------------------------------------

async def test_balance_unknown_user(client, uid):
    r = await client.get(f"/balance/{uid}")
    assert r.status_code == 404
    assert r.json()["error"] == "user_not_found"
    assert "message" in r.json()


async def test_debit_unknown_user_writes_nothing(client, db, uid):
    r = await client.post("/debit", json={"user_id": uid, "amount": 1})
    assert r.status_code == 404
    assert r.json()["error"] == "user_not_found"
    assert await db.fetchval("SELECT count(*) FROM ledger WHERE user_id = $1", uid) == 0
    assert await account_balance(db, uid) is None


async def test_insufficient_funds_writes_nothing(client, db, uid):
    await client.post("/credit", json={"user_id": uid, "amount": 100})
    r = await client.post("/debit", json={"user_id": uid, "amount": 101})
    assert r.status_code == 409
    assert r.json()["error"] == "insufficient_funds"
    assert await db.fetchval("SELECT count(*) FROM ledger WHERE user_id = $1", uid) == 1
    assert (await client.get(f"/balance/{uid}")).json()["balance"] == 100


# ---- validation -------------------------------------------------------------

BAD_AMOUNTS = ["1.0", "1.5", "1e3", '"100"', "true", "false", "null", "0", "-1", "-100",
               "1000000001", "[]", "{}", "99999999999999999999999"]
BAD_USER_IDS = ['""', json.dumps("x" * 65), '"a b"', '"a/b"', '"ü"', '"a.b"', '"a\\n"', "123", "null", "[]"]


@pytest.mark.parametrize("path", ["/credit", "/debit"])
@pytest.mark.parametrize("amount", BAD_AMOUNTS)
async def test_rejects_bad_amount(client, path, amount):
    body = f'{{"user_id": "{new_user()}", "amount": {amount}}}'.encode()
    r = await client.post(path, content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 400, amount
    assert r.json()["error"] == "invalid_request"


@pytest.mark.parametrize("path", ["/credit", "/debit"])
@pytest.mark.parametrize("user_id", BAD_USER_IDS)
async def test_rejects_bad_user_id(client, path, user_id):
    body = f'{{"user_id": {user_id}, "amount": 10}}'.encode()
    r = await client.post(path, content=body)
    assert r.status_code == 400, user_id
    assert r.json()["error"] == "invalid_request"


@pytest.mark.parametrize("path", ["/credit", "/debit"])
@pytest.mark.parametrize("body", [b"", b"{", b"not json", b"[]", b"null", b'"x"', b"42",
                                  b'{"amount": 10}', b'{"user_id": "abc"}', b"{}"])
async def test_rejects_malformed_body(client, path, body):
    r = await client.post(path, content=body)
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


@pytest.mark.parametrize("user_id", ["a%20b", "a.b", "x" * 65, "%C3%BC"])
async def test_balance_rejects_bad_user_id(client, user_id):
    r = await client.get(f"/balance/{user_id}")
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


async def test_rejected_request_writes_nothing(client, db, uid):
    await client.post("/credit", content=f'{{"user_id": "{uid}", "amount": 1.5}}'.encode())
    assert await account_balance(db, uid) is None


# ---- ledger immutability ------------------------------------------------------

async def test_ledger_rows_cannot_be_changed(client, db, uid):
    import asyncpg

    await client.post("/credit", json={"user_id": uid, "amount": 10})
    with pytest.raises(asyncpg.FeatureNotSupportedError):
        await db.execute("UPDATE ledger SET amount = 1 WHERE user_id = $1", uid)
    with pytest.raises(asyncpg.FeatureNotSupportedError):
        await db.execute("DELETE FROM ledger WHERE user_id = $1", uid)


async def test_database_rejects_negative_balance(db, client, uid):
    import asyncpg

    await client.post("/credit", json={"user_id": uid, "amount": 10})
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute("UPDATE accounts SET balance = -1 WHERE user_id = $1", uid)


# ---- optional extras --------------------------------------------------------------

async def test_idempotency_key_replays_response(client, db, uid):
    headers = {"Idempotency-Key": "abc-123"}
    first = await client.post("/credit", json={"user_id": uid, "amount": 700}, headers=headers)
    again = await client.post("/credit", json={"user_id": uid, "amount": 700}, headers=headers)
    assert first.status_code == again.status_code == 200
    assert first.json() == again.json() == {"user_id": uid, "balance": 700}
    assert await db.fetchval("SELECT count(*) FROM ledger WHERE user_id = $1", uid) == 1


async def test_idempotency_key_replays_409(client, db, uid):
    await client.post("/credit", json={"user_id": uid, "amount": 10})
    headers = {"Idempotency-Key": "debit-1"}
    first = await client.post("/debit", json={"user_id": uid, "amount": 50}, headers=headers)
    await client.post("/credit", json={"user_id": uid, "amount": 100})
    again = await client.post("/debit", json={"user_id": uid, "amount": 50}, headers=headers)
    assert first.status_code == again.status_code == 409


async def test_idempotency_key_reuse_with_other_body(client, uid):
    headers = {"Idempotency-Key": "k1"}
    await client.post("/credit", json={"user_id": uid, "amount": 1}, headers=headers)
    r = await client.post("/credit", json={"user_id": uid, "amount": 2}, headers=headers)
    assert r.status_code == 422
    assert r.json()["error"] == "idempotency_key_reused"


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
