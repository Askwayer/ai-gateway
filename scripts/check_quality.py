#!/usr/bin/env python3
"""Local quality gate. No external services, secrets, or deployment operations."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    paths = [
        "app",
        "migrations",
        "tests",
        "scripts/check_quality.py",
        "scripts/check_repository.py",
        "scripts/check_development.py",
        "scripts/verify_nonstream_slice.py",
        "scripts/verify_admin_slice.py",
        "scripts/verify_database_foundation.py",
    ]
    commands = [
        ["scripts/check_repository.py"],
        ["-m", "ruff", "check", *paths],
        ["-m", "ruff", "format", "--check", *paths],
        ["-m", "mypy"],
        ["-m", "pytest", "-q"],
        ["scripts/check_docs.py"],
        ["scripts/test_check_docs.py"],
        ["scripts/p0_config_candidates.py", "--check"],
    ]
    for command in commands:
        print("Running:", " ".join(command), flush=True)
        result = subprocess.run([sys.executable, *command], cwd=ROOT, check=False)
        if result.returncode:
            return result.returncode
    print("PASS: local foundation quality checks; not A acceptance or M0/M1 sign-off.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
