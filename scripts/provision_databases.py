#!/usr/bin/env python3
"""One-time provisioning of two NEW gateway databases. Never adopts existing names.

--check only inspects. --apply creates isolated roles/databases, verifies privileges,
then switches local development to the test database. Secrets never reach stdout.
"""
import argparse
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg

LAYOUT = [
    ('ai_gateway', 'gateway_app', 'gateway_migrator'),
    ('ai_gateway_test', 'gateway_test_app', 'gateway_test_migrator'),
]
ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / '.tools/database-provisioning-20261115'


def read_env(path):
    result = {}
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition('=')
        if sep and not key.startswith('#'):
            result[key.strip()] = value.strip().strip('\"\'')
    return result


def private_write(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as file:
        file.write(value)


def qi(value):
    return '"' + value.replace('"', '""') + '"'


def scram(password):
    # Send a verifier to CREATE ROLE, not the cleartext password in SQL logs.
    salt = secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 4096)
    client = hmac.digest(salted, b'Client Key', 'sha256')
    stored = hashlib.sha256(client).digest()
    server = hmac.digest(salted, b'Server Key', 'sha256')
    enc = lambda value: base64.b64encode(value).decode()
    return f'SCRAM-SHA-256$4096:{enc(salt)}${enc(stored)}:{enc(server)}'


def role_url(admin_url, database, role, password):
    parts = urlsplit(admin_url)
    host = parts.hostname
    if ':' in host:
        host = '[' + host + ']'
    netloc = f'{quote(role, safe="")}:{quote(password, safe="")}@{host}:{parts.port or 5432}'
    return urlunsplit(('postgresql+asyncpg', netloc, '/' + database, parts.query, ''))


async def connect(url):
    return await asyncpg.connect(url.replace('postgresql+asyncpg://', 'postgresql://', 1),
                                 timeout=10, command_timeout=10,
                                 server_settings={'application_name': 'ai-gateway-db-provision'})


async def expect_denied(conn, sql):
    try:
        async with conn.transaction():
            await conn.execute(sql)
            # If unexpectedly allowed, rollback the attempted DDL immediately.
            raise RuntimeError('Unexpected permission')
    except asyncpg.InsufficientPrivilegeError:
        return True


async def verify(database, app, migrator, urls):
    owner = await connect(urls[migrator])
    runtime = None
    table = '_p0_acl_' + secrets.token_hex(6)
    try:
        await owner.execute(f'CREATE TABLE public.{qi(table)} (id bigserial PRIMARY KEY, value text NOT NULL)')
        runtime = await connect(urls[app])
        flags = await runtime.fetchrow('''SELECT rolsuper, rolcreatedb, rolcreaterole,
                rolreplication, rolbypassrls FROM pg_roles WHERE rolname=current_user''')
        assert not any(flags.values())
        async with runtime.transaction():
            value = await runtime.fetchval(f'INSERT INTO public.{qi(table)} (value) VALUES ($1) RETURNING id', 'probe')
            assert await runtime.fetchval(f'SELECT value FROM public.{qi(table)} WHERE id=$1', value) == 'probe'
            assert await runtime.execute(f'UPDATE public.{qi(table)} SET value=$1 WHERE id=$2', 'checked', value) == 'UPDATE 1'
            assert await runtime.execute(f'DELETE FROM public.{qi(table)} WHERE id=$1', value) == 'DELETE 1'
        await expect_denied(runtime, f'CREATE TABLE public.{qi(table+"_forbidden")} (id int)')
        await expect_denied(runtime, f'DROP TABLE public.{qi(table)}')
        await expect_denied(runtime, f'TRUNCATE public.{qi(table)}')
        await expect_denied(runtime, f'CREATE TEMP TABLE {qi(table+"_temp")} (id int)')
        return {'database': database, 'runtime_role': app, 'migration_role': migrator,
                'login_and_crud': 'passed', 'sequence_defaults': 'passed',
                'runtime_create_drop_truncate_temp_denied': 'passed'}
    finally:
        if runtime:
            await runtime.close(timeout=5)
        try:
            await owner.execute(f'DROP TABLE IF EXISTS public.{qi(table)}')
        finally:
            await owner.close(timeout=5)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    admin_file = ROOT/'.env.db-admin'
    admin_url = read_env(admin_file if admin_file.exists() else ROOT/'.env')['DATABASE_URL']
    admin = await connect(admin_url)
    report = {'recorded_at_utc': datetime.now(timezone.utc).isoformat(), 'status': 'checking',
              'databases': [], 'actions': [], 'credentials_printed': False}
    try:
        await admin.execute('SELECT pg_advisory_lock(61115013432)')
        names = [db for db, _, _ in LAYOUT]
        roles = [r for _, app, migrator in LAYOUT for r in (app, migrator)]
        existing_dbs = await admin.fetch('SELECT datname FROM pg_database WHERE datname=ANY($1::text[])', names)
        existing_roles = await admin.fetch('SELECT rolname FROM pg_roles WHERE rolname=ANY($1::text[])', roles)
        report['existing_databases'] = [r['datname'] for r in existing_dbs]
        report['existing_roles'] = [r['rolname'] for r in existing_roles]
        report['server_version'] = await admin.fetchval('SHOW server_version')
        report['max_connections'] = int(await admin.fetchval('SHOW max_connections'))
        report['client_connections_snapshot'] = await admin.fetchval("SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend'")
        if existing_dbs or existing_roles:
            raise RuntimeError('Name conflict: no existing resources modified')
        if not args.apply:
            report['status'] = 'names_available_read_only_check'
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return
        if report['max_connections'] < 90:
            raise RuntimeError('Review connection allocation first')
        WORK.mkdir(mode=0o700, exist_ok=False)
        original = (ROOT/'.env').read_text()
        private_write(WORK/'original.env', original)
        if not admin_file.exists():
            private_write(admin_file, '# Administrative connection only; never load in the gateway.\nDATABASE_URL='+admin_url+'\n')
        passwords = {role: secrets.token_urlsafe(32) for role in roles}
        urls = {role: role_url(admin_url, db, role, passwords[role]) for db, app, migrator in LAYOUT for role in (app, migrator)}
        # Persist recovery material BEFORE remote mutations. Never overwritten on retry.
        private_write(WORK/'credentials.json', json.dumps({'urls': urls}, indent=2)+'\n')
        for db, app, migrator in LAYOUT:
            async with admin.transaction():
                for role, limit in ((app, 36), (migrator, 2)):
                    await admin.execute(f"CREATE ROLE {qi(role)} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT {limit} PASSWORD '{scram(passwords[role])}'")
                report['actions'].append('created roles for '+db)
            await admin.execute(f"CREATE DATABASE {qi(db)} OWNER {qi(migrator)} TEMPLATE template0 ENCODING 'UTF8' CONNECTION LIMIT 40")
            report['actions'].append('created database '+db)
            async with admin.transaction():
                await admin.execute(f'REVOKE ALL ON DATABASE {qi(db)} FROM PUBLIC')
                await admin.execute(f'GRANT CONNECT ON DATABASE {qi(db)} TO {qi(app)}')
                await admin.execute(f"ALTER DATABASE {qi(db)} SET timezone TO 'UTC'")
                for role in (app, migrator):
                    await admin.execute(f'ALTER ROLE {qi(role)} IN DATABASE {qi(db)} SET search_path TO public')
            owner = await connect(urls[migrator])
            try:
                async with owner.transaction():
                    await owner.execute('REVOKE ALL ON SCHEMA public FROM PUBLIC')
                    await owner.execute(f'GRANT USAGE ON SCHEMA public TO {qi(app)}')
                    await owner.execute(f'ALTER DEFAULT PRIVILEGES FOR ROLE {qi(migrator)} IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {qi(app)}')
                    await owner.execute(f'ALTER DEFAULT PRIVILEGES FOR ROLE {qi(migrator)} IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {qi(app)}')
                    await owner.execute(f'ALTER DEFAULT PRIVILEGES FOR ROLE {qi(migrator)} REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC')
            finally:
                await owner.close(timeout=5)
            report['databases'].append(await verify(db, app, migrator, urls))
        # Assert real login isolation, not just catalog ACLs.
        for i, (_, app, migrator) in enumerate(LAYOUT):
            other_db = LAYOUT[1-i][0]
            for role in (app, migrator):
                try:
                    unexpected = await connect(role_url(admin_url, other_db, role, passwords[role]))
                except asyncpg.InsufficientPrivilegeError:
                    pass
                else:
                    await unexpected.close(timeout=5)
                    raise RuntimeError('Cross-environment connection unexpectedly permitted')
        report['cross_environment_logins_denied'] = 4
        # Only update development config after every verification passes.
        config = {
            'DATABASE_URL': urls['gateway_test_app'],
            'MIGRATION_DATABASE_URL': urls['gateway_test_migrator'],
            'TEST_DATABASE_URL': urls['gateway_test_app'],
            'TEST_MIGRATION_DATABASE_URL': urls['gateway_test_migrator'],
            'PRODUCTION_DATABASE_URL': urls['gateway_app'],
            'PRODUCTION_MIGRATION_DATABASE_URL': urls['gateway_migrator'],
        }
        kept = [line for line in original.splitlines() if line.partition('=')[0].strip() not in config]
        content = '\n'.join(kept).rstrip()+'\n\n# Local development defaults to the isolated TEST database.\n'
        content += '\n'.join(k+'='+v for k,v in config.items())+'\n'
        if (ROOT/'.env').read_text() != original:
            raise RuntimeError('Local env changed during provisioning; do not overwrite')
        temp = ROOT/'.env.db-provision-new'
        private_write(temp, content)
        os.replace(temp, ROOT/'.env')
        report.update(status='created_verified_configured', active_database='ai_gateway_test',
                      business_tables_created=False, probe_tables_removed=True,
                      role_connection_limits={'runtime_each':36, 'migration_each':2}, database_connection_limit_each=40)
    except Exception as exc:
        report.update(status='failed', error_type=type(exc).__name__, sqlstate=getattr(exc,'sqlstate',None))
        raise
    finally:
        await admin.close(timeout=5)
        if args.apply and WORK.exists():
            private_write(WORK/('report-'+secrets.token_hex(4)+'.json'), json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        if args.apply:
            print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as exc:
        print(json.dumps({'status':'failed', 'error_type':type(exc).__name__, 'sqlstate':getattr(exc,'sqlstate',None)}))
        raise SystemExit(1)
