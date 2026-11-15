"""Validated configuration contract, independent of the design files at runtime."""

import ipaddress
import ssl
import unicodedata
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import parse_qsl, unquote, urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

PositiveInt = Annotated[int, Field(strict=True, gt=0)]
NonnegativeInt = Annotated[int, Field(strict=True, ge=0)]
PositiveNumber = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
NonnegativeNumber = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
JSON_FIELDS = frozenset({"ALLOWED_MODELS", "ALLOWED_TIMEZONES", "TRUSTED_PROXY_CIDRS"})
# Design §11.1.1: P0/design placeholder literals must never load as runtime configuration.
PLACEHOLDER_MARKERS = frozenset(
    {
        "p0必填",
        "p0 必填",
        "必填模型列表",
        "必填高熵随机值",
        "必填内网固定地址",
        "系统信任库",
        "空",
        "postgresql+asyncpg://...",
    }
)


def has_control(value: str) -> bool:
    return any(unicodedata.category(char).startswith("C") for char in value)


def is_placeholder(value: str) -> bool:
    """Reject P0/design placeholder literals in runtime configuration."""
    return " ".join(value.split()).casefold() in PLACEHOLDER_MARKERS


def database_parts(value: str) -> tuple[str, int, str, str]:
    """Validate without echoing the credential-bearing URL in any error."""
    try:
        if is_placeholder(value):
            raise ValueError
        url = urlsplit(value)
        port = url.port
        if (
            has_control(value)
            or any(c.isspace() for c in value)
            or url.scheme != "postgresql+asyncpg"
            or not url.hostname
            or port is None
            or not 1 <= port <= 65535
            or not url.username
            or not url.password
            or url.path in ("", "/")
            or "/" in url.path[1:]
            or url.fragment
            or "\\" in value
        ):
            raise ValueError
        params = parse_qsl(url.query, strict_parsing=True)
        if len(params) > 1 or any(
            key != "ssl" or val not in {"disable", "require", "verify-ca", "verify-full"}
            for key, val in params
        ):
            raise ValueError
        # SQLAlchemy decodes usernames before connecting; compare the actual role name.
        return url.hostname, port, url.path[1:], unquote(url.username)
    except (ValueError, TypeError):
        raise ValueError("invalid PostgreSQL connection configuration") from None


def packaged_timezone(name: str) -> ZoneInfo:
    if name not in {"UTC", "Asia/Shanghai"}:
        raise ValueError("timezone is outside the supported allowlist")
    resource = files("tzdata.zoneinfo").joinpath(*name.split("/"))
    with resource.open("rb") as stream:
        return ZoneInfo.from_file(stream, key=name)


class Settings(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, validate_default=True, hide_input_in_errors=True
    )

    DATABASE_URL: SecretStr = Field(repr=False)
    ADMIN_API_KEY: SecretStr = Field(repr=False)
    UPSTREAM_BASE_URL: str
    UPSTREAM_API_KEY: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)
    UPSTREAM_CA_BUNDLE: str = "system_trust_store"
    ALLOWED_MODELS: tuple[str, ...]
    UPSTREAM_STREAM_USAGE_MODE: Literal["include_usage", "unsupported"] = "include_usage"
    GLOBAL_MAX_CONCURRENCY: PositiveInt = 16
    DEFAULT_PROJECT_MAX_CONCURRENCY: PositiveInt = 4
    DEFAULT_KEY_MAX_CONCURRENCY: PositiveInt = 2
    CONCURRENCY_RETRY_AFTER: PositiveInt = 1
    SERVICE_UNAVAILABLE_RETRY_AFTER: PositiveInt = 1
    HTTP_CONNECT_TIMEOUT: PositiveNumber = 5.0
    HTTP_POOL_TIMEOUT: PositiveNumber = 2.0
    HTTP_WRITE_TIMEOUT: PositiveNumber = 10.0
    HTTP_READ_IDLE_TIMEOUT: PositiveNumber = 60.0
    REQUEST_TOTAL_TIMEOUT: PositiveNumber = 300.0
    CLEANUP_TIMEOUT: PositiveNumber = 5.0
    UPSTREAM_CLOSE_TIMEOUT: PositiveNumber = 0.5
    SETTLEMENT_MAX_ATTEMPTS: PositiveInt = 2
    SETTLEMENT_ATTEMPT_TIMEOUT: PositiveNumber = 1.5
    SETTLEMENT_RETRY_BACKOFF: NonnegativeNumber = 0.1
    DB_OPERATION_TIMEOUT: PositiveNumber = 2.0
    DB_QUERY_TIMEOUT: PositiveNumber = 3.0
    DB_POOL_SIZE: PositiveInt = 16
    DB_MAX_OVERFLOW: NonnegativeInt = 20
    AUTH_DB_MAX_INFLIGHT: PositiveInt = 16
    ADMIN_DB_MAX_INFLIGHT: PositiveInt = 2
    BACKGROUND_DB_MAX_INFLIGHT: PositiveInt = 2
    DB_READY_CHECK_INTERVAL: PositiveNumber = 5.0
    DB_READY_STALE_AFTER: PositiveNumber = 15.0
    HTTP_MAX_CONNECTIONS: PositiveInt = 32
    REQUEST_BODY_MAX_BYTES: PositiveInt = 1048576
    NONSTREAM_RESPONSE_MAX_BYTES: PositiveInt = 16777216
    SSE_EVENT_PARSE_MAX_BYTES: PositiveInt = 262144
    APP_DRAIN_TIMEOUT: PositiveNumber = 20.0
    UVICORN_GRACEFUL_TIMEOUT: PositiveNumber = 30.0
    BACKGROUND_SHUTDOWN_TIMEOUT: PositiveNumber = 3.0
    HOST_STOP_TIMEOUT: PositiveNumber = 40.0
    SHUTDOWN_SAFETY_MARGIN: PositiveNumber = 5.0
    HEALTH_REACHABILITY_ENABLED: bool = True
    UPSTREAM_HEALTH_PATH: str = ""
    HEALTH_GATE_ENABLED: bool = True
    HEALTH_INTERVAL: PositiveNumber = 30.0
    HEALTH_TIMEOUT: PositiveNumber = 3.0
    HEALTH_FAILURE_THRESHOLD: PositiveInt = 3
    HEALTH_RECOVERY_THRESHOLD: PositiveInt = 2
    HEALTH_STALE_AFTER: PositiveNumber = 90.0
    INFERENCE_PROBE_ENABLED: bool = True
    INFERENCE_PROBE_MODEL: str = ""
    INFERENCE_PROBE_PROMPT: str = "Reply OK."
    INFERENCE_PROBE_MAX_TOKENS: PositiveInt = 1
    INFERENCE_PROBE_INTERVAL: PositiveNumber = 300.0
    INFERENCE_PROBE_TIMEOUT: PositiveNumber = 30.0
    INFERENCE_PROBE_STALE_AFTER: PositiveNumber = 600.0
    PROBE_MIN_SPARE_SLOTS: PositiveInt = 2
    PAGINATION_DEFAULT_LIMIT: PositiveInt = 50
    PAGINATION_MAX_LIMIT: PositiveInt = 200
    USAGE_MAX_RANGE_DAYS: PositiveInt = 31
    USAGE_MAX_GROUPS: PositiveInt = 1000
    USAGE_MAX_GROUP_DIMENSIONS: PositiveInt = 2
    DEFAULT_TIMEZONE: str = "Asia/Shanghai"
    ALLOWED_TIMEZONES: tuple[str, ...] = ("UTC", "Asia/Shanghai")
    TZDATA_SOURCE: Literal["locked_python_package"] = "locked_python_package"
    REQUEST_LOG_RETENTION_DAYS: PositiveInt = 180
    HEALTH_LOG_RETENTION_DAYS: PositiveInt = 30
    AUDIT_LOG_RETENTION_DAYS: PositiveInt = 365
    LOG_MAX_BYTES: PositiveInt = 104857600
    LOG_RETENTION_DAYS: PositiveInt = 7
    RECOVERY_SCAN_INTERVAL: PositiveNumber = 30.0
    RECOVERY_GRACE_PERIOD: PositiveNumber = 60.0
    INGRESS_GLOBAL_RATE: PositiveNumber = 20.0
    INGRESS_GLOBAL_BURST: PositiveInt = 40
    INGRESS_IP_RATE: PositiveNumber = 5.0
    INGRESS_IP_BURST: PositiveInt = 20
    INGRESS_IP_MAX_ENTRIES: PositiveInt = 4096
    INGRESS_IP_IDLE_TTL: PositiveNumber = 120.0
    TRUSTED_PROXY_CIDRS: tuple[str, ...] = ()
    CLOCK_MAX_OFFSET: PositiveNumber = 1.0
    DEPENDENCY_LOCK_TOOL: Literal["uv"] = "uv"

    @field_validator("DATABASE_URL")
    @classmethod
    def valid_database(cls, value: SecretStr) -> SecretStr:
        database_parts(value.get_secret_value())
        return value

    @field_validator("ADMIN_API_KEY")
    @classmethod
    def valid_admin_secret(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if (
            is_placeholder(raw)
            or len(raw) < 43
            or not raw.isascii()
            or any(c.isspace() for c in raw)
            or has_control(raw)
        ):
            raise ValueError("ADMIN_API_KEY must be at least 43 non-whitespace ASCII characters")
        return value

    @field_validator("UPSTREAM_API_KEY")
    @classmethod
    def valid_upstream_secret(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if is_placeholder(raw) or has_control(raw):
            raise ValueError("UPSTREAM_API_KEY must not contain placeholders or control characters")
        return value

    @field_validator("UPSTREAM_BASE_URL")
    @classmethod
    def valid_upstream_url(cls, value: str) -> str:
        try:
            if is_placeholder(value):
                raise ValueError
            url = urlsplit(value)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username is not None
                or url.password is not None
                or url.query
                or url.fragment
                or has_control(value)
                or any(c.isspace() for c in value)
                or "\\" in value
                or (url.port is not None and not 1 <= url.port <= 65535)
            ):
                raise ValueError
        except ValueError:
            raise ValueError("invalid upstream base URL") from None
        return value.rstrip("/")

    @field_validator("UPSTREAM_HEALTH_PATH")
    @classmethod
    def valid_health_path(cls, value: str) -> str:
        url = urlsplit(value)
        if value and (
            is_placeholder(value)
            or not value.startswith("/")
            or value.startswith("//")
            or url.netloc
            or url.query
            or url.fragment
            or "\\" in value
            or "%" in value
            or any(part in {".", ".."} for part in value.split("/"))
            or any(c.isspace() for c in value)
            or has_control(value)
        ):
            raise ValueError("invalid relative upstream health path")
        return value

    @field_validator("ALLOWED_MODELS")
    @classmethod
    def valid_models(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if (
            not value
            or len(set(value)) != len(value)
            or any(
                is_placeholder(model) or not model.strip() or has_control(model) for model in value
            )
        ):
            raise ValueError("model allowlist must be nonempty and unique with valid text")
        return value

    @field_validator("INFERENCE_PROBE_MODEL", "INFERENCE_PROBE_PROMPT")
    @classmethod
    def valid_probe_text(cls, value: str) -> str:
        if is_placeholder(value) or has_control(value):
            raise ValueError("probe text contains placeholders or control characters")
        return value

    @field_validator("UPSTREAM_CA_BUNDLE")
    @classmethod
    def valid_ca(cls, value: str) -> str:
        if is_placeholder(value):
            raise ValueError("UPSTREAM_CA_BUNDLE must not be a placeholder")
        if value != "system_trust_store":
            try:
                if not Path(value).is_file():
                    raise ValueError
                ssl.create_default_context(cafile=value)
            except (OSError, ValueError):
                raise ValueError("UPSTREAM_CA_BUNDLE is not a readable valid CA file") from None
        return value

    @field_validator("TRUSTED_PROXY_CIDRS")
    @classmethod
    def valid_networks(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for item in value:
            try:
                network = ipaddress.ip_network(item, strict=True)
                if (
                    isinstance(network, ipaddress.IPv6Network)
                    and network.network_address.ipv4_mapped
                ):
                    if network.prefixlen < 96:
                        raise ValueError
                    network = ipaddress.ip_network(
                        f"{network.network_address.ipv4_mapped}/{network.prefixlen - 96}",
                        strict=True,
                    )
                if network.prefixlen == 0:
                    raise ValueError
            except ValueError:
                raise ValueError("invalid or overly broad proxy trust network") from None
            normalized.append(str(network))
        if len(set(normalized)) != len(normalized):
            raise ValueError("duplicate proxy trust network")
        return tuple(normalized)

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if self.ADMIN_API_KEY == self.UPSTREAM_API_KEY:
            raise ValueError("administrator and upstream credentials must differ")
        if not self.ALLOWED_TIMEZONES or len(set(self.ALLOWED_TIMEZONES)) != len(
            self.ALLOWED_TIMEZONES
        ):
            raise ValueError("timezone allowlist must be nonempty and unique")
        for name in self.ALLOWED_TIMEZONES:
            packaged_timezone(name)
        if self.DEFAULT_TIMEZONE not in self.ALLOWED_TIMEZONES:
            raise ValueError("DEFAULT_TIMEZONE must be allowed")
        if self.HEALTH_REACHABILITY_ENABLED and not self.UPSTREAM_HEALTH_PATH:
            raise ValueError("UPSTREAM_HEALTH_PATH required when reachability is enabled")
        if self.HEALTH_GATE_ENABLED and not self.HEALTH_REACHABILITY_ENABLED:
            raise ValueError("health gate requires reachability checks")
        if self.INFERENCE_PROBE_ENABLED and (
            self.INFERENCE_PROBE_MODEL not in self.ALLOWED_MODELS
            or not self.INFERENCE_PROBE_PROMPT.strip()
        ):
            raise ValueError(
                "enabled inference probe requires an allowed model and nonempty prompt"
            )
        if not 1 <= self.USAGE_MAX_GROUP_DIMENSIONS <= 2:
            raise ValueError("USAGE_MAX_GROUP_DIMENSIONS must be 1 or 2")
        if max(self.CONCURRENCY_RETRY_AFTER, self.SERVICE_UNAVAILABLE_RETRY_AFTER) > 3600:
            raise ValueError("Retry-After must not exceed 3600 seconds")
        if self.PAGINATION_DEFAULT_LIMIT > self.PAGINATION_MAX_LIMIT:
            raise ValueError("default pagination exceeds maximum")
        if (
            min(self.AUTH_DB_MAX_INFLIGHT, self.INGRESS_GLOBAL_BURST, self.INGRESS_IP_BURST)
            < self.GLOBAL_MAX_CONCURRENCY
        ):
            raise ValueError("auth lanes and ingress bursts must cover default concurrency")
        if self.HTTP_MAX_CONNECTIONS < self.GLOBAL_MAX_CONCURRENCY + 1:
            raise ValueError("HTTP pool must reserve a reachability connection")
        if self.DB_POOL_SIZE + self.DB_MAX_OVERFLOW < (
            self.GLOBAL_MAX_CONCURRENCY
            + self.AUTH_DB_MAX_INFLIGHT
            + self.ADMIN_DB_MAX_INFLIGHT
            + self.BACKGROUND_DB_MAX_INFLIGHT
        ):
            raise ValueError("DB pool does not cover the lane budget")
        if self.SETTLEMENT_ATTEMPT_TIMEOUT > self.DB_OPERATION_TIMEOUT:
            raise ValueError("settlement attempt exceeds DB operation budget")
        if (
            self.UPSTREAM_CLOSE_TIMEOUT
            + self.SETTLEMENT_MAX_ATTEMPTS * self.SETTLEMENT_ATTEMPT_TIMEOUT
            + (self.SETTLEMENT_MAX_ATTEMPTS - 1) * self.SETTLEMENT_RETRY_BACKOFF
        ) >= self.CLEANUP_TIMEOUT:
            raise ValueError("cleanup budget must exceed close and settlement budget")
        if (
            self.APP_DRAIN_TIMEOUT + self.CLEANUP_TIMEOUT + self.SHUTDOWN_SAFETY_MARGIN
            > self.UVICORN_GRACEFUL_TIMEOUT
        ):
            raise ValueError("application drain exceeds Uvicorn shutdown budget")
        if (
            self.UVICORN_GRACEFUL_TIMEOUT
            + self.BACKGROUND_SHUTDOWN_TIMEOUT
            + self.SHUTDOWN_SAFETY_MARGIN
            > self.HOST_STOP_TIMEOUT
        ):
            raise ValueError("Uvicorn shutdown exceeds host budget")
        if self.HEALTH_STALE_AFTER <= self.HEALTH_INTERVAL + self.HEALTH_TIMEOUT:
            raise ValueError("health stale window is too short")
        if self.DB_READY_STALE_AFTER <= self.DB_READY_CHECK_INTERVAL + self.DB_OPERATION_TIMEOUT:
            raise ValueError("DB readiness stale window is too short")
        if (
            self.INFERENCE_PROBE_STALE_AFTER
            <= self.INFERENCE_PROBE_INTERVAL + self.INFERENCE_PROBE_TIMEOUT
        ):
            raise ValueError("inference stale window is too short")
        if self.RECOVERY_GRACE_PERIOD < self.CLEANUP_TIMEOUT:
            raise ValueError("recovery grace must cover cleanup")
        return self

    def upstream_url(self, path: str) -> str:
        """Append a validated path without dropping the configured /v1 prefix."""
        checked = self.valid_health_path(path)
        if not checked:
            raise ValueError("upstream path cannot be empty")
        return self.UPSTREAM_BASE_URL + checked
