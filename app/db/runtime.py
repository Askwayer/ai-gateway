"""Database deadlines and bounded, credential-free pool observations."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, cast

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.pool import AsyncAdaptedQueuePool

DB_DEADLINE: ContextVar[float | None] = ContextVar("db_deadline", default=None)


@asynccontextmanager
async def database_timeout(seconds: float, *, deadline: float | None = None) -> AsyncIterator[None]:
    end = asyncio.get_running_loop().time() + seconds
    parent = DB_DEADLINE.get()
    end = min(end, deadline) if deadline is not None else end
    end = min(end, parent) if parent is not None else end
    token = DB_DEADLINE.set(end)
    try:
        async with asyncio.timeout_at(end):
            yield
    finally:
        DB_DEADLINE.reset(token)


class PoolObserver:
    """Cumulative counters only; live record identities are bounded by pool capacity."""

    def __init__(self, engine: AsyncEngine):
        self.pool = cast(AsyncAdaptedQueuePool, engine.sync_engine.pool)
        self.totals = dict.fromkeys(
            ("connect", "checkout", "checkin", "reset", "invalidate", "close"), 0
        )
        self.active: set[int] = set()
        self.peak = 0
        for name in self.totals:
            event.listen(engine.sync_engine, name, self.listener(name), named=True)
        install_acquisition_guard(engine, self)

    def listener(self, name: str) -> Any:
        def record(**values: Any) -> None:
            entry = values["connection_record"]
            self.totals[name] += 1
            if name == "checkout":
                self.active.add(id(entry))
                self.sample()
            elif name == "checkin":
                self.active.discard(id(entry))

        return record

    def sample(self) -> int:
        occupied = self.pool.checkedout()
        self.peak = max(self.peak, occupied)
        return occupied

    def snapshot(self) -> dict[str, int]:
        return {
            **self.totals,
            "checked_out": self.sample(),
            "successful_checkouts_active": len(self.active),
            "peak_checked_out": self.peak,
        }


# Acquisition starts before SQLAlchemy emits checkout (pre-ping runs earlier).
# Bind the actual driver via do_connect/do_ping; never abort a whole pool.
_ACQUIRING: ContextVar["_Acquisition | None"] = ContextVar("db_acquiring", default=None)


class _Acquisition:
    def __init__(self, deadline: float):
        self.loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        assert task is not None
        self.task = task
        self.initial_cancels = task.cancelling()
        self.cancellations = 0
        self.driver: Any = None
        self.expired = False
        self.again: asyncio.Handle | None = None
        self.timer = self.loop.call_at(deadline, self.expire)
        self.deadline = deadline

    def bind(self, adapter: Any) -> None:
        self.driver = adapter.driver_connection
        if self.expired:
            self.abort()
            raise TimeoutError

    def abort(self) -> None:
        if self.driver is not None and not self.driver.is_closed():
            self.driver.terminate()

    def cancel(self) -> None:
        if self.task.cancel():
            self.cancellations += 1

    def expire(self) -> None:
        self.expired = True
        self.abort()
        self.cancel()
        # An initial cancellation can enter a driver's rollback/finally await.
        # Interrupt that await as well; the socket has already been terminated.
        self.again = self.loop.call_soon(self.cancel)

    def finish(self) -> None:
        self.timer.cancel()
        if self.again is not None:
            self.again.cancel()
        # Preserve caller cancellation, while removing only our own requests.
        for _ in range(self.cancellations):
            self.task.uncancel()


async def acquire_connection(engine: AsyncEngine, fallback_timeout: float) -> AsyncConnection:
    deadline = DB_DEADLINE.get()
    if deadline is None:
        deadline = asyncio.get_running_loop().time() + fallback_timeout
    if deadline <= asyncio.get_running_loop().time():
        raise TimeoutError
    guard = _Acquisition(deadline)
    token = _ACQUIRING.set(guard)
    try:
        connection = await engine.connect()
        if guard.expired:
            sync = connection.sync_connection
            assert sync is not None
            sync.invalidate()
            sync.close()
            raise TimeoutError
        return connection
    except asyncio.CancelledError:
        guard.abort()
        if guard.expired and guard.task.cancelling() - guard.cancellations <= guard.initial_cancels:
            raise TimeoutError from None
        raise
    except BaseException:
        guard.abort()
        # A driver rollback error must not replace a pending caller cancellation.
        if guard.task.cancelling() - guard.cancellations > guard.initial_cancels:
            raise asyncio.CancelledError from None
        if guard.expired:
            raise TimeoutError from None
        raise
    finally:
        guard.finish()
        _ACQUIRING.reset(token)


def install_acquisition_guard(engine: AsyncEngine, observer: PoolObserver) -> None:
    def connect(dialect: Any, entry: Any, args: Any, params: Any) -> Any:
        observer.sample()
        guard = _ACQUIRING.get()
        options = dict(params)
        if guard is not None:
            remaining = guard.deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            options["timeout"] = min(options.get("timeout", remaining), remaining)
        adapter = dialect.connect(*args, **options)
        if guard is not None:
            guard.bind(adapter)
        return adapter

    original_ping = engine.sync_engine.dialect.do_ping

    def ping(dbapi_connection: Any) -> bool:
        observer.sample()
        guard = _ACQUIRING.get()
        if guard is not None:
            guard.bind(dbapi_connection)
        return original_ping(dbapi_connection)

    original_terminate = engine.sync_engine.dialect.do_terminate

    def terminate(dbapi_connection: Any) -> None:
        guard = _ACQUIRING.get()
        if guard is not None and guard.driver is dbapi_connection.driver_connection:
            # SQLAlchemy's asyncpg invalidation normally awaits a shielded close.
            # Cancellation there can interrupt the pre-ping reconnect branch
            # before it checks the pool record back in. Hard termination of this
            # acquisition's driver is synchronous; leave record return to the pool.
            guard.abort()
        else:
            original_terminate(dbapi_connection)

    engine.sync_engine.dialect.do_terminate = terminate  # type: ignore[method-assign]
    event.listen(engine.sync_engine, "do_connect", connect, retval=True)
    engine.sync_engine.dialect.do_ping = ping  # type: ignore[method-assign]
