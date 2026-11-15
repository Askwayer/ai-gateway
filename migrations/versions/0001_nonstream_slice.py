"""First non-streaming slice: projects, keys and request accounting.

DDL is a frozen snapshot, independent of future runtime model changes.
"""

from alembic import op

revision = "0001_nonstream_slice"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "\nCREATE TABLE projects (\n\tid UUID NOT NULL, \n\tcode VARCHAR(64) NOT NULL, \n\tname VARCHAR(128) NOT NULL, \n\tis_active BOOLEAN DEFAULT true NOT NULL, \n\tmax_concurrency INTEGER NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT statement_timestamp() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT statement_timestamp() NOT NULL, \n\tCONSTRAINT pk_projects PRIMARY KEY (id), \n\tCONSTRAINT ck_projects_positive_concurrency CHECK (max_concurrency > 0), \n\tCONSTRAINT uq_projects_code UNIQUE (code)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE api_keys (\n\tid UUID NOT NULL, \n\tproject_id UUID NOT NULL, \n\tname VARCHAR(128), \n\tkey_hash VARCHAR(64) NOT NULL, \n\tkey_prefix VARCHAR(24) NOT NULL, \n\tis_active BOOLEAN DEFAULT true NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE, \n\tmax_concurrency INTEGER NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT statement_timestamp() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT statement_timestamp() NOT NULL, \n\trevoked_at TIMESTAMP WITH TIME ZONE, \n\tCONSTRAINT pk_api_keys PRIMARY KEY (id), \n\tCONSTRAINT uq_api_keys_id UNIQUE (id, project_id), \n\tCONSTRAINT ck_api_keys_positive_concurrency CHECK (max_concurrency > 0), \n\tCONSTRAINT ck_api_keys_sha256_hash CHECK (key_hash ~ '^[0-9a-f]{64}$'), \n\tCONSTRAINT fk_api_keys_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id), \n\tCONSTRAINT uq_api_keys_key_hash UNIQUE (key_hash)\n)\n\n"
    )
    op.execute("CREATE INDEX ix_api_keys_project_created ON api_keys (project_id, created_at, id)")
    op.execute(
        "\nCREATE TABLE request_logs (\n\trequest_id UUID NOT NULL, \n\tproject_id UUID NOT NULL, \n\tapi_key_id UUID NOT NULL, \n\tupstream_id VARCHAR(64) NOT NULL, \n\tmodel VARCHAR(255) NOT NULL, \n\tupstream_request_id VARCHAR(128), \n\tis_stream BOOLEAN NOT NULL, \n\tadmitted BOOLEAN NOT NULL, \n\tstarted_at TIMESTAMP WITH TIME ZONE DEFAULT statement_timestamp() NOT NULL, \n\tupstream_started_at TIMESTAMP WITH TIME ZONE, \n\tfinished_at TIMESTAMP WITH TIME ZONE, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT statement_timestamp() NOT NULL, \n\tduration_ms FLOAT, \n\tfirst_event_ms FLOAT, \n\toutcome VARCHAR(32) NOT NULL, \n\terror_code VARCHAR(64), \n\tupstream_status_code INTEGER, \n\tresponse_status_code INTEGER, \n\tprompt_tokens BIGINT, \n\tcompletion_tokens BIGINT, \n\ttotal_tokens BIGINT, \n\tusage_status VARCHAR(32) NOT NULL, \n\tusage_source VARCHAR(32) NOT NULL, \n\tusage_missing_reason VARCHAR(64), \n\tusage_validation_error VARCHAR(64), \n\tprocess_run_id UUID NOT NULL, \n\tsettlement_attempts INTEGER DEFAULT '0' NOT NULL, \n\trejection_scope VARCHAR(32), \n\tCONSTRAINT pk_request_logs PRIMARY KEY (request_id), \n\tCONSTRAINT fk_request_logs_api_key_id_api_keys FOREIGN KEY(api_key_id, project_id) REFERENCES api_keys (id, project_id), \n\tCONSTRAINT ck_request_logs_outcome CHECK (outcome IN ('started','completed','client_cancelled','timeout','upstream_error','gateway_error','unknown','rejected')), \n\tCONSTRAINT ck_request_logs_usage_status CHECK (usage_status IN ('pending','reported','partial','missing','invalid','not_applicable')), \n\tCONSTRAINT ck_request_logs_usage_source CHECK (usage_source IN ('none','upstream_json','upstream_sse')), \n\tCONSTRAINT ck_request_logs_terminal_time CHECK ((outcome='started' AND finished_at IS NULL AND usage_status='pending' AND admitted) OR (outcome<>'started' AND finished_at IS NOT NULL AND usage_status<>'pending')), \n\tCONSTRAINT ck_request_logs_admission CHECK ((outcome='rejected' AND NOT admitted) OR (outcome<>'rejected' AND admitted AND rejection_scope IS NULL)), \n\tCONSTRAINT ck_request_logs_unknown_duration CHECK (outcome<>'unknown' OR duration_ms IS NULL), \n\tCONSTRAINT ck_request_logs_nonnegative_counters CHECK (settlement_attempts>=0 AND (duration_ms IS NULL OR duration_ms>=0) AND (first_event_ms IS NULL OR first_event_ms>=0)), \n\tCONSTRAINT ck_request_logs_nonnegative_tokens CHECK ((prompt_tokens IS NULL OR prompt_tokens>=0) AND (completion_tokens IS NULL OR completion_tokens>=0) AND (total_tokens IS NULL OR total_tokens>=0)), \n\tCONSTRAINT ck_request_logs_reported_usage CHECK (usage_status<>'reported' OR (prompt_tokens IS NOT NULL AND completion_tokens IS NOT NULL AND total_tokens IS NOT NULL AND total_tokens=prompt_tokens+completion_tokens AND usage_source<>'none')), \n\tCONSTRAINT ck_request_logs_missing_usage CHECK (usage_status<>'missing' OR (prompt_tokens IS NULL AND completion_tokens IS NULL AND total_tokens IS NULL)), \n\tCONSTRAINT ck_request_logs_rejected_values CHECK (outcome<>'rejected' OR (usage_status='not_applicable' AND usage_source='none' AND started_at=finished_at AND started_at=updated_at AND upstream_started_at IS NULL AND duration_ms IS NULL AND first_event_ms IS NULL AND prompt_tokens IS NULL AND completion_tokens IS NULL AND total_tokens IS NULL)), \n\tCONSTRAINT ck_rejected_http_contract CHECK (outcome<>'rejected' OR (upstream_status_code IS NULL AND ((rejection_scope IN ('global','project','key') AND response_status_code=429 AND error_code='concurrency_limit_exceeded') OR (rejection_scope='upstream_unhealthy' AND response_status_code=503 AND error_code='upstream_unhealthy')) IS TRUE)), \n\tCONSTRAINT fk_request_logs_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id)\n)\n\n"
    )
    op.execute(
        "CREATE INDEX ix_request_logs_key_time ON request_logs (api_key_id, started_at, request_id)"
    )
    op.execute("CREATE INDEX ix_request_logs_model_time ON request_logs (model, started_at)")
    op.execute(
        "CREATE INDEX ix_request_logs_project_time ON request_logs (project_id, started_at, request_id)"
    )
    op.execute(
        "CREATE INDEX ix_request_logs_started_run ON request_logs (process_run_id, started_at) WHERE outcome = 'started'"
    )
    op.execute("CREATE INDEX ix_request_logs_time ON request_logs (started_at, request_id)")
    # Default privileges granted to the runtime role must not include version writes.
    op.execute(
        "REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON alembic_version FROM gateway_test_app"
    )


def downgrade() -> None:
    # Available for separately approved rebuilds; never invoked by slice verification.
    op.drop_table("request_logs")
    op.drop_table("api_keys")
    op.drop_table("projects")
