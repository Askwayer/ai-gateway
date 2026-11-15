"""Explicit environment selection; never implicitly merges development/company files."""

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Literal, cast

from dotenv.parser import parse_stream
from pydantic import SecretStr, ValidationError

from app.core.settings import JSON_FIELDS, Settings, database_parts

Environment = Literal["development", "test", "company"]
ROOT = Path(__file__).resolve().parents[2]
AUXILIARY = frozenset(
    {
        "APP_ENV",
        "APP_ENV_FILE",
        "MIGRATION_DATABASE_URL",
        "TEST_DATABASE_URL",
        "TEST_MIGRATION_DATABASE_URL",
        "PRODUCTION_DATABASE_URL",
        "PRODUCTION_MIGRATION_DATABASE_URL",
        "AI_FAST_BASE_URL",
        "AI_FAST_MODEL",
        "AI_FAST_API_KEY",
    }
)


class ConfigurationError(ValueError):
    """Safe to display: fixed rule/field names only, no input values."""


@dataclass(frozen=True)
class LoadedConfig:
    environment: Environment
    env_file: Path
    settings: Settings


@dataclass(frozen=True)
class MigrationConfig:
    environment: Environment
    env_file: Path
    url: SecretStr


def _read_inputs(
    environment: str | None,
    env_file: str | Path | None,
    environ: Mapping[str, str] | None,
    root: Path,
) -> tuple[Environment, Path, dict[str, str]]:
    process = dict(os.environ if environ is None else environ)
    if environment and process.get("APP_ENV") and process["APP_ENV"] != environment:
        raise ConfigurationError("environment selection conflicts with APP_ENV")
    selected = environment or process.get("APP_ENV", "development")
    if selected not in ("development", "test", "company"):
        raise ConfigurationError("APP_ENV must be development, test, or company")
    if env_file is not None and process.get("APP_ENV_FILE"):
        left, right = Path(env_file), Path(process["APP_ENV_FILE"])
        if (root / left).resolve() != (root / right).resolve():
            raise ConfigurationError("environment file selection conflicts with APP_ENV_FILE")
    filename = (
        env_file
        or process.get("APP_ENV_FILE")
        or (".env.company" if selected == "company" else ".env")
    )
    path = (root / filename).resolve()
    if selected == "company" and path.name == ".env":
        raise ConfigurationError("company must not select the development .env")
    if selected != "company" and path.name == ".env.company":
        raise ConfigurationError("company file requires explicit APP_ENV=company")
    try:
        text = path.read_text()
    except (OSError, UnicodeError):
        raise ConfigurationError(
            "selected environment file is missing or unreadable; no fallback"
        ) from None
    values: dict[str, str] = {}
    for binding in parse_stream(StringIO(text)):
        if binding.error:
            raise ConfigurationError("malformed environment file")
        key = binding.key
        if key is None:
            continue
        if key not in Settings.model_fields and key not in AUXILIARY:
            raise ConfigurationError("unknown field in environment file")
        if key in values or binding.value is None:
            raise ConfigurationError("duplicate field or missing value in environment file")
        values[key] = binding.value
    # Selection comes from CLI/process only. A file cannot switch the active environment.
    if values.get("APP_ENV", selected) != selected or "APP_ENV_FILE" in values:
        raise ConfigurationError("environment file must not switch selection")
    for key in (*Settings.model_fields, "MIGRATION_DATABASE_URL"):
        if key in process:
            values[key] = process[key]
    return cast(Environment, selected), path, values


def _parse_values(values: Mapping[str, str], directory: Path) -> dict[str, object]:
    parsed: dict[str, object] = {}
    for name, field in Settings.model_fields.items():
        if name not in values:
            continue
        raw = values[name]
        try:
            if name in JSON_FIELDS:
                items = json.loads(raw)
                if not isinstance(items, list) or not all(type(item) is str for item in items):
                    raise ValueError
                parsed[name] = tuple(items)
            elif field.annotation is bool:
                if raw not in ("true", "false"):
                    raise ValueError
                parsed[name] = raw == "true"
            elif field.annotation is int:
                if not re.fullmatch(r"-?[0-9]+", raw):
                    raise ValueError
                parsed[name] = int(raw)
            elif field.annotation is float:
                parsed[name] = float(raw)
            elif name == "UPSTREAM_CA_BUNDLE" and raw != "system_trust_store":
                parsed[name] = str((directory / raw).resolve())
            else:
                parsed[name] = raw
        except (ValueError, TypeError, OverflowError):
            raise ConfigurationError(f"invalid type for {name}") from None
    return parsed


def load_config(
    *,
    environment: str | None = None,
    env_file: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    root: Path = ROOT,
) -> LoadedConfig:
    selected, path, values = _read_inputs(environment, env_file, environ, root)
    try:
        settings = Settings.model_validate(_parse_values(values, path.parent))
    except ValidationError as exc:
        # Do not log ValidationError.errors(): it can retain raw secret input.
        fields = sorted(
            {
                str(error["loc"][0]) if error["loc"] else "cross_field_contract"
                for error in exc.errors(include_input=False, include_context=False)
            }
        )
        raise ConfigurationError("configuration validation failed: " + ", ".join(fields)) from None
    _, _, database, role = database_parts(settings.DATABASE_URL.get_secret_value())
    if not settings.UPSTREAM_API_KEY.get_secret_value():
        raise ConfigurationError(
            "UPSTREAM_API_KEY is required for the selected environment's upstream"
        )
    if selected in ("development", "test"):
        if database != "ai_gateway_test" or role != "gateway_test_app":
            raise ConfigurationError(
                "development/test must use the dedicated test runtime database and role"
            )
        if settings.UPSTREAM_BASE_URL != "https://api.deepseek.com":
            raise ConfigurationError(
                "development/test upstream must be the approved DeepSeek endpoint"
            )
    else:
        if database != "ai_gateway" or role != "gateway_app":
            raise ConfigurationError("company must use its dedicated runtime database and role")
        if settings.UPSTREAM_BASE_URL != "http://172.31.34.204:5330/v1":
            raise ConfigurationError(
                "company upstream must match the approved intranet endpoint; no fallback"
            )
    return LoadedConfig(selected, path, settings)


def load_migration_config(
    *,
    environment: str | None = None,
    env_file: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    root: Path = ROOT,
) -> MigrationConfig:
    selected, path, values = _read_inputs(environment, env_file, environ, root)
    if selected == "company":
        raise ConfigurationError(
            "company migrations are outside the approved local foundation scope"
        )
    try:
        runtime = database_parts(values["DATABASE_URL"])
        url = values["MIGRATION_DATABASE_URL"]
        migration = database_parts(url)
    except (KeyError, ValueError):
        raise ConfigurationError(
            "valid runtime and migration database configurations are required"
        ) from None
    if (
        runtime[:3] != migration[:3]
        or runtime[2:] != ("ai_gateway_test", "gateway_test_app")
        or migration[3] != "gateway_test_migrator"
    ):
        raise ConfigurationError(
            "migrations require the same test database host and dedicated test migrator"
        )
    return MigrationConfig(selected, path, SecretStr(url))
