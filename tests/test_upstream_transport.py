"""Real HTTPX/httpcore pool tests; only the network endpoint is simulated/local."""

import asyncio
import json
import socket
import ssl
import subprocess
import time
from contextlib import suppress

import anyio
import httpcore
import httpx
import pytest
from test_nonstream_slice import FakeRepository, slice_settings

from app.core.settings import Settings
from app.main import create_app
from app.upstream_transport import AbortableStream, ConnectionAbort, GatewayHTTPTransport

CONNECT = socket.socket.connect
CONNECT_EX = socket.socket.connect_ex
BODY = {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "OK"}]}
PAYLOAD = json.dumps(
    {
        "choices": [{"message": {"content": "OK"}}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }
).encode()
HEADERS = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "


def settings_for(config_values, **overrides):
    return Settings(
        **(
            slice_settings(config_values).model_dump()
            | {
                "UPSTREAM_BASE_URL": "http://127.0.0.1:9999/v1",
                "HTTP_READ_IDLE_TIMEOUT": 0.02,
                "REQUEST_TOTAL_TIMEOUT": 0.15,
                "UPSTREAM_CLOSE_TIMEOUT": 0.02,
                "SETTLEMENT_ATTEMPT_TIMEOUT": 0.02,
                "SETTLEMENT_RETRY_BACKOFF": 0.01,
                "CLEANUP_TIMEOUT": 0.12,
            }
            | overrides
        )
    )


class Wire(anyio.abc.ByteStream):
    """A network resource whose graceful aclose hangs, but abort releases it."""

    def __init__(self, mode):
        self.mode = mode
        self.reads = 0
        self.waiting = asyncio.Event()
        self.aborted = False
        self.abort_calls = 0
        self.close_calls = 0

    async def send(self, item):
        assert not self.aborted

    async def receive(self, max_bytes=65536):
        assert not self.aborted
        self.reads += 1
        if self.mode == "healthy":
            return HEADERS + str(len(PAYLOAD)).encode() + b"\r\n\r\n" + PAYLOAD
        if self.mode == "success-close":
            return HEADERS + str(len(PAYLOAD)).encode() + b"\r\nConnection: close\r\n\r\n" + PAYLOAD
        if self.reads == 1 and self.mode not in {"headers-timeout", "headers-cancel"}:
            extra = b"Content-Encoding: gzip\r\n" if self.mode == "protocol" else b""
            return HEADERS + b"1000\r\n" + extra + b"\r\n"
        self.waiting.set()
        if self.mode == "read-error":
            raise anyio.BrokenResourceError
        if self.mode == "truncated":
            raise anyio.EndOfStream
        await asyncio.Event().wait()

    async def send_eof(self):
        pass

    async def aclose(self):
        self.close_calls += 1
        with anyio.CancelScope(shield=True):
            await asyncio.Event().wait()

    def abort(self):
        self.abort_calls += 1
        self.aborted = True


class MemoryStream(AbortableStream):
    def get_extra_info(self, info):
        if info == "is_readable":
            return False  # no unsolicited bytes on the simulated idle connection
        return super().get_extra_info(info)


class Backend(httpcore.AsyncNetworkBackend):
    def __init__(self, mode):
        self.wires = [Wire(mode)]

    async def connect_tcp(self, *args, **kwargs):
        if self.wires[-1].aborted:
            self.wires.append(Wire("healthy"))
        wire = self.wires[-1]
        return MemoryStream(wire, ConnectionAbort(wire.abort))


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("read-timeout", 504),
        ("total-timeout", 504),
        ("headers-timeout", 504),
        ("headers-cancel", None),
        ("cancel", None),
        ("protocol", 502),
        ("read-error", 502),
        ("truncated", 502),
        ("success-close", 200),
    ],
)
def test_real_pool_disposes_hanging_close_and_recovers(config_values, mode, expected):
    async def scenario():
        settings = settings_for(
            config_values,
            HTTP_READ_IDLE_TIMEOUT=10
            if mode in {"total-timeout", "cancel", "headers-cancel"}
            else 0.02,
        )
        backend = Backend(mode)
        # A single physical slot makes leaked capacity visible on the next request.
        transport = GatewayHTTPTransport(ssl.create_default_context(), 1, network_backend=backend)
        repo = FakeRepository()
        app = create_app(settings, repository=repo, transport=transport)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client,
        ):
            started = time.monotonic()
            task = asyncio.create_task(
                client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer valid-test-key"},
                    json=BODY,
                )
            )
            if expected is None:
                await asyncio.wait_for(backend.wires[0].waiting.wait(), 1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 0.5)
            else:
                response = await asyncio.wait_for(task, 0.5)
                assert response.status_code == expected
            assert (
                time.monotonic() - started
                < settings.REQUEST_TOTAL_TIMEOUT + settings.CLEANUP_TIMEOUT + 0.1
            )
            wire = backend.wires[0]
            assert wire.aborted and wire.abort_calls == 1 and wire.close_calls == 0
            assert transport._pool._requests == []
            assert transport._pool.connections == []
            assert app.state.admission.active == 0
            assert app.state.admission.keys == {} and app.state.admission.projects == {}
            row = next(iter(repo.rows.values()))
            assert row["outcome"] == (
                "client_cancelled"
                if expected is None
                else "timeout"
                if expected == 504
                else "upstream_error"
                if expected == 502
                else "completed"
            )
            assert app.state.accounting_write_failures == 0
            for _ in range(2):
                response = await client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer valid-test-key"},
                    json=BODY,
                )
                assert response.status_code == 200
                assert app.state.admission.active == 0 and transport._pool._requests == []
            # Healthy keep-alive remains reusable; no per-request pool/client recreation.
            assert len(backend.wires) == 2 and not backend.wires[1].aborted
            assert len(transport._pool.connections) == 1
        assert backend.wires[1].aborted and transport._pool.connections == []

    asyncio.run(scenario())


@pytest.mark.parametrize("tls", [False, True])
def test_default_backend_releases_real_loopback_socket(config_values, monkeypatch, tmp_path, tls):
    # The general suite blocks networking. Permit only this local ephemeral listener.
    def loopback_connect(sock, address):
        assert address[0] in {"127.0.0.1", "::1"}
        return CONNECT(sock, address)

    def loopback_connect_ex(sock, address):
        assert address[0] in {"127.0.0.1", "::1"}
        return CONNECT_EX(sock, address)

    monkeypatch.setattr(socket.socket, "connect", loopback_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", loopback_connect_ex)
    server_ssl = None
    overrides = {}
    if tls:
        cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=IP:127.0.0.1",
            ],
            check=True,
            capture_output=True,
        )
        key.chmod(0o600)
        server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ssl.load_cert_chain(cert, key)
        overrides["UPSTREAM_CA_BUNDLE"] = str(cert)

    async def scenario():
        peer_closed = asyncio.Event()
        handlers = set()
        received = 0

        async def handler(reader, writer):
            nonlocal received
            task = asyncio.current_task()
            handlers.add(task)
            try:
                headers = await reader.readuntil(b"\r\n\r\n")
                length = next(
                    int(line.split(b":", 1)[1])
                    for line in headers.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
                await reader.readexactly(length)
                received += 1
                if received == 1:
                    writer.write(HEADERS + b"1000\r\n\r\n")
                    await writer.drain()
                    with suppress(ConnectionResetError):
                        assert await reader.read() == b""
                    peer_closed.set()
                else:
                    writer.write(
                        HEADERS
                        + str(len(PAYLOAD)).encode()
                        + b"\r\nConnection: close\r\n\r\n"
                        + PAYLOAD
                    )
                    await writer.drain()
            finally:
                writer.close()
                with suppress(ConnectionResetError, BrokenPipeError):
                    await writer.wait_closed()

        server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=server_ssl)
        port = server.sockets[0].getsockname()[1]
        repo = FakeRepository()
        app = create_app(
            settings_for(
                config_values,
                UPSTREAM_BASE_URL=f"{'https' if tls else 'http'}://127.0.0.1:{port}/v1",
                **overrides,
            ),
            repository=repo,
        )
        try:
            async with (
                server,
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://gateway"
                ) as client,
            ):
                assert isinstance(app.state.client._transport, GatewayHTTPTransport)
                response = await client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer valid-test-key"},
                    json=BODY,
                )
                assert response.status_code == 504
                await asyncio.wait_for(peer_closed.wait(), 0.5)
                pool = app.state.client._transport._pool
                assert pool._requests == [] and pool.connections == []
                assert app.state.admission.active == 0
                assert next(iter(repo.rows.values()))["outcome"] == "timeout"
                response = await client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer valid-test-key"},
                    json=BODY,
                )
                assert response.status_code == 200 and received == 2
                assert pool._requests == [] and pool.connections == []
        finally:
            server.close()
            await server.wait_closed()
            if handlers:
                await asyncio.wait_for(asyncio.gather(*handlers), 1)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["error", "cancel"])
def test_tls_handshake_failure_aborts_before_response(monkeypatch, failure):
    async def scenario():
        wire = Wire("healthy")
        stream = AbortableStream(wire, ConnectionAbort(wire.abort))

        async def broken_wrap(*args, **kwargs):
            if failure == "error":
                raise ssl.SSLError("simulated handshake failure")
            await asyncio.Event().wait()

        monkeypatch.setattr(anyio.streams.tls.TLSStream, "wrap", broken_wrap)
        with pytest.raises((httpcore.ConnectError, TimeoutError)):
            async with asyncio.timeout(0.02):
                await stream.start_tls(ssl.create_default_context(), "localhost", 1)
        assert wire.aborted and wire.abort_calls == 1 and wire.close_calls == 0

    asyncio.run(scenario())


def test_real_pool_release_survives_settlement_timeout(config_values):
    async def scenario():
        repo = FakeRepository()
        attempts = 0
        original = repo.settle

        async def stuck_settle(request_id, values):
            nonlocal attempts
            attempts += 1
            await asyncio.Event().wait()

        repo.settle = stuck_settle
        backend = Backend("protocol")
        transport = GatewayHTTPTransport(ssl.create_default_context(), 1, network_backend=backend)
        settings = settings_for(config_values)
        app = create_app(settings, repository=repo, transport=transport)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client,
        ):
            start = time.monotonic()
            response = await asyncio.wait_for(
                client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer valid-test-key"},
                    json=BODY,
                ),
                0.5,
            )
            assert response.status_code == 502
            assert time.monotonic() - start < settings.CLEANUP_TIMEOUT + 0.1
            assert attempts == settings.SETTLEMENT_MAX_ATTEMPTS
            assert app.state.accounting_write_failures == 1
            assert next(iter(repo.rows.values()))["outcome"] == "started"
            assert app.state.admission.active == 0
            assert transport._pool._requests == [] and transport._pool.connections == []
            assert backend.wires[0].aborted
            repo.settle = original
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer valid-test-key"},
                json=BODY,
            )
            assert response.status_code == 200
            assert app.state.admission.active == 0 and transport._pool._requests == []

    asyncio.run(scenario())


def test_discarding_failed_connection_preserves_other_inflight_request(config_values):
    async def scenario():
        class GatedWire(Wire):
            def __init__(self, mode):
                super().__init__(mode)
                self.allow = asyncio.Event()

            async def receive(self, max_bytes=65536):
                self.waiting.set()
                await self.allow.wait()
                return await super().receive(max_bytes)

        bad, good = GatedWire("protocol"), GatedWire("healthy")

        class TwoConnections(httpcore.AsyncNetworkBackend):
            def __init__(self):
                self.wires = iter((bad, good))

            async def connect_tcp(self, *args, **kwargs):
                wire = next(self.wires)
                return MemoryStream(wire, ConnectionAbort(wire.abort))

        transport = GatewayHTTPTransport(
            ssl.create_default_context(), 2, network_backend=TwoConnections()
        )
        app = create_app(
            settings_for(config_values, HTTP_READ_IDLE_TIMEOUT=1, REQUEST_TOTAL_TIMEOUT=2),
            repository=FakeRepository(),
            transport=transport,
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client,
        ):

            async def send():
                return await client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer valid-test-key"},
                    json=BODY,
                )

            first = asyncio.create_task(send())
            await asyncio.wait_for(bad.waiting.wait(), 0.5)
            second = asyncio.create_task(send())
            await asyncio.wait_for(good.waiting.wait(), 0.5)
            bad.allow.set()
            assert (await asyncio.wait_for(first, 0.5)).status_code == 502
            assert bad.aborted and not good.aborted and not second.done()
            assert len(transport._pool._requests) == len(transport._pool.connections) == 1
            assert app.state.admission.active == 1
            good.allow.set()
            assert (await asyncio.wait_for(second, 0.5)).status_code == 200
            assert (await send()).status_code == 200
            assert transport._pool._requests == [] and app.state.admission.active == 0
            assert not good.aborted
        assert good.aborted

    asyncio.run(scenario())


def test_cancelling_pool_waiter_preserves_connection_owner(config_values):
    async def scenario():
        class HeldWire(Wire):
            def __init__(self):
                super().__init__("healthy")
                self.allow = asyncio.Event()

            async def receive(self, max_bytes=65536):
                self.waiting.set()
                await self.allow.wait()
                return await super().receive(max_bytes)

        wire = HeldWire()

        class SingleConnection(httpcore.AsyncNetworkBackend):
            async def connect_tcp(self, *args, **kwargs):
                return MemoryStream(wire, ConnectionAbort(wire.abort))

        transport = GatewayHTTPTransport(
            ssl.create_default_context(), 1, network_backend=SingleConnection()
        )
        repo = FakeRepository()
        app = create_app(
            settings_for(config_values, HTTP_READ_IDLE_TIMEOUT=1, REQUEST_TOTAL_TIMEOUT=2),
            repository=repo,
            transport=transport,
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client,
        ):

            async def send():
                return await client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer valid-test-key"},
                    json=BODY,
                )

            owner = asyncio.create_task(send())
            waiter = None
            try:
                await asyncio.wait_for(wire.waiting.wait(), 0.5)
                waiter = asyncio.create_task(send())
                async with asyncio.timeout(0.5):
                    while len(transport._pool._requests) != 2:
                        await asyncio.sleep(0)
                assert transport._pool._requests[1].is_queued()
                assert app.state.admission.active == 2
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(waiter, 0.5)
                assert not owner.done() and not wire.aborted
                assert len(transport._pool._requests) == len(transport._pool.connections) == 1
                assert app.state.admission.active == 1
                assert sorted(row["outcome"] for row in repo.rows.values()) == [
                    "client_cancelled",
                    "started",
                ]
                wire.allow.set()
                assert (await asyncio.wait_for(owner, 0.5)).status_code == 200
                assert (await send()).status_code == 200
                assert transport._pool._requests == [] and len(transport._pool.connections) == 1
                assert app.state.admission.active == 0
                assert app.state.admission.keys == {} and app.state.admission.projects == {}
                assert not wire.aborted and app.state.accounting_write_failures == 0
                assert sorted(row["outcome"] for row in repo.rows.values()) == [
                    "client_cancelled",
                    "completed",
                    "completed",
                ]
            finally:
                wire.allow.set()
                for task in (owner, waiter):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(
                    *(t for t in (owner, waiter) if t is not None), return_exceptions=True
                )
        assert wire.aborted and wire.abort_calls == 1
        assert transport._pool.connections == [] and transport._pool._requests == []

    asyncio.run(scenario())
