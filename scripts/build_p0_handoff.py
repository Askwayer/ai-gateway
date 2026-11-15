#!/usr/bin/env python3
"""Build/check the current P0 snapshot, including source drift and archive hashes.

Only explicit reviewed files are distributed. Real environment files are never
packaged or indexed. Old artifacts stay historical; M0 is never signed here.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT/'docs/P0-本地收尾-证据索引-20261115.json'
ARCHIVE = ROOT/'.tools/p0-local-handoff-20261115-r2.tar.gz'
PREFIX = 'p0-local-handoff-20261115-r2'
FILES = '''scripts/p0_preflight.py
scripts/p0_verify_dependencies.py
scripts/p0_direct_echo.py
scripts/p0_check_company_upstream.py
scripts/p0_config_candidates.py
scripts/check_docs.py
scripts/bootstrap_ubuntu_venv.sh
scripts/build_p0_handoff.py
p0/LOCAL_HANDOFF.md
p0/company.env.example
p0/config-candidates.json
p0/upstream/company-selection.json
p0/upstream/deepseek-probe-cases.json
p0/dependencies/README.md
p0/dependencies/pyproject.toml
p0/dependencies/uv.lock
p0/dependencies/requirements.txt
docs/P0-配置候选清单.md
docs/P0-直连原型与现场补验.md
docs/P0-执行记录.md
docs/P0-延期与P1例外记录.md
docs/P1-本地基座执行记录.md
docs/P1-首条非流式纵向切片-执行记录.md
docs/P1-管理闭环-执行记录.md
docs/AI-Gateway-项目开发详细设计书-v1.6.md
docs/AI-Gateway-验收记录模板-v1.6.md
docs/AI-Gateway-开发执行计划-v1.1.md
docs/数据库环境与账号.md
docs/P0-数据库初始化记录-20261115.json
.env.example
.tools/p0-direct-echo-20261115.json
.tools/p0-company-reachability-20261115.json
.tools/p0-local-inputs-20261115.json
.tools/p0-dependency-smoke-local-final-20261115.json
.tools/p0-test-database-configured-20261115.json
.tools/p1-slice-e2e-20261115-run01.json
.tools/p1-slice-e2e-20261115-run02.json
.tools/p1-slice-e2e-20261115-run03.json
.tools/p1-admin-e2e-20261115-run01.json
.tools/p1-admin-e2e-20261115-run02.json
.tools/p1-admin-e2e-20261115-run06.json
docs/evidence/p1-admin-20261115/admin-regressions.xml
docs/evidence/p1-admin-20261115/quality.log
docs/evidence/p1-nonstream-20261115/transport-regressions.xml
docs/evidence/p1-nonstream-20261115/quality.log
.tools/p0-deepseek-20261115-run01/report.json'''.splitlines()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2)+'\n').encode()


def source_map():
    mapping = {name: name for name in FILES}
    lock = tomllib.loads((ROOT/'p0/dependencies/uv.lock').read_text())
    allowed = {w['url'].rsplit('/',1)[-1]:w['hash'].split(':',1)[1]
               for package in lock['package'] for w in package.get('wheels',[])}
    wheels = sorted((ROOT/'.tools/p0-wheelhouse-linux-x86_64').glob('*.whl'))
    if len(wheels) != 26:
        raise ValueError('Expected 26 candidate wheels')
    for wheel in wheels:
        if digest(wheel.read_bytes()) != allowed.get(wheel.name):
            raise ValueError('Wheel does not match lock')
        mapping[str(wheel.relative_to(ROOT))] = 'wheelhouse/'+wheel.name
    return mapping


def secret_tokens():
    result = set()
    for name in ('.env', '.env.company', '.env.db-admin'):
        path = ROOT/name
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            key, sep, value = line.partition('=')
            value = value.strip().strip('\"\'')
            if sep and not key.lstrip().startswith('#') and value and any(s in key for s in ('KEY','PASSWORD','DATABASE_URL')):
                result.add(value.encode())
                if 'DATABASE_URL' in key:
                    password = urlsplit(value).password
                    if password:
                        result.add(password.encode())
    return result


def write_atomic(path, data):
    fd, temporary = tempfile.mkstemp(prefix='p0-build-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def preserve_history():
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    history = ROOT/'.tools/p0-handoff-history'/stamp
    history.mkdir(parents=True, mode=0o700)
    for path in (INDEX, ARCHIVE, ARCHIVE.with_suffix('.gz.sha256')):
        if path.exists():
            shutil.copyfile(path, history/path.name)
    return str(history.relative_to(ROOT))


def build():
    for script in ('check_docs.py', 'p0_config_candidates.py'):
        args = [sys.executable, str(ROOT/'scripts'/script)]
        if script.startswith('p0_'):
            args.append('--check')
        subprocess.run(args, cwd=ROOT, check=True)
    mapping = source_map()
    blobs, hashes = {}, {}
    secrets = secret_tokens()
    for source, destination in mapping.items():
        path = ROOT/source
        if path.is_symlink() or not path.is_file():
            raise ValueError('Invalid source: '+source)
        data = path.read_bytes()
        if any(value in data for value in secrets):
            raise ValueError('Secret detected in reviewed file: '+source)
        blobs[destination] = data
        hashes[source] = digest(data)
    snapshot = {'schema_version':2, 'recorded_at_utc':datetime.now(timezone.utc).isoformat(),
                'classification':'project-internal distributable; no credentials; may contain internal addresses',
                'milestone_signoff':False, 'artifacts':hashes, 'archive_paths':mapping}
    blobs['SOURCE_INDEX.json'] = encoded(snapshot)
    sums = ''.join(digest(value)+'  '+name+'\n' for name,value in sorted(blobs.items()))
    blobs['SHA256SUMS'] = sums.encode()
    history = preserve_history()
    fd, temporary = tempfile.mkstemp(prefix='handoff-', suffix='.tar.gz', dir=ARCHIVE.parent)
    os.close(fd)
    try:
        with tarfile.open(temporary,'w:gz') as archive:
            for name, data in sorted(blobs.items()):
                info = tarfile.TarInfo(PREFIX+'/'+name)
                info.size, info.mode = len(data), 0o644
                archive.addfile(info, io.BytesIO(data))
        # Refuse publishing an internally consistent snapshot if sources changed mid-build.
        if any(digest((ROOT/name).read_bytes()) != sha for name,sha in hashes.items()):
            raise ValueError('Sources changed during build')
        sha = digest(Path(temporary).read_bytes())
        index = {**snapshot, 'package':{'path':str(ARCHIVE.relative_to(ROOT)), 'sha256':sha,
                                      'prefix':PREFIX, 'bytes':Path(temporary).stat().st_size},
                 'historical_previous_artifacts':history,
                 'historical_v1_package':'.tools/p0-local-handoff-20261115.tar.gz'}
        os.chmod(temporary,0o644)
        os.replace(temporary,ARCHIVE)
        write_atomic(ARCHIVE.with_suffix('.gz.sha256'), (sha+'  '+ARCHIVE.name+'\n').encode())
        write_atomic(INDEX,encoded(index))
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()
    check()


def check():
    index = json.loads(INDEX.read_text())
    mapping = source_map()
    if index['archive_paths'] != mapping or set(index['artifacts']) != set(mapping):
        raise ValueError('Index source coverage differs')
    for path, sha in index['artifacts'].items():
        if digest((ROOT/path).read_bytes()) != sha:
            raise ValueError('Current source drift: '+path)
    if index['package']['path'] != str(ARCHIVE.relative_to(ROOT)):
        raise ValueError('Unexpected package path')
    sha = digest(ARCHIVE.read_bytes())
    if sha != index['package']['sha256'] or ARCHIVE.stat().st_size != index['package']['bytes']:
        raise ValueError('Package hash/size mismatch')
    if ARCHIVE.with_suffix('.gz.sha256').read_text() != sha+'  '+ARCHIVE.name+'\n':
        raise ValueError('Sidecar mismatch')
    with tarfile.open(ARCHIVE) as archive:
        members = archive.getmembers()
        expected = {PREFIX+'/'+name for name in mapping.values()} | {PREFIX+'/SOURCE_INDEX.json',PREFIX+'/SHA256SUMS'}
        if len(members)!=len(expected) or {m.name for m in members}!=expected or not all(m.isfile() for m in members):
            raise ValueError('Unexpected archive members')
        snapshot = json.load(archive.extractfile(PREFIX+'/SOURCE_INDEX.json'))
        if snapshot != {k:index[k] for k in snapshot} or snapshot['artifacts']!=index['artifacts']:
            raise ValueError('Embedded source index mismatch')
        for source,destination in mapping.items():
            if digest(archive.extractfile(PREFIX+'/'+destination).read()) != index['artifacts'][source]:
                raise ValueError('Archive content differs: '+source)
        sums = archive.extractfile(PREFIX+'/SHA256SUMS').read().decode().splitlines()
        pairs = [row.split('  ',1) for row in sums]
        if len(pairs)!=len(expected)-1 or {p[1] for p in pairs} != set(mapping.values()) | {'SOURCE_INDEX.json'}:
            raise ValueError('Incomplete SHA256SUMS')
        for expected_sha,path in pairs:
            if digest(archive.extractfile(PREFIX+'/'+path).read()) != expected_sha:
                raise ValueError('SHA256SUMS mismatch')
    print(f'PASS: {len(mapping)} current sources, archive contents, embedded index, SHA256SUMS and package hash agree; M0 remains unsigned.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--build',action='store_true')
    mode.add_argument('--check',action='store_true')
    args = parser.parse_args()
    try:
        build() if args.build else check()
    except Exception as exc:
        print('FAIL:',type(exc).__name__,str(exc))
        raise SystemExit(1)
