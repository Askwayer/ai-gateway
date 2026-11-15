"""Regressions for the slice review: cleanup budgets and HTTP/usage contracts."""

import asyncio
import json
import time

import anyio
import httpx
import pytest
from test_nonstream_slice import Body, FakeRepository, slice_settings

from app.accounting import parse_usage
from app.core.settings import Settings
from app.main import create_app

BODY = {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "OK"}]}
AUTH = {"authorization": "Bearer valid-test-key"}


@pytest.mark.parametrize(
    "failure,close_behavior,settle_hangs,expected_status",
    [
        ("read-timeout", "hang", False, 504),
        ("total-timeout", "hang", False, 504),
        ("success", "hang", False, 200),
        ("success", "raise", False, 200),
        ("success", "hang", True, 200),
        ("cancel", "hang", False, None),
    ],
)
def test_closing_cannot_block_accounting_or_release(
    config_values, failure, close_behavior, settle_hangs, expected_status
):
    async def scenario():
        repo = FakeRepository()
        settlement_attempts = []
        settle = repo.settle

        async def observed_settle(request_id, values):
            settlement_attempts.append(time.monotonic())
            if settle_hangs:
                await asyncio.Event().wait()
            return await settle(request_id, values)

        repo.settle = observed_settle
        reading = asyncio.Event()

        class FaultyStream(Body):
            def __init__(self):
                self.close_calls = 0
                self.close_cancelled = False
                self.close_finished = False
                self.close_started = 0.0

            async def __aiter__(self):
                reading.set()
                if failure == "read-timeout":
                    raise httpx.ReadTimeout("simulated read timeout")
                if failure in {"total-timeout", "cancel"}:
                    await asyncio.Event().wait()
                async for chunk in super().__aiter__():
                    yield chunk

            async def aclose(self):
                self.close_calls += 1
                self.close_started = time.monotonic()
                try:
                    if close_behavior == "raise":
                        raise RuntimeError("simulated close failure")
                    # HTTP transports can shield AnyIO cancellation internally.
                    with anyio.CancelScope(shield=True):
                        await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.close_cancelled = True
                    raise
                finally:
                    self.close_finished = True

        stream = FaultyStream()
        settings = Settings(
            **(
                slice_settings(config_values).model_dump()
                | {
                    "UPSTREAM_CLOSE_TIMEOUT": 0.03,
                    "SETTLEMENT_ATTEMPT_TIMEOUT": 0.02,
                    "SETTLEMENT_RETRY_BACKOFF": 0.01,
                    "CLEANUP_TIMEOUT": 0.2,
                    "REQUEST_TOTAL_TIMEOUT": 0.05 if failure == "total-timeout" else 10.0,
                }
            )
        )
        app = create_app(
            settings,
            repository=repo,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, headers={"content-type": "application/json"}, stream=stream
                )
            ),
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client,
        ):
            task = asyncio.create_task(client.post("/v1/chat/completions", headers=AUTH, json=BODY))
            await asyncio.wait_for(reading.wait(), 1)
            if failure == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 0.5)
            else:
                response = await asyncio.wait_for(task, 0.5)
                assert response.status_code == expected_status
                if expected_status == 504:
                    assert response.json()["error"]["type"] == "timeout_error"
                    assert response.headers["x-error-source"] == "gateway"
            elapsed = time.monotonic() - stream.close_started
            # Scheduling tolerance is separate from the injected 200ms cleanup budget.
            assert elapsed < settings.CLEANUP_TIMEOUT + 0.1
            assert stream.close_calls == 1 and stream.close_finished
            assert stream.close_cancelled == (close_behavior == "hang")
            assert settlement_attempts
            assert app.state.admission.active == 0
            assert app.state.admission.keys == {} and app.state.admission.projects == {}
            assert app.state.upstream_close_failures == 1
            row = next(iter(repo.rows.values()))
            if settle_hangs:
                assert len(settlement_attempts) == settings.SETTLEMENT_MAX_ATTEMPTS
                assert row["outcome"] == "started"
                assert app.state.accounting_write_failures == 1
            else:
                expected = (
                    "completed"
                    if failure == "success"
                    else "client_cancelled"
                    if failure == "cancel"
                    else "timeout"
                )
                assert row["outcome"] == expected
                assert app.state.accounting_write_failures == 0
                if failure == "success":
                    assert row["usage_status"] == "reported" and row["total_tokens"] == 3

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "headers,content",
    [
        ([], json.dumps(BODY).encode()),
        ([("content-type", "text/plain")], json.dumps(BODY).encode()),
        ([("content-type", "application/json")] * 2, json.dumps(BODY).encode()),
        (
            [("content-type", "application/json; charset=utf-8; charset=UTF-8")],
            json.dumps(BODY).encode(),
        ),
        ([("content-type", "application/json; charset=iso-8859-1")], json.dumps(BODY).encode()),
        ([("content-type", "application/json; charset")], json.dumps(BODY).encode()),
        ([("content-type", "application/problem+json")], json.dumps(BODY).encode()),
        ([("content-type", "application/json")], json.dumps(BODY).encode("utf-16")),
        (
            [("content-type", "application/json")],
            json.dumps({**BODY, "temperature": float("nan")}).encode(),
        ),
        (
            [("content-type", "application/json")],
            json.dumps({**BODY, "temperature": float("inf")}).encode(),
        ),
        (
            [("content-type", "application/json")],
            (json.dumps(BODY)[:-1] + ',"temperature":1e999}').encode(),
        ),
    ],
)
def test_invalid_request_rejected_before_registration(config_values, headers, content):
    async def scenario():
        repo = FakeRepository()

        async def forbidden(request):
            pytest.fail("invalid input must not call upstream")

        app = create_app(
            slice_settings(config_values), repository=repo, transport=httpx.MockTransport(forbidden)
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client,
        ):
            response = await client.post(
                "/v1/chat/completions", headers=[*AUTH.items(), *headers], content=content
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "invalid_request"
            assert repo.rows == {} and app.state.admission.active == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "content_type",
    [
        [],
        [("content-type", "text/plain")],
        [("content-type", "application/json")] * 2,
        [("content-type", "application/json; charset=iso-8859-1")],
        [("content-type", "application/json; charset=utf-8; charset=utf-8")],
        [("content-type", "application/json; broken")],
    ],
)
def test_invalid_upstream_media_type_is_protocol_failure(config_values, content_type):
    async def scenario():
        repo = FakeRepository()
        app = create_app(
            slice_settings(config_values),
            repository=repo,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, headers=content_type, stream=Body())
            ),
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client,
        ):
            response = await client.post("/v1/chat/completions", headers=AUTH, json=BODY)
            assert response.status_code == 502
            assert response.json()["error"]["code"] == "upstream_protocol_error"
            assert next(iter(repo.rows.values()))["outcome"] == "upstream_error"
            assert app.state.admission.active == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "header",
    [
        "application/json",
        'Application/JSON; Charset="UTF-8"',
        'application/json; note="semi;colon"; charset=utf-8',
    ],
)
def test_legal_media_types_and_generated_upstream_identity(config_values, header):
    async def scenario():
        repo = FakeRepository()
        calls = []

        async def upstream(request):
            calls.append(request)
            return httpx.Response(
                200, headers={"content-type": header, "x-request-id": "provider-id"}, stream=Body()
            )

        app = create_app(
            slice_settings(config_values), repository=repo, transport=httpx.MockTransport(upstream)
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client,
        ):
            response = await client.post(
                "/v1/chat/completions",
                headers={
                    **AUTH,
                    "content-type": header,
                    "x-request-id": "forged",
                    "x-gateway-request-source": "health_probe",
                },
                content=json.dumps(BODY),
            )
            assert response.status_code == 200
            assert calls[0].headers["x-request-id"] == response.headers["x-request-id"]
            assert response.headers["x-request-id"] not in {"forged", "provider-id"}
            assert calls[0].headers["x-gateway-request-source"] == "business"
            assert calls[0].headers["accept"] == "application/json"
            assert calls[0].headers["authorization"] == "Bearer unit-upstream"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "headers,expected",
    [
        ([("retry-after", "0")], "0"),
        ([("retry-after", "12")], "12"),
        ([("retry-after", "3600")], "3600"),
        ([("retry-after", "3601")], None),
        ([("retry-after", "-1")], None),
        ([("retry-after", "1.5")], None),
        ([("retry-after", "Wed, 21 Oct 2015 07:28:00 GMT")], None),
        ([("retry-after", "1"), ("retry-after", "2")], None),
    ],
)
def test_upstream_error_retry_after_allowlist(config_values, headers, expected):
    async def scenario():
        repo = FakeRepository()
        app = create_app(
            slice_settings(config_values),
            repository=repo,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    429, headers=[("content-type", "text/html"), *headers], stream=Body()
                )
            ),
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client,
        ):
            response = await client.post("/v1/chat/completions", headers=AUTH, json=BODY)
            assert response.status_code == 429 and response.headers.get("retry-after") == expected
            assert response.json()["error"]["type"] == "upstream_error"
            assert response.json()["error"]["source"] == "upstream"

    asyncio.run(scenario())


def test_empty_usage_is_missing_and_partial_requires_usable_field():
    empty = parse_usage({})
    assert empty["usage_status"] == "missing" and empty["usage_source"] == "none"
    assert empty["usage_missing_reason"] == "upstream_not_provided"
    assert all(
        empty[name] is None for name in ("prompt_tokens", "completion_tokens", "total_tokens")
    )
    assert parse_usage({"provider_metadata": 1})["usage_status"] == "missing"
    assert parse_usage({"prompt_tokens": 0})["usage_status"] == "partial"
    assert parse_usage({"prompt_tokens": True})["usage_status"] == "invalid"
