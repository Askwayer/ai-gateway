import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.config_check import check_local_development, main
from app.core.config import ConfigurationError, load_config


def test_development_profile_accepts_approved_configuration(env_root):
    check_local_development(load_config(root=env_root, environ={}))


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"UPSTREAM_BASE_URL": "https://llm.example.org/v1"}, "DeepSeek endpoint"),
        ({"UPSTREAM_API_KEY": ""}, "UPSTREAM_API_KEY"),
        (
            {
                "DATABASE_URL": "postgresql+asyncpg://custom_app:unit-only@db:5432/custom_db",
                "MIGRATION_DATABASE_URL": "postgresql+asyncpg://custom_owner:unit-only@db:5432/custom_db",
            },
            "dedicated test database",
        ),
    ],
)
def test_stage_rules_live_outside_runtime(env_root, overrides, reason):
    loaded = load_config(root=env_root, environ=overrides)
    with pytest.raises(ConfigurationError, match=reason):
        check_local_development(loaded)


def test_company_is_outside_local_development_even_with_test_values(env_root):
    (env_root / ".env.company").write_text((env_root / ".env").read_text())
    env = {"APP_ENV": "company"}
    with pytest.raises(ConfigurationError, match="development/test"):
        check_local_development(load_config(root=env_root, environ=env))


def test_development_check_cli_has_no_network_operations(env_root):
    # Execute the real entry point with a dummy, unreachable DB; it must only validate.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.config_check",
            "--profile",
            "local-development",
            "--env-file",
            str(env_root / ".env"),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert '"network_connections": 0' in result.stdout
    assert "unit-only" not in result.stdout + result.stderr


def test_company_profile_checks_runtime_without_enabling_migrations(company_env, capsys):
    assert main(["--profile", "company", "--env-file", str(company_env)]) == 0
    assert '"environment": "company"' in capsys.readouterr().out
    assert main(["--profile", "company", "--environment", "test"]) == 1
    assert "conflicts" in capsys.readouterr().out
