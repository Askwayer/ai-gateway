#!/usr/bin/env python3
"""Opt-in disposable database verification. Never resets/adopts an existing database."""

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import traceback
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from dotenv import dotenv_values
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_connection
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from provision_databases import scram  # noqa: E402

from app.core.settings import Settings  # noqa: E402
from app.db.models import REVISION, metadata  # noqa: E402
from app.db.repository import Repository  # noqa: E402
from app.db.runtime import database_timeout  # noqa: E402


def qi(name):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name):
        raise ValueError("invalid fixture identifier")
    return '"' + name + '"'


def target_url(admin_url, database, role, password):
    parts = urlsplit(admin_url)
    host = parts.hostname
    if ":" in host:
        host = "[" + host + "]"
    netloc = f"{quote(role, safe='')}:{quote(password, safe='')}@{host}:{parts.port or 5432}"
    return urlunsplit(("postgresql+asyncpg", netloc, "/" + database, parts.query, ""))


async def connect(url):
    return await asyncpg.connect(
        url.replace("postgresql+asyncpg://", "postgresql://", 1),
        timeout=5,
        command_timeout=10,
        server_settings={"application_name": "gateway-disposable-db-verification"},
    )


async def catalog(connection, schema):
    # Canonical PostgreSQL definitions compare CHECK/PK/FK/UNIQUE and every index.
    rows = await connection.fetch(
        """
        SELECT c.relname AS table_name, con.conname AS name,
               pg_get_constraintdef(con.oid) AS definition
        FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=$1
        AND c.relname<>'alembic_version'
        UNION ALL
        SELECT tablename,indexname,indexdef FROM pg_indexes
        WHERE schemaname=$1 AND tablename<>'alembic_version'
        ORDER BY 1,2
    """,
        schema,
    )
    return sorted(tuple(str(value).replace(schema + ".", "") for value in row) for row in rows)


async def expect_denied(connection, sql):
    transaction = connection.transaction()
    await transaction.start()
    denied = False
    try:
        await connection.execute(sql)
    except asyncpg.InsufficientPrivilegeError:
        denied = True
    finally:
        await transaction.rollback()
    assert denied, "runtime permission unexpectedly allowed"


async def verify_runtime(url, report):
    engine = create_async_engine(
        url,
        pool_size=2,
        max_overflow=0,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"timeout": 5, "command_timeout": 5},
    )
    repo = Repository(engine, cleanup_timeout=0.2)
    try:
        async with database_timeout(2):
            assert await repo.ready()
        project = await repo.create_project(
            {"code": "owned-fixture", "name": "fixture", "max_concurrency": 4}, uuid4()
        )
        key_hash = hashlib.sha256(b"disposable-fixture-only").hexdigest()
        key = await repo.create_key(
            project["id"],
            {"key_hash": key_hash, "key_prefix": "fixture", "max_concurrency": 2},
            uuid4(),
        )
        identity = await repo.authenticate(key_hash)
        assert identity and identity.project_id == project["id"]
        rid = uuid4()
        await repo.register(
            dict(
                request_id=rid,
                project_id=project["id"],
                api_key_id=key["id"],
                upstream_id="fixture",
                model="fixture",
                is_stream=False,
                admitted=True,
                outcome="started",
                usage_status="pending",
                usage_source="none",
                process_run_id=uuid4(),
            )
        )
        await repo.mark_upstream(rid)
        final = dict(
            outcome="completed",
            usage_status="reported",
            usage_source="upstream_json",
            prompt_tokens=2,
            completion_tokens=1,
            total_tokens=3,
            response_status_code=200,
            settlement_attempts=1,
        )
        assert await repo.settle(rid, final)
        assert not await repo.settle(rid, final)
        # Real SQL timeout and rollback; recovery on a fresh checkout.
        try:
            async with database_timeout(0.1):
                async with repo.connection() as connection:
                    await connection.execute(sa.text("SELECT pg_sleep(3)"))
        except TimeoutError:
            pass
        else:
            raise AssertionError("SQL timeout missing")
        assert engine.pool.checkedout() == repo.pool_observer.snapshot()["checked_out"] == 0
        async with database_timeout(2):
            assert await repo.ready()
        # Real physical connections, with only the graceful close delayed.
        original_context, original_close = repo.connection, AsyncConnection.close
        faults = []
        for cancel in (False, True):
            targets = []
            drivers = []
            entered = asyncio.Event()

            @asynccontextmanager
            async def owned_context(*args, _targets=targets, _drivers=drivers, **kwargs):
                async with original_context(*args, **kwargs) as conn:
                    _targets.append(conn)
                    _drivers.append(conn.sync_connection.connection.driver_connection)
                    yield conn

            async def close(conn, _targets=targets, _entered=entered):
                if conn in _targets:
                    _entered.set()
                    await asyncio.Event().wait()
                await original_close(conn)

            async def call():
                async with database_timeout(2):
                    await repo.ready()

            baseline = set(asyncio.all_tasks())
            repo.connection = owned_context
            try:
                with patch.object(AsyncConnection, "close", close):
                    task = asyncio.create_task(call())
                    await asyncio.wait_for(entered.wait(), 2)
                    start = asyncio.get_running_loop().time()
                    if cancel:
                        task.cancel()
                    try:
                        await asyncio.wait_for(task, 1)
                    except (TimeoutError, asyncio.CancelledError) as error:
                        assert isinstance(error, asyncio.CancelledError if cancel else TimeoutError)
                    else:
                        raise AssertionError("close fault did not propagate")
                    assert asyncio.get_running_loop().time() - start < 1
            finally:
                repo.connection = original_context
            assert all(driver.is_closed() for driver in drivers)
            assert repo.pool_observer.snapshot()["checked_out"] == engine.pool.checkedout() == 0
            await asyncio.sleep(0)
            assert not [t for t in asyncio.all_tasks() - baseline if not t.done()]
            async with database_timeout(2):
                assert await repo.ready()
            faults.append(
                {"cancelled": cancel, "driver_closed": True, "pool_empty": True, "recovered": True}
            )
        report["real_close_faults"] = faults
        ping_faults = []
        original_ping = AsyncAdapt_asyncpg_connection._async_ping
        for rollback_error, cancel in ((False, False), (False, True), (True, False), (True, True)):
            async with repo.connection() as connection:
                target = connection.sync_connection.connection.driver_connection
            entered = asyncio.Event()
            rollback_entered = asyncio.Event()

            async def ping(
                adapter,
                target=target,
                entered=entered,
                rollback_entered=rollback_entered,
                rollback_error=rollback_error,
            ):
                if adapter.driver_connection is not target:
                    return await original_ping(adapter)
                # A physical transaction starts, then only its cancellation
                # rollback is delayed. Production pre-ping/checkout still runs.
                transaction = target.transaction()
                await transaction.start()
                try:
                    entered.set()
                    await asyncio.Event().wait()
                finally:
                    rollback_entered.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        if rollback_error:
                            raise asyncpg.InterfaceError("connection is closed") from None
                        raise

            async def ping_call():
                async with database_timeout(0.2):
                    await repo.ready()

            baseline = set(asyncio.all_tasks())
            with patch.object(AsyncAdapt_asyncpg_connection, "_async_ping", ping):
                task = asyncio.create_task(ping_call())
                await asyncio.wait_for(entered.wait(), 2)
                snapshot = repo.pool_observer.snapshot()
                assert snapshot["checked_out"] == engine.pool.checkedout() == 1
                assert snapshot["successful_checkouts_active"] == 0
                start = asyncio.get_running_loop().time()
                if cancel:
                    task.cancel()
                try:
                    await asyncio.wait_for(task, 1)
                except (TimeoutError, asyncio.CancelledError) as error:
                    assert isinstance(error, asyncio.CancelledError if cancel else TimeoutError)
                else:
                    raise AssertionError("pre-ping fault did not propagate")
                assert asyncio.get_running_loop().time() - start < 1
            assert rollback_entered.is_set() and target.is_closed()
            assert engine.pool.checkedout() == repo.pool_observer.snapshot()["checked_out"] == 0
            await asyncio.sleep(0)
            assert not [t for t in asyncio.all_tasks() - baseline if not t.done()]
            async with database_timeout(2):
                assert await repo.ready()
            ping_faults.append(
                {
                    "cancelled": cancel,
                    "rollback_error": rollback_error,
                    "rollback_interrupted": True,
                    "driver_closed": True,
                    "pool_empty": True,
                    "recovered": True,
                    "pre_ping_occupancy": snapshot,
                }
            )
        report["real_pre_ping_faults"] = ping_faults
        report["runtime_cycle"] = {
            "identity": True,
            "register_before_settle": True,
            "settlement_idempotent": True,
            "sql_timeout_recovery": True,
        }
        report["pool_events"] = repo.pool_observer.snapshot()
    finally:
        await engine.dispose()


class DisposableResources:
    """Durable intent plus server-side ownership proofs, never names alone."""

    def __init__(self, path, data):
        self.path = Path(path)
        self.data = data
        token = data["token"]
        if not re.fullmatch(r"[0-9a-f]{12}", token):
            raise ValueError("invalid resource token")
        expected = {
            "database": "gw_verify_" + token,
            "runtime_role": "gw_verify_" + token + "_app",
            "migration_role": "gw_verify_" + token + "_owner",
        }
        if data["resources"] != expected or not re.fullmatch(
            r"ai-gateway-disposable:[0-9a-f]{32}", data["marker"]
        ):
            raise ValueError("invalid resource identity")
        self.database = expected["database"]
        self.runtime = expected["runtime_role"]
        self.owner = expected["migration_role"]

    @classmethod
    async def plan(cls, admin):
        token = secrets.token_hex(6)
        names = {
            "database": "gw_verify_" + token,
            "runtime_role": "gw_verify_" + token + "_app",
            "migration_role": "gw_verify_" + token + "_owner",
        }
        if await admin.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", names["database"]
        ):
            raise ValueError("resource name already exists")
        if await admin.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=ANY($1::text[]))",
            [names["runtime_role"], names["migration_role"]],
        ):
            raise ValueError("resource name already exists")
        data = {
            "version": 2,
            "creator_session": dict(
                await admin.fetchrow(
                    "SELECT pid,backend_start::text AS backend_start FROM pg_stat_activity WHERE pid=pg_backend_pid()"
                )
            ),
            "token": token,
            "resources": names,
            "marker": "ai-gateway-disposable:" + uuid4().hex,
            "server_id": str(
                await admin.fetchval("SELECT system_identifier::text FROM pg_control_system()")
            ),
            "phase": "planned",
            "events": [],
        }
        folder = ROOT / ".tools" / "db-disposable"
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = folder / (names["database"] + ".json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(data, file)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        directory = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return cls(path, data)

    @classmethod
    def load(cls, path):
        path = Path(path)
        folder = (ROOT / ".tools" / "db-disposable").resolve()
        if path.is_symlink() or path.resolve().parent != folder:
            raise ValueError("journal must be an owned local file")
        path = path.resolve()
        return cls(path, json.loads(path.read_text()))

    def record(self, phase, **details):
        self.data["phase"] = phase
        self.data["events"].append({"phase": phase, **details})
        temporary = self.path.with_suffix(".pending")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(self.data, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, self.path)
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    async def create_role(self, admin, role, password):
        if role not in (self.runtime, self.owner):
            raise ValueError("role outside owned plan")
        self.record("role_create_started", role=role)
        # CREATE ROLE and its marker commit atomically, including lost COMMIT ACK.
        async with admin.transaction():
            await admin.execute(
                f"CREATE ROLE {qi(role)} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 4 PASSWORD '{scram(password)}'"
            )
            await admin.execute(f"COMMENT ON ROLE {qi(role)} IS '{self.data['marker']}'")
        self.record("role_create_confirmed", role=role)

    async def create_database(self, admin):
        self.record("database_create_started")
        # CREATE DATABASE cannot share a transaction with COMMENT. Its unique,
        # atomically marked owner is the proof when the CREATE ACK is lost.
        await admin.execute(
            f"CREATE DATABASE {qi(self.database)} OWNER {qi(self.owner)} TEMPLATE template0 CONNECTION LIMIT 8"
        )
        self.record("database_create_confirmed")
        await admin.execute(f"COMMENT ON DATABASE {qi(self.database)} IS '{self.data['marker']}'")

    async def wait_creator_stopped(self, admin):
        creator = self.data.get("creator_session")
        if (
            not isinstance(creator, dict)
            or not isinstance(creator.get("pid"), int)
            or not creator.get("backend_start")
        ):
            raise ValueError("creator identity missing; manual reconciliation required")
        # A socket close/command timeout is not proof that the backend finished.
        # Match PID plus backend start time to avoid confusing a reused PID.
        async with asyncio.timeout(2):
            while await admin.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=$1 AND backend_start=$2::text::timestamptz)",
                creator["pid"],
                creator["backend_start"],
            ):
                await asyncio.sleep(0.05)
        self.record("creator_stopped")

    async def cleanup(self, admin_url):
        # Reconnect independently of the provisioning connection. Two bounded
        # passes also resolve lost DROP acknowledgements by re-reading catalogs.
        self.record("cleanup_started")
        for attempt in (1, 2):
            admin = None
            try:
                admin = await connect(admin_url)
                server_id = str(
                    await admin.fetchval("SELECT system_identifier::text FROM pg_control_system()")
                )
                if server_id != self.data["server_id"]:
                    raise ValueError("cleanup server identity mismatch")
                await self.wait_creator_stopped(admin)
                rows = await admin.fetch(
                    "SELECT oid,rolname,shobj_description(oid,'pg_authid') AS marker FROM pg_roles WHERE rolname=ANY($1::text[])",
                    [self.runtime, self.owner],
                )
                roles = {row["rolname"]: row for row in rows}
                if any(row["marker"] != self.data["marker"] for row in roles.values()):
                    raise ValueError("cleanup role ownership mismatch")
                db = await admin.fetchrow(
                    "SELECT oid,datdba,shobj_description(oid,'pg_database') AS marker FROM pg_database WHERE datname=$1",
                    self.database,
                )
                if db is not None:
                    owner = roles.get(self.owner)
                    if (
                        owner is None
                        or db["datdba"] != owner["oid"]
                        or db["marker"] not in (None, self.data["marker"])
                    ):
                        raise ValueError("cleanup database ownership mismatch")
                    # Pin the observed OID in the journal for review/recovery.
                    self.record("database_drop_started", oid=db["oid"])
                    await admin.execute(f"DROP DATABASE {qi(self.database)} WITH (FORCE)")
                errors = []
                for role in (self.runtime, self.owner):
                    if role in roles:
                        try:
                            self.record("role_drop_started", role=role, oid=roles[role]["oid"])
                            await admin.execute(f"DROP ROLE {qi(role)}")
                        except Exception as error:
                            errors.append(type(error).__name__)
                remaining_db = await admin.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", self.database
                )
                remaining_roles = await admin.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=ANY($1::text[]))",
                    [self.runtime, self.owner],
                )
                if remaining_db or remaining_roles:
                    raise RuntimeError("owned resources remain")
                self.record("removed", attempt=attempt, drop_errors=errors)
                return
            except Exception as error:
                self.record(
                    "cleanup_attempt_failed", attempt=attempt, error_type=type(error).__name__
                )
                if isinstance(error, ValueError):
                    # Ownership mismatch is not transient: never adopt or delete.
                    break
            finally:
                if admin is not None:
                    try:
                        await admin.close(timeout=2)
                    except Exception:
                        admin.terminate()
        raise RuntimeError("cleanup incomplete; use the persisted journal for safe recovery")


async def close_creator(admin):
    try:
        async with asyncio.timeout(2):
            await admin.close(timeout=2)
    except asyncio.CancelledError:
        admin.terminate()
        raise
    except Exception:
        admin.terminate()


async def verify_lost_ack(admin_url, report):
    results = []
    for stage in ("role_commit_ack", "database_create_ack"):
        admin = await connect(admin_url)
        resources = None
        try:
            resources = await DisposableResources.plan(admin)

            class LostAck:
                @asynccontextmanager
                async def transaction(self, admin=admin, stage=stage):
                    async with admin.transaction():
                        yield
                    if stage == "role_commit_ack":
                        raise ConnectionError("injected lost commit acknowledgement")

                async def execute(self, sql, admin=admin, stage=stage):
                    result = await admin.execute(sql)
                    if stage == "database_create_ack" and sql.startswith("CREATE DATABASE"):
                        raise ConnectionError("injected lost create acknowledgement")
                    return result

            lost = False
            try:
                proxy = LostAck()
                for role in (resources.runtime, resources.owner):
                    await resources.create_role(proxy, role, secrets.token_urlsafe(32))
                await resources.create_database(proxy)
            except ConnectionError:
                lost = True
            assert lost
            assert await admin.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=$1)", resources.runtime
            )
            if stage == "database_create_ack":
                assert await admin.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", resources.database
                )
        finally:
            # Recovery must not depend on this now-unusable creation connection.
            await close_creator(admin)
            if resources is not None:
                restored = DisposableResources.load(resources.path)
                await restored.cleanup(admin_url)
                results.append(
                    {
                        "fault": stage,
                        "owned_resources_removed": True,
                        "resource_journal": str(resources.path.relative_to(ROOT)),
                    }
                )
                report["real_lost_ack_faults"] = results


async def verify_late_commit(admin_url, report):
    admin = await connect(admin_url)
    resources = None
    cleanup = None
    try:
        resources = await DisposableResources.plan(admin)
        transaction = admin.transaction()
        await transaction.start()
        for role in (resources.runtime, resources.owner):
            resources.record("role_create_started", role=role)
            await admin.execute(
                f"CREATE ROLE {qi(role)} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE"
            )
            await admin.execute(f"COMMENT ON ROLE {qi(role)} IS '{resources.data['marker']}'")
        # Deliberately overlap cleanup with an uncommitted CREATE ROLE. Catalog
        # absence cannot count as removal until this backend has stopped.
        entered = asyncio.Event()
        original_wait = resources.wait_creator_stopped

        async def wait_creator(connection):
            entered.set()
            await original_wait(connection)

        resources.wait_creator_stopped = wait_creator
        cleanup = asyncio.create_task(resources.cleanup(admin_url))
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.sleep(0.1)
        assert not cleanup.done() and resources.data["phase"] != "removed"
        await transaction.commit()
        await close_creator(admin)
        await asyncio.wait_for(cleanup, 3)
        assert resources.data["phase"] == "removed"
        report["real_late_commit"] = {
            "cleanup_waited_for_creator": True,
            "commit_after_cleanup_started": True,
            "owned_resources_removed": True,
            "resource_journal": str(resources.path.relative_to(ROOT)),
        }
    finally:
        await close_creator(admin)
        if cleanup is not None:
            await asyncio.gather(cleanup, return_exceptions=True)
        if resources is not None and resources.data["phase"] != "removed":
            await resources.cleanup(admin_url)


async def verify(report):
    assert len(Settings.model_fields) == 80
    frozen = [
        "app/core/settings.py",
        "app/db/config.py",
        "app/db/migration_guard.py",
        "migrations/versions/0001_nonstream_slice.py",
        "migrations/versions/0002_admin_management.py",
    ]
    for name in frozen:
        assert (ROOT / name).read_bytes() == subprocess.check_output(
            ["git", "show", "9dbd62e:" + name], cwd=ROOT
        )
    # Administrative credentials are used only for provisioning owned resources.
    admin_url = dotenv_values(ROOT / ".env.db-admin")["DATABASE_URL"]
    assert admin_url
    await verify_lost_ack(admin_url, report)
    await verify_late_commit(admin_url, report)
    admin = await connect(admin_url)
    resources = None
    try:
        resources = await DisposableResources.plan(admin)
        database, runtime, owner = resources.database, resources.runtime, resources.owner
        passwords = {role: secrets.token_urlsafe(32) for role in (runtime, owner)}
        urls = {role: target_url(admin_url, database, role, passwords[role]) for role in passwords}
        report["resource_journal"] = str(resources.path.relative_to(ROOT))
        # Historical 0001 explicitly revokes from this pre-existing role. Do not
        # create/modify that role or claim a clean-cluster installation was tested.
        assert await admin.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='gateway_test_app')"
        )
        report["legacy_migration_role_dependency"] = (
            "0001 requires existing gateway_test_app; database-isolated rebuild, not fresh cluster"
        )
        report["server_version"] = await admin.fetchval("SHOW server_version")
        report["resources"] = {
            "database": database,
            "runtime_role": runtime,
            "migration_role": owner,
        }
        for role in (runtime, owner):
            await resources.create_role(admin, role, passwords[role])
        await resources.create_database(admin)
        await admin.execute(f"REVOKE ALL ON DATABASE {qi(database)} FROM PUBLIC")
        await admin.execute(f"GRANT CONNECT ON DATABASE {qi(database)} TO {qi(runtime)}")
        setup = await connect(urls[owner])
        try:
            assert not await setup.fetchval(
                "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_schema='public')"
            )
            await setup.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
            await setup.execute(f"GRANT USAGE ON SCHEMA public TO {qi(runtime)}")
        finally:
            await setup.close(timeout=2)
        # A private ephemeral fixture file exercises the unchanged Alembic entry
        # and identity guard, without editing any application environment file.
        with tempfile.TemporaryDirectory(prefix="gateway-db-", dir=ROOT / ".tools") as tmp:
            env_file = Path(tmp) / ".env"
            fd = os.open(env_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as file:
                file.write(
                    f"APP_ENV=test\nDATABASE_URL={urls[runtime]}\nMIGRATION_DATABASE_URL={urls[owner]}\n"
                )
            process_env = {
                k: v
                for k, v in os.environ.items()
                if k
                not in {*Settings.model_fields, "APP_ENV", "APP_ENV_FILE", "MIGRATION_DATABASE_URL"}
            }
            process_env.update(APP_ENV="test", APP_ENV_FILE=str(env_file))
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "alembic",
                "upgrade",
                "head",
                cwd=ROOT,
                env=process_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.wait_for(process.communicate(), 30)
            except BaseException:
                process.kill()
                await process.wait()
                raise
            assert process.returncode == 0, "migration failed; driver output suppressed"
        owner_engine = create_async_engine(urls[owner], hide_parameters=True)
        try:
            async with owner_engine.begin() as connection:
                assert (
                    await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
                    == REVISION
                )
                diffs = await connection.run_sync(
                    lambda c: compare_metadata(
                        MigrationContext.configure(c, opts={"compare_type": True}), metadata
                    )
                )
                assert not diffs, "reflected model differs from migration"
                await connection.execute(sa.text("CREATE SCHEMA model_check"))
                translated = await connection.execution_options(
                    schema_translate_map={None: "model_check"}
                )
                await translated.run_sync(metadata.create_all)
            check = await connect(urls[owner])
            try:
                migrated, expected = (
                    await catalog(check, "public"),
                    await catalog(check, "model_check"),
                )
                if migrated != expected:
                    report["schema_difference"] = {
                        "migration_only": sorted(set(migrated) - set(expected)),
                        "model_only": sorted(set(expected) - set(migrated)),
                    }
                assert migrated == expected, "constraint or index definitions differ"
                for table in metadata.tables:
                    await check.execute(
                        f"GRANT SELECT,INSERT,UPDATE,DELETE ON {qi(table)} TO {qi(runtime)}"
                    )
                await check.execute(f"GRANT SELECT ON alembic_version TO {qi(runtime)}")
                report["schema"] = {
                    "revision": REVISION,
                    "tables": sorted(metadata.tables),
                    "metadata_diff": [],
                    "constraint_and_index_definitions_compared": len(migrated),
                }
            finally:
                await check.close(timeout=2)
        finally:
            await owner_engine.dispose()
        app = await connect(urls[runtime])
        try:
            assert tuple(await app.fetchrow("SELECT current_database(),current_user")) == (
                database,
                runtime,
            )
            assert not any(
                await app.fetchrow(
                    "SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=current_user"
                )
            )
            for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                assert not await app.fetchval(
                    "SELECT has_table_privilege(current_user, 'alembic_version', $1)", privilege
                )
            for sql in (
                "CREATE TABLE public.forbidden(id int)",
                "CREATE TEMP TABLE forbidden(id int)",
                "DROP TABLE projects",
                "TRUNCATE projects CASCADE",
                "UPDATE alembic_version SET version_num='forbidden'",
            ):
                await expect_denied(app, sql)
            report["runtime_privilege_negatives"] = 5
        finally:
            await app.close(timeout=2)
        await verify_runtime(urls[runtime], report)
    finally:
        try:
            await close_creator(admin)
        finally:
            if resources is not None:
                await resources.cleanup(admin_url)
                report["owned_resources_removed"] = True


async def recover(path, report):
    resources = DisposableResources.load(path)
    admin_url = dotenv_values(ROOT / ".env.db-admin")["DATABASE_URL"]
    if not admin_url:
        raise ValueError("administrative connection is required")
    report["resource_journal"] = str(resources.path.relative_to(ROOT))
    await resources.cleanup(admin_url)
    report["owned_resources_removed"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--create-disposable", action="store_true")
    mode.add_argument("--recover-journal", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists():
        raise SystemExit("use a fresh report path")
    report = {
        "scope": "EX-P0-DB-01",
        "recorded_at_utc": datetime.now(UTC).isoformat(),
        "status": "running",
        "existing_test_database_modified": False,
        "company_migration": False,
        "upstream_calls": 0,
        "m0_signed": False,
    }
    try:
        asyncio.run(
            recover(args.recover_journal, report) if args.recover_journal else verify(report)
        )
        report["status"] = "passed"
    except (Exception, KeyboardInterrupt) as error:
        report.update(
            status="failed",
            error_type=type(error).__name__,
            error_locations=[
                f"{Path(f.filename).name}:{f.lineno}"
                for f in traceback.extract_tb(error.__traceback__)
            ],
        )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
        file.write("\n")
    print("Disposable database verification:", report["status"], "(details sanitized)")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
