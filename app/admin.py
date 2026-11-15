"""Bounded management routes; every write is coupled to its audit transaction."""

import base64
import hashlib
import json
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)
from sqlalchemy.exc import SQLAlchemyError

from app.core.settings import Settings
from app.db.repository import ProjectConflict, Repository, ResourceNotFound
from app.db.runtime import database_timeout
from app.errors import GatewayError

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Code = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
Positive = Annotated[int, Field(gt=0, le=2147483647)]


class Input(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    @field_validator("max_concurrency", "is_active", check_fields=False)
    @classmethod
    def not_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("explicit null is not allowed")
        return value

    @field_validator("expires_at", mode="before", check_fields=False)
    @classmethod
    def expiry(cls, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("expiry requires an ISO timestamp")
        return datetime.fromisoformat(value.replace("Z", "+00:00"))


class ProjectCreate(Input):
    code: Code
    name: Name
    max_concurrency: Positive | None = None


class ProjectUpdate(Input):
    name: Name | None = None
    is_active: bool | None = None
    max_concurrency: Positive | None = None

    @field_validator("name")
    @classmethod
    def name_not_null(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("name must not be null")
        return value


class KeyCreate(Input):
    name: Name | None = None
    expires_at: AwareDatetime | None = None
    max_concurrency: Positive | None = None


class KeyUpdate(Input):
    is_active: bool | None = None
    expires_at: AwareDatetime | None = None
    max_concurrency: Positive | None = None


def decode_cursor(value: str | None) -> tuple[datetime, UUID] | None:
    if value is None:
        return None
    try:
        if not value or len(value) > 512:
            raise ValueError
        raw = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        if not isinstance(raw, list) or len(raw) != 2 or not all(isinstance(x, str) for x in raw):
            raise ValueError
        stamp = datetime.fromisoformat(raw[0])
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError
        return stamp, UUID(raw[1])
    except (ValueError, TypeError, UnicodeError):
        raise GatewayError(400, "invalid_request") from None


def encode_cursor(row: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(
        json.dumps([row["created_at"].isoformat(), str(row["id"])]).encode()
    ).decode()


def install_admin_routes(
    app: FastAPI,
    config: Settings,
    repo: Repository,
    authenticate: Callable[[Request], Awaitable[None]],
    read_json: Callable[[Request], Awaitable[Any]],
) -> None:
    app.state.admin_db_active = 0
    router = APIRouter(dependencies=[Depends(authenticate)])

    @asynccontextmanager
    async def db_operation() -> AsyncIterator[None]:
        if app.state.admin_db_active >= config.ADMIN_DB_MAX_INFLIGHT:
            raise GatewayError(503, "admin_capacity_exceeded")
        app.state.admin_db_active += 1
        try:
            async with database_timeout(config.DB_OPERATION_TIMEOUT):
                yield
        except ResourceNotFound:
            raise GatewayError(404, "not_found") from None
        except ProjectConflict:
            raise GatewayError(409, "project_code_conflict") from None
        except (SQLAlchemyError, TimeoutError):
            raise GatewayError(503, "database_unavailable") from None
        finally:
            app.state.admin_db_active -= 1

    async def body(request: Request, schema: type[Input], patch: bool = False) -> dict[str, Any]:
        try:
            parsed = schema.model_validate(await read_json(request))
        except (ValidationError, ValueError):
            raise GatewayError(400, "invalid_request") from None
        values = parsed.model_dump(exclude_unset=True)
        if patch and not values:
            raise GatewayError(400, "invalid_request")
        return values

    def response(value: Any, status: int = 200) -> JSONResponse:
        return JSONResponse(
            jsonable_encoder(value), status_code=status, headers={"Cache-Control": "no-store"}
        )

    @router.post("/admin/projects")
    async def create_project(request: Request) -> JSONResponse:
        values = await body(request, ProjectCreate)
        values.setdefault("max_concurrency", config.DEFAULT_PROJECT_MAX_CONCURRENCY)
        async with db_operation():
            row = await repo.create_project(values, request.state.request_id)
        return response(row, 201)

    @router.patch("/admin/projects/{resource_id}")
    async def update_project(resource_id: UUID, request: Request) -> JSONResponse:
        values = await body(request, ProjectUpdate, patch=True)
        async with db_operation():
            row = await repo.update_resource(
                "project", resource_id, values, request.state.request_id
            )
        return response(row)

    @router.post("/admin/projects/{project_id}/keys")
    async def create_key(project_id: UUID, request: Request) -> JSONResponse:
        values = await body(request, KeyCreate)
        values.setdefault("max_concurrency", config.DEFAULT_KEY_MAX_CONCURRENCY)
        key = "sk-gw-" + secrets.token_urlsafe(32)
        async with db_operation():
            row = await repo.create_key(
                project_id,
                {
                    **values,
                    "key_hash": hashlib.sha256(key.encode()).hexdigest(),
                    "key_prefix": key[:12],
                },
                request.state.request_id,
            )
        return response({**row, "key": key}, 201)

    @router.patch("/admin/keys/{resource_id}")
    async def update_key(resource_id: UUID, request: Request) -> JSONResponse:
        values = await body(request, KeyUpdate, patch=True)
        async with db_operation():
            row = await repo.update_resource("key", resource_id, values, request.state.request_id)
        return response(row)

    async def listing(request: Request, kind: str, project_id: UUID | None = None) -> JSONResponse:
        query = request.query_params
        allowed = {"cursor", "limit", "active"} if kind == "project" else {"cursor", "limit"}
        if any(k not in allowed or len(query.getlist(k)) != 1 for k in query):
            raise GatewayError(400, "invalid_request")
        raw_limit = query.get("limit", str(config.PAGINATION_DEFAULT_LIMIT))
        if len(raw_limit) > 6 or not re.fullmatch(r"[0-9]+", raw_limit):
            raise GatewayError(400, "invalid_request")
        limit = int(raw_limit)
        if not 1 <= limit <= config.PAGINATION_MAX_LIMIT:
            raise GatewayError(400, "invalid_request")
        raw_active = query.get("active")
        if raw_active is not None and raw_active not in {"true", "false"}:
            raise GatewayError(400, "invalid_request")
        active = None if raw_active is None else raw_active == "true"
        cursor = decode_cursor(query.get("cursor"))
        async with db_operation():
            rows = await repo.list_resources(kind, project_id, cursor, limit, active)
        has_more = len(rows) > limit
        rows = rows[:limit]
        return response(
            {"data": rows, "next_cursor": encode_cursor(rows[-1]) if has_more else None}
        )

    @router.get("/admin/projects")
    async def list_projects(request: Request) -> JSONResponse:
        return await listing(request, "project")

    @router.get("/admin/projects/{project_id}/keys")
    async def list_keys(project_id: UUID, request: Request) -> JSONResponse:
        return await listing(request, "key", project_id)

    app.include_router(router)
