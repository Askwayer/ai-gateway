import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.settings import Settings, packaged_timezone

ROOT = Path(__file__).resolve().parents[1]


def test_contract_covers_all_design_fields(config_values):
    source = (ROOT / "docs/AI-Gateway-项目开发详细设计书-v1.6.md").read_text()
    block = source.split("<!-- CONFIG_BEGIN -->")[1].split("<!-- CONFIG_END -->")[0]
    names = set(re.findall(r"^\| ([A-Z][A-Z0-9_]+) \|", block, re.M))
    assert names == set(Settings.model_fields)
    assert len(names) == 80
    settings = Settings(**config_values)
    assert settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW == 36
    assert settings.upstream_url("/models") == "https://api.deepseek.com/models"


@pytest.mark.parametrize(
    "changes",
    [
        {"GLOBAL_MAX_CONCURRENCY": True},
        {"GLOBAL_MAX_CONCURRENCY": 1.5},
        {"PROBE_MIN_SPARE_SLOTS": 0},
        {"DB_MAX_OVERFLOW": -1},
        {"HTTP_CONNECT_TIMEOUT": float("nan")},
        {"HTTP_CONNECT_TIMEOUT": float("inf")},
        {"SETTLEMENT_RETRY_BACKOFF": -0.1},
        {"SERVICE_UNAVAILABLE_RETRY_AFTER": 3601},
        {"CONCURRENCY_RETRY_AFTER": 0},
        {"USAGE_MAX_GROUP_DIMENSIONS": 3},
        {"PAGINATION_DEFAULT_LIMIT": 201},
        {"AUTH_DB_MAX_INFLIGHT": 15},
        {"INGRESS_IP_BURST": 15},
        {"INGRESS_GLOBAL_BURST": 15},
        {"HTTP_MAX_CONNECTIONS": 16},
        {"DB_MAX_OVERFLOW": 19},
        {"SETTLEMENT_ATTEMPT_TIMEOUT": 2.1},
        {"CLEANUP_TIMEOUT": 3.6},
        {"UVICORN_GRACEFUL_TIMEOUT": 29},
        {"HOST_STOP_TIMEOUT": 37},
        {"HEALTH_STALE_AFTER": 33},
        {"DB_READY_STALE_AFTER": 7},
        {"INFERENCE_PROBE_STALE_AFTER": 330},
        {"RECOVERY_GRACE_PERIOD": 4},
        {"HEALTH_REACHABILITY_ENABLED": False},
        {"UPSTREAM_HEALTH_PATH": ""},
        {"UPSTREAM_HEALTH_PATH": "//elsewhere/path"},
        {"UPSTREAM_HEALTH_PATH": "/models?token=anything"},
        {"UPSTREAM_HEALTH_PATH": "/../admin"},
        {"UPSTREAM_HEALTH_PATH": "/%2fhost"},
        {"INFERENCE_PROBE_MODEL": "not-allowed"},
        {"INFERENCE_PROBE_PROMPT": " "},
        {"ALLOWED_MODELS": ()},
        {"ALLOWED_MODELS": ("same", "same")},
        {"ALLOWED_MODELS": ("bad\nmodel",)},
        {"UPSTREAM_STREAM_USAGE_MODE": "guess"},
        {"TZDATA_SOURCE": "system"},
        {"DEPENDENCY_LOCK_TOOL": "pip"},
        {"DEFAULT_TIMEZONE": "Europe/London"},
        {"ALLOWED_TIMEZONES": ("UTC", "Europe/London")},
        {"ALLOWED_TIMEZONES": ("UTC", "UTC")},
        {"ALLOWED_TIMEZONES": ()},
        {"TRUSTED_PROXY_CIDRS": ("0.0.0.0/0",)},
        {"TRUSTED_PROXY_CIDRS": ("::/0",)},
        {"TRUSTED_PROXY_CIDRS": ("*",)},
        {"TRUSTED_PROXY_CIDRS": ("192.0.2.1/24",)},
        {"TRUSTED_PROXY_CIDRS": ("::ffff:0.0.0.0/96",)},
        {"TRUSTED_PROXY_CIDRS": ("127.0.0.1", "::ffff:127.0.0.1/128")},
        {"UPSTREAM_BASE_URL": "https://user:secret@api.deepseek.com"},
        {"UPSTREAM_BASE_URL": "https://api.deepseek.com?redirect=other"},
        {"UPSTREAM_BASE_URL": "ftp://api.deepseek.com"},
        {"UPSTREAM_BASE_URL": "http://localhost:99999"},
        {"UPSTREAM_BASE_URL": "http://local\nhost"},
        {"DATABASE_URL": "postgresql://user:secret@host:5432/db"},
        {"DATABASE_URL": "postgresql+asyncpg://user:secret@host/db"},
        {"DATABASE_URL": "postgresql+asyncpg://user:secret@host:5432/db?database=other"},
        {"ADMIN_API_KEY": "short"},
        {"ADMIN_API_KEY": "a" * 43 + "\n"},
        {"UPSTREAM_API_KEY": "key\r\nheader"},
        {"ADMIN_API_KEY": "a" * 43, "UPSTREAM_API_KEY": "a" * 43},
        {"HEALTH_GATE_ENABLED": "false"},
        {"unexpected_field": 1},
    ],
)
def test_invalid_contract_is_rejected(config_values, changes):
    with pytest.raises(ValidationError):
        Settings(**(config_values | changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"ALLOWED_MODELS": ("P0必填",)},
        {"ALLOWED_MODELS": ("P0 必填",)},
        {"ALLOWED_MODELS": ("必填模型列表",)},
        {"ALLOWED_MODELS": ("P0必填",), "INFERENCE_PROBE_MODEL": "P0必填"},
        {"INFERENCE_PROBE_PROMPT": "P0必填"},
        {"UPSTREAM_API_KEY": "空"},
        {"ADMIN_API_KEY": "必填高熵随机值"},
        {"UPSTREAM_BASE_URL": "必填内网固定地址"},
        {"UPSTREAM_HEALTH_PATH": "P0必填"},
        {"UPSTREAM_HEALTH_PATH": "P0 必填"},
        {"UPSTREAM_CA_BUNDLE": "系统信任库"},
        {"DATABASE_URL": "postgresql+asyncpg://..."},
    ],
)
def test_placeholder_values_are_rejected(config_values, changes):
    with pytest.raises(ValidationError):
        Settings(**(config_values | changes))


def test_disabled_probes_still_validate_supplied_values(config_values):
    disabled = config_values | {
        "HEALTH_REACHABILITY_ENABLED": False,
        "HEALTH_GATE_ENABLED": False,
        "UPSTREAM_HEALTH_PATH": "",
        "INFERENCE_PROBE_ENABLED": False,
        "INFERENCE_PROBE_MODEL": "",
        "INFERENCE_PROBE_PROMPT": "",
    }
    assert not Settings(**disabled).INFERENCE_PROBE_ENABLED
    with pytest.raises(ValidationError):
        Settings(**(disabled | {"UPSTREAM_HEALTH_PATH": "//bad"}))
    with pytest.raises(ValidationError):
        Settings(**(disabled | {"INFERENCE_PROBE_TIMEOUT": -1}))


def test_secrets_are_hidden(config_values):
    settings = Settings(**config_values)
    for key in ("ADMIN_API_KEY", "UPSTREAM_API_KEY", "DATABASE_URL"):
        assert config_values[key] not in repr(settings)
        assert config_values[key] not in settings.model_dump_json()
    with pytest.raises(ValidationError) as caught:
        Settings(**(config_values | {"ADMIN_API_KEY": "sensitive-invalid"}))
    assert "sensitive-invalid" not in str(caught.value)


def test_invalid_ca_and_locked_timezones(config_values, tmp_path):
    fake_ca = tmp_path / "not-ca.pem"
    fake_ca.write_text("not a certificate")
    for value in (str(tmp_path / "missing"), str(fake_ca)):
        with pytest.raises(ValidationError):
            Settings(**(config_values | {"UPSTREAM_CA_BUNDLE": value}))
    assert packaged_timezone("UTC").key == "UTC"
    assert packaged_timezone("Asia/Shanghai").key == "Asia/Shanghai"


def test_private_prefix_and_mapped_address(config_values):
    settings = Settings(
        **(
            config_values
            | {
                "UPSTREAM_BASE_URL": "http://172.31.34.204:5330/v1/",
                "TRUSTED_PROXY_CIDRS": ("::ffff:127.0.0.1/128",),
            }
        )
    )
    assert settings.upstream_url("/models") == "http://172.31.34.204:5330/v1/models"
    assert settings.TRUSTED_PROXY_CIDRS == ("127.0.0.1/32",)


def test_numeric_fields_all_reject_booleans(config_values):
    for name, field in Settings.model_fields.items():
        if field.annotation in (int, float):
            with pytest.raises(ValidationError):
                Settings(**(config_values | {name: True}))


def test_defaults_match_design_candidates(config_values):
    candidates = json.loads((ROOT / "p0/config-candidates.json").read_text())
    values = Settings(**config_values).model_dump()
    for entry in candidates["entries"]:
        if entry["type"] in {
            "positive_int",
            "nonnegative_int",
            "positive_number",
            "nonnegative_number",
            "boolean",
            "enum",
        }:
            assert values[entry["name"]] == entry["candidate"]
