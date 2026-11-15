"""Short database transactions; no connection is held while waiting for the model."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.settings import Settings
from app.db.models import REVISION, api_keys, projects, request_logs


@dataclass(frozen=True)
class Identity:
    project_id: UUID
    key_id: UUID
    project_limit: int
    key_limit: int


def make_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.DATABASE_URL.get_secret_value(),
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_OPERATION_TIMEOUT,
        pool_pre_ping=True,
        echo=False,
        hide_parameters=True,
        connect_args={
            "timeout": settings.DB_OPERATION_TIMEOUT,
            "command_timeout": settings.DB_QUERY_TIMEOUT,
        },
    )


class Repository:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    async def ready(self) -> bool:
        async with self.engine.connect() as connection:
            revision = await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            tables = await connection.scalar(
                sa.text(
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name IN ('projects','api_keys','request_logs')"
                )
            )
            return bool(revision == REVISION and tables == 3)

    async def authenticate(self, key_hash: str) -> Identity | None:
        query = (
            sa.select(
                projects.c.id,
                api_keys.c.id,
                projects.c.max_concurrency,
                api_keys.c.max_concurrency,
            )
            .join(api_keys, api_keys.c.project_id == projects.c.id)
            .where(
                api_keys.c.key_hash == key_hash,
                api_keys.c.is_active.is_(True),
                projects.c.is_active.is_(True),
                api_keys.c.revoked_at.is_(None),
                sa.or_(
                    api_keys.c.expires_at.is_(None),
                    api_keys.c.expires_at > sa.func.statement_timestamp(),
                ),
            )
        )
        async with self.engine.connect() as connection:
            row = (await connection.execute(query)).one_or_none()
        return None if row is None else Identity(*row)

    async def register(self, values: dict[str, Any]) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(request_logs.insert().values(**values))

    async def mark_upstream(self, request_id: UUID) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                request_logs.update()
                .where(
                    request_logs.c.request_id == request_id,
                    request_logs.c.outcome == "started",
                )
                .values(
                    upstream_started_at=sa.func.statement_timestamp(),
                    updated_at=sa.func.statement_timestamp(),
                )
            )

    async def settle(self, request_id: UUID, values: dict[str, Any]) -> bool:
        async with self.engine.begin() as connection:
            result = await connection.execute(
                request_logs.update()
                .where(
                    request_logs.c.request_id == request_id,
                    request_logs.c.outcome == "started",
                )
                .values(
                    **values,
                    finished_at=sa.func.statement_timestamp(),
                    updated_at=sa.func.statement_timestamp(),
                )
                .returning(request_logs.c.request_id)
            )
            return result.scalar_one_or_none() is not None
