"""Lost DDL acknowledgements and journal recovery must never adopt resources."""

import asyncio
import importlib.util
import json
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location(
    "verify_db_fixture", SCRIPTS / "verify_database_foundation.py"
)
assert spec and spec.loader
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class Server:
    def __init__(self):
        self.roles = {}
        self.database = None
        self.cluster = "fixture-cluster"
        self.connections = []
        self.drop_calls = []
        self.fault = None
        self.journal = None

    async def connect(self, url):
        connection = Admin(self)
        self.connections.append(connection)
        return connection


class Admin:
    def __init__(self, server):
        self.server = server
        self.closed = False
        self.pid = len(server.connections) + 100

    @asynccontextmanager
    async def transaction(self):
        yield
        if self.server.fault == "role_commit_ack":
            self.server.fault = None
            self.closed = True
            raise ConnectionError("lost commit confirmation")

    async def execute(self, sql):
        assert not self.closed
        s = self.server
        name = re.search(r'"([a-z0-9_]+)"', sql)[1]
        # The on-disk intent must precede the first server-side mutation.
        journal = json.loads(s.journal.read_text())
        assert journal["phase"].endswith("started") or journal["phase"].endswith("confirmed")
        if sql.startswith("CREATE ROLE"):
            assert name not in s.roles
            s.roles[name] = {"oid": len(s.roles) + 1, "rolname": name, "marker": None}
        elif sql.startswith("COMMENT ON ROLE"):
            s.roles[name]["marker"] = sql.split(" IS '")[1][:-1]
        elif sql.startswith("CREATE DATABASE"):
            owner = re.findall(r'"([a-z0-9_]+)"', sql)[1]
            s.database = {"oid": 42, "datdba": s.roles[owner]["oid"], "marker": None}
            if s.fault == "database_create_ack":
                s.fault = None
                self.closed = True
                raise ConnectionError("lost create confirmation")
        elif sql.startswith("COMMENT ON DATABASE"):
            s.database["marker"] = sql.split(" IS '")[1][:-1]
        elif sql.startswith("DROP DATABASE"):
            s.drop_calls.append(sql)
            s.database = None
            if s.fault == "database_drop_ack":
                s.fault = None
                self.closed = True
                raise ConnectionError("lost drop confirmation")
        elif sql.startswith("DROP ROLE"):
            s.drop_calls.append(sql)
            if s.fault == "first_role_drop":
                s.fault = None
                raise RuntimeError("temporary drop failure")
            del s.roles[name]
        else:
            raise AssertionError("unexpected DDL")

    async def fetchval(self, sql, *args):
        assert not self.closed
        if "pg_stat_activity" in sql:
            return any(c.pid == args[0] and not c.closed for c in self.server.connections)
        if "system_identifier" in sql:
            return self.server.cluster
        if "pg_database" in sql:
            return self.server.database is not None
        if "pg_roles" in sql:
            return bool(self.server.roles)
        raise AssertionError("unexpected catalog query")

    async def fetch(self, sql, *args):
        assert not self.closed
        return list(self.server.roles.values())

    async def fetchrow(self, sql, *args):
        assert not self.closed
        if "pg_stat_activity" in sql:
            return {"pid": self.pid, "backend_start": "2026-11-15 00:00:00+00"}
        return self.server.database

    async def close(self, **kwargs):
        self.closed = True

    def terminate(self):
        self.closed = True


async def prepare(monkeypatch, tmp_path):
    monkeypatch.setattr(fixture, "ROOT", tmp_path)
    server = Server()
    monkeypatch.setattr(fixture, "connect", server.connect)
    admin = await server.connect("private")
    resources = await fixture.DisposableResources.plan(admin)
    server.journal = resources.path
    assert resources.path.stat().st_mode & 0o777 == 0o600
    assert resources.path.parent.stat().st_mode & 0o777 == 0o700
    return server, admin, resources


@pytest.mark.parametrize(
    "fault", ["role_commit_ack", "database_create_ack", "database_drop_ack", "first_role_drop"]
)
def test_lost_ddl_ack_recovers_from_durable_intent(monkeypatch, tmp_path, fault):
    async def run():
        server, admin, resources = await prepare(monkeypatch, tmp_path)
        server.fault = fault
        try:
            await resources.create_role(admin, resources.runtime, "fixture-password")
            await resources.create_role(admin, resources.owner, "fixture-password")
            await resources.create_database(admin)
        except ConnectionError:
            assert fault in ("role_commit_ack", "database_create_ack")
        # A dead provisioning connection and a reconstructed object simulate restart.
        admin.closed = True
        restored = fixture.DisposableResources.load(resources.path)
        await restored.cleanup("private")
        assert not server.roles and server.database is None
        assert len(server.connections) >= 2
        assert all(c.closed for c in server.connections)
        assert restored.data["phase"] == "removed"
        assert "fixture-password" not in resources.path.read_text()
        assert "SCRAM" not in resources.path.read_text()
        # Repeated recovery is safe and does not issue another DROP.
        previous = len(server.drop_calls)
        await restored.cleanup("private")
        assert len(server.drop_calls) == previous

    asyncio.run(run())


@pytest.mark.parametrize(
    "mismatch", ["cluster", "role_marker", "database_owner", "database_marker"]
)
def test_recovery_refuses_foreign_resources(monkeypatch, tmp_path, mismatch):
    async def run():
        server, admin, resources = await prepare(monkeypatch, tmp_path)
        for role in (resources.runtime, resources.owner):
            await resources.create_role(admin, role, "fixture-password")
        await resources.create_database(admin)
        if mismatch == "cluster":
            server.cluster = "other-cluster"
        elif mismatch == "role_marker":
            server.roles[resources.owner]["marker"] = "foreign"
        elif mismatch == "database_owner":
            server.database["datdba"] = 999
        else:
            server.database["marker"] = "foreign"
        await admin.close()
        with pytest.raises(RuntimeError, match="cleanup incomplete"):
            await resources.cleanup("private")
        assert not server.drop_calls
        assert server.database and len(server.roles) == 2
        assert len(server.connections) == 2  # Non-transient mismatch is not retried.
        assert server.connections[-1].closed

    asyncio.run(run())


@pytest.mark.parametrize("existing", ["database", "role"])
def test_plan_never_adopts_existing_names(monkeypatch, tmp_path, existing):
    async def run():
        monkeypatch.setattr(fixture, "ROOT", tmp_path)
        server = Server()
        if existing == "database":
            server.database = {"oid": 42}
        else:
            server.roles["existing"] = {"oid": 42}
        with pytest.raises(ValueError, match="already exists"):
            await fixture.DisposableResources.plan(await server.connect("private"))
        assert not server.drop_calls
        assert not list(tmp_path.rglob("*.json"))

    asyncio.run(run())


def test_relative_recovery_journal_is_normalized(monkeypatch, tmp_path):
    async def run():
        server, admin, resources = await prepare(monkeypatch, tmp_path)
        monkeypatch.chdir(tmp_path)
        relative = resources.path.relative_to(tmp_path)
        loaded = fixture.DisposableResources.load(relative)
        assert loaded.path == resources.path
        await admin.close()
        await loaded.cleanup("private")
        assert not server.drop_calls
        await admin.close()

    asyncio.run(run())


@pytest.mark.parametrize("late_result", ["role_commit", "database_commit", "rollback"])
def test_cleanup_waits_for_creator_before_catalog_and_late_commit(
    monkeypatch, tmp_path, late_result
):
    async def run():
        server, creator, resources = await prepare(monkeypatch, tmp_path)
        creator.closed = True  # Socket gone, but the backend is still working.
        pending = True
        observed = asyncio.Event()
        original = Admin.fetchval

        async def fetchval(connection, sql, *args):
            if "pg_stat_activity" in sql:
                assert args == (creator.pid, resources.data["creator_session"]["backend_start"])
                observed.set()
                return pending
            return await original(connection, sql, *args)

        monkeypatch.setattr(Admin, "fetchval", fetchval)
        cleanup = asyncio.create_task(resources.cleanup("private"))
        await asyncio.wait_for(observed.wait(), 0.2)
        assert not cleanup.done() and not server.drop_calls
        assert resources.data["phase"] != "removed"
        if late_result != "rollback":
            server.roles = {
                name: {"oid": oid, "rolname": name, "marker": resources.data["marker"]}
                for oid, name in enumerate((resources.runtime, resources.owner), 1)
            }
            if late_result == "database_commit":
                server.database = {"oid": 42, "datdba": 2, "marker": None}
        pending = False  # COMMIT/ROLLBACK has completed and backend exited.
        await asyncio.wait_for(cleanup, 0.5)
        assert not server.roles and server.database is None
        assert resources.data["phase"] == "removed"
        assert any(e["phase"] == "creator_stopped" for e in resources.data["events"])

    asyncio.run(run())


def test_unconfirmed_creator_exit_never_marks_removed(monkeypatch, tmp_path):
    async def run():
        server, creator, resources = await prepare(monkeypatch, tmp_path)

        async def stuck_close(**kwargs):
            await asyncio.Event().wait()

        monkeypatch.setattr(creator, "close", stuck_close)
        timeout = asyncio.timeout
        monkeypatch.setattr(fixture.asyncio, "timeout", lambda seconds: timeout(0.02))
        await fixture.close_creator(creator)
        assert creator.closed  # terminate was called, still no backend proof.
        original = Admin.fetchval

        async def fetchval(connection, sql, *args):
            if "pg_stat_activity" in sql:
                return True
            return await original(connection, sql, *args)

        monkeypatch.setattr(Admin, "fetchval", fetchval)
        with pytest.raises(RuntimeError, match="cleanup incomplete"):
            await resources.cleanup("private")
        assert not server.drop_calls
        assert resources.data["phase"] == "cleanup_attempt_failed"
        assert not any(e["phase"] == "removed" for e in resources.data["events"])
        assert all(c.closed for c in server.connections)

    asyncio.run(run())


def test_legacy_journal_without_creator_is_not_auto_reconciled(monkeypatch, tmp_path):
    async def run():
        server, admin, resources = await prepare(monkeypatch, tmp_path)
        await admin.close()
        del resources.data["creator_session"]
        with pytest.raises(RuntimeError, match="cleanup incomplete"):
            await resources.cleanup("private")
        assert not server.drop_calls and resources.data["phase"] != "removed"

    asyncio.run(run())
