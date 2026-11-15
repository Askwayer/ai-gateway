#!/usr/bin/env python3
"""One bounded authenticated GET /v1/models to the selected company endpoint.

No inference, redirects, retries, environment proxies, or credential output.
Reachability is separate from final protocol acceptance.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

import httpx


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError('Evidence exists')
    settings = {}
    for line in Path('.env.company').read_text().splitlines():
        key, sep, value = line.partition('=')
        if sep and not key.lstrip().startswith('#'):
            settings[key.strip()] = value.strip().strip('\"\'')
    base = settings['UPSTREAM_BASE_URL']
    if base != 'http://172.31.34.204:5330/v1':
        raise ValueError('Unexpected origin')
    key = settings['UPSTREAM_API_KEY']
    if not key or any(c.isspace() for c in key):
        raise ValueError('Invalid credential')
    report = {'recorded_at_utc': datetime.now(timezone.utc).isoformat(),
              'url': base+'/models', 'proxy_mode': 'disabled', 'inference_requests': 0,
              'milestone_signoff': False, 'credential_source': '.env.company:UPSTREAM_API_KEY',
              'status': 'pending_verification'}
    start = time.monotonic()
    try:
        async with asyncio.timeout(7):
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5) as client:
                async with client.stream('GET', base+'/models', headers={'Authorization': 'Bearer '+key, 'Accept-Encoding':'identity'}) as response:
                    report['http_status'] = response.status_code
                    report['content_type'] = response.headers.get('content-type', '')
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body)+len(chunk)>262144:
                            raise ValueError('Body too large')
                        body.extend(chunk)
                    if response.status_code == 200:
                        payload = json.loads(body)
                        report['model_ids'] = [m['id'] for m in payload.get('data', []) if isinstance(m,dict) and isinstance(m.get('id'),str)]
                        report['status'] = 'models_query_succeeded' if report['model_ids'] else 'unexpected_response'
                    else:
                        report['status'] = 'http_failure'
    except Exception as exc:
        report.update(status='connection_or_protocol_check_failed', error_type=type(exc).__name__)
    report['elapsed_ms'] = round((time.monotonic()-start)*1000, 3)
    encoded = json.dumps(report, ensure_ascii=False, indent=2).replace(key, '[REDACTED]')
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd,'w') as out:
        out.write(encoded+'\n')
    print(encoded)
    return int(report['status'] != 'models_query_succeeded')


if __name__ == '__main__':
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:
        print(json.dumps({'status':'failed', 'error_type':type(exc).__name__}))
        raise SystemExit(1)
