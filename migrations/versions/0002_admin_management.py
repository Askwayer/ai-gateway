"""Management audit and health history structure; frozen incremental DDL."""

from alembic import op

revision = "0002_admin_management"
down_revision = "0001_nonstream_slice"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "\nCREATE TABLE health_checks (\n\tid UUID NOT NULL, \n\tupstream_id VARCHAR(64) NOT NULL, \n\tprobe_kind VARCHAR(32) NOT NULL, \n\tstatus VARCHAR(32) NOT NULL, \n\tchecked_at TIMESTAMP WITH TIME ZONE DEFAULT statement_timestamp() NOT NULL, \n\tlatency_ms FLOAT NOT NULL, \n\thttp_status INTEGER, \n\terror_code VARCHAR(64), \n\tmodel VARCHAR(255), \n\tCONSTRAINT pk_health_checks PRIMARY KEY (id), \n\tCONSTRAINT ck_health_checks_probe_kind CHECK (probe_kind IN ('reachability','inference')), \n\tCONSTRAINT ck_health_checks_status CHECK (status IN ('healthy','degraded','unhealthy','skipped')), \n\tCONSTRAINT ck_health_checks_latency CHECK (latency_ms >= 0 AND latency_ms < 'Infinity'::float8), \n\tCONSTRAINT ck_health_checks_http_status CHECK (http_status IS NULL OR http_status BETWEEN 100 AND 599)\n)\n\n"
    )
    op.execute(
        "CREATE INDEX ix_health_checks_upstream_time ON health_checks (upstream_id, checked_at, id)"
    )
    op.execute(
        "\nCREATE TABLE admin_audit_logs (\n\tid UUID NOT NULL, \n\tactor VARCHAR(32) NOT NULL, \n\taction VARCHAR(64) NOT NULL, \n\tresource_type VARCHAR(32) NOT NULL, \n\tresource_id UUID NOT NULL, \n\trequest_id UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT statement_timestamp() NOT NULL, \n\tchanges JSONB NOT NULL, \n\tCONSTRAINT pk_admin_audit_logs PRIMARY KEY (id), \n\tCONSTRAINT ck_admin_audit_logs_actor CHECK (actor = 'admin'), \n\tCONSTRAINT ck_admin_audit_logs_action CHECK ((resource_type='project' AND action IN ('project.create','project.update')) OR (resource_type='key' AND action IN ('key.create','key.update'))), \n\tCONSTRAINT ck_admin_audit_logs_changes_object CHECK (jsonb_typeof(changes) = 'object')\n)\n\n"
    )
    op.execute(
        "CREATE INDEX ix_admin_audit_action_created ON admin_audit_logs (action, created_at DESC, id DESC)"
    )
    op.execute("CREATE INDEX ix_admin_audit_created ON admin_audit_logs (created_at DESC, id DESC)")
    op.execute(
        "CREATE INDEX ix_admin_audit_resource_created ON admin_audit_logs (resource_id, created_at DESC, id DESC)"
    )
    op.execute("CREATE INDEX ix_projects_created ON projects (created_at DESC, id DESC)")


def downgrade() -> None:
    op.drop_index("ix_projects_created", table_name="projects")
    op.drop_table("admin_audit_logs")
    op.drop_table("health_checks")
