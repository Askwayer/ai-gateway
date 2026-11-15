import asyncio
import hashlib
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.accounting import parse_usage
from app.core.settings import Settings
from app.db.repository import Identity, Repository
from app.main import create_app


class FakeRepository(Repository):
    def __init__(self):
        self.rows: dict[Any, dict[str, Any]] = {}
        self.identity = Identity(uuid4(), uuid4(), 4, 2)
        self.fail_register = False
        self.fail_settle = False
        self.is_ready = True

    async def ready(self):
        return self.is_ready

    async def authenticate(self, key_hash):
        return self.identity if key_hash == hashlib.sha256(b"valid-test-key").hexdigest() else None

    async def register(self, values):
        if self.fail_register:
            raise SQLAlchemyError("simulated DB failure")
        self.rows[values["request_id"]] = values.copy()

    async def mark_upstream(self, request_id):
        assert self.rows[request_id]["outcome"] == "started"

    async def settle(self, request_id, values):
        if self.fail_settle:
            raise SQLAlchemyError("simulated settlement failure")
        row = self.rows.get(request_id)
        if row is None or row["outcome"] != "started":
            return False
        row.update(values)
        return True


class Body(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"id":"fake","choices":[{"message":{"content":"OK"}}],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}}'


def slice_settings(config_values):
    return Settings(
        **(
            config_values
            | {
                "HEALTH_GATE_ENABLED": False,
                "HEALTH_REACHABILITY_ENABLED": False,
                "INFERENCE_PROBE_ENABLED": False,
            }
        )
    )


def test_success_and_authentication_never_leaks_caller_key(config_values):
    async def scenario():
        repo = FakeRepository()
        calls = []

        async def upstream(request):
            calls.append(request)
            assert request.headers["authorization"] == "Bearer unit-upstream"
            assert len(repo.rows) == 1
            assert next(iter(repo.rows.values()))["outcome"] == "started"
            return httpx.Response(
                200,
                headers={"content-type": "application/json", "x-request-id": "upstream-id"},
                stream=Body(),
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
            assert (await client.get("/health/live")).status_code == 200
            assert (await client.get("/health/ready")).status_code == 200
            for key in ("invalid-key", config_values["ADMIN_API_KEY"]):
                response = await client.post(
                    "/v1/chat/completions",
                    headers={"authorization": "Bearer " + key},
                    json={
                        "model": "deepseek-v4-flash",
                        "messages": [{"role": "user", "content": "OK"}],
                    },
                )
                assert response.status_code == 401
            assert calls == [] and repo.rows == {}
            response = await client.post(
                "/v1/chat/completions",
                headers={"authorization": "Bearer valid-test-key", "X-Request-ID": "untrusted"},
                json={
                    "model": "deepseek-v4-flash",
                    "messages": [{"role": "user", "content": "OK"}],
                },
            )
            assert response.status_code == 200
            assert response.headers["x-request-id"] != "untrusted"
            row = next(iter(repo.rows.values()))
            assert str(row["request_id"]) == response.headers["x-request-id"]
            assert row["outcome"] == "completed" and row["total_tokens"] == 3
            assert row["upstream_request_id"] == "upstream-id"
            assert not await repo.settle(row["request_id"], {"total_tokens": 999})
            assert app.state.admission.active == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "case,status,outcome",
    [
        ("register", 503, None),
        ("timeout", 504, "timeout"),
        ("upstream", 401, "upstream_error"),
        ("protocol", 502, "upstream_error"),
        ("settle", 200, "started"),
        ("stream", 400, None),
    ],
)
def test_failure_paths(config_values, case, status, outcome):
    async def scenario():
        repo = FakeRepository()
        repo.fail_register = case == "register"
        repo.fail_settle = case == "settle"
        calls = []

        async def upstream(request):
            calls.append(request)
            if case == "timeout":
                raise httpx.ReadTimeout("fake")
            return httpx.Response(
                401 if case == "upstream" else 200,
                headers={
                    "content-type": "text/plain" if case == "protocol" else "application/json"
                },
                stream=Body(),
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
                headers={"authorization": "Bearer valid-test-key"},
                json={
                    "model": "deepseek-v4-flash",
                    "messages": [{"role": "user", "content": "OK"}],
                    "stream": case == "stream",
                },
            )
            assert response.status_code == status, response.text
            if case in ("register", "stream"):
                assert calls == [] and repo.rows == {}
            else:
                assert next(iter(repo.rows.values()))["outcome"] == outcome
            if case == "upstream":
                assert response.json()["error"]["source"] == "upstream"
            if case == "settle":
                assert app.state.accounting_write_failures == 1
            assert app.state.admission.active == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "value,status",
    [
        (None, "missing"),
        ({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, "reported"),
        ({"prompt_tokens": 2}, "partial"),
        ({"prompt_tokens": True}, "invalid"),
        ({"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 5}, "invalid"),
    ],
)
def test_usage_states(value, status):
    assert parse_usage(value)["usage_status"] == status


def test_concurrency_rejection_releases_capacity_and_ready_failure_is_honest(config_values):
    async def scenario():
        repo = FakeRepository()
        repo.identity = Identity(repo.identity.project_id, repo.identity.key_id, 4, 1)
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def upstream(request):
            calls.append(request)
            started.set()
            await release.wait()
            return httpx.Response(200, headers={"content-type": "application/json"}, stream=Body())

        app = create_app(
            slice_settings(config_values), repository=repo, transport=httpx.MockTransport(upstream)
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client,
        ):
            body = {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "OK"}]}
            headers = {"authorization": "Bearer valid-test-key"}
            first = asyncio.create_task(
                client.post("/v1/chat/completions", json=body, headers=headers)
            )
            try:
                await asyncio.wait_for(started.wait(), 2)
                rejected = await client.post("/v1/chat/completions", json=body, headers=headers)
                assert rejected.status_code == 429 and rejected.headers["retry-after"] == "1"
                assert len(calls) == 1 and app.state.admission.active == 1
                row = next(row for row in repo.rows.values() if row["outcome"] == "rejected")
                assert row["rejection_scope"] == "key" and not row["admitted"]
            finally:
                release.set()
                assert (await first).status_code == 200
            assert app.state.admission.active == 0
            assert (
                await client.post("/v1/chat/completions", json=body, headers=headers)
            ).status_code == 200
        repo.is_ready = False
        failed_app = create_app(
            slice_settings(config_values), repository=repo, transport=httpx.MockTransport(upstream)
        )
        async with (
            failed_app.router.lifespan_context(failed_app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=failed_app), base_url="http://gateway"
            ) as client,
        ):
            assert (await client.get("/health/live")).status_code == 200
            response = await client.get("/health/ready")
            assert response.status_code == 503 and response.headers["retry-after"] == "1"

    asyncio.run(scenario())
