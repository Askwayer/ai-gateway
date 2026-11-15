"""The three tables needed by the first non-streaming slice."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base

metadata = Base.metadata
now = sa.text("statement_timestamp()")
projects = sa.Table(
    "projects",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("code", sa.String(64), nullable=False, unique=True),
    sa.Column("name", sa.String(128), nullable=False),
    sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("max_concurrency", sa.Integer, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
    sa.CheckConstraint("max_concurrency > 0", name="positive_concurrency"),
)
api_keys = sa.Table(
    "api_keys",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False),
    sa.Column("name", sa.String(128)),
    sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
    sa.Column("key_prefix", sa.String(24), nullable=False),
    sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("expires_at", sa.DateTime(timezone=True)),
    sa.Column("max_concurrency", sa.Integer, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
    sa.Column("revoked_at", sa.DateTime(timezone=True)),
    sa.UniqueConstraint("id", "project_id"),
    sa.CheckConstraint("max_concurrency > 0", name="positive_concurrency"),
    sa.CheckConstraint("key_hash ~ '^[0-9a-f]{64}$'", name="sha256_hash"),
)
request_logs = sa.Table(
    "request_logs",
    metadata,
    sa.Column("request_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False),
    sa.Column("api_key_id", UUID(as_uuid=True), nullable=False),
    sa.Column("upstream_id", sa.String(64), nullable=False),
    sa.Column("model", sa.String(255), nullable=False),
    sa.Column("upstream_request_id", sa.String(128)),
    sa.Column("is_stream", sa.Boolean, nullable=False),
    sa.Column("admitted", sa.Boolean, nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
    sa.Column("upstream_started_at", sa.DateTime(timezone=True)),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
    sa.Column("duration_ms", sa.Float),
    sa.Column("first_event_ms", sa.Float),
    sa.Column("outcome", sa.String(32), nullable=False),
    sa.Column("error_code", sa.String(64)),
    sa.Column("upstream_status_code", sa.Integer),
    sa.Column("response_status_code", sa.Integer),
    sa.Column("prompt_tokens", sa.BigInteger),
    sa.Column("completion_tokens", sa.BigInteger),
    sa.Column("total_tokens", sa.BigInteger),
    sa.Column("usage_status", sa.String(32), nullable=False),
    sa.Column("usage_source", sa.String(32), nullable=False),
    sa.Column("usage_missing_reason", sa.String(64)),
    sa.Column("usage_validation_error", sa.String(64)),
    sa.Column("process_run_id", UUID(as_uuid=True), nullable=False),
    sa.Column("settlement_attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("rejection_scope", sa.String(32)),
    sa.ForeignKeyConstraint(["api_key_id", "project_id"], ["api_keys.id", "api_keys.project_id"]),
    sa.CheckConstraint(
        "outcome IN ('started','completed','client_cancelled','timeout','upstream_error','gateway_error','unknown','rejected')",
        name="outcome",
    ),
    sa.CheckConstraint(
        "usage_status IN ('pending','reported','partial','missing','invalid','not_applicable')",
        name="usage_status",
    ),
    sa.CheckConstraint(
        "usage_source IN ('none','upstream_json','upstream_sse')", name="usage_source"
    ),
    sa.CheckConstraint(
        "(outcome='started' AND finished_at IS NULL AND usage_status='pending' AND admitted) OR (outcome<>'started' AND finished_at IS NOT NULL AND usage_status<>'pending')",
        name="terminal_time",
    ),
    sa.CheckConstraint(
        "(outcome='rejected' AND NOT admitted) OR (outcome<>'rejected' AND admitted AND rejection_scope IS NULL)",
        name="admission",
    ),
    sa.CheckConstraint("outcome<>'unknown' OR duration_ms IS NULL", name="unknown_duration"),
    sa.CheckConstraint(
        "settlement_attempts>=0 AND (duration_ms IS NULL OR duration_ms>=0) AND (first_event_ms IS NULL OR first_event_ms>=0)",
        name="nonnegative_counters",
    ),
    sa.CheckConstraint(
        "(prompt_tokens IS NULL OR prompt_tokens>=0) AND (completion_tokens IS NULL OR completion_tokens>=0) AND (total_tokens IS NULL OR total_tokens>=0)",
        name="nonnegative_tokens",
    ),
    sa.CheckConstraint(
        "usage_status<>'reported' OR (prompt_tokens IS NOT NULL AND completion_tokens IS NOT NULL AND total_tokens IS NOT NULL AND total_tokens=prompt_tokens+completion_tokens AND usage_source<>'none')",
        name="reported_usage",
    ),
    sa.CheckConstraint(
        "usage_status<>'missing' OR (prompt_tokens IS NULL AND completion_tokens IS NULL AND total_tokens IS NULL)",
        name="missing_usage",
    ),
    sa.CheckConstraint(
        "outcome<>'rejected' OR (usage_status='not_applicable' AND usage_source='none' AND started_at=finished_at AND started_at=updated_at AND upstream_started_at IS NULL AND duration_ms IS NULL AND first_event_ms IS NULL AND prompt_tokens IS NULL AND completion_tokens IS NULL AND total_tokens IS NULL)",
        name="rejected_values",
    ),
    sa.CheckConstraint(
        "outcome<>'rejected' OR (upstream_status_code IS NULL AND ((rejection_scope IN ('global','project','key') AND response_status_code=429 AND error_code='concurrency_limit_exceeded') OR (rejection_scope='upstream_unhealthy' AND response_status_code=503 AND error_code='upstream_unhealthy')) IS TRUE)",
        name=sa.schema.conv("ck_rejected_http_contract"),
    ),
)
sa.Index("ix_api_keys_project_created", api_keys.c.project_id, api_keys.c.created_at, api_keys.c.id)
for name, columns in {
    "project_time": [
        request_logs.c.project_id,
        request_logs.c.started_at,
        request_logs.c.request_id,
    ],
    "key_time": [request_logs.c.api_key_id, request_logs.c.started_at, request_logs.c.request_id],
    "time": [request_logs.c.started_at, request_logs.c.request_id],
    "model_time": [request_logs.c.model, request_logs.c.started_at],
}.items():
    sa.Index("ix_request_logs_" + name, *columns)
sa.Index(
    "ix_request_logs_started_run",
    request_logs.c.process_run_id,
    request_logs.c.started_at,
    postgresql_where=request_logs.c.outcome == "started",
)

REVISION = "0001_nonstream_slice"
