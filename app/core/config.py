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
from pydantic import ValidationError

from app.core.settings import JSON_FIELDS, Settings

Environment = Literal["development", "test", "company"]
ROOT = Path(__file__).resolve().parents[2]
TOOL_FIELDS = frozenset({"APP_ENV", "MIGRATION_DATABASE_URL"})


class ConfigurationError(ValueError):
    """Safe to display: fixed rule/field names only, no input values."""


@dataclass(frozen=True)
class LoadedConfig:
    environment: Environment
    env_file: Path
    settings: Settings


def read_environment_file(
    *,
    environment: str | None = None,
    env_file: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    root: Path = ROOT,
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
        if key not in Settings.model_fields and key not in TOOL_FIELDS:
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
    selected, path, values = read_environment_file(
        environment=environment, env_file=env_file, environ=environ, root=root
    )
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
    return LoadedConfig(selected, path, settings)
