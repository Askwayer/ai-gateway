import os
import subprocess
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.db.base import Base

ROOT = Path(__file__).resolve().parents[1]


def test_no_business_tables_or_revisions():
    assert not Base.metadata.tables
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == []
    assert list(scripts.walk_revisions()) == []


def test_offline_empty_migration_and_company_rejection(env_root):
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
    assert "CREATE TABLE" not in result.stdout
    assert "unit-only" not in result.stdout + result.stderr
    (env_root / ".env.company").write_text("")
    process.update(APP_ENV="company", APP_ENV_FILE=str(env_root / ".env.company"))
    result = subprocess.run(
        command, env=process, cwd=ROOT, capture_output=True, text=True, timeout=10
    )
    assert result.returncode != 0
    assert "outside the approved" in result.stderr
    assert "Traceback" not in result.stderr
