"""Shared database cleanup, caller deadlines, background lane and pool events."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from test_nonstream_slice import FakeRepository, slice_settings

from app.db.models import REVISION
from app.db.repository import Repository
from app.db.runtime import DB_DEADLINE, database_timeout
from app.main import create_app


async def operation(repo, method):
    if method == "ready":
        return await repo.ready()
    if method == "authenticate":
        return await repo.authenticate("a" * 64)
    if method == "register":
        return await repo.register(dict(request_id=uuid4()))
    if method == "mark_upstream":
        return await repo.mark_upstream(uuid4())
    return await repo.settle(uuid4(), {"outcome": "completed"})


@pytest.mark.parametrize("method", ["ready", "authenticate", "register", "mark_upstream", "settle"])
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("fault", ["close", "sql"])
def test_all_repository_paths_share_bounded_cleanup(monkeypatch, method, cancel, fault):
    async def run():
        drivers = []
        entered = asyncio.Event()
        recovered = False

        class Statement:
            def __init__(self, sql):
                self.sql = sql

            def get_attributes(self):
                return [SimpleNamespace(name="value", type=SimpleNamespace(oid=25))]

            async def fetch(self, *args):
                if fault == "sql" and not recovered:
                    entered.set()
                    await asyncio.Event().wait()
                if "alembic_version" in self.sql:
                    return [(REVISION,)]
                if "information_schema" in self.sql:
                    return [(5,)]
                if "RETURNING" in self.sql:
                    return [(uuid4(),)]
                return []

            def get_statusmsg(self):
                return "SELECT 1"

        class Transaction:
            async def start(self):
                pass

            async def commit(self):
                pass

            async def rollback(self):
                pass

        class Driver:
            def __init__(self):
                self.closed = False
                self.terminated = asyncio.Event()
                drivers.append(self)

            async def set_type_codec(self, *args, **kwargs):
                pass

            async def prepare(self, sql, **kwargs):
                return Statement(sql)

            def transaction(self, **kwargs):
                return Transaction()

            def is_closed(self):
                return self.closed

            def terminate(self):
                self.closed = True
                self.terminated.set()

            async def close(self, **kwargs):
                if not recovered:
                    await self.terminated.wait()
                self.terminate()

        async def creator():
            return Driver()

        engine = create_async_engine(
            "postgresql+asyncpg://", async_creator=creator, pool_size=1, max_overflow=0
        )
        monkeypatch.setattr(engine.sync_engine.dialect, "initialize", lambda conn: None)
        repo = Repository(engine, cleanup_timeout=5)
        original = AsyncConnection.close

        async def hanging_close(connection):
            if fault == "close" and not recovered:
                entered.set()
                await asyncio.Event().wait()
            await original(connection)

        monkeypatch.setattr(AsyncConnection, "close", hanging_close)

        async def call():
            async with database_timeout(0.04):
                await operation(repo, method)

        baseline = set(asyncio.all_tasks())
        started = asyncio.get_running_loop().time()
        task = asyncio.create_task(call())
        await asyncio.wait_for(entered.wait(), 0.5)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await asyncio.wait_for(task, 0.5)
        assert asyncio.get_running_loop().time() - started < 0.3
        assert DB_DEADLINE.get() is None
        snapshot = repo.pool_observer.snapshot()
        assert snapshot["checked_out"] == engine.pool.checkedout() == 0
        assert snapshot["checkout"] == snapshot["checkin"] == 1
        # Driver cancellation may start invalidation before the hard-deadline
        # callback. Counters count callbacks, not distinct physical sockets.
        assert snapshot["invalidate"] == snapshot["close"] >= 1
        closes = snapshot["close"]
        assert drivers[0].is_closed()
        await asyncio.sleep(0)
        assert not [t for t in asyncio.all_tasks() - baseline if not t.done()]
        recovered = True
        async with database_timeout(1):
            await operation(repo, method)
        assert len(drivers) == 2
        snapshot = repo.pool_observer.snapshot()
        assert snapshot["checked_out"] == engine.pool.checkedout() == 0
        assert snapshot["connect"] == snapshot["checkout"] == snapshot["checkin"] == 2
        assert snapshot["reset"] >= 1
        assert snapshot["peak_checked_out"] == 1
        await engine.dispose()
        assert repo.pool_observer.snapshot()["close"] == closes + 1

    asyncio.run(run())


def test_nested_database_deadlines_never_extend_parent():
    async def run():
        assert DB_DEADLINE.get() is None
        async with database_timeout(1):
            outer = DB_DEADLINE.get()
            async with database_timeout(5):
                assert DB_DEADLINE.get() == outer
            async with database_timeout(0.01):
                assert DB_DEADLINE.get() < outer
            assert DB_DEADLINE.get() == outer
        assert DB_DEADLINE.get() is None

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_background_lane_is_bounded_and_recovers(config_values, cancel):
    async def run():
        repo = FakeRepository()
        settings = slice_settings(config_values).model_copy(update={"DB_OPERATION_TIMEOUT": 0.05})
        app = create_app(settings, repository=repo)
        entered = asyncio.Queue()

        async def slow():
            entered.put_nowait(True)
            await asyncio.Event().wait()

        async with app.router.lifespan_context(app):
            original = repo.ready
            repo.ready = slow
            tasks = [
                asyncio.create_task(app.state.refresh_ready())
                for _ in range(settings.BACKGROUND_DB_MAX_INFLIGHT)
            ]
            for _ in tasks:
                await asyncio.wait_for(entered.get(), 0.5)
            assert app.state.background_db_inflight() == settings.BACKGROUND_DB_MAX_INFLIGHT
            await asyncio.wait_for(app.state.refresh_ready(), 0.02)
            assert entered.empty()
            if cancel:
                for task in tasks:
                    task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            if cancel:
                assert all(isinstance(result, asyncio.CancelledError) for result in results)
            assert app.state.background_db_inflight() == 0
            repo.ready = original
            await app.state.refresh_ready()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client:
                assert (await client.get("/health/ready")).status_code == 200

    asyncio.run(run())


@pytest.mark.parametrize(
    "stage", ["connect", "codec", "ping", "ping_rollback", "ping_rollback_error", "ping_reconnect"]
)
@pytest.mark.parametrize("cancel", [False, True])
def test_acquisition_deadline_pre_ping_and_peer_isolation(monkeypatch, stage, cancel):
    check_acquisition_fault(monkeypatch, stage, cancel)


@pytest.mark.parametrize(
    "stage", ["ping", "ping_rollback", "ping_rollback_error", "ping_reconnect"]
)
@pytest.mark.parametrize(
    "cancel_delay", [0.020, 0.035], ids=["20ms-before-deadline", "5ms-before-deadline"]
)
def test_acquisition_cancellation_near_deadline(monkeypatch, stage, cancel_delay):
    check_acquisition_fault(monkeypatch, stage, True, cancel_delay=cancel_delay)


def check_acquisition_fault(monkeypatch, stage, cancel, *, cancel_delay=0):
    async def run():
        entered = asyncio.Event()
        armed = False
        drivers = []

        class Transaction:
            async def start(self):
                pass

            async def rollback(self):
                if armed and stage in {"ping", "ping_rollback", "ping_rollback_error"}:
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        if stage == "ping_rollback_error":
                            raise asyncpg.InterfaceError("connection is closed") from None
                        raise

        class Driver:
            def __init__(self):
                self.dead = asyncio.Event()
                drivers.append(self)

            async def set_type_codec(self, *args, **kwargs):
                if armed and stage == "codec":
                    entered.set()
                    await asyncio.Event().wait()

            def transaction(self, **kwargs):
                return Transaction()

            async def fetchrow(self, *args):
                if armed and stage == "ping_reconnect":
                    self.terminate()
                    raise asyncpg.ConnectionDoesNotExistError("connection lost")
                if armed and stage == "ping":
                    entered.set()
                    await asyncio.Event().wait()

            def is_closed(self):
                return self.dead.is_set()

            def terminate(self):
                self.dead.set()

            async def close(self, **kwargs):
                if armed and stage == "ping_rollback_error":
                    raise AssertionError("acquisition invalidation must not await graceful close")
                if armed:
                    await self.dead.wait()
                self.terminate()

        async def creator():
            if armed and (stage == "connect" or (stage == "ping_reconnect" and len(drivers) >= 2)):
                entered.set()
                await asyncio.Event().wait()
            return Driver()

        engine = create_async_engine(
            "postgresql+asyncpg://",
            async_creator=creator,
            pool_pre_ping=True,
            pool_size=2,
            max_overflow=0,
        )
        monkeypatch.setattr(engine.sync_engine.dialect, "initialize", lambda c: None)
        repo = Repository(engine, cleanup_timeout=0.06)
        peer = await engine.connect()
        peer_driver = peer.sync_connection.connection.driver_connection
        if stage.startswith("ping"):
            warm = await engine.connect()
            await warm.close()
        armed = True
        baseline = set(asyncio.all_tasks())

        async def call():
            cancellation = None
            try:
                async with database_timeout(0.04):
                    if cancel_delay:
                        deadline = DB_DEADLINE.get()
                        task = asyncio.current_task()
                        assert deadline is not None and task is not None
                        # Schedule against the same absolute deadline. If the loop
                        # stalls, this cancellation still precedes timeout expiry.
                        cancellation = asyncio.get_running_loop().call_at(
                            deadline - 0.04 + cancel_delay, task.cancel
                        )
                    async with repo.connection():
                        raise AssertionError(
                            "fault must be reached before connection is handed out"
                        )
            finally:
                if cancellation is not None:
                    cancellation.cancel()

        started = asyncio.get_running_loop().time()
        task = asyncio.create_task(call())
        await asyncio.wait_for(entered.wait(), 0.5)
        snapshot = repo.pool_observer.snapshot()
        assert snapshot["checked_out"] == engine.pool.checkedout() == 2
        assert snapshot["successful_checkouts_active"] == 1
        if cancel and not cancel_delay:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await asyncio.wait_for(task, 0.3)
        assert asyncio.get_running_loop().time() - started < 0.25
        assert task.cancelling() == (1 if cancel else 0)
        assert engine.pool.checkedout() == repo.pool_observer.snapshot()["checked_out"] == 1
        assert repo.pool_observer.snapshot()["peak_checked_out"] == 2
        assert not peer_driver.is_closed()
        assert all(d.is_closed() for d in drivers if d is not peer_driver)
        await asyncio.sleep(0)
        assert not [t for t in asyncio.all_tasks() - baseline if not t.done()]
        armed = False
        async with database_timeout(0.2):
            async with repo.connection():
                pass
        assert engine.pool.checkedout() == 1
        await peer.close()
        assert engine.pool.checkedout() == repo.pool_observer.snapshot()["checked_out"] == 0
        await engine.dispose()

    asyncio.run(run())
