#!/usr/bin/env python3
"""Opt-in integration verification against the approved test DB, with optional one DeepSeek call."""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config_check import check_local_development  # noqa: E402
from app.core.config import load_config  # noqa: E402
from app.db.config import load_migration_config  # noqa: E402
from app.db.models import REVISION, api_keys, projects, request_logs  # noqa: E402
from app.db.repository import Repository, make_engine  # noqa: E402
from app.main import create_app  # noqa: E402


class JSONStream(httpx.AsyncByteStream):
    def __init__(self, payload, hang_on_close=False):
        self.hang_on_close = hang_on_close
        self.close_cancelled = False
        self.body = json.dumps(payload).encode()

    async def __aiter__(self):
        yield self.body

    async def aclose(self):
        if self.hang_on_close:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.close_cancelled = True
                raise


async def verify(args, report):
    overrides = dict(os.environ)
    overrides.update(
        HEALTH_GATE_ENABLED="false",
        HEALTH_REACHABILITY_ENABLED="false",
        INFERENCE_PROBE_ENABLED="false",
    )
    loaded = load_config(environ=overrides)
    check_local_development(loaded)
    config = loaded.settings
    migration = load_migration_config()
    assert len(type(config).model_fields) == 80
    settings_source = (ROOT / "app/core/settings.py").read_bytes()
    assert settings_source == subprocess.check_output(
        ["git", "show", "b943aba:app/core/settings.py"], cwd=ROOT
    )
    report["settings_source_sha256"] = hashlib.sha256(settings_source).hexdigest()
    report["profile_deviations"] = {
        name: False
        for name in (
            "HEALTH_GATE_ENABLED",
            "HEALTH_REACHABILITY_ENABLED",
            "INFERENCE_PROBE_ENABLED",
        )
    }
    owner = create_async_engine(
        migration.url.get_secret_value(),
        poolclass=NullPool,
        echo=False,
        hide_parameters=True,
        connect_args={"timeout": 5, "command_timeout": 10},
    )
    try:
        async with owner.connect() as conn:
            identity = (await conn.execute(sa.text("SELECT current_database(),current_user"))).one()
            assert tuple(identity) == ("ai_gateway_test", "gateway_test_migrator")
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
            revision = (
                await conn.scalar(sa.text("SELECT version_num FROM alembic_version"))
                if "alembic_version" in tables
                else None
            )
            expected = {
                "projects",
                "api_keys",
                "request_logs",
                "health_checks",
                "admin_audit_logs",
                "alembic_version",
            }
            assert (not tables and revision is None) or (
                tables == expected and revision == REVISION
            ), "unexpected existing schema; no migration executed"
            report["migration_before"] = {
                "database": identity[0],
                "role": identity[1],
                "tables": sorted(tables),
                "revision": revision,
            }
        if revision is None:
            assert args.migrate, "initial migration requires --migrate"
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
            stdout, stderr = await process.communicate()
            assert process.returncode == 0, "migration failed; driver details suppressed"
        async with owner.connect() as conn:
            assert await conn.scalar(sa.text("SELECT version_num FROM alembic_version")) == REVISION
            report["migration_after"] = {
                "revision": REVISION,
                "tables": sorted(
                    (
                        await conn.execute(
                            sa.text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
                        )
                    ).scalars()
                ),
            }
    finally:
        await owner.dispose()

    engine = make_engine(config)
    repo = Repository(engine, cleanup_timeout=config.CLEANUP_TIMEOUT)
    project_id, key_id = uuid4(), uuid4()
    key = secrets.token_urlsafe(32)
    fixture_created = False
    request_ids = []
    calls = []
    streams = []
    mode = "ok"
    transport_payload = {
        "id": "slice-mock",
        "model": config.ALLOWED_MODELS[0],
        "choices": [{"message": {"role": "assistant", "content": "OK"}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
    }
    body = {
        "model": config.ALLOWED_MODELS[0],
        "messages": [{"role": "user", "content": "Reply with exactly OK."}],
        "max_tokens": 8,
        "stream": False,
    }
    auth = {"Authorization": "Bearer " + key}
    try:
        async with engine.connect() as conn:
            assert tuple(
                (await conn.execute(sa.text("SELECT current_database(),current_user"))).one()
            ) == ("ai_gateway_test", "gateway_test_app")
            assert not await conn.scalar(
                sa.text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")
            )
            privileges = {
                operation: bool(
                    await conn.scalar(
                        sa.text(
                            "SELECT has_table_privilege(current_user,'alembic_version',:operation)"
                        ),
                        {"operation": operation},
                    )
                )
                for operation in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
            }
            assert privileges == {
                "SELECT": True,
                "INSERT": False,
                "UPDATE": False,
                "DELETE": False,
                "TRUNCATE": False,
            }
            assert not await conn.scalar(
                sa.text("SELECT has_schema_privilege(current_user,'public','CREATE')")
            )
            assert not await conn.scalar(
                sa.text("SELECT has_database_privilege(current_user,current_database(),'TEMP')")
            )
            report["runtime_privileges"] = {
                "role": "gateway_test_app",
                "version_table": privileges,
                "schema_create": False,
                "temporary_tables": False,
            }
        async with engine.begin() as conn:
            await conn.execute(
                projects.insert().values(
                    id=project_id,
                    code="slice-" + project_id.hex,
                    name="Nonstream slice fixture",
                    max_concurrency=config.DEFAULT_PROJECT_MAX_CONCURRENCY,
                )
            )
            await conn.execute(
                api_keys.insert().values(
                    id=key_id,
                    project_id=project_id,
                    name="Temporary integration key",
                    key_hash=hashlib.sha256(key.encode()).hexdigest(),
                    key_prefix=key[:8],
                    max_concurrency=config.DEFAULT_KEY_MAX_CONCURRENCY,
                )
            )
        fixture_created = True

        async def upstream(request):
            assert (
                request.headers["authorization"]
                == "Bearer " + config.UPSTREAM_API_KEY.get_secret_value()
            )
            assert request.headers["x-gateway-request-source"] == "business"
            request_id = UUID(request.headers["x-request-id"])
            assert request.headers["authorization"] != auth["Authorization"]
            async with engine.connect() as conn:
                assert (
                    await conn.scalar(
                        sa.select(sa.func.count())
                        .select_from(request_logs)
                        .where(
                            request_logs.c.project_id == project_id,
                            request_logs.c.outcome == "started",
                            request_logs.c.request_id == request_id,
                        )
                    )
                    == 1
                )
            calls.append(mode)
            if mode == "timeout":
                raise httpx.ReadTimeout("simulated timeout")
            stream = JSONStream(transport_payload, hang_on_close=mode == "close-hang")
            streams.append(stream)
            return httpx.Response(
                503 if mode == "upstream-error" else 200,
                headers={"content-type": "application/json"},
                stream=stream,
            )

        app = create_app(config, repository=repo, transport=httpx.MockTransport(upstream))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client,
        ):
            assert (await client.get("/health/live")).status_code == 200
            assert (await client.get("/health/ready")).status_code == 200
            rejected = []
            for case in (
                "invalid",
                "administrator",
                "disabled-key",
                "expired-key",
                "disabled-project",
            ):
                async with engine.begin() as conn:
                    await conn.execute(
                        api_keys.update()
                        .where(api_keys.c.id == key_id)
                        .values(
                            is_active=case != "disabled-key",
                            expires_at=datetime.now(UTC) - timedelta(seconds=1)
                            if case == "expired-key"
                            else None,
                        )
                    )
                    await conn.execute(
                        projects.update()
                        .where(projects.c.id == project_id)
                        .values(is_active=case != "disabled-project")
                    )
                supplied = (
                    "unknown"
                    if case == "invalid"
                    else config.ADMIN_API_KEY.get_secret_value()
                    if case == "administrator"
                    else key
                )
                response = await client.post(
                    "/v1/chat/completions",
                    json=body,
                    headers={"Authorization": "Bearer " + supplied},
                )
                assert response.status_code == 401 and calls == []
                rejected.append(case)
            report["authentication_rejections"] = rejected
            async with engine.begin() as conn:
                await conn.execute(
                    projects.update().where(projects.c.id == project_id).values(is_active=True)
                )
                await conn.execute(
                    api_keys.update()
                    .where(api_keys.c.id == key_id)
                    .values(is_active=True, expires_at=None)
                )
            outcomes = []
            for mode, status, outcome in [
                ("ok", 200, "completed"),
                ("close-hang", 200, "completed"),
                ("timeout", 504, "timeout"),
                ("upstream-error", 503, "upstream_error"),
            ]:
                began = time.monotonic()
                response = await client.post("/v1/chat/completions", json=body, headers=auth)
                assert response.status_code == status, "unexpected slice response status"
                request_id = UUID(response.headers["x-request-id"])
                request_ids.append(str(request_id))
                async with engine.connect() as conn:
                    row = (
                        (
                            await conn.execute(
                                sa.select(request_logs).where(
                                    request_logs.c.request_id == request_id
                                )
                            )
                        )
                        .mappings()
                        .one()
                    )
                    assert row["outcome"] == outcome and row["finished_at"] is not None
                    if mode in ("ok", "close-hang"):
                        assert (
                            row["usage_status"] == "reported"
                            and row["total_tokens"] == response.json()["usage"]["total_tokens"] == 9
                        )
                        assert not await repo.settle(
                            request_id, {"outcome": "completed", "total_tokens": 999}
                        )
                assert app.state.admission.active == 0
                if mode == "close-hang":
                    elapsed = time.monotonic() - began
                    assert streams[-1].close_cancelled
                    assert elapsed < config.CLEANUP_TIMEOUT + 1
                    assert app.state.upstream_close_failures == 1
                    report["close_hang"] = {
                        "elapsed_seconds": round(elapsed, 3),
                        "closed_by_timeout": True,
                        "settled": True,
                        "active": 0,
                    }
                outcomes.append(
                    {"request_id": str(request_id), "status": status, "outcome": outcome}
                )
            report["database_mock_upstream_cases"] = outcomes
            # Real DB constraint failure during registration must prevent any upstream send.
            original_register = repo.register

            async def invalid_registration(values):
                await original_register({**values, "project_id": uuid4()})

            repo.register = invalid_registration
            before = len(calls)
            try:
                response = await client.post("/v1/chat/completions", json=body, headers=auth)
                assert (
                    response.status_code == 503
                    and len(calls) == before
                    and app.state.admission.active == 0
                )
            finally:
                repo.register = original_register
            report["registration_constraint_failure_no_upstream"] = True

        if args.deepseek:
            real_app = create_app(config, repository=repo)
            async with (
                real_app.router.lifespan_context(real_app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=real_app), base_url="http://gateway"
                ) as client,
            ):
                response = await client.post("/v1/chat/completions", json=body, headers=auth)
                assert response.status_code == 200, (
                    "DeepSeek non-streaming verification failed; no automatic retry"
                )
                request_id = UUID(response.headers["x-request-id"])
                request_ids.append(str(request_id))
                async with engine.connect() as conn:
                    row = (
                        (
                            await conn.execute(
                                sa.select(request_logs).where(
                                    request_logs.c.request_id == request_id
                                )
                            )
                        )
                        .mappings()
                        .one()
                    )
                    assert row["outcome"] == "completed" and row["usage_status"] == "reported"
                    usage = {
                        name: row[name]
                        for name in ("prompt_tokens", "completion_tokens", "total_tokens")
                    }
                    assert usage == {name: response.json()["usage"][name] for name in usage}
                assert real_app.state.admission.active == 0
                report["deepseek"] = {
                    "request_id": str(request_id),
                    "status": 200,
                    "outcome": "completed",
                    "usage_status": "reported",
                    "usage": usage,
                    "calls": 1,
                    "max_tokens": 8,
                }
        report["request_ids"] = request_ids
    finally:
        try:
            if fixture_created:
                async with engine.begin() as conn:
                    await conn.execute(
                        request_logs.delete().where(request_logs.c.project_id == project_id)
                    )
                    await conn.execute(api_keys.delete().where(api_keys.c.project_id == project_id))
                    await conn.execute(projects.delete().where(projects.c.id == project_id))
                async with engine.connect() as conn:
                    assert (
                        await conn.scalar(
                            sa.select(sa.func.count())
                            .select_from(request_logs)
                            .where(request_logs.c.project_id == project_id)
                        )
                        == 0
                    )
                    assert (
                        await conn.scalar(
                            sa.select(sa.func.count())
                            .select_from(projects)
                            .where(projects.c.id == project_id)
                        )
                        == 0
                    )
                report["fixture_cleanup"] = (
                    "only this run project/key/request rows deleted and verified"
                )
        finally:
            await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--deepseek", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    assert not args.report.exists(), "use a new evidence file; do not overwrite previous attempts"
    report = {
        "recorded_at_utc": datetime.now(UTC).isoformat(),
        "scope": "EX-P0-SLICE-01",
        "status": "started",
        "m0_signed": False,
        "a_acceptance": False,
    }
    try:
        asyncio.run(verify(args, report))
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error_type=type(error).__name__)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    args.report.chmod(0o600)
    print(
        json.dumps(
            {
                "status": report["status"],
                "report": str(args.report),
                "error_type": report.get("error_type"),
            }
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
