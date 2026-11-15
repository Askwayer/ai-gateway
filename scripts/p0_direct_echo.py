#!/usr/bin/env python3
"""Loopback-only TCP evidence for P0 direct ingress. Not production middleware.

No credentials or business payloads. Uvicorn preserves the original socket peer.
The empty trust list means every forwarded header is ignored for client identity.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import platform
import socket
import time

import httpx
import uvicorn


def normalize_ip(value):
    address = ipaddress.ip_address(value)
    return str(address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address)


async def echo_app(scope, receive, send):
    if scope['type'] != 'http':
        return
    status = 200 if scope['path'] == '/echo' else 404
    peer = scope['client'][0]
    payload = json.dumps({'socket_peer': peer, 'client_ip': normalize_ip(peer),
                          'trusted_proxy_cidrs': [], 'forwarded_headers_used': False}).encode()
    await send({'type': 'http.response.start', 'status': status,
                'headers': [(b'content-type', b'application/json'), (b'content-length', str(len(payload)).encode())]})
    await send({'type': 'http.response.body', 'body': payload})


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError('Evidence must not be overwritten')
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(('127.0.0.1', 0))
    sock.listen(128)
    sock.setblocking(False)
    port = sock.getsockname()[1]
    config = uvicorn.Config(echo_app, host='127.0.0.1', port=port, workers=1,
                            proxy_headers=False, lifespan='off', access_log=False,
                            log_level='error', timeout_graceful_shutdown=2)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    report = {'recorded_at_utc': datetime.now(timezone.utc).isoformat(),
              'system': platform.system(), 'architecture': platform.machine(),
              'uvicorn_version': uvicorn.__version__, 'binding': '127.0.0.1:ephemeral',
              'proxy_headers': False, 'workers': 1, 'trusted_proxy_cidrs': [],
              'milestone_signoff': False, 'cases': [], 'source_ip_trials': []}
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError('Server stopped before ready')
                await asyncio.sleep(.01)
        cases = [
            ('direct', {}),
            ('forged_xff_single', {'X-Forwarded-For': '203.0.113.11'}),
            ('forged_xff_chain', {'X-Forwarded-For': '203.0.113.11, 198.51.100.12'}),
            ('malformed_xff', {'X-Forwarded-For': 'not-an-ip, unknown'}),
            ('alternate_forwarding_headers', {'Forwarded': 'for=203.0.113.14', 'X-Real-IP': '203.0.113.15'}),
            ('same_peer_different_claim', {'X-Forwarded-For': '198.51.100.99'}),
        ]
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=3) as client:
            for name, headers in cases:
                response = await client.get('/echo', headers=headers)
                body = response.json()
                passed = response.status_code == 200 and body['socket_peer'] == '127.0.0.1' and body['client_ip'] == '127.0.0.1'
                report['cases'].append({'name': name, 'passed': passed, 'observed': body})
        report['ipv4_mapped_normalization'] = normalize_ip('::ffff:127.0.0.1') == '127.0.0.1'
        # Feasibility samples only. Not the N+1 source count required by A41.
        for source in ('127.0.0.2', '127.0.0.3'):
            row = {'requested_source': source}
            try:
                async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(local_address=source, retries=0),
                                             trust_env=False, timeout=2) as client:
                    async with asyncio.timeout(3):
                        response = await client.get(f'http://127.0.0.1:{port}/echo')
                    row.update(observed=response.json(), bind_and_connect=response.status_code == 200 and response.json()['socket_peer'] == source)
            except Exception as exc:
                row.update(bind_and_connect=False, error_type=type(exc).__name__)
            report['source_ip_trials'].append(row)
        report['passed'] = all(c['passed'] for c in report['cases']) and report['ipv4_mapped_normalization']
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        finally:
            if not task.done():
                task.cancel()
            sock.close()
    report['server_stopped'] = task.done() and not task.cancelled() and task.exception() is None
    report['remaining'] = ['Ubuntu source bind/connect confirmation', 'A41 N+1 source construction and production ingress tests',
                           'P1/P2 production parser; this prototype has only empty-trust direct behavior']
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(report, out, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(not (report['passed'] and report['server_stopped']))


if __name__ == '__main__':
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__}))
        raise SystemExit(1)
