from pathlib import Path

import pytest

from app.core.config import ConfigurationError, load_config
from app.db.config import load_migration_config


def test_default_development_ignores_company_file(env_root):
    (env_root / ".env.company").write_text("malformed company input")
    loaded = load_config(root=env_root, environ={})
    assert loaded.environment == "development"
    assert loaded.settings.UPSTREAM_BASE_URL == "https://api.deepseek.com"
    assert "ai_gateway_test" in loaded.settings.DATABASE_URL.get_secret_value()


def test_company_never_falls_back(env_root):
    with pytest.raises(ConfigurationError, match="no fallback"):
        load_config(root=env_root, environ={"APP_ENV": "company"})
    (env_root / ".env.company").write_text("UPSTREAM_BASE_URL=http://172.31.34.204:5330/v1\n")
    with pytest.raises(ConfigurationError):
        load_config(root=env_root, environ={"APP_ENV": "company"})
    with pytest.raises(ConfigurationError, match="development .env"):
        load_config(root=env_root, environ={"APP_ENV": "company", "APP_ENV_FILE": ".env"})
    with pytest.raises(ConfigurationError, match="explicit"):
        load_config(root=env_root, environ={"APP_ENV_FILE": ".env.company"})


def test_complete_company_config_can_validate_without_connecting(env_root):
    company = (
        (env_root / ".env")
        .read_text()
        .replace("gateway_test_app", "gateway_app")
        .replace("/ai_gateway_test", "/ai_gateway")
        .replace("https://api.deepseek.com", "http://172.31.34.204:5330/v1")
        .replace("deepseek-v4-flash", "Qwen3-VL-8B-Instruct")
    )
    (env_root / ".env.company").write_text(company)
    loaded = load_config(root=env_root, environ={"APP_ENV": "company"})
    assert loaded.settings.ALLOWED_MODELS == ("Qwen3-VL-8B-Instruct",)


@pytest.mark.parametrize(
    "key,value",
    [
        ("GLOBAL_MAX_CONCURRENCY", "true"),
        ("GLOBAL_MAX_CONCURRENCY", "16.0"),
        ("HEALTH_GATE_ENABLED", "0"),
        ("HEALTH_GATE_ENABLED", "False"),
        ("ALLOWED_MODELS", "deepseek-v4-flash"),
        ("ALLOWED_MODELS", "[1]"),
        ("HTTP_CONNECT_TIMEOUT", "NaN"),
        ("HTTP_CONNECT_TIMEOUT", "Infinity"),
    ],
)
def test_invalid_environment_type(env_root, key, value):
    with pytest.raises(ConfigurationError):
        load_config(root=env_root, environ={key: value})


def test_environment_overrides_are_explicit_and_secrets_safe(env_root):
    loaded = load_config(
        root=env_root, environ={"GLOBAL_MAX_CONCURRENCY": "8", "INGRESS_IP_BURST": "8"}
    )
    assert loaded.settings.GLOBAL_MAX_CONCURRENCY == 8
    with pytest.raises(ConfigurationError) as caught:
        load_config(root=env_root, environ={"DATABASE_URL": "bad://sensitive-value"})
    assert "sensitive-value" not in str(caught.value)
    assert "DATABASE_URL" in str(caught.value)


def test_generic_upstream_allows_empty_key(env_root):
    loaded = load_config(root=env_root, environ={"UPSTREAM_API_KEY": ""})
    assert loaded.settings.UPSTREAM_API_KEY.get_secret_value() == ""


def test_placeholder_values_fail_without_echoing_value(env_root):
    path = env_root / ".env"
    text = path.read_text()
    text = text.replace(
        'ALLOWED_MODELS=["deepseek-v4-flash"]', 'ALLOWED_MODELS=["P0必填"]'
    ).replace("INFERENCE_PROBE_MODEL=deepseek-v4-flash", "INFERENCE_PROBE_MODEL=P0必填")
    path.write_text(text)

    with pytest.raises(ConfigurationError) as caught:
        load_config(root=env_root, environ={})

    message = str(caught.value)
    assert "P0必填" not in message
    assert "ALLOWED_MODELS" in message or "INFERENCE_PROBE_MODEL" in message


@pytest.mark.parametrize(
    "contents",
    [
        "GLOBAL_MAX_CONCURRENCY=16\nGLOBAL_MAX_CONCURRENCY=17\n",
        "GLOBAL_MAX_CONCURENCY=16\n",
        "GLOBAL_MAX_CONCURRENCY\n",
        'ADMIN_API_KEY="unterminated\n',
        "APP_ENV=company\n",
        "APP_ENV_FILE=.env.company\n",
    ],
)
def test_malformed_or_ambiguous_file_fails(env_root, contents):
    (env_root / ".env").write_text(contents)
    with pytest.raises(ConfigurationError):
        load_config(root=env_root, environ={})


def test_no_shell_interpolation(env_root):
    secret = "${DO_NOT_INTERPOLATE}" + "a" * 43
    file = env_root / ".env"
    file.write_text(
        file.read_text().replace("ADMIN_API_KEY=" + "a" * 43, "ADMIN_API_KEY=" + secret)
    )
    loaded = load_config(root=env_root, environ={"DO_NOT_INTERPOLATE": "unexpected"})
    assert loaded.settings.ADMIN_API_KEY.get_secret_value() == secret


def test_selection_conflicts_fail(env_root):
    with pytest.raises(ConfigurationError, match="conflicts"):
        load_config(root=env_root, environment="test", environ={"APP_ENV": "company"})
    with pytest.raises(ConfigurationError, match="conflicts"):
        load_config(root=env_root, env_file=".env", environ={"APP_ENV_FILE": ".env.other"})


def test_runtime_accepts_custom_endpoint_database_and_role(env_root):
    loaded = load_config(
        root=env_root,
        environ={
            "DATABASE_URL": "postgresql+asyncpg://custom_app:unit-only@db.internal:5432/custom_db",
            "UPSTREAM_BASE_URL": "https://llm.example.org/v1",
        },
    )
    assert loaded.settings.UPSTREAM_BASE_URL == "https://llm.example.org/v1"


@pytest.mark.parametrize(
    "field",
    [
        "TEST_DATABASE_URL",
        "TEST_MIGRATION_DATABASE_URL",
        "PRODUCTION_DATABASE_URL",
        "PRODUCTION_MIGRATION_DATABASE_URL",
        "AI_FAST_MODEL",
        "AI_FAST_BASE_URL",
        "AI_FAST_API_KEY",
    ],
)
def test_obsolete_aliases_rejected(env_root, field):
    with (env_root / ".env").open("a") as stream:
        stream.write(field + "=unused\n")
    with pytest.raises(ConfigurationError, match="unknown field"):
        load_config(root=env_root, environ={})


def test_migration_uses_separate_credentials_on_same_target(env_root):
    loaded = load_migration_config(root=env_root, environ={})
    assert loaded.expected_identity == ("ai_gateway_test", "gateway_test_migrator")
    assert "unit-only" not in repr(loaded)
    for value in (
        "postgresql+asyncpg://gateway_test_app:unit-only@127.0.0.1:13432/ai_gateway_test",
        "postgresql+asyncpg://%67ateway_test_app:unit-only@127.0.0.1:13432/ai_gateway_test",
        "postgresql+asyncpg://owner:unit-only@127.0.0.1:13432/other_db",
        "postgresql+asyncpg://owner:unit-only@127.0.0.2:13432/ai_gateway_test",
        "postgresql+asyncpg://owner:unit-only@127.0.0.1:5432/ai_gateway_test",
    ):
        with pytest.raises(ConfigurationError):
            load_migration_config(root=env_root, environ={"MIGRATION_DATABASE_URL": value})


def test_complete_company_migration_config_is_blocked(company_env):
    with pytest.raises(ConfigurationError, match="company migrations are blocked"):
        load_migration_config(environment="company", env_file=company_env, environ={})


def test_relative_ca_uses_selected_file_directory(env_root):
    with pytest.raises(ConfigurationError, match="UPSTREAM_CA_BUNDLE"):
        load_config(root=env_root, environ={"UPSTREAM_CA_BUNDLE": "missing.pem"})
    assert not (Path.cwd() / "missing.pem").exists()
