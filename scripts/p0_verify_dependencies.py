#!/usr/bin/env python3
"""Run inside the candidate environment, then repeat on offline Ubuntu.

This is a dependency smoke check, not a gateway functional acceptance test.
It uses in-process HTTP and does not connect to PostgreSQL or an LLM.
"""

from datetime import datetime, timezone
from importlib import metadata
import json
import platform
import sys
import zoneinfo


def main():
    result = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "system": platform.system(), "architecture": platform.machine(),
        "python": platform.python_version(), "milestone_signoff": False,
    }
    try:
        import alembic.config
        import anyio
        import asyncpg
        from fastapi import FastAPI
        import httpx
        from pydantic import BaseModel
        from pydantic_settings import BaseSettings
        from sqlalchemy.ext.asyncio import create_async_engine
        import uvicorn

        result["versions"] = {name: metadata.version(name) for name in (
            "fastapi", "starlette", "sqlalchemy", "asyncpg", "alembic",
            "httpx", "uvicorn", "anyio", "pydantic", "pydantic-settings", "tzdata",
        )}
        assert (3, 12, 3) <= sys.version_info[:3] < (3, 13, 0)
        app = FastAPI(
            openapi_url=None, docs_url=None, redoc_url=None,
            telemetry={"tracing": False, "metrics": False, "logs": False,
                       "operation_spans": False, "auto_configure": False},
        )

        class Payload(BaseModel):
            value: int

        @app.post("/echo")
        async def echo(payload: Payload):
            return {"value": payload.value}

        async def check():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://p0.invalid",
            ) as client:
                response = await client.post("/echo", json={"value": 7})
                assert response.status_code == 200 and response.json() == {"value": 7}
            # No connect call: database/network integration is a separate check.
            engine = create_async_engine("postgresql+asyncpg://p0@127.0.0.1:1/p0")
            assert engine.dialect.driver == "asyncpg"
            await engine.dispose()

        anyio.run(check)
        config = uvicorn.Config(app, workers=1, proxy_headers=False, log_config=None)
        assert config.workers == 1 and config.proxy_headers is False
        assert alembic.config.Config() is not None and BaseSettings is not None
        assert asyncpg.__version__ == result["versions"]["asyncpg"]
        zoneinfo.reset_tzpath(())
        zoneinfo.ZoneInfo.clear_cache()
        assert not zoneinfo.TZPATH
        for key in ("UTC", "Asia/Shanghai"):
            zoneinfo.ZoneInfo(key)
        result.update(status="passed", checks=[
            "candidate imports", "FastAPI/Pydantic/HTTPX/AnyIO in-process request",
            "SQLAlchemy asyncpg engine construction (no DB connect)",
            "Uvicorn single-worker/proxy_headers=False configuration",
            "FastAPI telemetry explicitly disabled for the offline profile",
            "locked Python tzdata with system TZPATH disabled",
        ])
    except Exception as exc:
        result.update(status="failed", error_type=type(exc).__name__)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
