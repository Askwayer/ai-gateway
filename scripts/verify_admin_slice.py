#!/usr/bin/env python3
"""Opt-in test-database management slice verification; no public/company upstream calls."""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import traceback
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config_check import check_local_development  # noqa: E402
from app.core.config import load_config  # noqa: E402
from app.db.config import load_migration_config  # noqa: E402
from app.db.models import REVISION, admin_audit_logs, api_keys, projects, request_logs  # noqa: E402
from app.db.repository import Repository, make_engine  # noqa: E402
from app.main import create_app  # noqa: E402


async def verify_cleanup_faults(repo, app, client, admin, code):
    """Target only owned connections; real asyncpg sockets and real rollback state."""
    results = []
    original_context = repo.admin_connection
    original_close = AsyncConnection.close
    original_rollback = AsyncAdapt_asyncpg_connection._rollback_and_discard
    original_audit = repo.audit
    original_budget = repo.cleanup_timeout
    repo.cleanup_timeout = 0.1  # fault profile; production settings remain unchanged
    try:

        async def run_case(fault, cancel):
            repo.cleanup_timeout = 0.1
            entered = asyncio.Event()
            connections = []
            adapters = []
            drivers = []

            @asynccontextmanager
            async def owned_context(*args, **kwargs):
                async with original_context(*args, **kwargs) as connection:
                    connections.append(connection)
                    raw = connection.sync_connection.connection
                    adapters.append(raw.dbapi_connection)
                    drivers.append(raw.driver_connection)
                    yield connection

            async def close(connection):
                if fault == "close" and connection in connections:
                    entered.set()
                    await asyncio.Event().wait()
                await original_close(connection)

            async def rollback(adapter):
                if fault == "rollback" and adapter in adapters:
                    entered.set()
                    await asyncio.Event().wait()
                await original_rollback(adapter)

            async def fail_audit(*args, **kwargs):
                raise SQLAlchemyError("injected audit failure before commit")

            # This peer must remain usable throughout hard invalidation.
            async with repo.engine.connect() as peer:
                assert await peer.scalar(sa.text("SELECT 1")) == 1
                peer_driver = peer.sync_connection.connection.driver_connection
                baseline = set(asyncio.all_tasks())
                repo.admin_connection = owned_context
                if fault == "rollback":
                    repo.audit = fail_audit
                try:
                    with (
                        patch.object(AsyncConnection, "close", close),
                        patch.object(
                            AsyncAdapt_asyncpg_connection, "_rollback_and_discard", rollback
                        ),
                    ):
                        start = asyncio.get_running_loop().time()
                        request = (
                            client.get("/admin/projects", headers=admin)
                            if fault == "close"
                            else client.post(
                                "/admin/projects",
                                headers=admin,
                                json={
                                    "code": code + "-rollback",
                                    "name": "Rollback fault fixture",
                                },
                            )
                        )
                        task = asyncio.create_task(request)
                        await asyncio.wait_for(entered.wait(), 2)
                        assert app.state.admin_db_active == 1
                        if cancel:
                            task.cancel()
                            try:
                                await asyncio.wait_for(task, 1)
                            except asyncio.CancelledError:
                                pass
                            else:
                                raise AssertionError("cancellation was swallowed")
                        else:
                            response = await asyncio.wait_for(task, 1)
                            assert response.status_code == 503
                            assert response.json()["error"]["code"] == "database_unavailable"
                            assert response.headers["retry-after"]
                        elapsed = asyncio.get_running_loop().time() - start
                        assert elapsed < 1
                finally:
                    repo.admin_connection = original_context
                    repo.audit = original_audit
                assert app.state.admin_db_active == 0
                assert all(driver.is_closed() for driver in drivers)
                assert not peer_driver.is_closed()
                assert await peer.scalar(sa.text("SELECT 1")) == 1
                assert repo.engine.pool.checkedout() == 1
                await asyncio.sleep(0)
                assert not [t for t in asyncio.all_tasks() - baseline if not t.done()]
                assert not await peer.scalar(
                    sa.select(projects.c.id).where(projects.c.code == code + "-rollback")
                )
                repo.cleanup_timeout = original_budget
                assert (await client.get("/admin/projects", headers=admin)).status_code == 200
            assert repo.engine.pool.checkedout() == 0
            results.append(
                {
                    "fault": fault,
                    "cancelled": cancel,
                    "elapsed_ms": round(elapsed * 1000, 1),
                    "driver_closed": True,
                    "peer_usable": True,
                    "no_pending_tasks": True,
                    "pool_checked_out": 0,
                    "admin_slots": 0,
                    "recovery_status": 200,
                    "uncommitted_project_absent": True,
                }
            )

        for fault in ("close", "rollback"):
            for cancel in (False, True):
                await run_case(fault, cancel)
    finally:
        repo.admin_connection = original_context
        repo.audit = original_audit
        repo.cleanup_timeout = original_budget
    return results


async def verify(args, report):
    env = dict(
        os.environ,
        HEALTH_GATE_ENABLED="false",
        HEALTH_REACHABILITY_ENABLED="false",
        INFERENCE_PROBE_ENABLED="false",
        DB_READY_CHECK_INTERVAL="60",
        DB_READY_STALE_AFTER="90",
    )
    loaded = load_config(environ=env)
    check_local_development(loaded)
    config = loaded.settings
    migration = load_migration_config()
    assert len(type(config).model_fields) == 80
    for name in (
        "app/core/settings.py",
        "app/db/config.py",
        "app/db/migration_guard.py",
        "migrations/versions/0001_nonstream_slice.py",
    ):
        assert (ROOT / name).read_bytes() == subprocess.check_output(
            ["git", "show", "9357935:" + name], cwd=ROOT
        )
    old_tables = {"projects", "api_keys", "request_logs", "alembic_version"}
    new_tables = old_tables | {"health_checks", "admin_audit_logs"}
    owner = create_async_engine(
        migration.url.get_secret_value(),
        poolclass=NullPool,
        echo=False,
        hide_parameters=True,
        connect_args={"timeout": 5, "command_timeout": 10},
    )
    try:
        async with owner.connect() as conn:
            identity = tuple(
                (await conn.execute(sa.text("SELECT current_database(),current_user"))).one()
            )
            assert identity == ("ai_gateway_test", "gateway_test_migrator")
            assert not await conn.scalar(
                sa.text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")
            )
            tables = set(
                (
                    await conn.execute(
                        sa.text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
                    )
                ).scalars()
            )
            before = await conn.scalar(sa.text("SELECT version_num FROM alembic_version"))
            assert (before == "0001_nonstream_slice" and tables == old_tables) or (
                before == REVISION and tables == new_tables
            )
            report["before"] = {
                "database": identity[0],
                "role": identity[1],
                "revision": before,
                "tables": sorted(tables),
            }
        if before != REVISION:
            assert args.migrate, "incremental migration requires --migrate"
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "alembic",
                "upgrade",
                "head",
                cwd=ROOT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await process.communicate()
            assert process.returncode == 0, "migration failed; driver output suppressed"
            report["migration_executed"] = True
        async with owner.connect() as conn:
            assert await conn.scalar(sa.text("SELECT version_num FROM alembic_version")) == REVISION
            tables = set(
                (
                    await conn.execute(
                        sa.text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
                    )
                ).scalars()
            )
            assert tables == new_tables
            report["after"] = {"revision": REVISION, "tables": sorted(tables)}
            for table in ("health_checks", "admin_audit_logs"):
                assert (
                    await conn.scalar(
                        sa.text(
                            "SELECT count(*) FROM pg_constraint WHERE conrelid=to_regclass(:table) AND contype='c'"
                        ),
                        {"table": table},
                    )
                    >= 3
                )
            indexes = set(
                (
                    await conn.execute(
                        sa.text("SELECT indexname FROM pg_indexes WHERE schemaname='public'")
                    )
                ).scalars()
            )
            assert {
                "ix_admin_audit_created",
                "ix_admin_audit_action_created",
                "ix_admin_audit_resource_created",
                "ix_health_checks_upstream_time",
                "ix_projects_created",
            } <= indexes
    finally:
        await owner.dispose()
    engine = make_engine(config)
    repo = Repository(engine, cleanup_timeout=config.CLEANUP_TIMEOUT)
    code = "admin-check-" + uuid4().hex
    project_id = None
    calls = []
    request_ids = []
    try:
        async with engine.connect() as conn:
            identity = tuple(
                (await conn.execute(sa.text("SELECT current_database(),current_user"))).one()
            )
            assert identity == ("ai_gateway_test", "gateway_test_app")
            assert not await conn.scalar(
                sa.text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")
            )
            assert not await conn.scalar(
                sa.text("SELECT has_schema_privilege(current_user,'public','CREATE')")
            )
            for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                assert not await conn.scalar(
                    sa.text(
                        "SELECT has_table_privilege(current_user,'alembic_version',:privilege)"
                    ),
                    {"privilege": privilege},
                )
            report["runtime"] = {
                "database": identity[0],
                "role": identity[1],
                "version_writes": False,
                "schema_create": False,
            }

        async def upstream(request):
            rid = UUID(request.headers["x-request-id"])
            async with engine.connect() as conn:
                assert (
                    await conn.scalar(
                        sa.select(request_logs.c.outcome).where(request_logs.c.request_id == rid)
                    )
                    == "started"
                )
            assert request.headers["x-gateway-request-source"] == "business"
            assert (
                request.headers["authorization"]
                == "Bearer " + config.UPSTREAM_API_KEY.get_secret_value()
            )
            calls.append(rid)

            class Body(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b'{"choices":[{"message":{"content":"OK"}}],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}}'

            return httpx.Response(200, headers={"content-type": "application/json"}, stream=Body())

        app = create_app(config, repository=repo, transport=httpx.MockTransport(upstream))
        admin = {"Authorization": "Bearer " + config.ADMIN_API_KEY.get_secret_value()}
        body = {"model": config.ALLOWED_MODELS[0], "messages": [{"role": "user", "content": "OK"}]}
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client,
        ):
            assert (await client.get("/health/ready")).status_code == 200
            assert (await client.get("/admin/projects")).status_code == 401
            response = await client.post(
                "/admin/projects",
                headers=admin,
                json={"code": code, "name": "Management verification"},
            )
            assert response.status_code == 201
            project_id = UUID(response.json()["id"])
            request_ids.append(UUID(response.headers["x-request-id"]))
            assert (
                await client.post(
                    "/admin/projects", headers=admin, json={"code": code, "name": "Duplicate"}
                )
            ).status_code == 409
            path = f"/admin/projects/{project_id}/keys"
            response = await client.post(path, headers=admin, json={})
            assert response.status_code == 201 and response.headers["cache-control"] == "no-store"
            key = response.json()
            key_id = UUID(key["id"])
            request_ids.append(UUID(response.headers["x-request-id"]))
            auth = {"Authorization": "Bearer " + key["key"]}
            listing = (await client.get(path, headers=admin)).json()
            assert key["key"] not in json.dumps(listing) and "key_hash" not in json.dumps(listing)
            assert (await client.get("/admin/projects", headers=auth)).status_code == 403
            assert (await client.get("/v1/models", headers=admin)).status_code == 401
            assert (await client.get("/v1/models", headers=auth)).status_code == 200
            assert (
                await client.post("/v1/chat/completions", headers=auth, json=body)
            ).status_code == 200
            async with engine.connect() as conn:
                stored = await conn.scalar(
                    sa.select(api_keys.c.key_hash).where(api_keys.c.id == key_id)
                )
                assert stored == hashlib.sha256(key["key"].encode()).hexdigest()
                audits = [
                    dict(r)
                    for r in (
                        await conn.execute(
                            sa.select(admin_audit_logs).where(
                                admin_audit_logs.c.request_id.in_(request_ids)
                            )
                        )
                    ).mappings()
                ]
                assert len(audits) == 2
                encoded = json.dumps(audits, default=str)
                assert (
                    key["key"] not in encoded
                    and stored not in encoded
                    and "key_hash" not in encoded
                )
            # Inject a real CHECK violation into the audit INSERT, on the same transaction.
            original = repo.audit

            async def rejected_audit(connection, kind, action, before, after, request_id):
                await connection.execute(
                    admin_audit_logs.insert().values(
                        id=uuid4(),
                        actor="invalid",
                        action=action,
                        resource_type=kind,
                        resource_id=after["id"],
                        request_id=request_id,
                        changes={},
                    )
                )

            repo.audit = rejected_audit
            try:
                assert (
                    await client.post(
                        "/admin/projects",
                        headers=admin,
                        json={"code": code + "-rollback", "name": "Rollback"},
                    )
                ).status_code == 503
                assert (await client.post(path, headers=admin, json={})).status_code == 503
                assert (
                    await client.patch(
                        f"/admin/keys/{key_id}", headers=admin, json={"is_active": False}
                    )
                ).status_code == 503
                assert (
                    await client.patch(
                        f"/admin/projects/{project_id}",
                        headers=admin,
                        json={"name": "Must roll back"},
                    )
                ).status_code == 503
            finally:
                repo.audit = original
            async with engine.connect() as conn:
                assert not await conn.scalar(
                    sa.select(projects.c.id).where(projects.c.code == code + "-rollback")
                )
                assert (
                    await conn.scalar(
                        sa.select(sa.func.count())
                        .select_from(api_keys)
                        .where(api_keys.c.project_id == project_id)
                    )
                    == 1
                )
                assert await conn.scalar(
                    sa.select(api_keys.c.is_active).where(api_keys.c.id == key_id)
                )
                assert (
                    await conn.scalar(sa.select(projects.c.name).where(projects.c.id == project_id))
                    == "Management verification"
                )
            report["audit_failure_rollback"] = [
                "project_create",
                "key_create",
                "key_disable",
                "project_update",
            ]
            response = await client.patch(
                f"/admin/keys/{key_id}", headers=admin, json={"is_active": False}
            )
            assert response.status_code == 200
            request_ids.append(UUID(response.headers["x-request-id"]))
            assert (
                await client.post("/v1/chat/completions", headers=auth, json=body)
            ).status_code == 401
            assert (await client.get("/admin/projects", headers=auth)).status_code == 403
            assert len(calls) == 1
            response = await client.post(path, headers=admin, json={})
            assert response.status_code == 201
            request_ids.append(UUID(response.headers["x-request-id"]))
            replacement = response.json()
            new_auth = {"Authorization": "Bearer " + replacement["key"]}
            assert (
                await client.post("/v1/chat/completions", headers=new_auth, json=body)
            ).status_code == 200
            response = await client.patch(
                f"/admin/projects/{project_id}", headers=admin, json={"is_active": False}
            )
            assert response.status_code == 200
            request_ids.append(UUID(response.headers["x-request-id"]))
            assert (await client.get("/v1/models", headers=new_auth)).status_code == 401
            assert len(calls) == 2
            page = (await client.get(path, headers=admin, params={"limit": 1})).json()
            assert page["next_cursor"]
            next_page = (
                await client.get(
                    path, headers=admin, params={"limit": 1, "cursor": page["next_cursor"]}
                )
            ).json()
            assert (
                page["data"][0]["id"] != next_page["data"][0]["id"]
                and next_page["next_cursor"] is None
            )
            async with engine.connect() as conn:
                rows = (
                    (
                        await conn.execute(
                            sa.select(request_logs).where(request_logs.c.project_id == project_id)
                        )
                    )
                    .mappings()
                    .all()
                )
                assert len(rows) == 2 and all(
                    r["outcome"] == "completed" and r["total_tokens"] == 3 for r in rows
                )
                assert (
                    await conn.scalar(
                        sa.select(sa.func.count())
                        .select_from(admin_audit_logs)
                        .where(admin_audit_logs.c.request_id.in_(request_ids))
                    )
                    == 5
                )
            assert app.state.admin_db_active == app.state.admission.active == 0
            assert engine.pool.checkedout() == 0
            report["cleanup_faults"] = await verify_cleanup_faults(repo, app, client, admin, code)
            report["cleanup_fault_profile"] = {
                "cleanup_seconds": 0.1,
                "db_ready_interval_seconds": 60,
                "db_ready_stale_seconds": 90,
                "scope": "owned management connections only",
            }
            report.update(
                api_cycle="create project/key -> call -> disable old key -> create replacement -> call -> disable project",
                audit_count=5,
                business_calls=2,
                reported_tokens=6,
                raw_key_not_persisted=True,
                pagination=True,
                permissions=True,
                admin_slots=0,
                business_slots=0,
                pool_checked_out=0,
                request_ids=[str(x) for x in request_ids],
            )
    finally:
        try:
            async with engine.begin() as conn:
                # Scope all cleanup to this run's unique project codes/resource IDs.
                owned = list(
                    (
                        await conn.execute(
                            sa.select(projects.c.id).where(
                                projects.c.code.in_([code, code + "-rollback"])
                            )
                        )
                    ).scalars()
                )
                keys = (
                    list(
                        (
                            await conn.execute(
                                sa.select(api_keys.c.id).where(api_keys.c.project_id.in_(owned))
                            )
                        ).scalars()
                    )
                    if owned
                    else []
                )
                await conn.execute(
                    admin_audit_logs.delete().where(
                        admin_audit_logs.c.resource_id.in_(owned + keys)
                    )
                )
                if owned:
                    await conn.execute(
                        request_logs.delete().where(request_logs.c.project_id.in_(owned))
                    )
                    await conn.execute(api_keys.delete().where(api_keys.c.project_id.in_(owned)))
                    await conn.execute(projects.delete().where(projects.c.id.in_(owned)))
                assert not await conn.scalar(
                    sa.select(sa.func.count())
                    .select_from(projects)
                    .where(projects.c.code.in_([code, code + "-rollback"]))
                )
                assert not await conn.scalar(
                    sa.select(sa.func.count())
                    .select_from(admin_audit_logs)
                    .where(admin_audit_logs.c.resource_id.in_(owned + keys))
                )
            report["fixture_cleanup"] = (
                "own projects/keys/request/audit rows removed and verified; schema retained"
            )
        finally:
            await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists():
        raise SystemExit("report already exists; use a fresh path")
    report = {
        "scope": "EX-P0-ADMIN-01",
        "recorded_at_utc": datetime.now(UTC).isoformat(),
        "status": "running",
        "migration_executed": False,
        "m0_signed": False,
        "a_acceptance": False,
        "upstream": "mock; no external inference",
    }
    try:
        asyncio.run(verify(args, report))
        report["status"] = "passed"
    except Exception as error:
        report.update(
            status="failed",
            error_type=type(error).__name__,
            error_locations=[
                f"{Path(frame.filename).name}:{frame.lineno}"
                for frame in traceback.extract_tb(error.__traceback__)
            ],
        )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
        file.write("\n")
    print("Admin slice verification:", report["status"], "(details sanitized)")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
