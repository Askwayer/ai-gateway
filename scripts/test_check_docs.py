#!/usr/bin/env python3
"""Exercise documentation checks against isolated copies, without touching reports."""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts/check_docs.py"
DESIGN = "AI-Gateway-项目开发详细设计书-v1.6.md"
TEMPLATE = "AI-Gateway-验收记录模板-v1.6.md"
PLAN = "AI-Gateway-开发执行计划-v1.1.md"


class DocumentContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ai-gateway-docs-")
        self.addCleanup(self.temp.cleanup)
        self.docs = Path(self.temp.name)
        for path in (ROOT / "docs").glob("*.md"):
            (self.docs / path.name).write_bytes(path.read_bytes())

    def run_check(self, *args: str, ok: bool = True, reason: str = "") -> None:
        result = subprocess.run(
            [sys.executable, str(CHECK), "--docs-dir", str(self.docs), *args],
            capture_output=True, text=True, timeout=15,
        )
        output = result.stdout + result.stderr
        if ok:
            self.assertEqual(result.returncode, 0, output)
        else:
            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn(reason, output)

    def replace(self, filename: str, old: str, new: str) -> None:
        path = self.docs / filename
        value = path.read_text()
        self.assertIn(old, value)
        path.write_text(value.replace(old, new, 1))

    def config(self, name: str, value: str) -> None:
        path = self.docs / DESIGN
        text, count = re.subn(
            rf"(^\| {re.escape(name)} \| )[^|]+( \|)",
            lambda m: m[1] + value + m[2], path.read_text(), count=1, flags=re.M,
        )
        self.assertEqual(count, 1)
        path.write_text(text)

    def test_current_and_historical_baselines(self) -> None:
        self.run_check()
        self.run_check("--version", "v1.2")
        self.run_check("--version", "v1.3")
        self.run_check("--version", "v1.4")
        self.run_check("--version", "v1.5")

    def test_generated_block_drift(self) -> None:
        source = (self.docs / TEMPLATE).read_text()
        for old, new, reason in (
            ("| A08 | 非流式 usage |", "| A08 | 改名 |", "CASES: design/template drift"),
            ("| AUTH_DB_MAX_INFLIGHT | 16 |", "| AUTH_DB_MAX_INFLIGHT | 4 |", "CONFIG: design/template drift"),
            ("P95 差值 ≤ 30ms", "P95 差值 ≤ 31ms", "PERF: design/template drift"),
            ("A02,A03,A06,A25,A31,A41,A44", "A02,A03,A06,A25,A31,A41", "TRACE: design/template drift"),
        ):
            with self.subTest(reason=reason):
                (self.docs / TEMPLATE).write_text(source)
                self.replace(TEMPLATE, old, new)
                self.run_check(ok=False, reason=reason)

    def test_configuration_failures_even_during_sync(self) -> None:
        source = (self.docs / DESIGN).read_text()
        for name, value, reason in (
            ("PROBE_MIN_SPARE_SLOTS", "0", "invalid positive integer"),
            ("PROBE_MIN_SPARE_SLOTS", "1.5", "invalid positive integer"),
            ("DB_MAX_OVERFLOW", "0", "database pool smaller"),
            ("AUTH_DB_MAX_INFLIGHT", "4", "first-request capacity"),
            ("INGRESS_IP_BURST", "10", "first-request capacity"),
            ("INGRESS_GLOBAL_BURST", "10", "first-request capacity"),
            ("HTTP_MAX_CONNECTIONS", "16", "HTTP pool lacks"),
            ("SETTLEMENT_MAX_ATTEMPTS", "4", "cleanup budget exceeded"),
            ("APP_DRAIN_TIMEOUT", "26 秒", "no Uvicorn shutdown headroom"),
            ("HOST_STOP_TIMEOUT", "35 秒", "no host shutdown headroom"),
            ("SHUTDOWN_SAFETY_MARGIN", "0 秒", "nonpositive timeout/rate/margin"),
            ("SERVICE_UNAVAILABLE_RETRY_AFTER", "0 秒", "invalid Retry-After"),
            ("DB_OPERATION_TIMEOUT", "1 秒", "settlement timeout exceeds"),
        ):
            with self.subTest(config=name, value=value):
                (self.docs / DESIGN).write_text(source)
                before = (self.docs / TEMPLATE).read_bytes()
                self.config(name, value)
                self.run_check("--sync-template", ok=False, reason=reason)
                self.assertEqual((self.docs / TEMPLATE).read_bytes(), before)

    def test_narrative_thresholds_and_conclusion(self) -> None:
        originals = {name: (self.docs / name).read_text() for name in (DESIGN, TEMPLATE)}
        cases = (
            (DESIGN, "清理预算自停止业务读写时开始：", "清理预算自停止业务读写时开始：最多8秒。", "design cleanup"),
            (DESIGN, "正常结算次数/单次时限/退避", "正常结算最多8秒，次数/单次时限/退避", "design settlement"),
            (DESIGN, "停止时序从收到首次", "停止时序限时45s，从收到首次", "design shutdown"),
            (TEMPLATE, "SSE 首事件绝对时间（引用", "SSE 首事件绝对时间（≤800ms，引用", "template performance"),
            (TEMPLATE, "§3核心用例全部通过", "A01–A43核心项通过", "duplicate case range"),
            (TEMPLATE, "性能验收：待验收。", "性能验收：≤20ms时通过。", "template conclusion"),
            (DESIGN, "健康环境在CLEANUP_TIMEOUT内", "健康环境在8s内", "A13: source reference missing"),
        )
        for name, old, new, reason in cases:
            with self.subTest(reason=reason):
                for filename, content in originals.items():
                    (self.docs / filename).write_text(content)
                self.replace(name, old, new)
                self.run_check(ok=False, reason=reason)

    def test_nonexistent_case_and_phase_mismatch(self) -> None:
        self.replace(DESIGN, "A02,A03,A06,A25,A31,A41,A44", "A02,A03,A06,A25,A31,A41,A99")
        self.run_check(ok=False, reason="nonexistent case reference")
        (self.docs / DESIGN).write_bytes((ROOT / "docs" / DESIGN).read_bytes())
        self.replace(DESIGN, "D18–D24 | 7 天", "D18–D24 | 5 天")
        self.run_check(ok=False, reason="phase days/effort mismatch")

    def test_sync_preserves_handwritten_content_and_is_idempotent(self) -> None:
        marker = "\n手写验证标记：保留。\n"
        path = self.docs / TEMPLATE
        path.write_text(path.read_text() + marker)
        self.replace(TEMPLATE, "| A08 | 非流式 usage |", "| A08 | 待修名称 |")
        self.run_check("--sync-template")
        self.assertIn(marker, path.read_text())
        before = path.read_bytes()
        self.run_check("--sync-template")
        self.assertEqual(before, path.read_bytes())

    def test_filled_reports_never_overwritten(self) -> None:
        source = (self.docs / TEMPLATE).read_text()
        for old, new, reason in (
            ("| A08 | 非流式 usage | 未执行 | — |", "| A08 | 非流式 usage | 通过 | report.json |", "acceptance results"),
            ("| 待填写 | 待填写 |", "| 已冻结 | 记录 |", "filled configuration"),
            ("最终上限取实测值 | 待填写 |", "最终上限取实测值 | 已冻结 |", "frozen performance"),
        ):
            with self.subTest(reason=reason):
                (self.docs / TEMPLATE).write_text(source)
                self.replace(TEMPLATE, old, new)
                before = (self.docs / TEMPLATE).read_bytes()
                self.run_check("--sync-template", ok=False, reason=reason)
                self.assertEqual(before, (self.docs / TEMPLATE).read_bytes())

    def test_v14_startup_and_health_budget_failures(self) -> None:
        source = (self.docs / DESIGN).read_text()
        for name, value, reason in (
            ("HEALTH_FAILURE_THRESHOLD", "0", "startup numeric bound"),
            ("PAGINATION_DEFAULT_LIMIT", "0", "startup numeric bound"),
            ("INGRESS_IP_MAX_ENTRIES", "0", "startup numeric bound"),
            ("REQUEST_BODY_MAX_BYTES", "0.5", "startup integer required"),
            ("HEALTH_INTERVAL", "NaN", "invalid numeric literal"),
            ("HEALTH_INTERVAL", "40 秒", "health performance window lacks"),
            ("HEALTH_STALE_AFTER", "32 秒", "stale window too short"),
            ("DB_READY_STALE_AFTER", "6 秒", "stale window too short"),
            ("INFERENCE_PROBE_STALE_AFTER", "320 秒", "stale window too short"),
            ("RECOVERY_GRACE_PERIOD", "1 秒", "recovery grace below cleanup"),
            ("USAGE_MAX_GROUP_DIMENSIONS", "3", "unsupported grouping"),
            ("HEALTH_REACHABILITY_ENABLED", "false", "health gate dependency"),
            ("HEALTH_GATE_ENABLED", "yes", "startup boolean invalid"),
            ("UPSTREAM_STREAM_USAGE_MODE", "automatic", "startup enum invalid"),
            ("ADMIN_API_KEY", "fake-secret-do-not-store", "secret source must contain placeholder"),
        ):
            with self.subTest(config=name):
                (self.docs / DESIGN).write_text(source)
                before = (self.docs / TEMPLATE).read_bytes()
                self.config(name, value)
                self.run_check("--sync-template", ok=False, reason=reason)
                self.assertEqual(before, (self.docs / TEMPLATE).read_bytes())

    def test_v14_contract_references_and_new_narrative_areas(self) -> None:
        source = (self.docs / DESIGN).read_text()
        for old, new, reason in (
            ("DB_MAX_OVERFLOW | 整数且>=0", " | 整数且>=0", "unknown startup field"),
            ("可达性探测周期/时限引用", "可达性探测每30秒一次，周期/时限引用", "health schedule"),
            ("A15 | 全局并发限制 | limit-isolation", "A15 | 全局并发限制 | default-protection", "A15: v1.4"),
            ("started_at=finished_at=updated_at", "started_at=NULL", "rejected time contract missing"),
            ("C0<GLOBAL_MAX_CONCURRENCY", "C0任意", "cold pool criterion missing"),
            ("SELECT pg_sleep(:delay)", "Python sleep代替SQL", "real DB injection missing"),
        ):
            with self.subTest(reason=reason):
                (self.docs / DESIGN).write_text(source)
                self.replace(DESIGN, old, new)
                self.run_check(ok=False, reason=reason)

    def test_secret_source_column_cannot_be_overwritten(self) -> None:
        self.replace(TEMPLATE, "仅记录配置来源（不填值）", "已记录环境变量DATABASE_URL的来源")
        before = (self.docs / TEMPLATE).read_bytes()
        self.run_check("--sync-template", ok=False, reason="filled configuration")
        self.assertEqual(before, (self.docs / TEMPLATE).read_bytes())

    def test_v15_profile_and_contract_omissions(self) -> None:
        originals = {name: (self.docs / name).read_text() for name in (DESIGN, TEMPLATE)}
        for filename, old, new, reason in (
            (TEMPLATE, "functional-paced / limit-isolation / 实验profile", "functional-paced / 实验profile", "template profile list missing"),
            (DESIGN, "A24 | 推理探测隔离 | limit-isolation", "A24 | 推理探测隔离 | 专项", "A24 profile missing"),
            (DESIGN, "| UPSTREAM_HEALTH_PATH | 启用时P0必填，禁用时可空 |", "| UPSTREAM_HEALTH_PATH | P0 必填 |", "health path candidate"),
            (DESIGN, "健康路径可达性启用时必填，禁用时可空；其余字段必填", "健康路径仅启用时必填；其余字段必填", "health path startup rule"),
            (DESIGN, "若睡眠未超时则继续原鉴权查询；本用例预期在睡眠期间触发超时", "之后仍调用原鉴权查询", "sleep timeout path ambiguous"),
            (DESIGN, "及response_status_code/upstream_status_code/error_code与scope组合", "及HTTP字段", "A20 HTTP contract assertion"),
            (DESIGN, "ck_rejected_http_contract", "待补充CHECK", "rejected HTTP constraint missing"),
            (DESIGN, "桶容量专项另用§14.1.3实验profile", "桶容量专项使用默认profile", "A41 capacity profile/source reference"),
            (DESIGN, "客户端显式绑定local_address", "客户端伪造XFF", "A41 source/capacity procedure missing"),
        ):
            with self.subTest(reason=reason):
                for name, text in originals.items():
                    (self.docs / name).write_text(text)
                self.replace(filename, old, new)
                self.run_check("--sync-template", ok=False, reason=reason)

    def edit_work(self, work_id: str, column: int, value: str) -> None:
        path = self.docs / PLAN
        lines = path.read_text().splitlines()
        indices = [i for i, line in enumerate(lines) if line.startswith(f"| {work_id} |")]
        self.assertEqual(len(indices), 1)
        index = indices[0]
        cells = [cell.strip() for cell in lines[index].strip("|").split("|")]
        cells[column] = value
        lines[index] = "| " + " | ".join(cells) + " |"
        path.write_text("\n".join(lines) + "\n")

    def test_plan_effort_and_dependency_failures(self) -> None:
        original = (self.docs / PLAN).read_text()
        for work_id, column, value, reason in (
            ("WP-QA-13", 6, "0.25", "phase work sum mismatch"),
            ("WP-QA-13", 6, "0", "invalid work effort"),
            ("WP-QA-13", 6, "NaN", "invalid work effort"),
            ("WP-ING-01", 4, "WP-ING-01", "self dependency"),
            ("WP-ING-01", 4, "WP-MISSING-01", "unknown dependency"),
            ("WP-ING-01", 4, "WP-QA-13", "future-phase dependency"),
            ("WP-ING-01", 4, "WP-ING-02", "dependency cycle"),
            ("WP-ENG-02", 4, "WP-QA-01", "work order violates dependency"),
            ("WP-QA-13", 5, "A99", "invalid work case reference"),
            ("WP-QA-13", 1, "P8", "unknown work phase"),
        ):
            with self.subTest(work=work_id, value=value):
                (self.docs / PLAN).write_text(original)
                self.edit_work(work_id, column, value)
                before = (self.docs / TEMPLATE).read_bytes()
                self.run_check("--sync-template", ok=False, reason=reason)
                self.assertEqual(before, (self.docs / TEMPLATE).read_bytes())

    def test_plan_schedule_and_ownership_failures(self) -> None:
        original = (self.docs / PLAN).read_text()
        for old, new, reason in (
            ("| P6 | D32–D36 | 5 |", "| P6 | D32–D36 | 1 |", "plan/design phase schedule mismatch"),
            ("| A44 | WP-DB-02 | WP-QA-06 |", "| A44 | WP-DB-02 | WP-MISSING-01 |", "unknown plan case owner"),
            ("| A44 | WP-DB-02 | WP-QA-06 |", "| A44 | WP-DB-02 | WP-OPS-03 |", "owner lacks associated case"),
            ("| A44 | WP-DB-02 | WP-QA-06 |", "| A44 | WP-QA-06 | WP-DB-02 |", "verification lacks implementation dependency"),
            ("| A44 | WP-DB-02 | WP-QA-06 |\n", "", "plan case ownership coverage mismatch"),
            ("D37–D42", "D38–D43", "plan contingency/total mismatch"),
            ("项目开发详细设计书-v1.6.md)", "项目开发详细设计书-v1.5.md)", "plan baseline links mismatch"),
        ):
            with self.subTest(reason=reason):
                (self.docs / PLAN).write_text(original)
                self.replace(PLAN, old, new)
                self.run_check(ok=False, reason=reason)

    def test_v16_preserves_acceptance_contract_and_plan_on_sync(self) -> None:
        from check_docs import block
        old = (self.docs / "AI-Gateway-项目开发详细设计书-v1.5.md").read_text()
        new = (self.docs / DESIGN).read_text()
        for name in ("CONFIG", "PERF", "CASES", "TRACE", "VALIDATION"):
            self.assertEqual(block(old, name), block(new, name), name)
        before = (self.docs / PLAN).read_bytes()
        self.run_check("--sync-template")
        self.assertEqual(before, (self.docs / PLAN).read_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
