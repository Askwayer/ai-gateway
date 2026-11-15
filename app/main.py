"""Single-worker, non-streaming vertical slice. No management or multi-upstream routes."""

import asyncio
import hashlib
import hmac
import json
import logging
import math
import re
import ssl
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect

from app.accounting import parse_usage
from app.core.config import ConfigurationError, load_config
from app.core.settings import Settings
from app.db.repository import Identity, Repository, make_engine
from app.upstream_transport import GatewayHTTPTransport

logger = logging.getLogger("ai_gateway")


class GatewayError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        source: str = "gateway",
        extra: dict[str, Any] | None = None,
        retry_after: str | None = None,
    ):
        self.status, self.code, self.source = status, code, source
        self.extra = extra or {}
        self.retry_after = retry_after


_TOKEN = r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+"
_MEDIA_TYPE = re.compile(rf"({_TOKEN})/({_TOKEN})")
_PARAMETER = re.compile(
    rf"[ \t]*;[ \t]*({_TOKEN})[ \t]*=[ \t]*"
    rf'({_TOKEN}|"(?:[\t\x20\x21\x23-\x5b\x5d-\x7e]|\\[\t\x20-\x7e])*")'
)


def is_utf8_json(headers: list[str]) -> bool:
    """One syntactically valid JSON media type; charset may occur once and be UTF-8."""
    if len(headers) != 1:
        return False
    value = headers[0].strip(" \t")
    media = _MEDIA_TYPE.match(value)
    if media is None or tuple(part.lower() for part in media.groups()) != ("application", "json"):
        return False
    position, charset_seen = media.end(), False
    while value[position:].strip(" \t"):
        parameter = _PARAMETER.match(value, position)
        if parameter is None:
            return False
        name, content = parameter.groups()
        if content.startswith('"'):
            content = re.sub(r"\\(.)", r"\1", content[1:-1])
        if name.lower() == "charset":
            if charset_seen or content.lower() != "utf-8":
                return False
            charset_seen = True
        position = parameter.end()
    return True


def allowed_retry_after(headers: list[str]) -> str | None:
    if len(headers) != 1:
        return None
    value = headers[0].strip(" \t")
    if not re.fullmatch(r"[0-9]+", value):
        return None
    normalized = value.lstrip("0") or "0"
    if len(normalized) <= 4 and int(normalized) <= 3600:
        return normalized
    return None


def strict_json(data: bytes | bytearray) -> Any:
    def reject_constant(value: str) -> Any:
        raise ValueError("non-finite JSON constant")

    def finite_float(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite JSON number")
        return number

    return json.loads(
        data.decode("utf-8"), parse_constant=reject_constant, parse_float=finite_float
    )


class DeferredCloseStream(httpx.AsyncByteStream):
    """Defer HTTPX response-level EOF close to the request cleanup.

    Response.aiter_raw() calls aclose() at EOF, which would otherwise bypass our
    cleanup timeout. This adapter only defers that close; it never decodes bytes.
    HTTPcore internal error disposal is handled by GatewayHTTPTransport below it.
    """

    def __init__(self, original: httpx.AsyncByteStream):
        self.original = original

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.original:
            yield chunk

    async def aclose(self) -> None:
        pass


class Admission:
    """Synchronous mutations are atomic within the one supported event loop."""

    def __init__(self, maximum: int):
        self.maximum = maximum
        self.active = 0
        self.projects: dict[UUID, int] = {}
        self.keys: dict[UUID, int] = {}

    def acquire(self, identity: Identity) -> str | None:
        if self.active >= self.maximum:
            return "global"
        if self.projects.get(identity.project_id, 0) >= identity.project_limit:
            return "project"
        if self.keys.get(identity.key_id, 0) >= identity.key_limit:
            return "key"
        self.active += 1
        self.projects[identity.project_id] = self.projects.get(identity.project_id, 0) + 1
        self.keys[identity.key_id] = self.keys.get(identity.key_id, 0) + 1
        return None

    def release(self, identity: Identity) -> None:
        self.active -= 1
        for counts, key in ((self.projects, identity.project_id), (self.keys, identity.key_id)):
            counts[key] -= 1
            if not counts[key]:
                del counts[key]


def create_app(
    settings: Settings | None = None,
    *,
    repository: Repository | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    config = settings or load_config().settings
    if (
        config.HEALTH_GATE_ENABLED
        or config.HEALTH_REACHABILITY_ENABLED
        or config.INFERENCE_PROBE_ENABLED
    ):
        raise ConfigurationError(
            "non-streaming slice requires existing upstream health/probe switches disabled"
        )
    engine = None if repository is not None else make_engine(config)
    if repository is None:
        assert engine is not None
        repository = Repository(engine)
    repo = repository
    admission = Admission(config.GLOBAL_MAX_CONCURRENCY)
    auth_active = 0
    ready = False
    ready_checked = 0.0
    stopping = False
    run_id = uuid4()

    async def refresh_ready() -> None:
        nonlocal ready, ready_checked
        try:
            async with asyncio.timeout(config.DB_OPERATION_TIMEOUT):
                ready = await repo.ready()
        except (SQLAlchemyError, TimeoutError):
            ready = False
        ready_checked = time.monotonic()

    async def readiness_loop() -> None:
        while True:
            await asyncio.sleep(config.DB_READY_CHECK_INTERVAL)
            await refresh_ready()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal stopping
        verify = (
            ssl.create_default_context()
            if config.UPSTREAM_CA_BUNDLE == "system_trust_store"
            else ssl.create_default_context(cafile=config.UPSTREAM_CA_BUNDLE)
        )
        try:
            async with httpx.AsyncClient(
                transport=transport
                if transport is not None
                else GatewayHTTPTransport(verify, config.HTTP_MAX_CONNECTIONS),
                trust_env=False,
                follow_redirects=False,
                verify=verify,
                timeout=httpx.Timeout(
                    connect=config.HTTP_CONNECT_TIMEOUT,
                    read=config.HTTP_READ_IDLE_TIMEOUT,
                    write=config.HTTP_WRITE_TIMEOUT,
                    pool=config.HTTP_POOL_TIMEOUT,
                ),
                limits=httpx.Limits(
                    max_connections=config.HTTP_MAX_CONNECTIONS,
                    max_keepalive_connections=config.HTTP_MAX_CONNECTIONS,
                ),
            ) as client:
                app.state.client = client
                await refresh_ready()
                task = asyncio.create_task(readiness_loop())
                try:
                    yield
                finally:
                    stopping = True
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
        finally:
            if engine is not None:
                await engine.dispose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.admission = admission
    app.state.accounting_write_failures = 0
    app.state.upstream_close_failures = 0

    def error_response(request: Request, error: GatewayError) -> JSONResponse:
        category = {
            400: "invalid_request_error",
            401: "authentication_error",
            403: "permission_error",
            404: "invalid_request_error",
            405: "invalid_request_error",
            413: "invalid_request_error",
            429: "rate_limit_error",
            503: "service_unavailable_error",
            504: "timeout_error",
        }.get(error.status, "internal_error")
        if error.source == "upstream" or (
            error.status != 504 and error.code.startswith("upstream_")
        ):
            category = "upstream_error"
        headers = {"X-Request-ID": str(request.state.request_id), "X-Error-Source": error.source}
        if error.source == "gateway" and error.status in (429, 503):
            headers["Retry-After"] = str(
                config.CONCURRENCY_RETRY_AFTER
                if error.status == 429
                else config.SERVICE_UNAVAILABLE_RETRY_AFTER
            )
        if error.source == "upstream" and error.retry_after is not None:
            headers["Retry-After"] = error.retry_after
        return JSONResponse(
            {
                "error": {
                    "type": category,
                    "code": error.code,
                    "message": "Gateway request failed.",
                    "source": error.source,
                    **error.extra,
                },
                "request_id": str(request.state.request_id),
            },
            status_code=error.status,
            headers=headers,
        )

    @app.middleware("http")
    async def request_identity(request: Request, call_next: Any) -> Response:
        request.state.request_id = uuid4()
        request.state.deadline = time.monotonic() + config.REQUEST_TOTAL_TIMEOUT
        try:
            response: Response = await call_next(request)
        except Exception:
            response = error_response(request, GatewayError(500, "internal_error"))
        response.headers["X-Request-ID"] = str(request.state.request_id)
        return response

    @app.exception_handler(GatewayError)
    async def gateway_error(request: Request, error: GatewayError) -> JSONResponse:
        return error_response(request, error)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        response = error_response(
            request,
            GatewayError(
                error.status_code, "method_not_allowed" if error.status_code == 405 else "not_found"
            ),
        )
        if error.headers and "Allow" in error.headers:
            response.headers["Allow"] = error.headers["Allow"]
        return response

    @app.exception_handler(RequestValidationError)
    async def input_error(request: Request, error: RequestValidationError) -> JSONResponse:
        return error_response(request, GatewayError(400, "invalid_request"))

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready")
    async def readiness() -> dict[str, str]:
        if stopping or not ready or time.monotonic() - ready_checked > config.DB_READY_STALE_AFTER:
            raise GatewayError(503, "database_unavailable")
        return {"status": "ready"}

    async def authenticate(request: Request) -> Identity:
        nonlocal auth_active
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 512 or not token.isascii():
            raise GatewayError(401, "invalid_api_key")
        if hmac.compare_digest(token, config.ADMIN_API_KEY.get_secret_value()):
            raise GatewayError(401, "invalid_api_key")
        if auth_active >= config.AUTH_DB_MAX_INFLIGHT:
            raise GatewayError(503, "auth_capacity_exceeded")
        auth_active += 1
        try:
            async with asyncio.timeout(config.DB_OPERATION_TIMEOUT):
                identity = await repo.authenticate(hashlib.sha256(token.encode()).hexdigest())
        except (SQLAlchemyError, TimeoutError):
            raise GatewayError(503, "database_unavailable") from None
        finally:
            auth_active -= 1
        if identity is None:
            raise GatewayError(401, "invalid_api_key")
        return identity

    async def read_body(request: Request) -> dict[str, Any]:
        if not is_utf8_json(request.headers.getlist("content-type")):
            raise GatewayError(400, "invalid_request")
        data = bytearray()
        try:
            async with asyncio.timeout_at(request.state.deadline):
                async for chunk in request.stream():
                    if len(data) + len(chunk) > config.REQUEST_BODY_MAX_BYTES:
                        raise GatewayError(413, "request_too_large")
                    data.extend(chunk)
            value = strict_json(data)
        except (ValueError, UnicodeError, TimeoutError):
            raise GatewayError(400, "invalid_request") from None
        if not isinstance(value, dict) or not isinstance(value.get("model"), str):
            raise GatewayError(400, "invalid_request")
        if value["model"] not in config.ALLOWED_MODELS:
            raise GatewayError(400, "invalid_model")
        if value.get("stream", False) is not False:
            raise GatewayError(400, "stream_not_supported")
        messages = value.get("messages")
        if (
            not isinstance(messages, list)
            or not messages
            or any(
                not isinstance(m, dict)
                or not isinstance(m.get("role"), str)
                or m.get("role") not in {"system", "user", "assistant", "tool", "developer"}
                or not isinstance(m.get("content"), str)
                for m in messages
            )
        ):
            raise GatewayError(400, "invalid_request")
        value["stream"] = False
        return value

    async def finish(
        request_id: UUID,
        values: dict[str, Any],
        upstream: httpx.Response | None,
        stream: DeferredCloseStream | None,
    ) -> None:
        # Close and settlement share one monotonic cleanup deadline. asyncio timeout
        # also bounds HTTP transports that internally shield AnyIO cancellation.
        deadline = time.monotonic() + config.CLEANUP_TIMEOUT
        settled = False
        with anyio.CancelScope(shield=True):
            if upstream is not None and stream is not None:
                try:
                    async with asyncio.timeout_at(
                        min(deadline, time.monotonic() + config.UPSTREAM_CLOSE_TIMEOUT)
                    ):
                        await upstream.aclose()  # only marks the deferred response closed
                        await stream.original.aclose()
                except Exception:
                    app.state.upstream_close_failures += 1
                    logger.error("upstream close failed; request_id=%s", request_id)
            for attempt in range(1, config.SETTLEMENT_MAX_ATTEMPTS + 1):
                if time.monotonic() >= deadline:
                    break
                try:
                    async with asyncio.timeout_at(
                        min(deadline, time.monotonic() + config.SETTLEMENT_ATTEMPT_TIMEOUT)
                    ):
                        await repo.settle(request_id, {**values, "settlement_attempts": attempt})
                    settled = True
                    break
                except (SQLAlchemyError, TimeoutError):
                    if attempt < config.SETTLEMENT_MAX_ATTEMPTS:
                        await asyncio.sleep(
                            min(
                                config.SETTLEMENT_RETRY_BACKOFF, max(0, deadline - time.monotonic())
                            )
                        )
        if not settled:
            app.state.accounting_write_failures += 1
            logger.error("request settlement failed; request_id=%s", request_id)

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> Response:
        nonlocal auth_active
        if stopping:
            raise GatewayError(503, "shutting_down")
        if not ready or time.monotonic() - ready_checked > config.DB_READY_STALE_AFTER:
            raise GatewayError(503, "database_unavailable")
        identity = await authenticate(request)
        body = await read_body(request)
        request_id: UUID = request.state.request_id
        scope = admission.acquire(identity)
        base = {
            "request_id": request_id,
            "project_id": identity.project_id,
            "api_key_id": identity.key_id,
            "upstream_id": "default",
            "model": body["model"],
            "is_stream": False,
            "process_run_id": run_id,
        }
        if scope:
            from sqlalchemy import func

            if auth_active >= config.AUTH_DB_MAX_INFLIGHT:
                raise GatewayError(503, "auth_capacity_exceeded")
            auth_active += 1
            try:
                async with asyncio.timeout(config.DB_OPERATION_TIMEOUT):
                    await repo.register(
                        {
                            **base,
                            "admitted": False,
                            "outcome": "rejected",
                            "usage_status": "not_applicable",
                            "usage_source": "none",
                            "started_at": func.statement_timestamp(),
                            "finished_at": func.statement_timestamp(),
                            "updated_at": func.statement_timestamp(),
                            "rejection_scope": scope,
                            "response_status_code": 429,
                            "error_code": "concurrency_limit_exceeded",
                        }
                    )
            except (SQLAlchemyError, TimeoutError):
                raise GatewayError(503, "database_unavailable") from None
            finally:
                auth_active -= 1
            raise GatewayError(429, "concurrency_limit_exceeded")
        start = time.monotonic()
        registered = False
        upstream: httpx.Response | None = None
        stream: DeferredCloseStream | None = None
        values: dict[str, Any] = {
            "outcome": "gateway_error",
            "error_code": "internal_error",
            "response_status_code": 500,
            **parse_usage(None),
        }
        try:
            async with asyncio.timeout_at(request.state.deadline):
                # Conditional cleanup is safe even if an INSERT acknowledgement was lost.
                registered = True
                try:
                    async with asyncio.timeout(config.DB_OPERATION_TIMEOUT):
                        await repo.register(
                            {
                                **base,
                                "admitted": True,
                                "outcome": "started",
                                "usage_status": "pending",
                                "usage_source": "none",
                            }
                        )
                        await repo.mark_upstream(request_id)
                except (SQLAlchemyError, TimeoutError):
                    raise GatewayError(503, "database_unavailable") from None
                headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "X-Request-ID": str(request_id),
                    "X-Gateway-Request-Source": "business",
                }
                if config.UPSTREAM_API_KEY.get_secret_value():
                    headers["Authorization"] = (
                        "Bearer " + config.UPSTREAM_API_KEY.get_secret_value()
                    )
                outbound = app.state.client.build_request(
                    "POST", config.upstream_url("/chat/completions"), json=body, headers=headers
                )
                upstream = await app.state.client.send(outbound, stream=True)
                assert isinstance(upstream.stream, httpx.AsyncByteStream)
                stream = DeferredCloseStream(upstream.stream)
                upstream.stream = stream
                values["upstream_status_code"] = upstream.status_code
                upstream_id = upstream.headers.get("x-request-id")
                if upstream_id and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", upstream_id):
                    values["upstream_request_id"] = upstream_id
                raw = bytearray()
                if upstream.headers.get("content-encoding", "identity").lower() != "identity":
                    raise GatewayError(502, "upstream_protocol_error")
                async for chunk in upstream.aiter_raw():
                    if len(raw) + len(chunk) > config.NONSTREAM_RESPONSE_MAX_BYTES:
                        raise GatewayError(502, "upstream_response_too_large")
                    raw.extend(chunk)
                if upstream.status_code >= 400:
                    raise GatewayError(
                        upstream.status_code,
                        "upstream_http_error",
                        "upstream",
                        {"upstream_status": upstream.status_code},
                        retry_after=allowed_retry_after(upstream.headers.get_list("retry-after")),
                    )
                if upstream.status_code != 200 or not is_utf8_json(
                    upstream.headers.get_list("content-type")
                ):
                    raise GatewayError(502, "upstream_protocol_error")
                try:
                    payload = strict_json(raw)
                except (ValueError, UnicodeError):
                    raise GatewayError(502, "upstream_protocol_error") from None
                if (
                    not isinstance(payload, dict)
                    or not isinstance(payload.get("choices"), list)
                    or not payload["choices"]
                ):
                    raise GatewayError(502, "upstream_protocol_error")
                values.update(
                    outcome="completed",
                    error_code=None,
                    response_status_code=200,
                    **parse_usage(payload.get("usage")),
                )
                return Response(bytes(raw), status_code=200, media_type="application/json")
        except (httpx.TimeoutException, TimeoutError):
            values.update(
                outcome="timeout",
                error_code="upstream_timeout",
                response_status_code=504,
                **parse_usage(None, "timeout_before_usage"),
            )
            raise GatewayError(504, "upstream_timeout") from None
        except SQLAlchemyError:
            values.update(
                outcome="gateway_error", error_code="database_unavailable", response_status_code=503
            )
            raise GatewayError(503, "database_unavailable") from None
        except httpx.HTTPError:
            values.update(
                outcome="upstream_error",
                error_code="upstream_connection_error",
                response_status_code=502,
                **parse_usage(None, "upstream_interrupted"),
            )
            raise GatewayError(502, "upstream_connection_error") from None
        except GatewayError as error:
            values.update(
                outcome="gateway_error"
                if error.code == "database_unavailable"
                else "upstream_error",
                error_code=error.code,
                response_status_code=error.status,
            )
            raise
        except (asyncio.CancelledError, ClientDisconnect):
            values.update(
                outcome="client_cancelled",
                error_code="client_cancelled",
                response_status_code=None,
                **parse_usage(None, "client_cancelled_before_usage"),
            )
            raise
        finally:
            try:
                if registered:
                    values["duration_ms"] = (time.monotonic() - start) * 1000
                    await finish(request_id, values, upstream, stream)
            finally:
                admission.release(identity)

    return app
