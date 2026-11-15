#!/usr/bin/env python3
"""P0 evidence collection. Read-only checks; never a milestone sign-off.

Host mode uses only the standard library and can run before dependencies exist.
Database mode needs asyncpg and reads DATABASE_URL from the environment or .env.
No credentials, full DSNs, environment dumps or exception messages are emitted.
"""

import argparse
import asyncio
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit


def command(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=5)
        return {"exit_code": result.returncode, "stdout": result.stdout.strip()}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error_type": type(exc).__name__}


def host_report(require_target):
    linux = platform.system() == "Linux"
    release = platform.freedesktop_os_release() if linux else {}
    target = (
        release.get("ID") == "ubuntu"
        and release.get("VERSION_ID") == "24.04"
        and platform.machine() == "x86_64"
        and (3, 12, 3) <= sys.version_info[:3] < (3, 13, 0)
    )
    memory = None
    if linux:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                memory = int(line.split()[1]) * 1024
                break
    elif platform.system() == "Darwin":
        value = command(["sysctl", "-n", "hw.memsize"]).get("stdout", "")
        memory = int(value) if value.isdigit() else None
    packages = {}
    for name in ("asyncpg", "fastapi", "sqlalchemy", "alembic", "httpx",
                 "uvicorn", "anyio", "pydantic", "pydantic-settings", "tzdata"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    report = {
        "status": "target_mismatch" if require_target and not target else "collected",
        "system": platform.system(), "architecture": platform.machine(),
        "libc": dict(zip(("name", "version"), platform.libc_ver())),
        "os_release": {k: release.get(k) for k in ("ID", "VERSION_ID", "PRETTY_NAME")},
        "python": platform.python_version(), "python_executable": sys.executable,
        "base_executable": getattr(sys, "_base_executable", None),
        "isolated_environment": sys.prefix != sys.base_prefix,
        "target_os_arch_python_match": target,
        "logical_cpus": os.cpu_count(), "physical_memory_bytes": memory,
        "workspace_disk_free_bytes": shutil.disk_usage(Path.cwd()).free,
        "installed_packages": packages,
        "scope": "machine running this command; not proof of deployment on another host",
    }
    if linux:
        report["time_sync"] = command([
            "timedatectl", "show", "--property=NTPSynchronized",
            "--property=NTP", "--property=Timezone",
        ])
        report["ubuntu_python_packages"] = command([
            "dpkg-query", "-W", "-f=${Package} ${Version}\n",
            "python3", "python3.12", "python3.12-venv",
        ])
    else:
        report["time_sync"] = {"status": "not_checked_on_non_target_os"}
    report["remaining_evidence"] = [
        "NTP source and offset on gateway and database hosts",
        "deployment service/stop budget, topology and resource allocation",
    ]
    return report


def database_url(env_file):
    value = os.environ.get("DATABASE_URL")
    if value:
        return value
    # Literal KEY=VALUE only: do not execute/source a file or expand shell syntax.
    for raw in env_file.read_text().splitlines():
        key, sep, value = raw.strip().partition("=")
        if sep and key.strip() == "DATABASE_URL":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if value:
                return value
    raise ValueError("DATABASE_URL missing")


async def database_report(env_file):
    import asyncpg

    raw_url = database_url(env_file)
    parsed = urlsplit(raw_url)
    if parsed.scheme not in ("postgresql+asyncpg", "postgresql", "postgres"):
        raise ValueError("Unsupported database scheme")
    if not parsed.hostname or not parsed.path.strip("/"):
        raise ValueError("Database host/name required")
    dsn = raw_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = None
    try:
        start = time.monotonic()
        conn = await asyncpg.connect(
            dsn=dsn, timeout=10, command_timeout=10,
            server_settings={"application_name": "ai-gateway-p0-readonly",
                             "default_transaction_read_only": "on"},
        )
        connect_ms = (time.monotonic() - start) * 1000
        async with conn.transaction(readonly=True):
            row = await conn.fetchrow("""
                SELECT current_database() AS database, current_user AS username,
                  current_setting('server_version') AS server_version,
                  current_setting('TimeZone') AS server_timezone,
                  current_setting('transaction_read_only') AS transaction_read_only,
                  current_setting('max_connections')::int AS max_connections,
                  current_setting('superuser_reserved_connections')::int AS superuser_reserved_connections,
                  r.rolsuper AS is_superuser, r.rolcreatedb AS can_create_database,
                  r.rolconnlimit AS role_connection_limit,
                  (SELECT datconnlimit FROM pg_database WHERE datname=current_database()) AS database_connection_limit,
                  has_database_privilege(current_database(), 'CONNECT') AS can_connect,
                  has_database_privilege(current_database(), 'CREATE') AS can_create_schema,
                  has_schema_privilege('public', 'CREATE') AS public_create,
                  (SELECT count(*) FROM pg_tables WHERE schemaname='public') AS existing_public_tables,
                  (SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')) AS existing_user_tables,
                  (SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend') AS client_connections_snapshot,
                  (SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()) AS ssl_in_use
                FROM pg_roles r WHERE r.rolname=current_user
            """)
            before = datetime.now(timezone.utc)
            mono = time.monotonic()
            server_time = await conn.fetchval("SELECT clock_timestamp()")
            rtt_ms = (time.monotonic() - mono) * 1000
            after = datetime.now(timezone.utc)
        return {
            "status": "read_only_checks_passed", "host": parsed.hostname,
            "port": parsed.port or 5432, "asyncpg_version": asyncpg.__version__,
            "connect_ms": round(connect_ms, 3), **dict(row),
            "clock_sample": {
                "database_utc": server_time.isoformat(),
                "client_before_utc": before.isoformat(), "client_after_utc": after.isoformat(),
                "query_round_trip_ms": round(rtt_ms, 3),
                "db_minus_client_offset_lower_ms": round((server_time-after).total_seconds()*1000, 3),
                "db_minus_client_offset_upper_ms": round((server_time-before).total_seconds()*1000, 3),
                "interpretation": "Single client/DB sample, assuming no wall-clock jump; not target-host NTP evidence",
            },
            "schema_or_data_changes": False,
            "capacity_note": "Snapshot includes this connection; neither a load test nor an approved connection allocation",
            "isolation_note": "Empty tables do not prove a dedicated test database; owner approval and least-privilege roles remain required",
        }
    finally:
        if conn is not None:
            await conn.close(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("host", "database"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--require-target", action="store_true")
    parser.add_argument("--output", type=Path, help="New JSON evidence file; refuses overwrite")
    args = parser.parse_args()
    if args.require_target and args.mode != "host":
        parser.error("--require-target applies to host mode only")
    report = {"schema_version": 1, "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
              "mode": args.mode, "milestone_signoff": False}
    try:
        data = host_report(args.require_target) if args.mode == "host" else asyncio.run(database_report(args.env_file))
        report.update(data)
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__,
                      sqlstate=getattr(exc, "sqlstate", None))
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        try:
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(encoded)
        except OSError as exc:
            print(json.dumps({"status": "evidence_write_failed", "error_type": type(exc).__name__}))
            return 1
    print(encoded, end="")
    return int(report["status"] not in ("collected", "read_only_checks_passed"))


if __name__ == "__main__":
    raise SystemExit(main())
