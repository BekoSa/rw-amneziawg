from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID
import psycopg
from psycopg.rows import dict_row
from .domain import free_address

LOCK_ID = 472319813


class Database:
    def __init__(self, url):
        self.url = url

    @asynccontextmanager
    async def connect(self, *, autocommit=False):
        async with await psycopg.AsyncConnection.connect(self.url, row_factory=dict_row, autocommit=autocommit) as conn:
            yield conn

    async def migrate(self):
        async with self.connect() as conn:
            await conn.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_ID,))
            await conn.execute('CREATE TABLE IF NOT EXISTS awg_schema_migrations(version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())')
            for path in sorted(Path(__file__).with_name('migrations').glob('*.sql')):
                version = int(path.name.split('_')[0])
                present = await (await conn.execute('SELECT 1 FROM awg_schema_migrations WHERE version=%s',(version,))).fetchone()
                if not present:
                    await conn.execute(path.read_text())

    async def allocate(self, conn, profile_id, pool, server, peer_id=None):
        # Lock per profile, cross-process; unique constraints are the final guard.
        await conn.execute('SELECT pg_advisory_xact_lock(%s)', (UUID(str(profile_id)).int % (2**63-1),))
        await conn.execute('DELETE FROM awg_ip_allocations WHERE profile_id=%s AND peer_id IS NULL AND release_after <= now()', (profile_id,))
        cursor = await conn.execute('SELECT host(address) AS address FROM awg_ip_allocations WHERE profile_id=%s', (profile_id,))
        address = free_address(pool, {r['address'] for r in await cursor.fetchall()}, server)
        await conn.execute('INSERT INTO awg_ip_allocations(profile_id,address,peer_id) VALUES(%s,%s,%s)', (profile_id,address,peer_id))
        return address
