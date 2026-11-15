"""Migration credentials and target checks, independent of upstream settings."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

from app.core.config import ROOT, ConfigurationError, Environment, read_environment_file
from app.core.settings import database_parts


@dataclass(frozen=True)
class MigrationConfig:
    environment: Environment
    env_file: Path
    url: SecretStr

    @property
    def expected_identity(self) -> tuple[str, str]:
        _, _, database, role = database_parts(self.url.get_secret_value())
        return database, role


def load_migration_config(
    *,
    environment: str | None = None,
    env_file: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    root: Path = ROOT,
) -> MigrationConfig:
    selected, path, values = read_environment_file(
        environment=environment, env_file=env_file, environ=environ, root=root
    )
    if selected == "company":
        raise ConfigurationError("company migrations are blocked pending explicit authorization")
    try:
        runtime = database_parts(values["DATABASE_URL"])
        url = values["MIGRATION_DATABASE_URL"]
        migration = database_parts(url)
    except (KeyError, ValueError):
        raise ConfigurationError(
            "valid runtime and migration database configurations are required"
        ) from None
    if runtime[:3] != migration[:3] or runtime[3] == migration[3]:
        raise ConfigurationError(
            "migrations require the same database host/port/name and a separate role"
        )
    return MigrationConfig(selected, path, SecretStr(url))
