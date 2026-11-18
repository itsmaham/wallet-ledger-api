"""The six concurrency and consistency rules from the brief, against the live API."""

import asyncio
import random
import statistics
import time

import pytest

from conftest import account_balance, ledger_balance, min_running_balance, new_user


async def credit(client, user_id, amount):
    return await client.post("/credit", json={"user_id": user_id, "amount": amount})


async def debit(client, user_id, amount):
    return await client.post("/debit", json={"user_id": user_id, "amount": amount})


# 1. Non-negative balance ----------------------------------------------------------

@pytest.mark.parametrize("round_", range(25))
async def test_two_racing_debits_exactly_one_wins(client, db, round_):
    uid = new_user()
    await credit(client, uid, 100)
    a, b = await asyncio.gather(debit(client, uid, 60), debit(client, uid, 60))
    assert sorted([a.status_code, b.status_code]) == [200, 409]
    assert (await client.get(f"/balance/{uid}")).json()["balance"] == 40
    assert await ledger_balance(db, uid) == 40 == await account_balance(db, uid)


async def test_many_racing_debits_never_overdraw(client, db, uid):
    await credit(client, uid, 1000)
    results = await asyncio.gather(*(debit(client, uid, 100) for _ in range(60)))
    statuses = [r.status_code for r in results]
    assert statuses.count(200) == 10
    assert statuses.count(409) == 50
    assert await account_balance(db, uid) == 0 == await ledger_balance(db, uid)
    assert await min_running_balance(db, [uid]) >= 0


# 2. No lost updates -----------------------------------------------------------------

async def test_1000_concurrent_credits(client, db, uid):
    await credit(client, uid, 1)
    results = await asyncio.gather(*(credit(client, uid, 100) for _ in range(1000)))
    assert all(r.status_code == 200 for r in results)
    # Each response is the balance right after that credit: all distinct.
    returned = sorted(r.json()["balance"] for r in results)
    assert returned == list(range(101, 100_002, 100))
    assert (await client.get(f"/balance/{uid}")).json()["balance"] == 100_001
    assert await ledger_balance(db, uid) == 100_001 == await account_balance(db, uid)
    assert await db.fetchval("SELECT count(*) FROM ledger WHERE user_id = $1", uid) == 1001


async def test_concurrent_first_credits_create_user_once(client, db, uid):
    results = await asyncio.gather(*(credit(client, uid, 7) for _ in range(200)))
    assert all(r.status_code == 200 for r in results)
    assert await account_balance(db, uid) == 1400 == await ledger_balance(db, uid)


# Mixed traffic, then reconcile (covers 1, 2 and "no silent failures") --------------

async def test_mixed_random_traffic_reconciles(client, db):
    users = [new_user() for _ in range(20)]
    await asyncio.gather(*(credit(client, u, 5_000) for u in users))
    rng = random.Random(42)

    async def one_op():
        u = rng.choice(users)
        kind = rng.random()
        if kind < 0.4:
            r = await credit(client, u, rng.randint(1, 3_000))
        elif kind < 0.8:
            r = await debit(client, u, rng.randint(1, 6_000))
        else:
            r = await client.get(f"/balance/{u}")
        return r.status_code

    statuses = await asyncio.gather(*(one_op() for _ in range(3000)))
    assert set(statuses) <= {200, 409}, f"unexpected statuses: {set(statuses)}"
    for u in users:
        api = (await client.get(f"/balance/{u}")).json()["balance"]
        assert api >= 0
        assert api == await ledger_balance(db, u) == await account_balance(db, u)
    assert await min_running_balance(db, users) >= 0


# 4. Read-your-writes -------------------------------------------------------------------

async def test_read_your_writes_sequential(client, uid):
    await credit(client, uid, 1_000_000)
    for i in range(100):
        r = await (credit(client, uid, 3) if i % 2 else debit(client, uid, 2))
        assert r.status_code == 200
        after = (await client.get(f"/balance/{uid}")).json()["balance"]
        assert after == r.json()["balance"]


async def test_read_your_writes_under_concurrency(client, uid):
    """Only credits run, so the balance only grows: a read after a write
    returned must be at least what that write reported."""
    await credit(client, uid, 1)

    async def write_then_read():
        w = await credit(client, uid, 5)
        r = await client.get(f"/balance/{uid}")
        return w.json()["balance"], r.json()["balance"]

    for written, read in await asyncio.gather(*(write_then_read() for _ in range(300))):
        assert read >= written


# 5. Isolation across users ---------------------------------------------------------------

def p95(samples):
    return statistics.quantiles(samples, n=20)[-1]


async def _timed_other_user_traffic(client, users, n):
    timings = []

    async def one(i):
        u = users[i % len(users)]
        start = time.perf_counter()
        r = await (credit(client, u, 1) if i % 2 else client.get(f"/balance/{u}"))
        timings.append(time.perf_counter() - start)
        assert r.status_code == 200

    # 10 at a time, like a handful of independent clients.
    sem = asyncio.Semaphore(10)

    async def guarded(i):
        async with sem:
            await one(i)

    await asyncio.gather(*(guarded(i) for i in range(n)))
    return timings


@pytest.mark.slow
async def test_hot_user_does_not_block_other_users(client, db):
    hot = new_user()
    others = [new_user() for _ in range(50)]
    await credit(client, hot, 10_000_000)
    await asyncio.gather(*(credit(client, u, 1) for u in others))

    baseline = await _timed_other_user_traffic(client, others, 400)

    stop = asyncio.Event()
    hot_statuses = []

    async def hammer():
        while not stop.is_set():
            r = await (credit(client, hot, 10) if random.random() < 0.5 else debit(client, hot, 10))
            hot_statuses.append(r.status_code)

    hammers = [asyncio.create_task(hammer()) for _ in range(80)]
    await asyncio.sleep(0.5)
    try:
        during = await _timed_other_user_traffic(client, others, 400)
    finally:
        stop.set()
        await asyncio.gather(*hammers)

    assert set(hot_statuses) <= {200, 409}
    assert len(hot_statuses) > 100
    # Hot-user writes serialise on one row lock; other users must not queue
    # behind them. Some slowdown from shared CPU is expected, so the bound is
    # generous, but blocking (seconds) would fail it.
    assert p95(during) < max(5 * p95(baseline), p95(baseline) + 0.25), (
        f"baseline p95={p95(baseline):.4f}s during p95={p95(during):.4f}s"
    )
    assert await account_balance(db, hot) == await ledger_balance(db, hot)
