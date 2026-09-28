"""Real PostgreSQL tests; run only by the Docker tooling service."""
import asyncio
import os
from uuid import uuid4
import pytest
from awg_controller.db import Database


@pytest.mark.asyncio
async def test_migration_idempotent_and_pool_race_safe():
    url = os.environ.get('TEST_DATABASE_URL')
    if not url:
        pytest.skip('requires isolated PostgreSQL')
    db = Database(url)
    await db.migrate()
    await db.migrate()
    profile = uuid4()
    async with db.connect() as conn:
        await conn.execute('INSERT INTO awg_profiles(id,draft) VALUES(%s,%s)', (profile, '{}'))
    # Reserve addresses without a peer FK, modeling the allocation transaction.
    async def allocate():
        async with db.connect() as conn:
            return await db.allocate(conn, profile, '10.89.0.0/29', '10.89.0.1')
    addresses = await asyncio.gather(*[allocate() for _ in range(5)])
    assert len(set(addresses)) == 5
    with pytest.raises(ValueError, match='exhausted'):
        await allocate()
    async with db.connect() as conn:
        await conn.execute('DELETE FROM awg_ip_allocations WHERE profile_id=%s', (profile,))
        await conn.execute('DELETE FROM awg_profiles WHERE id=%s', (profile,))
