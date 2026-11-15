#!/usr/bin/env python3
"""Check design/template contracts (v1.6 by default); sync unfilled blocks only.

Uses Python's standard library only. Run from any working directory.
Actual acceptance reports must be copied away from the template before filling.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

BLOCKS = ("CONFIG", "PERF", "CASES", "TRACE")
SECRETS = {"DATABASE_URL", "ADMIN_API_KEY", "UPSTREAM_API_KEY"}
SECRET_PLACEHOLDER = "仅记录配置来源（不填值）"
PLAN = "AI-Gateway-开发执行计划-v1.1.md"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def bounds(text: str, name: str) -> tuple[int, int]:
    start = f"<!-- {name}_BEGIN -->"
    end = f"<!-- {name}_END -->"
    require(text.count(start) == text.count(end) == 1, f"{name}: missing/duplicate markers")
    left = text.index(start) + len(start)
    right = text.index(end)
    require(left < right, f"{name}: reversed markers")
    return left, right


def block(text: str, name: str) -> str:
    left, right = bounds(text, name)
    return text[left:right].strip()


def rows(table: str) -> list[list[str]]:
    lines = [line for line in table.splitlines() if line.startswith("|")]
    if not lines:
        return []
    require(len(lines) >= 2, "table lacks header/separator")
    cells = [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in lines]
    require(all(len(row) == len(cells[0]) for row in cells), "inconsistent table columns")
    require(all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells[1]), "bad table separator")
    return cells[2:]


def table(headers: list[str], data: list[list[str]]) -> str:
    return "\n".join("| " + " | ".join(row) + " |" for row in
                     [headers, ["---"] * len(headers), *data])


def unique_mapping(data: list[list[str]], label: str) -> dict[str, list[str]]:
    require(bool(data), f"{label}: empty table")
    result = {row[0]: row[1:] for row in data}
    require(len(result) == len(data), f"{label}: duplicate IDs/names")
    return result


def validate_markdown(text: str, label: str) -> None:
    in_fence = False
    table_lines: list[str] = []

    def flush() -> None:
        if table_lines:
            rows("\n".join(table_lines))
            table_lines.clear()

    for line in text.splitlines():
        if line.startswith("```"):
            flush()
            in_fence = not in_fence
        elif not in_fence:
            if line.startswith("|"):
                require(line.endswith("|"), f"{label}: missing table boundary")
                table_lines.append(line)
            else:
                flush()
    flush()
    require(not in_fence, f"{label}: unclosed code fence")


def expected_blocks(design: str, version: str = "v1.6") -> dict[str, str]:
    config_rows = rows(block(design, "CONFIG"))
    case_rows = rows(block(design, "CASES"))
    perf_rows = rows(block(design, "PERF"))
    trace_rows = rows(block(design, "TRACE"))
    config = unique_mapping(config_rows, "config")
    cases = unique_mapping(case_rows, "cases")
    unique_mapping(perf_rows, "performance")
    trace = unique_mapping(trace_rows, "trace")
    case_count = 43 if version == "v1.2" else 44
    require(set(cases) == {f"A{i:02}" for i in range(1, case_count + 1)},
            f"expected contiguous A01–A{case_count}")
    functional = re.findall(r"^\| (F\d{2}) \|", design, re.M)
    # Function IDs occur once in scope and once in trace.
    require(set(functional) == set(trace) == {f"F{i:02}" for i in range(1, 15)},
            "expected F01–F14 scope and trace")
    require(all(functional.count(key) == 2 for key in trace), "duplicate/missing functional row")
    covered: set[str] = set()
    for feature, values in trace.items():
        refs = values[0].split(",")
        require(len(set(refs)) == len(refs), f"{feature}: duplicate case reference")
        require(set(refs) <= set(cases), f"{feature}: nonexistent case reference")
        covered.update(refs)
    require(covered == set(cases), f"unmapped cases: {sorted(set(cases) - covered)}")

    def number(name: str) -> float:
        require(name in config, f"missing config: {name}")
        match = re.match(r"^\d+(?:\.\d+)?", config[name][0])
        require(match is not None, f"nonnumeric config: {name}")
        return float(match.group())

    attempts = number("SETTLEMENT_MAX_ATTEMPTS")
    require(attempts >= 1 and attempts.is_integer(), "invalid attempt count")
    cleanup_work = (number("UPSTREAM_CLOSE_TIMEOUT")
                    + attempts * number("SETTLEMENT_ATTEMPT_TIMEOUT")
                    + (attempts - 1) * number("SETTLEMENT_RETRY_BACKOFF"))
    require(cleanup_work < number("CLEANUP_TIMEOUT"), "cleanup budget exceeded")
    margin = 5 if version == "v1.2" else number("SHUTDOWN_SAFETY_MARGIN")
    require(number("APP_DRAIN_TIMEOUT") + number("CLEANUP_TIMEOUT") + margin
            <= number("UVICORN_GRACEFUL_TIMEOUT"), "no Uvicorn shutdown headroom")
    require(number("UVICORN_GRACEFUL_TIMEOUT") + number("BACKGROUND_SHUTDOWN_TIMEOUT") + margin
            <= number("HOST_STOP_TIMEOUT"), "no host shutdown headroom")
    required_pool = sum(number(key) for key in ("GLOBAL_MAX_CONCURRENCY", "AUTH_DB_MAX_INFLIGHT",
                                               "ADMIN_DB_MAX_INFLIGHT", "BACKGROUND_DB_MAX_INFLIGHT"))
    require(number("DB_POOL_SIZE") + number("DB_MAX_OVERFLOW") >= required_pool,
            "database pool smaller than configured lane budget")
    if version != "v1.2":
        positive_integers = (
            "GLOBAL_MAX_CONCURRENCY", "DEFAULT_PROJECT_MAX_CONCURRENCY",
            "DEFAULT_KEY_MAX_CONCURRENCY", "AUTH_DB_MAX_INFLIGHT", "ADMIN_DB_MAX_INFLIGHT",
            "BACKGROUND_DB_MAX_INFLIGHT", "DB_POOL_SIZE", "HTTP_MAX_CONNECTIONS",
            "INGRESS_GLOBAL_BURST", "INGRESS_IP_BURST", "PROBE_MIN_SPARE_SLOTS",
        )
        for name in positive_integers:
            value = number(name)
            require(value >= 1 and value.is_integer(), f"invalid positive integer: {name}")
        overflow = number("DB_MAX_OVERFLOW")
        require(overflow >= 0 and overflow.is_integer(), "invalid DB_MAX_OVERFLOW")
        for name in config:
            if name.endswith("_TIMEOUT") or name in (
                "SHUTDOWN_SAFETY_MARGIN", "INGRESS_GLOBAL_RATE", "INGRESS_IP_RATE",
            ):
                require(number(name) > 0, f"nonpositive timeout/rate/margin: {name}")
        require(number("SETTLEMENT_RETRY_BACKOFF") >= 0, "negative backoff")
        for name in ("CONCURRENCY_RETRY_AFTER", "SERVICE_UNAVAILABLE_RETRY_AFTER"):
            value = number(name)
            require(1 <= value <= 3600 and value.is_integer(), f"invalid Retry-After: {name}")
        for name in ("AUTH_DB_MAX_INFLIGHT", "INGRESS_GLOBAL_BURST", "INGRESS_IP_BURST"):
            require(number(name) >= number("GLOBAL_MAX_CONCURRENCY"),
                    f"first-request capacity below global limit: {name}")
        require(number("HTTP_MAX_CONNECTIONS") >= number("GLOBAL_MAX_CONCURRENCY") + 1,
                "HTTP pool lacks reachability probe headroom")
        require(number("SETTLEMENT_ATTEMPT_TIMEOUT") <= number("DB_OPERATION_TIMEOUT"),
                "settlement timeout exceeds ordinary DB timeout")
    require(number("PAGINATION_DEFAULT_LIMIT") <= number("PAGINATION_MAX_LIMIT"), "pagination bounds")
    require(config["DEFAULT_TIMEZONE"][0] in config["ALLOWED_TIMEZONES"][0].split(","),
            "default timezone outside allowlist")
    if version in ("v1.4", "v1.5", "v1.6"):
        validate_startup_checklist(design, config)
    # All uppercase backticked configuration references must resolve to the source table.
    referenced = set(re.findall(r"`([A-Z][A-Z0-9_]+)`", design))
    exceptions = {"NULL", "UTC", "TIMESTAMPTZ"}
    require(referenced <= set(config) | exceptions,
            f"unlisted config reference(s): {sorted(referenced - set(config) - exceptions)}")

    return {
        "CONFIG": table(["配置", "候选值", "说明",
                         "冻结值/秘密来源" if version in ("v1.4", "v1.5", "v1.6") else "冻结值", "冻结日期/依据"],
                        [row + [SECRET_PLACEHOLDER if version in ("v1.4", "v1.5", "v1.6") and row[0] in SECRETS
                                else "待填写", "待填写"] for row in config_rows]),
        "PERF": table(["指标", "候选目标", "测量方法", "冻结值/依据"],
                      [row + ["待填写"] for row in perf_rows]),
        "CASES": table(["编号", "验收项", "结果", "证据位置/问题编号"],
                       [[row[0], row[1], "未执行", "—"] for row in case_rows]),
        "TRACE": table(["功能", "验收用例"], trace_rows),
    }


def section(text: str, heading: str) -> str:
    """Return a heading's section up to the next heading of equal/less depth."""
    matches = list(re.finditer(rf"^{re.escape(heading)}[^\n]*\n", text, re.M))
    require(len(matches) == 1, f"missing/duplicate section: {heading}")
    start = matches[0].end()
    depth = len(heading) - len(heading.lstrip("#"))
    end = re.search(rf"^#{{1,{depth}}} ", text[start:], re.M)
    return text[start:start + end.start()] if end else text[start:]


def validate_startup_checklist(design: str, config: dict[str, list[str]]) -> None:
    checklist = unique_mapping(rows(block(design, "VALIDATION")), "startup checklist")
    allowed_kinds = {"positive_int", "nonnegative_int", "positive_number", "nonnegative_number",
                     "boolean", "enum", "address", "secret", "timezone", "cidr", "file", "model_text"}
    require(set(checklist) == allowed_kinds, "startup checklist types incomplete")
    covered: list[str] = []
    numeric: dict[str, float] = {}
    for kind, (names, rule) in checklist.items():
        require(bool(rule), "startup rule missing")
        for name in names.split(","):
            require(name in config, f"unknown startup field: {name}")
            covered.append(name)
            raw = config[name][0]
            if kind in {"positive_int", "nonnegative_int", "positive_number", "nonnegative_number"}:
                match = re.fullmatch(r"(\d+(?:\.\d+)?)(?: (?:秒|次/秒))?", raw)
                require(match is not None, f"invalid numeric literal: {name}")
                value = float(match[1])
                require(math.isfinite(value), f"nonfinite value: {name}")
                require(value > 0 if kind.startswith("positive") else value >= 0,
                        f"startup numeric bound: {name}")
                if kind.endswith("_int"):
                    require(value.is_integer(), f"startup integer required: {name}")
                numeric[name] = value
            elif kind == "boolean":
                require(raw in ("true", "false"), f"startup boolean invalid: {name}")
    require(len(covered) == len(set(covered)) and set(covered) == set(config),
            "startup checklist must cover every config exactly once")
    for name, values in {
        "UPSTREAM_STREAM_USAGE_MODE": {"include_usage", "unsupported"},
        "TZDATA_SOURCE": {"locked_python_package"}, "DEPENDENCY_LOCK_TOOL": {"uv"},
    }.items():
        require(config[name][0] in values, f"startup enum invalid: {name}")
    require(config["HEALTH_GATE_ENABLED"][0] != "true"
            or config["HEALTH_REACHABILITY_ENABLED"][0] == "true", "health gate dependency")
    for stale, interval, timeout in (
        ("HEALTH_STALE_AFTER", "HEALTH_INTERVAL", "HEALTH_TIMEOUT"),
        ("DB_READY_STALE_AFTER", "DB_READY_CHECK_INTERVAL", "DB_OPERATION_TIMEOUT"),
        ("INFERENCE_PROBE_STALE_AFTER", "INFERENCE_PROBE_INTERVAL", "INFERENCE_PROBE_TIMEOUT"),
    ):
        require(numeric[stale] > numeric[interval] + numeric[timeout], f"stale window too short: {stale}")
    require(numeric["RECOVERY_GRACE_PERIOD"] >= numeric["CLEANUP_TIMEOUT"], "recovery grace below cleanup")
    require(1 <= numeric["USAGE_MAX_GROUP_DIMENSIONS"] <= 2, "unsupported grouping dimension limit")
    performance = unique_mapping(rows(block(design, "PERF")), "performance")
    windows = re.fullmatch(r"故障 ≤(\d+)s；恢复 ≤(\d+)s", performance["健康发现/恢复"][0])
    require(windows is not None, "health performance window format")
    for target, threshold in zip(map(float, windows.groups()),
                                 ("HEALTH_FAILURE_THRESHOLD", "HEALTH_RECOVERY_THRESHOLD")):
        require(target > numeric["HEALTH_INTERVAL"] * numeric[threshold] + numeric["HEALTH_TIMEOUT"],
                "health performance window lacks scheduling margin")
    for name, placeholder in {
        "DATABASE_URL": "postgresql+asyncpg://...", "ADMIN_API_KEY": "必填高熵随机值", "UPSTREAM_API_KEY": "空",
    }.items():
        require(config[name][0] == placeholder, f"secret source must contain placeholder only: {name}")


def validate_reference_contracts(design: str, template: str, version: str = "v1.6") -> None:
    """Guard the explicit no-duplicate-threshold regions, not arbitrary prose."""
    areas = {
        "design cleanup": section(design, "### 5.3 "),
        "design settlement": section(design, "### 8.4 "),
        "design shutdown": section(design, "### 12.2 "),
        "design conclusion": section(design, "### 14.3 "),
        "template performance": section(template, "## 5. "),
        "template conclusion": section(template, "## 8. "),
    }
    if version in ("v1.4", "v1.5", "v1.6"):
        for name, heading in (("health schedule", "### 10.2 "), ("health timing", "### 10.4 "),
                              ("retention", "### 7.7 "), ("concurrency", "### 9.1 "),
                              ("pagination", "### 6.3 ")):
            areas[name] = section(design, heading)
    literal_threshold = r"\d+(?:\.\d+)?\s*(?:ms|秒|小时|分钟|毫秒|工作日|s\b|%|并发|条在途)"
    for label, value in areas.items():
        require(not re.search(literal_threshold, value),
                f"{label}: duplicate numeric threshold; reference source configuration/metric")
    for label in ("design conclusion", "template conclusion"):
        require(not re.search(r"A\d{2}", areas[label]),
                f"{label}: duplicate case range; reference case table")
    require("§14.2核心用例全部通过" in areas["design conclusion"], "design conclusion lacks case reference")
    require("§3核心用例全部通过" in areas["template conclusion"]
            and "§2性能冻结基线" in areas["template conclusion"], "template conclusion lacks source references")
    require("§2“即时流式交付”" in areas["template performance"]
            and "§2“持续运行”" in areas["template performance"], "template performance lacks metric references")
    for label in ("design cleanup", "design settlement", "design shutdown"):
        require("CLEANUP_TIMEOUT" in areas[label], f"{label}: cleanup config reference missing")
    cases = unique_mapping(rows(block(design, "CASES")), "cases")
    for case_id, refs in {
        "A10": ("§15.2", "即时流式交付"),
        "A13": ("CLEANUP_TIMEOUT",),
        "A21": ("DB_READY_CHECK_INTERVAL", "DB_OPERATION_TIMEOUT", "DB_READY_STALE_AFTER"),
        "A33": ("APP_DRAIN_TIMEOUT", "CLEANUP_TIMEOUT", "§11.1"),
    }.items():
        value = cases[case_id][1]
        require(all(ref in value for ref in refs), f"{case_id}: source reference missing")
        require(not re.search(literal_threshold, value), f"{case_id}: duplicated timing threshold")
    require("测试配置偏差" in section(template, "## 2. "), "test profile deviation row missing")
    require("代理信任链冻结" in section(template, "## 1. "), "proxy trust freeze row missing")
    phases = re.findall(r"^\| P[0-6] [^|]+ \| D(\d+)–D(\d+) \| (\d+) 天 \|", design, re.M)
    require(len(phases) == 7, "expected seven development phases")
    next_day = 1
    for start, end, effort in phases:
        require(int(start) == next_day and int(end) - int(start) + 1 == int(effort),
                "phase days/effort mismatch")
        next_day = int(end) + 1
    last_day, estimate = (31, "30～36个工作日") if version in ("v1.4", "v1.5", "v1.6") else (29, "28～34个工作日")
    if version == "v1.6":
        last_day, estimate = 37, "36～42个工作日"
    require(next_day == last_day and estimate in section(design, "### 13.1 "),
            "phase total differs from current estimate")
    if version in ("v1.4", "v1.5", "v1.6"):
        for case_id, refs in {"A15": ("limit-isolation",), "A20": ("§7.4",),
                              "A40": ("Content-Type矩阵",), "A41": ("§14.1.2",),
                              "A42": ("§11.1.1",), "A44": ("§14.1.1", "C0")}.items():
            require(all(ref in cases[case_id][1] for ref in refs), f"{case_id}: v1.4 contract reference missing")
        require("started_at=finished_at=updated_at" in section(design, "### 7.4 "), "rejected time contract missing")
        require("C0<GLOBAL_MAX_CONCURRENCY" in section(design, "#### 14.1.1 "), "cold pool criterion missing")
        require("SELECT pg_sleep(:delay)" in section(design, "#### 14.1.2 "), "real DB injection missing")
    if version in ("v1.5", "v1.6"):
        profile_text = section(template, "## 2. ").split("<!-- CONFIG_BEGIN -->")[0]
        for profile in ("default-protection", "functional-paced", "limit-isolation", "实验profile"):
            require(profile in profile_text, f"template profile list missing: {profile}")
        require("limit-isolation" in cases["A24"][1], "A24 profile missing: limit-isolation")
        config = unique_mapping(rows(block(design, "CONFIG")), "config")
        require("启用时P0必填，禁用时可空" == config["UPSTREAM_HEALTH_PATH"][0],
                "health path candidate must state conditional requirement")
        require("可达性启用时必填，禁用时可空" in block(design, "VALIDATION"),
                "health path startup rule must state conditional requirement")
        require("若睡眠未超时则继续原鉴权查询；本用例预期在睡眠期间触发超时" in
                section(design, "#### 14.1.2 "), "sleep timeout path ambiguous")
        for ref in ("response_status_code", "upstream_status_code", "error_code", "CHECK"):
            require(ref in cases["A20"][1], f"A20 HTTP contract assertion missing: {ref}")
        rejected = section(design, "### 7.4 ")
        for ref in ("ck_rejected_http_contract", "IS TRUE", "concurrency_limit_exceeded"):
            require(ref in rejected, f"rejected HTTP constraint missing: {ref}")
        require("§14.1.3" in cases["A41"][1] and "实验profile" in cases["A41"][1],
                "A41 capacity profile/source reference missing")
        require("§14.1.3" in profile_text, "template capacity deviation reference missing")
        capacity = section(design, "#### 14.1.3 ")
        for ref in ("local_address", "TRUSTED_PROXY_CIDRS", "INGRESS_GLOBAL_RATE",
                    "INGRESS_IP_IDLE_TTL", "N+1", "恢复原始速率与信任表"):
            require(ref in capacity, f"A41 source/capacity procedure missing: {ref}")


def validate_execution_plan(plan: str, design: str) -> None:
    validate_markdown(plan, PLAN)
    require("(./AI-Gateway-项目开发详细设计书-v1.6.md)" in plan
            and "(./AI-Gateway-验收记录模板-v1.6.md)" in plan, "plan baseline links mismatch")
    phase_rows = rows(block(plan, "PLAN_PHASES"))
    phase_map = unique_mapping(phase_rows, "plan phases")
    require(list(phase_map) == [f"P{i}" for i in range(7)], "plan phase IDs/order invalid")
    design_phases = re.findall(
        r"^\| (P[0-6]) [^|]+ \| (D\d+–D\d+) \| (\d+) 天 \|", design, re.M)
    require([(row[0], row[1], row[2]) for row in phase_rows] == design_phases,
            "plan/design phase schedule mismatch")
    work_rows = rows(block(plan, "WORKPACKAGES"))
    work = unique_mapping(work_rows, "work packages")
    cases = unique_mapping(rows(block(design, "CASES")), "cases")
    dependencies: dict[str, list[str]] = {}
    totals = {phase: Decimal(0) for phase in phase_map}
    position = {name: index for index, name in enumerate(work)}
    for name, values in work.items():
        require(len(values) == 6, f"invalid work package columns: {name}")
        phase, title, dod, deps, case_ids, effort_text = values
        require(bool(re.fullmatch(r"(?:P0-\d{2}|WP-[A-Z]+-\d{2})", name)), f"invalid work ID: {name}")
        require(phase in phase_map, f"unknown work phase: {name}")
        require(bool(title and dod), f"empty work title/DoD: {name}")
        refs = case_ids.split(",")
        require(len(refs) == len(set(refs)) and set(refs) <= set(cases), f"invalid work case reference: {name}")
        effort = Decimal(effort_text)
        require(effort.is_finite() and effort > 0, f"invalid work effort: {name}")
        totals[phase] += effort
        dependencies[name] = [] if deps == "—" else deps.split(",")
        require(len(dependencies[name]) == len(set(dependencies[name])), f"duplicate dependency: {name}")
        require(name not in dependencies[name], f"self dependency: {name}")
        require(set(dependencies[name]) <= set(work), f"unknown dependency: {name}")
        for dep in dependencies[name]:
            require(int(work[dep][0][1:]) <= int(phase[1:]), f"future-phase dependency: {name}")
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(name: str) -> None:
        require(name not in visiting, f"dependency cycle: {name}")
        if name in visited:
            return
        visiting.add(name)
        for dep in dependencies[name]:
            visit(dep)
        visiting.remove(name)
        visited.add(name)

    for name in work:
        visit(name)
        require(all(position[dep] < position[name] for dep in dependencies[name]),
                f"work order violates dependency: {name}")
    require([int(row[1][1:]) for row in work_rows] == sorted(int(row[1][1:]) for row in work_rows),
            "work order violates phase order")
    for phase, values in phase_map.items():
        require(totals[phase] == Decimal(values[1]), f"phase work sum mismatch: {phase}")
    require(sum(totals.values()) == 36 and totals["P6"] == 5, "plan total/final validation budget mismatch")
    for doc in (plan, design):
        require("D37–D42" in doc and "36～42个工作日" in doc, "plan contingency/total mismatch")
    trace = unique_mapping(rows(block(plan, "PLAN_TRACE")), "plan trace")
    require(set(trace) == set(cases), "plan case ownership coverage mismatch")
    def prerequisites(name: str) -> set[str]:
        result: set[str] = set()
        pending = list(dependencies[name])
        while pending:
            dep = pending.pop()
            if dep not in result:
                result.add(dep)
                pending.extend(dependencies[dep])
        return result

    for case_id, owners in trace.items():
        require(len(owners) == 2, f"plan case needs implementation and verification owner: {case_id}")
        for owner in owners:
            require(owner in work, f"unknown plan case owner: {case_id}")
            require(case_id in work[owner][4].split(","), f"owner lacks associated case: {case_id}/{owner}")
        implementation, verification = owners
        require(implementation == verification or implementation in prerequisites(verification),
                f"verification lacks implementation dependency: {case_id}")


def ensure_unfilled(template: str, version: str) -> None:
    for row in rows(block(template, "CONFIG")):
        placeholder = SECRET_PLACEHOLDER if version in ("v1.4", "v1.5", "v1.6") and row[0] in SECRETS else "待填写"
        require(row[-2:] == [placeholder, "待填写"], "refuse to overwrite filled configuration")
    for row in rows(block(template, "PERF")):
        require(row[-1] == "待填写", "refuse to overwrite frozen performance values")
    for row in rows(block(template, "CASES")):
        require(row[2:] == ["未执行", "—"], "refuse to overwrite acceptance results/evidence")


def run() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs-dir", type=Path, default=Path(__file__).resolve().parents[1] / "docs")
    parser.add_argument("--sync-template", action="store_true")
    parser.add_argument("--version", choices=("v1.2", "v1.3", "v1.4", "v1.5", "v1.6"), default="v1.6")
    args = parser.parse_args()
    design_name = f"AI-Gateway-项目开发详细设计书-{args.version}.md"
    template_name = f"AI-Gateway-验收记录模板-{args.version}.md"
    design_path, template_path = args.docs_dir / design_name, args.docs_dir / template_name
    design, template = design_path.read_text(), template_path.read_text()
    validate_markdown(design, design_name)
    validate_markdown(template, template_name)
    generated = expected_blocks(design, args.version)
    if args.sync_template:
        ensure_unfilled(template, args.version)
        for name, value in generated.items():
            left, right = bounds(template, name)
            template = template[:left] + "\n" + value + "\n" + template[right:]
        validate_markdown(template, template_name)
    for name, value in generated.items():
        require(block(template, name) == value, f"{name}: design/template drift; review then --sync-template")
    require(f"(./{design_name})" in template, "template must link to matching design version")
    if args.version != "v1.2":
        validate_reference_contracts(design, template, args.version)
    documents = [design, template]
    if args.version == "v1.6":
        plan = (args.docs_dir / PLAN).read_text()
        validate_execution_plan(plan, design)
        documents.append(plan)
    for doc in documents:
        for target in re.findall(r"\]\((\./[^)#]+)(?:#[^)]*)?\)", doc):
            require((args.docs_dir / target).exists(), f"broken local link: {target}")
    if args.sync_template:
        template_path.write_text(template)
    print(f"PASS {args.version}: {len(rows(block(design, 'CONFIG')))} configs, "
          f"{len(rows(block(design, 'CASES')))} cases, 14 feature mappings, "
          f"{len(rows(block(design, 'PERF')))} performance targets; budgets and Markdown valid.")
    if args.version == "v1.6":
        print(f"PLAN PASS: {len(rows(block(plan, 'WORKPACKAGES')))} work packages; "
              "36 person-days + 6 contingency; phase sums, dependency DAG and case ownership valid.")


if __name__ == "__main__":
    try:
        run()
    except (ValueError, OSError, IndexError, InvalidOperation) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
