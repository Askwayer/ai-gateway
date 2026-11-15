#!/usr/bin/env python3
"""Generate/check all 80 P0 candidates from the authoritative design table.

Never reads or exports secret values; never changes .env or acceptance results.
This manifest is not an application Settings implementation or M0 sign-off.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from check_docs import block, rows, validate_markdown

ROOT = Path(__file__).resolve().parents[1]
DESIGN = ROOT/'docs/AI-Gateway-项目开发详细设计书-v1.6.md'
OUTPUT = ROOT/'p0/config-candidates.json'
DOC = ROOT/'docs/P0-配置候选清单.md'
SELECTION = ROOT/'p0/upstream/company-selection.json'
DEVELOPMENT = ROOT/'p0/upstream/deepseek-probe-cases.json'
SECRET_KEYS = {'DATABASE_URL', 'ADMIN_API_KEY', 'UPSTREAM_API_KEY'}


def build():
    design = DESIGN.read_text()
    source = rows(block(design, 'CONFIG'))
    types = {key.strip(): row[0] for row in rows(block(design, 'VALIDATION')) for key in row[1].split(',')}
    selected = json.loads(SELECTION.read_text())
    development = json.loads(DEVELOPMENT.read_text())
    overrides = {
        'DATABASE_URL': {'development': '.env:DATABASE_URL', 'company': '.env.company:DATABASE_URL'},
        'ADMIN_API_KEY': {'development': '.env:ADMIN_API_KEY', 'company': '.env.company:ADMIN_API_KEY'},
        'UPSTREAM_API_KEY': {'development': '.env:UPSTREAM_API_KEY', 'company': '.env.company:UPSTREAM_API_KEY'},
        'UPSTREAM_BASE_URL': selected['base_url'], 'ALLOWED_MODELS': [selected['model']],
        'INFERENCE_PROBE_MODEL': selected['model'], 'UPSTREAM_HEALTH_PATH': selected['health_path_candidate'],
        'TRUSTED_PROXY_CIDRS': selected['trusted_proxy_cidrs'], 'UPSTREAM_CA_BUNDLE': 'system_trust_store',
    }
    upstream_pending = {'UPSTREAM_BASE_URL', 'ALLOWED_MODELS', 'INFERENCE_PROBE_MODEL',
                        'UPSTREAM_HEALTH_PATH', 'UPSTREAM_STREAM_USAGE_MODE', 'INFERENCE_PROBE_MAX_TOKENS',
                        'HEALTH_REACHABILITY_ENABLED', 'HEALTH_GATE_ENABLED', 'INFERENCE_PROBE_ENABLED'}
    target_pending = {'HOST_STOP_TIMEOUT', 'CLOCK_MAX_OFFSET', 'DB_POOL_SIZE', 'DB_MAX_OVERFLOW',
                      'GLOBAL_MAX_CONCURRENCY', 'HTTP_MAX_CONNECTIONS'}
    local_verified = {'DATABASE_URL', 'ADMIN_API_KEY', 'UPSTREAM_API_KEY', 'TRUSTED_PROXY_CIDRS',
                      'DEPENDENCY_LOCK_TOOL', 'TZDATA_SOURCE', 'ALLOWED_TIMEZONES', 'DEFAULT_TIMEZONE'}
    evidence = {
        'DATABASE_URL': 'docs/P0-数据库初始化记录-20261115.json；默认开发使用测试库',
        'ADMIN_API_KEY': '.tools/p0-local-inputs-20261115.json；仅记录生成方法和来源，不记录值或哈希',
        'UPSTREAM_API_KEY': '用户提供，已存本地；凭证存在不等于上游鉴权成功',
        'TRUSTED_PROXY_CIDRS': '用户确认直连；.tools/p0-direct-echo-20261115.json；目标机复核待执行',
        'DEPENDENCY_LOCK_TOOL': 'uv 0.13.8；p0/dependencies/uv.lock；目标机离线安装待验证',
        'TZDATA_SOURCE': '锁定 tzdata 2026.6，本地导入通过；目标机待验证',
        'ALLOWED_TIMEZONES': '本地禁用系统 TZPATH 后可加载 UTC/Asia/Shanghai',
        'DEFAULT_TIMEZONE': '与 ALLOWED_TIMEZONES 一致；不替代目标机校时',
    }
    entries = []
    for name, initial, note in source:
        kind = types[name]
        if name in overrides:
            value = overrides[name]
        elif kind in ('positive_int', 'nonnegative_int'):
            value = int(initial.split()[0])
        elif kind in ('positive_number', 'nonnegative_number'):
            value = float(initial.split()[0])
        elif kind == 'boolean':
            if initial not in ('true', 'false'):
                raise ValueError('Invalid boolean candidate')
            value = initial == 'true'
        elif name == 'ALLOWED_TIMEZONES':
            value = initial.split(',')
        else:
            value = initial
        status = ('本地输入已核对' if name in local_verified else
                  '待内网上游验证' if name in upstream_pending else
                  '待目标环境验证' if name in target_pending else '设计默认候选')
        basis = evidence.get(name, '设计书 §11.1/§11.1.1；尚未进行最终配置冻结')
        if name in upstream_pending:
            basis = '用户选定 Fast；实际协议/健康/探测兼容性待验证；不以 DeepSeek 代替'
        if name == 'UPSTREAM_HEALTH_PATH':
            basis += '；候选拼接为 '+selected['health_request_url_candidate']+'，需保留 /v1 前缀'
        if name == 'UPSTREAM_CA_BUNDLE':
            basis += '；当前候选为 HTTP，此项保留系统信任库约定；如改 HTTPS 重新确认 CA'
        if name in ('HOST_STOP_TIMEOUT',):
            basis = '需在 systemd/实际进程管理器设置并现场取证；环境变量本身不能控制宿主'
        if name == 'CLOCK_MAX_OFFSET':
            basis = '目标机及 DB 主机的内网 NTP/等效校时与偏差待取证'
        entries.append({'name': name, 'type': kind, 'candidate': value,
                        'status': status, 'basis': basis, 'design_note': note,
                        'secret_source_only': name in SECRET_KEYS})
    assert len(entries) == len({e['name'] for e in entries}) == 80
    assert set(types) == {e['name'] for e in entries}
    values = {e['name']: e['candidate'] for e in entries}
    cleanup = values['UPSTREAM_CLOSE_TIMEOUT']+values['SETTLEMENT_MAX_ATTEMPTS']*values['SETTLEMENT_ATTEMPT_TIMEOUT']+(values['SETTLEMENT_MAX_ATTEMPTS']-1)*values['SETTLEMENT_RETRY_BACKOFF']
    pool = values['DB_POOL_SIZE']+values['DB_MAX_OVERFLOW']
    lanes = sum(values[k] for k in ('GLOBAL_MAX_CONCURRENCY','AUTH_DB_MAX_INFLIGHT','ADMIN_DB_MAX_INFLIGHT','BACKGROUND_DB_MAX_INFLIGHT'))
    stop1 = values['APP_DRAIN_TIMEOUT']+values['CLEANUP_TIMEOUT']+values['SHUTDOWN_SAFETY_MARGIN']
    stop2 = values['UVICORN_GRACEFUL_TIMEOUT']+values['BACKGROUND_SHUTDOWN_TIMEOUT']+values['SHUTDOWN_SAFETY_MARGIN']
    assert cleanup < values['CLEANUP_TIMEOUT'] and pool >= lanes
    assert stop1 <= values['UVICORN_GRACEFUL_TIMEOUT'] and stop2 <= values['HOST_STOP_TIMEOUT']
    assert values['DEFAULT_TIMEZONE'] in values['ALLOWED_TIMEZONES']
    assert values['INFERENCE_PROBE_MODEL'] in values['ALLOWED_MODELS']
    assert values['TRUSTED_PROXY_CIDRS'] == []
    report = {'schema_version': 1, 'status': 'candidate_only_not_frozen', 'milestone_signoff': False,
              'design': str(DESIGN.relative_to(ROOT)), 'design_sha256': hashlib.sha256(DESIGN.read_bytes()).hexdigest(),
              'selection_sha256': hashlib.sha256(SELECTION.read_bytes()).hexdigest(),
              'production_config_loader': 'P1 local model/CLI implemented in app.core.config; company required inputs and deployment remain pending',
              'default_environment': 'development', 'entries_scope': 'company_target_candidates',
              'development_spec_sha256': hashlib.sha256(DEVELOPMENT.read_bytes()).hexdigest(),
              'development_overrides': {'UPSTREAM_BASE_URL': development['base_url'],
                  'ALLOWED_MODELS': [development['requested_model']],
                  'INFERENCE_PROBE_MODEL': development['requested_model'],
                  'UPSTREAM_API_KEY': {'source': '.env:UPSTREAM_API_KEY'},
                  'DATABASE_URL': {'source': '.env:DATABASE_URL'},
                  'ADMIN_API_KEY': {'source': '.env:ADMIN_API_KEY'},
                  'UPSTREAM_HEALTH_PATH': '/models', 'TRUSTED_PROXY_CIDRS': []},
              'entries': entries, 'budget_checks': {'cleanup_seconds':cleanup,'cleanup_limit':values['CLEANUP_TIMEOUT'],
               'pool_max':pool,'lane_sum':lanes,'drain_cleanup_margin':stop1,'uvicorn_limit':values['UVICORN_GRACEFUL_TIMEOUT'],
               'uvicorn_background_margin':stop2,'host_limit':values['HOST_STOP_TIMEOUT']}}
    counts = Counter(e['status'] for e in entries)
    intro = '''# P0 配置候选清单

依据设计书 v1.6 的 CONFIG 与 VALIDATION 源表生成，覆盖全部 80 项；这是候选审查清单，不是配置冻结记录或可直接加载的运行配置。所有秘密只记录来源，不记录值、连接串或秘密哈希。

每个环境仍只接一个上游：开发阶段始终使用 DeepSeek；公司环境才使用 Fast / Qwen3-VL-8B-Instruct。用户确认客户端直连网关、无 Nginx。TRUSTED_PROXY_CIDRS=[]，Uvicorn proxy_headers=False。公司参考文件中的其他服务、输入限制、OSS 代理均未引入本网关；P1本地基座已实现APP_ENV/APP_ENV_FILE显式选择，尚无API服务部署。

开发 `.env` 使用测试库、DeepSeek 和开发管理员凭证；`.env.company` 保存正式库、内网 Fast 和独立管理员凭证。默认不加载公司文件，也不自动探测公司地址。公司配置缺少必要健康路径时校验失败，不回退开发文件。公司网络当前不可达是环境边界，不属于开发上游故障；正式阶段门禁由获批例外限定，现场协议、离线能力和部署签收仍需后补验证。

| 项目 | 开发环境（默认） | 公司环境（后续联调/部署） |
| --- | --- | --- |
| 配置来源 | .env | .env.company |
| 上游 | https://api.deepseek.com | http://172.31.34.204:5330/v1 |
| 请求模型 | deepseek-v4-flash | Qwen3-VL-8B-Instruct |
| 数据库 | ai_gateway_test | ai_gateway |
| 当前协议证据 | 非流/SSE/usage 已验证；故障及确定值测试仍使用模拟上游 | 待公司网段验证 |

下面 80 项源表展示公司目标候选；不能直接拿公司的上游值覆盖开发配置。机器可读清单以 default_environment=development 标记默认环境，并用 development_overrides 明确开发差异；不是新增运行配置字段或多上游路由。配置模型与读取入口已在P1本地基座实现，服务启动器和现场门槛仍待后续工作。

本地输入已核对只表示来源/局部证据成立；没有一项因此自动获得最终冻结状态。候选列表采用 JSON 类型表达；P1模型已覆盖字段及静态交叉校验，但不替代服务启动/外部资源检查。更新设计或选择后运行 `python scripts/p0_config_candidates.py --write`，检查运行 `python scripts/p0_config_candidates.py --check`。

'''
    intro += '状态数量：'+'；'.join(f'{k} {v} 项' for k,v in counts.items())+'。\n\n'
    intro += f'静态预算：清理 {cleanup:g} < {values["CLEANUP_TIMEOUT"]:g} 秒；池 {pool} ≥ 通道总和 {lanes}；排空/清理/余量 {stop1:g} ≤ Uvicorn {values["UVICORN_GRACEFUL_TIMEOUT"]:g} 秒；Uvicorn/后台/余量 {stop2:g} ≤ 宿主 {values["HOST_STOP_TIMEOUT"]:g} 秒。只验证算术，不证明现场容量或停止时序。\n\n'
    intro += '| 配置 | 候选值或秘密来源 | 状态 | 依据/后续验证 |\n| --- | --- | --- | --- |\n'
    for entry in entries:
        rendered = json.dumps(entry['candidate'],ensure_ascii=False,separators=(',', ':'))
        intro += f'| {entry["name"]} | `{rendered}` | {entry["status"]} | {entry["basis"]} |\n'
    intro += '''
## 待验证与后续冻结

- 目标 Ubuntu 架构/Python/资源、内网连通性、校时、实际服务停止时限与离线安装。
- 最终 Fast 引擎版本、模型响应名、非流/SSE usage、include_usage、健康候选路径、探测 max_tokens=1、断开后的上游行为。
- 默认并发和 DB 池需结合目标机资源、其他业务与管理保留连接确认；同实例正式/测试负载不能被当作物理隔离。
- P0 完成本地 A41/A44 方法评审；生产入口/完整启动校验及容量实测分别在 P1/P2/P4 执行，不提前填通过。
- P0-04 最终冻结与 M0 签收等待以上证据。80 项候选的静态结构与预算检查不能替代 P1 Settings 负例和 A42。
'''
    validate_markdown(intro, 'P0 candidates')
    return json.dumps(report, ensure_ascii=False, indent=2)+'\n', intro


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--write', action='store_true')
    mode.add_argument('--check', action='store_true')
    args = parser.parse_args()
    data, markdown = build()
    if args.write:
        OUTPUT.write_text(data)
        DOC.write_text(markdown)
    else:
        if OUTPUT.read_text() != data or DOC.read_text() != markdown:
            raise ValueError('Candidate artifacts drifted from source; review then regenerate')
    print('PASS: 80 candidates; complete type coverage; secret sources only; budgets and source consistency checked. Not M0 sign-off.')


if __name__ == '__main__':
    main()
