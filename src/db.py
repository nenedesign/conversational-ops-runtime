import asyncpg
from .config import settings

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(settings.database_url, min_size=2, max_size=10)
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tenants (tenant_id, name, status, created_at, updated_at)
            VALUES ($1, 'Development Tenant', 'active', NOW(), NOW())
            ON CONFLICT (tenant_id) DO NOTHING
            """,
            settings.tenant_id,
        )


async def close_pool() -> None:
    if _pool:
        await _pool.close()


def get_pool() -> asyncpg.Pool:
    assert _pool is not None, "Database pool not initialized"
    return _pool
