"""Real SQLAlchemy connection/pool/asyncpg adapter lifecycle, with a faulting driver."""

import asyncio

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from test_nonstream_slice import FakeRepository, slice_settings

from app.db.repository import Repository
from app.main import create_app


@pytest.mark.parametrize("fault", ["close", "rollback"])
@pytest.mark.parametrize("cancel", [False, True])
def test_admin_cleanup_discards_only_faulted_connection(config_values, monkeypatch, fault, cancel):
    async def run():
        drivers = []
        entered = asyncio.Event()
        recovery = False

        class Driver:
            def __init__(self):
                self.terminated = asyncio.Event()
                drivers.append(self)

            async def set_type_codec(self, *args, **kwargs):
                pass

            def is_closed(self):
                return self.terminated.is_set()

            def terminate(self):
                self.terminated.set()

            async def close(self, **kwargs):
                # Even dialect graceful termination is stuck until hard abort.
                await self.terminated.wait()

        async def creator():
            return Driver()

        engine = create_async_engine(
            "postgresql+asyncpg://",
            async_creator=creator,
            pool_size=2,
            max_overflow=0,
        )
        monkeypatch.setattr(engine.sync_engine.dialect, "initialize", lambda conn: None)
        original_close = AsyncConnection.close
        faulted = []

        class Transaction:
            async def rollback(self):
                entered.set()
                await asyncio.Event().wait()

        async def execute(conn, *args, **kwargs):
            if not recovery:
                faulted.append(conn)
                # Use real SQLAlchemy transaction close -> dialect -> driver rollback.
                await conn.begin()
                adapted = conn.sync_connection.connection.dbapi_connection
                adapted._started = True
                adapted._transaction = Transaction()

            class Rows:
                def mappings(self):
                    return []

            return Rows()

        async def close(conn):
            if fault == "close" and conn in faulted and not recovery:
                entered.set()
                await asyncio.Event().wait()
            await original_close(conn)

        monkeypatch.setattr(AsyncConnection, "execute", execute)
        monkeypatch.setattr(AsyncConnection, "close", close)
        repo = FakeRepository()
        Repository.__init__(repo, engine, cleanup_timeout=0.08)
        config = slice_settings(config_values).model_copy(
            update={
                "DB_OPERATION_TIMEOUT": 0.04,
                "DB_READY_CHECK_INTERVAL": 60,
            }
        )
        app = create_app(config, repository=repo)
        admin = {"Authorization": "Bearer " + config.ADMIN_API_KEY.get_secret_value()}
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client,
        ):
            # Keep an unrelated connection checked out throughout the failure.
            peer = await engine.connect()
            peer_driver = peer.sync_connection.connection.driver_connection
            baseline = set(asyncio.all_tasks())
            started = asyncio.get_running_loop().time()
            task = asyncio.create_task(client.get("/admin/projects", headers=admin))
            await asyncio.wait_for(entered.wait(), 0.5)
            assert app.state.admin_db_active == 1
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 0.5)
            else:
                response = await asyncio.wait_for(task, 0.5)
                assert response.status_code == 503
                assert response.json()["error"]["code"] == "database_unavailable"
                assert response.headers["retry-after"]
            assert asyncio.get_running_loop().time() - started < 0.4
            assert app.state.admin_db_active == 0
            assert engine.pool.checkedout() == 1
            assert drivers[1].terminated.is_set()
            assert not peer_driver.is_closed()
            await asyncio.sleep(0)
            assert not [t for t in asyncio.all_tasks() - baseline if not t.done()]
            recovery = True
            assert (await client.get("/admin/projects", headers=admin)).status_code == 200
            assert len(drivers) == 3
            assert engine.pool.checkedout() == 1
            await peer.close()
            assert engine.pool.checkedout() == 0
        # Do not invoke fake driver's deliberately hanging graceful close.
        for driver in drivers:
            driver.terminate()
        await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["sql", "commit", "pool_wait"])
def test_admin_cancellation_during_database_work(config_values, monkeypatch, phase):
    """Keep SQLAlchemy execute/commit/checkout real; pause only the fake driver."""

    async def run():
        from types import SimpleNamespace

        from sqlalchemy import text

        entered = asyncio.Event()
        drivers = []
        recovering = False

        class Statement:
            def get_attributes(self):
                return [SimpleNamespace(name="value", type=SimpleNamespace(oid=23))]

            async def fetch(self, *args):
                if phase == "sql" and not recovering:
                    entered.set()
                    await asyncio.Event().wait()
                return [(1,)]

            def get_statusmsg(self):
                return "SELECT 1"

        class Transaction:
            async def start(self):
                pass

            async def commit(self):
                if phase == "commit" and not recovering:
                    entered.set()
                    await asyncio.Event().wait()

            async def rollback(self):
                pass

        class Driver:
            def __init__(self):
                self.closed = False
                drivers.append(self)

            async def set_type_codec(self, *args, **kwargs):
                pass

            async def prepare(self, *args, **kwargs):
                return Statement()

            def transaction(self, **kwargs):
                return Transaction()

            def is_closed(self):
                return self.closed

            def terminate(self):
                self.closed = True

            async def close(self, **kwargs):
                self.terminate()

        async def creator():
            return Driver()

        engine = create_async_engine(
            "postgresql+asyncpg://",
            async_creator=creator,
            pool_size=1,
            max_overflow=0,
        )
        monkeypatch.setattr(engine.sync_engine.dialect, "initialize", lambda conn: None)

        class ProbeRepository(FakeRepository):
            async def list_resources(self, *args, **kwargs):
                if phase == "pool_wait" and not recovering:
                    entered.set()
                async with self.admin_connection(write=phase == "commit") as connection:
                    assert (await connection.execute(text("SELECT 1"))).scalar_one() == 1
                return []

        repo = ProbeRepository()
        Repository.__init__(repo, engine, cleanup_timeout=0.08)
        settings = slice_settings(config_values)
        app = create_app(settings, repository=repo)
        admin = {"Authorization": "Bearer " + settings.ADMIN_API_KEY.get_secret_value()}
        holder = None
        try:
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://gateway"
                ) as client,
            ):
                if phase == "pool_wait":
                    holder = await engine.connect()
                baseline = set(asyncio.all_tasks())
                task = asyncio.create_task(client.get("/admin/projects", headers=admin))
                await asyncio.wait_for(entered.wait(), 0.5)
                assert app.state.admin_db_active == 1
                if phase == "pool_wait":
                    # Prove actual checkout queuing, not just cancelling an unstarted task.
                    assert len(engine.pool._pool._queue._getters) == 1
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 0.5)
                assert app.state.admin_db_active == 0
                assert engine.pool.checkedout() == (1 if holder is not None else 0)
                if holder is not None:
                    assert not drivers[0].is_closed()
                    assert not engine.pool._pool._queue._getters
                    assert (await holder.execute(text("SELECT 1"))).scalar_one() == 1
                    await holder.close()
                    holder = None
                else:
                    assert drivers[0].is_closed()
                await asyncio.sleep(0)
                assert not [t for t in asyncio.all_tasks() - baseline if not t.done()]
                recovering = True
                assert (await client.get("/admin/projects", headers=admin)).status_code == 200
                assert app.state.admin_db_active == engine.pool.checkedout() == 0
                assert len(drivers) == (1 if phase == "pool_wait" else 2)
        finally:
            if holder is not None:
                await holder.close()
            await engine.dispose()

    asyncio.run(run())
