import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.exc import SQLAlchemyError
from test_nonstream_slice import Body, FakeRepository, slice_settings

from app.db.repository import Identity, ProjectConflict, ResourceNotFound, public_key
from app.main import create_app


class AdminRepository(FakeRepository):
    def __init__(self):
        super().__init__()
        self.projects = {}
        self.keys = {}
        self.changes = []
        self.fail_admin = False

    async def key_exists(self, key_hash):
        return any(row["key_hash"] == key_hash for row in self.keys.values())

    async def authenticate(self, key_hash):
        for key in self.keys.values():
            project = self.projects[key["project_id"]]
            if (
                key["key_hash"] == key_hash
                and key["is_active"]
                and project["is_active"]
                and (key["expires_at"] is None or key["expires_at"] > datetime.now(UTC))
            ):
                return Identity(
                    project["id"], key["id"], project["max_concurrency"], key["max_concurrency"]
                )
        return None

    async def create_project(self, values, request_id):
        if self.fail_admin:
            raise SQLAlchemyError("simulated failure")
        if any(row["code"] == values["code"] for row in self.projects.values()):
            raise ProjectConflict
        row = dict(
            id=uuid4(),
            is_active=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            **values,
        )
        self.projects[row["id"]] = row
        self.changes.append(request_id)
        return row.copy()

    async def create_key(self, project_id, values, request_id):
        if self.fail_admin:
            raise SQLAlchemyError("simulated failure")
        if project_id not in self.projects:
            raise ResourceNotFound
        row = dict(
            id=uuid4(),
            project_id=project_id,
            is_active=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            revoked_at=None,
            **values,
        )
        row.setdefault("name", None)
        row.setdefault("expires_at", None)
        self.keys[row["id"]] = row
        self.changes.append(request_id)
        return public_key(row)

    async def update_resource(self, kind, resource_id, values, request_id):
        if self.fail_admin:
            raise SQLAlchemyError("simulated failure")
        rows = self.projects if kind == "project" else self.keys
        if resource_id not in rows:
            raise ResourceNotFound
        rows[resource_id].update(values)
        self.changes.append(request_id)
        return rows[resource_id].copy() if kind == "project" else public_key(rows[resource_id])

    async def list_resources(self, kind, project_id, cursor, limit, active=None):
        if self.fail_admin:
            raise SQLAlchemyError("simulated failure")
        if kind == "key" and project_id not in self.projects:
            raise ResourceNotFound
        rows = list((self.projects if kind == "project" else self.keys).values())
        rows = [
            r
            for r in rows
            if (kind == "project" or r["project_id"] == project_id)
            and (active is None or r["is_active"] == active)
            and (cursor is None or (r["created_at"], r["id"]) < cursor)
        ]
        rows = sorted(rows, key=lambda r: (r["created_at"], r["id"]), reverse=True)[: limit + 1]
        return [r.copy() if kind == "project" else public_key(r) for r in rows]


def test_admin_management_lifecycle_and_isolation(config_values):
    async def run():
        repo = AdminRepository()
        calls = []

        async def upstream(request):
            calls.append(request)
            assert any(row["outcome"] == "started" for row in repo.rows.values())
            return httpx.Response(200, headers={"content-type": "application/json"}, stream=Body())

        settings = slice_settings(config_values)
        app = create_app(settings, repository=repo, transport=httpx.MockTransport(upstream))
        admin = {"Authorization": "Bearer " + settings.ADMIN_API_KEY.get_secret_value()}
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client,
        ):
            assert (await client.get("/admin/projects")).status_code == 401
            project = (
                await client.post(
                    "/admin/projects", headers=admin, json={"code": "demo", "name": "Demo"}
                )
            ).json()
            assert project["max_concurrency"] == settings.DEFAULT_PROJECT_MAX_CONCURRENCY
            path = f"/admin/projects/{project['id']}/keys"
            result = await client.post(path, headers=admin, json={})
            assert result.status_code == 201 and result.headers["cache-control"] == "no-store"
            key = result.json()
            auth = {"Authorization": "Bearer " + key["key"]}
            assert len(key["key"]) >= 49
            listing = (await client.get(path, headers=admin)).json()
            assert "key" not in listing["data"][0] and "key_hash" not in listing["data"][0]
            assert key["key"] not in str(repo.keys)
            assert (await client.get("/admin/projects", headers=auth)).status_code == 403
            assert (await client.get("/v1/models", headers=admin)).status_code == 401
            models = await client.get("/v1/models", headers=auth)
            assert models.json()["data"][0]["id"] == settings.ALLOWED_MODELS[0]
            body = {
                "model": settings.ALLOWED_MODELS[0],
                "messages": [{"role": "user", "content": "OK"}],
            }
            assert (
                await client.post("/v1/chat/completions", headers=auth, json=body)
            ).status_code == 200
            assert len(calls) == 1
            assert (
                await client.patch(
                    f"/admin/keys/{key['id']}", headers=admin, json={"is_active": False}
                )
            ).status_code == 200
            assert (
                await client.post("/v1/chat/completions", headers=auth, json=body)
            ).status_code == 401
            assert (await client.get("/v1/models", headers=auth)).status_code == 401
            assert (await client.get("/admin/projects", headers=auth)).status_code == 403
            replacement = (await client.post(path, headers=admin, json={})).json()
            assert replacement["key"] != key["key"]
            new_auth = {"Authorization": "Bearer " + replacement["key"]}
            assert (
                await client.post("/v1/chat/completions", headers=new_auth, json=body)
            ).status_code == 200
            assert (
                await client.patch(
                    f"/admin/projects/{project['id']}", headers=admin, json={"is_active": False}
                )
            ).status_code == 200
            assert (await client.get("/v1/models", headers=new_auth)).status_code == 401
            assert len(calls) == 2 and app.state.admin_db_active == app.state.admission.active == 0

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload",
    [
        {"code": "x", "name": "x", "max_concurrency": True},
        {"code": "x", "name": "x", "max_concurrency": None},
        {"code": "x", "name": "x", "max_concurrency": 0},
        {"code": "x", "name": "x", "max_concurrency": 2147483648},
        {"code": "", "name": "x"},
        {"code": "x", "name": " "},
        {"code": "x", "name": "x", "extra": "not allowed"},
    ],
)
def test_admin_invalid_create_does_not_write(config_values, payload):
    async def run():
        repo = AdminRepository()
        settings = slice_settings(config_values)
        app = create_app(settings, repository=repo)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client,
        ):
            response = await client.post(
                "/admin/projects",
                headers={"Authorization": "Bearer " + settings.ADMIN_API_KEY.get_secret_value()},
                json=payload,
            )
            assert response.status_code == 400 and not repo.projects and not repo.changes

    asyncio.run(run())


@pytest.mark.parametrize(
    "query", ["limit=0", "limit=201", "cursor=bad", "limit=1&limit=2", "active=yes", "unknown=x"]
)
def test_admin_list_query_validation(config_values, query):
    async def run():
        repo = AdminRepository()
        settings = slice_settings(config_values)
        app = create_app(settings, repository=repo)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client,
        ):
            response = await client.get(
                "/admin/projects?" + query,
                headers={"Authorization": "Bearer " + settings.ADMIN_API_KEY.get_secret_value()},
            )
            assert response.status_code == 400 and app.state.admin_db_active == 0

    asyncio.run(run())


def test_admin_pagination_errors_and_capacity(config_values):
    async def run():
        repo = AdminRepository()
        settings = slice_settings(config_values)
        app = create_app(settings, repository=repo)
        admin = {"Authorization": "Bearer " + settings.ADMIN_API_KEY.get_secret_value()}
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client,
        ):
            for code in ("a", "b", "c"):
                assert (
                    await client.post(
                        "/admin/projects", headers=admin, json={"code": code, "name": code}
                    )
                ).status_code == 201
            assert (
                await client.post("/admin/projects", headers=admin, json={"code": "a", "name": "a"})
            ).status_code == 409
            cursor = None
            ids = []
            while True:
                page = (
                    await client.get(
                        "/admin/projects",
                        headers=admin,
                        params={"limit": 1, **({"cursor": cursor} if cursor else {})},
                    )
                ).json()
                ids.extend(r["id"] for r in page["data"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break
            assert len(ids) == len(set(ids)) == 3
            assert (
                await client.patch(
                    "/admin/projects/" + str(uuid4()), headers=admin, json={"name": "changed"}
                )
            ).status_code == 404
            assert (
                await client.patch("/admin/projects/" + ids[0], headers=admin, json={})
            ).status_code == 400
            assert (
                await client.patch("/admin/projects/" + ids[0], headers=admin, json={"name": None})
            ).status_code == 400
            path = f"/admin/projects/{ids[0]}/keys"
            assert (
                await client.post(path, headers=admin, json={"expires_at": "2027-01-01T00:00:00"})
            ).status_code == 400
            expiry = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            key = (await client.post(path, headers=admin, json={"expires_at": expiry})).json()
            auth = {"Authorization": "Bearer " + key["key"]}
            assert (await client.get("/v1/models", headers=auth)).status_code == 401
            assert (
                await client.patch(
                    "/admin/keys/" + key["id"], headers=admin, json={"expires_at": None}
                )
            ).status_code == 200
            assert (await client.get("/v1/models", headers=auth)).status_code == 200
            repo.fail_admin = True
            result = await client.get("/admin/projects", headers=admin)
            assert result.status_code == 503 and result.headers["retry-after"] == "1"
            assert app.state.admin_db_active == 0
            app.state.admin_db_active = settings.ADMIN_DB_MAX_INFLIGHT
            result = await client.get("/admin/projects", headers=admin)
            assert (
                result.status_code == 503
                and result.json()["error"]["code"] == "admin_capacity_exceeded"
            )
            assert app.state.admin_db_active == settings.ADMIN_DB_MAX_INFLIGHT
            app.state.admin_db_active = 0

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_admin_db_operation_is_bounded_and_releases_lane(config_values, cancel):
    async def run():
        from app.core.settings import Settings

        repo = AdminRepository()
        entered = asyncio.Event()
        original = repo.list_resources

        async def slow(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        repo.list_resources = slow
        settings = Settings(
            **(
                slice_settings(config_values).model_dump()
                | {
                    "DB_OPERATION_TIMEOUT": 0.05,
                    "SETTLEMENT_ATTEMPT_TIMEOUT": 0.02,
                }
            )
        )
        app = create_app(settings, repository=repo)
        admin = {"Authorization": "Bearer " + settings.ADMIN_API_KEY.get_secret_value()}
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client,
        ):
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
            assert app.state.admin_db_active == 0
            repo.list_resources = original
            assert (await client.get("/admin/projects", headers=admin)).status_code == 200

    asyncio.run(run())


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PATCH", "/admin/projects/not-a-uuid"),
        ("POST", "/admin/projects/not-a-uuid/keys"),
        ("GET", "/admin/projects/not-a-uuid/keys"),
        ("PATCH", "/admin/keys/not-a-uuid"),
    ],
)
def test_admin_authentication_precedes_path_validation(config_values, method, path):
    async def run():
        import hashlib

        settings = slice_settings(config_values)
        repo = AdminRepository()
        # Known but disabled and expired project key must still be forbidden.
        repo.keys[uuid4()] = {"key_hash": hashlib.sha256(b"known-key").hexdigest()}
        app = create_app(settings, repository=repo)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client,
        ):
            for credential, expected in [
                (None, 401),
                ("unknown", 401),
                ("known-key", 403),
                (settings.ADMIN_API_KEY.get_secret_value(), 400),
            ]:
                headers = {} if credential is None else {"Authorization": "Bearer " + credential}
                response = await client.request(method, path, headers=headers, json={})
                assert response.status_code == expected
                assert response.headers["x-request-id"]
                assert app.state.admin_db_active == 0

    asyncio.run(run())
