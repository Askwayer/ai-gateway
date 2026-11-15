"""Validate configuration without opening network connections or printing secrets."""

import argparse
import json

from app.core.config import ConfigurationError, LoadedConfig, load_config
from app.core.settings import database_parts
from app.db.config import load_migration_config


def check_local_development(loaded: LoadedConfig) -> None:
    """Stage-specific checks; never part of the application's settings schema."""
    if loaded.environment not in {"development", "test"}:
        raise ConfigurationError("local development requires development/test configuration")
    migration = load_migration_config(environment=loaded.environment, env_file=loaded.env_file)
    runtime = database_parts(loaded.settings.DATABASE_URL.get_secret_value())
    target = database_parts(migration.url.get_secret_value())
    if (
        runtime[:3] != target[:3]
        or runtime[2:] != ("ai_gateway_test", "gateway_test_app")
        or target[3] != "gateway_test_migrator"
    ):
        raise ConfigurationError("local development requires the dedicated test database and roles")
    if loaded.settings.UPSTREAM_BASE_URL != "https://api.deepseek.com":
        raise ConfigurationError("local development requires the approved DeepSeek endpoint")
    if not loaded.settings.UPSTREAM_API_KEY.get_secret_value():
        raise ConfigurationError("local DeepSeek validation requires UPSTREAM_API_KEY")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=("development", "test", "company"))
    parser.add_argument("--env-file")
    parser.add_argument("--profile", choices=("local-development", "company"))
    args = parser.parse_args(argv)
    try:
        environment = args.environment
        if args.profile == "company":
            if environment not in (None, "company"):
                raise ConfigurationError("company profile conflicts with selected environment")
            environment = "company"
        loaded = load_config(environment=environment, env_file=args.env_file)
        if args.profile == "local-development":
            check_local_development(loaded)
    except ConfigurationError as exc:
        print(json.dumps({"status": "invalid", "reason": str(exc)}))
        return 1
    print(
        json.dumps(
            {
                "status": "valid",
                "environment": loaded.environment,
                "profile": args.profile,
                "configuration_fields": len(type(loaded.settings).model_fields),
                "network_connections": 0,
                "milestone_signoff": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
