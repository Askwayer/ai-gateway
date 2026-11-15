"""Async migration entry point. Validates the selected database and dedicated migration identity."""

import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from app.core.config import ConfigurationError
from app.db.config import load_migration_config
from app.db.migration_guard import run_online_migration
from app.db.models import metadata


def configure(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def online() -> None:
    await run_online_migration(configure)


def run() -> None:
    if context.is_offline_mode():
        settings = load_migration_config()
        context.configure(
            url=settings.url.get_secret_value(),
            target_metadata=metadata,
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
