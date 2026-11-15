"""Short database transactions; no connection is held while waiting for the model."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.core.settings import Settings
from app.db.models import REVISION, admin_audit_logs, api_keys, projects, request_logs


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
    def __init__(self, engine: AsyncEngine, *, cleanup_timeout: float):
        self.engine = engine
        self.cleanup_timeout = cleanup_timeout

    async def ready(self) -> bool:
        async with self.engine.connect() as connection:
            revision = await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            tables = await connection.scalar(
                sa.text(
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name IN ('projects','api_keys','request_logs','health_checks','admin_audit_logs')"
                )
            )
            return bool(revision == REVISION and tables == 5)

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

    @asynccontextmanager
    async def admin_connection(self, *, write: bool = False) -> AsyncIterator[AsyncConnection]:
        # Do not use AsyncConnection.__aexit__: it waits for shielded close even
        # after cancellation. Own the connection explicitly and retain the pool
        # proxy, including when SQLAlchemy clears its reference during failure.
        connection = await self.engine.connect()
        sync_connection = connection.sync_connection
        assert sync_connection is not None
        pooled = sync_connection.connection

        def discard() -> None:
            # Deliberately outside greenlet_spawn: the pinned asyncpg dialect
            # uses synchronous driver.terminate(), not its graceful async close.
            # Hard invalidation returns the pool record without reset/rollback IO.
            if pooled.is_valid:
                pooled.invalidate()
            if not connection.closed:
                sync_connection.invalidate()
                sync_connection.close()

        try:
            yield connection
            if write:
                await connection.commit()
        except asyncio.CancelledError:
            # Cancellation during SQL/commit must propagate, even if a graceful
            # rollback would also time out. Never retry an uncertain commit.
            discard()
            raise
        finally:
            if not connection.closed:
                # One cleanup budget, also during exception unwinding. Abort
                # the socket even when driver cancellation cleanup shields IO.
                # No detached close task and no pool-wide disposal.
                expired = False

                def expire() -> None:
                    nonlocal expired
                    expired = True
                    discard()

                loop = asyncio.get_running_loop()
                deadline = loop.time() + self.cleanup_timeout
                watchdog = loop.call_at(deadline, expire)
                try:
                    async with asyncio.timeout_at(deadline):
                        await connection.close()
                    if expired:
                        raise TimeoutError
                except BaseException:
                    discard()
                    raise
                finally:
                    watchdog.cancel()

    async def key_exists(self, key_hash: str) -> bool:
        async with self.admin_connection() as connection:
            return bool(
                await connection.scalar(
                    sa.select(sa.exists().where(api_keys.c.key_hash == key_hash))
                )
            )

    async def audit(
        self,
        connection: AsyncConnection,
        resource_type: str,
        action: str,
        before: dict[str, Any],
        after: dict[str, Any],
        request_id: UUID,
    ) -> None:
        fields = {
            "code",
            "name",
            "project_id",
            "key_prefix",
            "is_active",
            "expires_at",
            "max_concurrency",
            "revoked_at",
        }

        def value(item: Any) -> Any:
            return (
                item.isoformat()
                if isinstance(item, datetime)
                else str(item)
                if isinstance(item, UUID)
                else item
            )

        changes = {
            field: {"before": value(before.get(field)), "after": value(after.get(field))}
            for field in fields
            if field in after and (not before or before.get(field) != after.get(field))
        }
        await connection.execute(
            admin_audit_logs.insert().values(
                id=uuid4(),
                actor="admin",
                action=action,
                resource_type=resource_type,
                resource_id=after["id"],
                request_id=request_id,
                changes=changes,
            )
        )

    async def create_project(self, values: dict[str, Any], request_id: UUID) -> dict[str, Any]:
        try:
            async with self.admin_connection(write=True) as connection:
                row = dict(
                    (
                        await connection.execute(
                            projects.insert().values(id=uuid4(), **values).returning(projects)
                        )
                    )
                    .mappings()
                    .one()
                )
                await self.audit(connection, "project", "project.create", {}, row, request_id)
                return row
        except IntegrityError as error:
            cause = getattr(error.orig, "__cause__", None)
            if getattr(cause, "constraint_name", None) == "uq_projects_code":
                raise ProjectConflict from None
            raise

    async def create_key(
        self, project_id: UUID, values: dict[str, Any], request_id: UUID
    ) -> dict[str, Any]:
        async with self.admin_connection(write=True) as connection:
            if not await connection.scalar(
                sa.select(projects.c.id).where(projects.c.id == project_id).with_for_update()
            ):
                raise ResourceNotFound
            row = dict(
                (
                    await connection.execute(
                        api_keys.insert()
                        .values(id=uuid4(), project_id=project_id, **values)
                        .returning(api_keys)
                    )
                )
                .mappings()
                .one()
            )
            await self.audit(connection, "key", "key.create", {}, row, request_id)
            return public_key(row)

    async def update_resource(
        self, kind: str, resource_id: UUID, values: dict[str, Any], request_id: UUID
    ) -> dict[str, Any]:
        table = projects if kind == "project" else api_keys
        async with self.admin_connection(write=True) as connection:
            original = (
                (
                    await connection.execute(
                        sa.select(table).where(table.c.id == resource_id).with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if original is None:
                raise ResourceNotFound
            row = dict(
                (
                    await connection.execute(
                        table.update()
                        .where(table.c.id == resource_id)
                        .values(**values, updated_at=sa.func.statement_timestamp())
                        .returning(table)
                    )
                )
                .mappings()
                .one()
            )
            await self.audit(connection, kind, kind + ".update", dict(original), row, request_id)
            return row if kind == "project" else public_key(row)

    async def list_resources(
        self,
        kind: str,
        project_id: UUID | None,
        cursor: tuple[datetime, UUID] | None,
        limit: int,
        active: bool | None = None,
    ) -> list[dict[str, Any]]:
        table = projects if kind == "project" else api_keys
        query = sa.select(table)
        if kind == "key":
            query = query.where(api_keys.c.project_id == project_id)
        if active is not None:
            query = query.where(table.c.is_active == active)
        if cursor:
            query = query.where(sa.tuple_(table.c.created_at, table.c.id) < cursor)
        query = query.order_by(table.c.created_at.desc(), table.c.id.desc()).limit(limit + 1)
        async with self.admin_connection() as connection:
            if kind == "key" and not await connection.scalar(
                sa.select(projects.c.id).where(projects.c.id == project_id)
            ):
                raise ResourceNotFound
            rows = [dict(row) for row in (await connection.execute(query)).mappings()]
            return rows if kind == "project" else [public_key(row) for row in rows]


class ResourceNotFound(Exception):
    pass


class ProjectConflict(Exception):
    pass


def public_key(row: dict[str, Any]) -> dict[str, Any]:
    return {
        name: row[name]
        for name in (
            "id",
            "project_id",
            "name",
            "key_prefix",
            "is_active",
            "expires_at",
            "max_concurrency",
            "created_at",
            "updated_at",
            "revoked_at",
        )
    }
