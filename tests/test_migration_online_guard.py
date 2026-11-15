import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from app.core.config import ConfigurationError
from app.db.config import MigrationConfig, load_migration_config
from app.db.migration_guard import run_online_migration

APPROVED = ("custom_db", "custom_owner")


def test_company_is_blocked_before_engine_creation(company_env):
    def forbidden_engine(*args, **kwargs):
        pytest.fail("company migration must not create an engine")

    async def scenario():
        with pytest.raises(ConfigurationError, match="company migrations are blocked"):
            await run_online_migration(
                lambda connection: pytest.fail("must not configure migrations"),
                config_loader=lambda: load_migration_config(
                    environment="company", env_file=company_env, environ={}
                ),
                engine_factory=forbidden_engine,
            )

    asyncio.run(scenario())


class FakeResult:
    def __init__(self, row: tuple[str, str]) -> None:
        self.row = row

    def one(self) -> tuple[str, str]:
        return self.row


class FakeConnection:
    def __init__(self, row: tuple[str, str] | None = None, error: Exception | None = None):
        self.row = row
        self.error = error
        self.closed = False
        self.events: list[str] = []

    async def __aenter__(self) -> "FakeConnection":
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed = True

    async def execute(self, statement: Any) -> FakeResult:
        if self.error is not None:
            raise self.error
        assert self.row is not None
        return FakeResult(self.row)

    async def rollback(self) -> None:
        self.events.append("rollback")

    async def run_sync(self, fn: Callable[["FakeConnection"], None]) -> None:
        self.events.append("run_sync")
        fn(self)


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.disposed = False

    def connect(self) -> FakeConnection:
        return self.connection

    async def dispose(self) -> None:
        self.disposed = True


def _migration_config() -> MigrationConfig:
    return MigrationConfig(
        environment="test",
        env_file=Path(".env"),
        url=SecretStr("postgresql+asyncpg://custom_owner:unit-only@127.0.0.1:1/custom_db"),
    )


def _fixture(
    row: tuple[str, str] | None = None, error: Exception | None = None
) -> tuple[FakeConnection, FakeEngine, list[FakeConnection]]:
    connection = FakeConnection(row=row, error=error)
    engine = FakeEngine(connection)
    configured: list[FakeConnection] = []
    return connection, engine, configured


@pytest.mark.parametrize("identity", [("custom_db", "custom_app"), ("wrong_db", "custom_owner")])
def test_identity_mismatch_is_rejected_and_releases_connection(identity) -> None:
    connection, engine, configured = _fixture(identity)

    async def scenario() -> None:
        with pytest.raises(ConfigurationError, match="configured migration identity"):
            await run_online_migration(
                lambda value: configured.append(value),
                config_loader=_migration_config,
                engine_factory=lambda *args, **kwargs: engine,
            )

    asyncio.run(scenario())

    assert connection.closed
    assert engine.disposed
    assert connection.events == []
    assert configured == []


def test_approved_identity_rolls_back_before_configuring() -> None:
    connection, engine, configured = _fixture(APPROVED)

    async def scenario() -> None:
        await run_online_migration(
            lambda value: configured.append(value),
            config_loader=_migration_config,
            engine_factory=lambda *args, **kwargs: engine,
        )

    asyncio.run(scenario())

    assert connection.closed
    assert engine.disposed
    assert connection.events == ["rollback", "run_sync"]
    assert configured == [connection]


def test_identity_query_failure_releases_connection_and_engine() -> None:
    connection, engine, configured = _fixture(error=RuntimeError("database unavailable"))

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="database unavailable"):
            await run_online_migration(
                lambda value: configured.append(value),
                config_loader=_migration_config,
                engine_factory=lambda *args, **kwargs: engine,
            )

    asyncio.run(scenario())

    assert connection.closed
    assert engine.disposed
    assert configured == []
