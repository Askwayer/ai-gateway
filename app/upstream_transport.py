"""HTTP/1.1 transport with non-waiting physical connection disposal.

HTTPcore closes a failed stream inside its iterator before propagating errors.
A response-level timeout cannot safely interrupt that close: pool bookkeeping
would be skipped. Abort the physical asyncio transport synchronously instead.
Keep-alive reuse never calls this physical close path.

Compatibility boundary: HTTPX's _pool and AnyIO's asyncio SocketStream._transport
are covered by transport-stack and loopback tests. Recheck on dependency upgrades.
"""

import asyncio
import ssl
from collections.abc import Callable, Iterable
from typing import Any

import anyio
import httpcore
import httpx
from httpcore._backends.anyio import AnyIOBackend, AnyIOStream
from httpcore._backends.base import SOCKET_OPTION


class ConnectionAbort:
    def __init__(self, abort: Callable[[], None]):
        self.abort = abort
        self.called = False

    def __call__(self) -> None:
        if not self.called:
            self.abort()
            self.called = True


class AbortableStream(AnyIOStream):
    def __init__(self, stream: anyio.abc.ByteStream, abort: ConnectionAbort):
        super().__init__(stream)
        self.abort = abort

    async def aclose(self) -> None:
        # Deliberately no await: cancellation cannot split physical disposal from
        # httpcore's following pool-request removal. Do not await TLS close_notify.
        self.abort()

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            tls = await super().start_tls(ssl_context, server_hostname, timeout)
        except BaseException:
            # Also cover cancellation during the handshake, before a response exists.
            self.abort()
            raise
        assert isinstance(tls, AnyIOStream)
        return AbortableStream(tls._stream, self.abort)


class AbortableBackend(AnyIOBackend):
    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raw = await super().connect_tcp(host, port, timeout, local_address, socket_options)
        assert isinstance(raw, AnyIOStream)
        transport: Any = getattr(raw._stream, "_transport", None)
        if not isinstance(transport, asyncio.Transport):
            # Fail closed if a dependency/backend change invalidates this adapter.
            await anyio.aclose_forcefully(raw._stream)
            raise RuntimeError("upstream transport requires the pinned asyncio socket backend")
        return AbortableStream(raw._stream, ConnectionAbort(transport.abort))


class GatewayHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(
        self,
        verify: ssl.SSLContext,
        max_connections: int,
        *,
        network_backend: httpcore.AsyncNetworkBackend | None = None,
    ):
        # Reuse HTTPX's request/response conversion and exception translation.
        # No proxy, HTTP/2 or retry support is enabled by this slice.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=verify,
            max_connections=max_connections,
            max_keepalive_connections=max_connections,
            http1=True,
            http2=False,
            retries=0,
            network_backend=network_backend or AbortableBackend(),
        )
