import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.db.models import REVISION, metadata

ROOT = Path(__file__).resolve().parents[1]


def test_only_approved_slice_tables_and_revision():
    assert set(metadata.tables) == {"projects", "api_keys", "request_logs"}
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == [REVISION]
    assert len(list(scripts.walk_revisions())) == 1


def test_offline_empty_migration_and_company_no_fallback(env_root):
    process = {
        k: v
        for k, v in os.environ.items()
        if k not in {"APP_ENV", "APP_ENV_FILE", "DATABASE_URL", "MIGRATION_DATABASE_URL"}
    }
    process.update(APP_ENV="test", APP_ENV_FILE=str(env_root / ".env"))
    command = [
        sys.executable,
        "-m",
        "alembic",
        "-c",
        str(ROOT / "alembic.ini"),
        "upgrade",
        "head",
        "--sql",
    ]
    result = subprocess.run(
        command, env=process, cwd=ROOT, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert "CREATE TABLE projects" in result.stdout
    assert "CREATE TABLE health_checks" not in result.stdout
    assert "REVOKE INSERT" in result.stdout
    assert "unit-only" not in result.stdout + result.stderr
    (env_root / ".env.company").write_text("")
    process.update(APP_ENV="company", APP_ENV_FILE=str(env_root / ".env.company"))
    result = subprocess.run(
        command, env=process, cwd=ROOT, capture_output=True, text=True, timeout=10
    )
    assert result.returncode != 0
    assert "company migrations are blocked" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("offline", [False, True], ids=["online", "offline-sql"])
def test_complete_company_config_rejected_by_alembic(company_env, monkeypatch, offline):
    monkeypatch.setenv("APP_ENV", "company")
    monkeypatch.setenv("APP_ENV_FILE", str(company_env))
    for name in ("DATABASE_URL", "MIGRATION_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit, match="company migrations are blocked"):
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head", sql=offline)
