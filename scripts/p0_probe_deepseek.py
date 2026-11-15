#!/usr/bin/env python3
"""Bounded P0 cloud compatibility probe, not a gateway or offline acceptance.

Exactly the three versioned cases, serially, with no retries or redirects.
Credentials stay in the local environment/.env. Evidence uses a fresh directory.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time

import httpx


def load_settings():
    values = {}
    for line in Path('.env').read_text().splitlines():
        key, sep, value = line.strip().partition('=')
        if sep and not key.startswith('#'):
            values[key.strip()] = value.strip().strip('\"\'')
    for key in ('AI_FAST_API_KEY', 'UPSTREAM_API_KEY', 'AI_FAST_MODEL', 'AI_FAST_BASE_URL'):
        if os.environ.get(key):
            values[key] = os.environ[key]
    key_name = 'UPSTREAM_API_KEY' if values.get('UPSTREAM_API_KEY') else 'AI_FAST_API_KEY'
    secret = values.get(key_name, '')
    if not secret or any(c.isspace() for c in secret):
        raise ValueError('Missing or malformed credential')
    return values, key_name, secret


def valid_usage(usage):
    if not isinstance(usage, dict):
        return False
    fields = ('prompt_tokens', 'completion_tokens', 'total_tokens')
    return (all(type(usage.get(k)) is int and usage[k] >= 0 for k in fields)
            and usage['prompt_tokens'] + usage['completion_tokens'] == usage['total_tokens'])


async def run_case(client, case, directory, redact):
    result = {'name': case['name'], 'passed': False}
    body = bytearray()
    start = time.monotonic()
    try:
        async with asyncio.timeout(45):
            async with client.stream(case['method'], case['path'], json=case.get('json')) as response:
                result.update(http_status=response.status_code,
                              content_type=response.headers.get('content-type', ''),
                              content_encoding=response.headers.get('content-encoding', 'identity'),
                              headers_ms=round((time.monotonic()-start)*1000, 3))
                async for chunk in response.aiter_raw():
                    if len(body) + len(chunk) > 1024 * 1024:
                        raise ValueError('Body limit exceeded')
                    body.extend(chunk)
                    if case['name'] == 'sse_usage' and 'first_event_ms' not in result:
                        if b'\n\n' in body or b'\r\n\r\n' in body:
                            result['first_event_ms'] = round((time.monotonic()-start)*1000, 3)
        if result['http_status'] != 200:
            result['failure'] = 'http_status'
            return result
        if result['content_encoding'].lower() not in ('identity', ''):
            result['failure'] = 'unexpected_content_encoding'
            return result
        if case['name'] == 'sse_usage':
            if result['content_type'].split(';')[0].strip().lower() != 'text/event-stream':
                raise ValueError('Unexpected SSE content type')
            events, done = [], False
            for block in body.decode('utf-8').replace('\r\n', '\n').split('\n\n'):
                data = '\n'.join(line[5:].lstrip(' ') for line in block.split('\n') if line.startswith('data:'))
                if not data:
                    continue
                if data == '[DONE]':
                    done = True
                    continue
                if done:
                    raise ValueError('Data after DONE')
                events.append(json.loads(data))
            usages = [{'event_index': i, 'choices_count': len(e.get('choices', [])), 'usage': e['usage']}
                      for i, e in enumerate(events) if e.get('usage') is not None]
            result.update(event_count=len(events), done=done, usage_events=usages,
                          response_models=sorted({e['model'] for e in events if isinstance(e.get('model'), str)}),
                          finish_reasons=[c['finish_reason'] for e in events for c in e.get('choices', []) if c.get('finish_reason')])
            result['passed'] = bool(done and usages and all(valid_usage(e['usage']) for e in usages))
        else:
            if result['content_type'].split(';')[0].strip().lower() != 'application/json':
                raise ValueError('Unexpected JSON content type')
            data = json.loads(body)
            if case['name'] == 'models_candidate_reachability':
                ids = [m['id'] for m in data.get('data', []) if isinstance(m, dict) and isinstance(m.get('id'), str)]
                result.update(model_ids=ids, passed=bool(ids))
            else:
                usage = data.get('usage')
                result.update(response_model=data.get('model'), usage=usage,
                              finish_reasons=[c.get('finish_reason') for c in data.get('choices', [])],
                              passed=bool(data.get('choices')) and valid_usage(usage))
    except Exception as exc:
        result.update(failure='exception', error_type=type(exc).__name__)
    finally:
        result['elapsed_ms'] = round((time.monotonic()-start)*1000, 3)
        # Synthetic prompt only. Persist redacted bytes, never auth/request headers.
        safe = redact(bytes(body).decode('utf-8', errors='replace')).encode('utf-8')
        filename = case['name'] + '.body.txt'
        with (directory/filename).open('xb') as stream:
            stream.write(safe)
        result.update(captured_bytes=len(body), evidence_file=filename,
                      redacted_evidence_sha256=hashlib.sha256(safe).hexdigest())
    return result


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    settings, key_name, secret = load_settings()
    spec_path = Path('p0/upstream/deepseek-probe-cases.json')
    spec = json.loads(spec_path.read_text())
    if spec['base_url'] != 'https://api.deepseek.com' or settings.get('AI_FAST_BASE_URL') != spec['base_url']:
        raise ValueError('Origin mismatch')
    if settings.get('AI_FAST_MODEL') != spec['requested_model']:
        raise ValueError('Model mismatch')
    if len(spec['cases']) != 3:
        raise ValueError('Expected three bounded cases')
    for case in spec['cases']:
        if (case['method'], case['path']) not in {('GET', '/models'), ('POST', '/chat/completions')}:
            raise ValueError('Unexpected request')
        if case['method'] == 'POST' and not (1 <= case['json']['max_tokens'] <= 32):
            raise ValueError('Output budget exceeded')
    args.output_dir.mkdir(mode=0o700, exist_ok=False)

    def redact(value):
        return re.sub(r'sk-[A-Za-z0-9_-]+', '[REDACTED]', value.replace(secret, '[REDACTED]'))

    results = []
    async with httpx.AsyncClient(
        base_url=spec['base_url'], trust_env=False, follow_redirects=False,
        timeout=httpx.Timeout(20, connect=10, pool=5),
        headers={'Authorization': 'Bearer '+secret, 'Accept-Encoding': 'identity'},
        transport=httpx.AsyncHTTPTransport(retries=0),
    ) as client:
        for case in spec['cases']:
            result = await run_case(client, case, args.output_dir, redact)
            results.append(result)
            (args.output_dir/(case['name']+'.json')).write_text(redact(json.dumps(result, ensure_ascii=False, indent=2))+'\n')
            print(json.dumps({'case': case['name'], 'passed': result['passed'],
                              'http_status': result.get('http_status'), 'error_type': result.get('error_type') }), flush=True)
            if result.get('http_status') in (401, 402, 403, 429) or result.get('failure') == 'exception':
                break
    report = {
        'recorded_at_utc': datetime.now(timezone.utc).isoformat(),
        'scope': 'temporary cloud compatibility; not final intranet or Ubuntu acceptance',
        'base_url': spec['base_url'], 'requested_model': spec['requested_model'],
        'credential_source': key_name, 'milestone_signoff': False,
        'proxy_mode': 'direct; environment proxies disabled',
        'spec_sha256': hashlib.sha256(spec_path.read_bytes()).hexdigest(),
        'results': results, 'skipped_cases': [c['name'] for c in spec['cases'][len(results):]],
        'passed': len(results) == 3 and all(r['passed'] for r in results),
    }
    (args.output_dir/'report.json').write_text(redact(json.dumps(report, ensure_ascii=False, indent=2))+'\n')
    print('Report:', str(args.output_dir/'report.json'))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__}))
        raise SystemExit(1)
