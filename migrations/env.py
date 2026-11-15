"""Empty async migration scaffold. Only the isolated test database is permitted."""

import asyncio

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import ConfigurationError, load_migration_config
from app.db.base import Base


def configure(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def online() -> None:
    settings = load_migration_config()
    engine = create_async_engine(
        settings.url.get_secret_value(),
        poolclass=NullPool,
        echo=False,
        hide_parameters=True,
        connect_args={"timeout": 5, "command_timeout": 5},
    )
    try:
        async with engine.connect() as connection:
            # Catalog check after actual login protects against a misleading connection path.
            from sqlalchemy import text

            row = (await connection.execute(text("SELECT current_database(), current_user"))).one()
            if tuple(row) != ("ai_gateway_test", "gateway_test_migrator"):
                raise ConfigurationError(
                    "actual migration connection is not the approved test identity"
                )
            # End the read-only identity check transaction before Alembic controls its own.
            await connection.rollback()
            await connection.run_sync(configure)
    finally:
        await engine.dispose()


def run() -> None:
    if context.is_offline_mode():
        settings = load_migration_config()
        context.configure(
            url=settings.url.get_secret_value(),
            target_metadata=Base.metadata,
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
        )
        with context.begin_transaction():
            context.run_migrations()
    else:
        asyncio.run(online())


try:
    run()
except ConfigurationError as exc:
    raise SystemExit(str(exc)) from None
except Exception as exc:
    # Driver/SQLAlchemy exception messages may contain URLs or database parameters.
    raise SystemExit(
        f"migration command failed: {type(exc).__name__}; details suppressed"
    ) from None
