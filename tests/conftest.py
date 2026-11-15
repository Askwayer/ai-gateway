from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def config_values() -> dict[str, Any]:
    return {
        "DATABASE_URL": "postgresql+asyncpg://gateway_test_app:unit-only@127.0.0.1:13432/ai_gateway_test",
        "ADMIN_API_KEY": "unit-admin-" + "a" * 43,
        "UPSTREAM_BASE_URL": "https://api.deepseek.com",
        "UPSTREAM_API_KEY": "unit-upstream",
        "ALLOWED_MODELS": ("deepseek-v4-flash",),
        "UPSTREAM_HEALTH_PATH": "/models",
        "INFERENCE_PROBE_MODEL": "deepseek-v4-flash",
    }


@pytest.fixture
def env_root(tmp_path: Path) -> Path:
    (tmp_path / ".env").write_text(
        "DATABASE_URL=postgresql+asyncpg://gateway_test_app:unit-only@127.0.0.1:13432/ai_gateway_test\n"
        "MIGRATION_DATABASE_URL=postgresql+asyncpg://gateway_test_migrator:unit-only@127.0.0.1:13432/ai_gateway_test\n"
        "ADMIN_API_KEY=" + "a" * 43 + "\n"
        "UPSTREAM_BASE_URL=https://api.deepseek.com\n"
        "UPSTREAM_API_KEY=unit-upstream\n"
        'ALLOWED_MODELS=["deepseek-v4-flash"]\n'
        "INFERENCE_PROBE_MODEL=deepseek-v4-flash\n"
        "UPSTREAM_HEALTH_PATH=/models\n"
    )
    return tmp_path


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("unit tests must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
