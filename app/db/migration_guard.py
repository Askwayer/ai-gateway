"""Online migration identity guard, split from Alembic env for unit testing."""

from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import ConfigurationError
from app.db.config import MigrationConfig, load_migration_config


async def run_online_migration(
    configure: Callable[[Connection], None],
    *,
    config_loader: Callable[[], MigrationConfig] = load_migration_config,
    engine_factory: Callable[..., AsyncEngine] = create_async_engine,
) -> None:
    settings = config_loader()
    engine = engine_factory(
        settings.url.get_secret_value(),
        poolclass=NullPool,
        echo=False,
        hide_parameters=True,
        connect_args={"timeout": 5, "command_timeout": 5},
    )
    try:
        async with engine.connect() as connection:
            # Catalog check after actual login protects against a misleading connection path.
            row = (await connection.execute(text("SELECT current_database(), current_user"))).one()
            if (row[0], row[1]) != settings.expected_identity:
                raise ConfigurationError(
                    "actual migration connection is not the configured migration identity"
                )
            # End the read-only identity check transaction before Alembic controls its own.
            await connection.rollback()
            await connection.run_sync(configure)
    finally:
        await engine.dispose()
