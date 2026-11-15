# P0 候选依赖预演

本目录是独立候选项目，尚未晋升为主项目依赖，也不代表 P0-05 完成。工具为 uv 0.13.8；Python 范围 >=3.12.3,<3.13。精确版本由 uv.lock / requirements.txt 定义，禁止离线现场重新解析版本。

## 已做与待做

- 已在 macOS arm64 的独立 `.tools/p0-candidate-venv` 安装候选依赖并通过导入/进程内 HTTP 检查。
- Linux CPython 3.12 x86_64 wheels 单独收集在 `.tools/p0-wheelhouse-linux-x86_64`；不混入 macOS wheel/虚拟环境。
- Ubuntu 现场断网安装、glibc/系统 Python/venv 包、依赖导入与实际直连 PG 尚待验证。工具检查不证明流式、信号、代理或数据库事务等业务兼容性。
- 主项目已在获批P1本地基座例外中采用这些精确运行依赖版本，并单独锁定开发工具；本目录/包保留为P0候选现场验证材料，不含新增开发工具。P0现场验证未因此通过，最终离线发布应以届时主项目锁重建并验证。

## 联网构建机复现

从仓库根目录运行。首次选版使用 uv lock；后续使用 --locked，升级候选必须重新验证和生成包。

```bash
.tools/bin/uv lock --project p0/dependencies --python .venv/bin/python --no-python-downloads --cache-dir .tools/cache --default-index https://pypi.org/simple
.tools/bin/uv export --project p0/dependencies --format requirements.txt --no-dev --no-emit-project --locked --python .venv/bin/python --no-python-downloads --cache-dir .tools/cache --offline --quiet -o p0/dependencies/requirements.txt
UV_PROJECT_ENVIRONMENT="$PWD/.tools/p0-candidate-venv" .tools/bin/uv sync --project p0/dependencies --locked --python .venv/bin/python --no-python-downloads --cache-dir .tools/cache
.tools/p0-candidate-venv/bin/python scripts/p0_verify_dependencies.py
.venv/bin/python -m pip download --index-url https://pypi.org/simple --require-hashes --only-binary=:all: --platform manylinux_2_28_x86_64 --platform manylinux_2_17_x86_64 --platform manylinux2014_x86_64 --implementation cp --python-version 3.12 --abi cp312 --dest .tools/p0-wheelhouse-linux-x86_64 -r p0/dependencies/requirements.txt --disable-pip-version-check
```

收集包时不能仅指定 manylinux2014/2_17：本锁的 asyncpg 0.32.0 提供 cp312 manylinux_2_28_x86_64 wheel。目标 libc 是否支持须以目标 host 报告和实际安装为准。跨平台下载不是执行验证；当前清单无环境 marker，后续若引入平台条件依赖，必须重新审查目标依赖闭包，不能照搬开发机解析结果。

## Ubuntu 现场断网复现

候选离线包根目录含 `p0/dependencies/`、`scripts/`、`wheelhouse/`、`SHA256SUMS`。包不含 `.env`、用户数据库密码、私钥、macOS 工具链或系统 deb 包。

先在已准备 Ubuntu 24.04 x86_64、系统 Python 3.12、python3.12-venv 的目标机解包；记录断公网方式（保留必要内网），然后在解包根目录执行。若 venv 前置条件不满足，先由现场按批准的离线系统包源补齐，不在此脚本自动 apt 联网。

```bash
set -e
set -C
umask 077
sha256sum -c SHA256SUMS
mkdir -p .tools/p0-evidence
/usr/bin/python3 scripts/p0_preflight.py host --require-target --output .tools/p0-evidence/target-host.json
test ! -e .tools/p0-offline-venv
/usr/bin/python3 -m venv .tools/p0-offline-venv
.tools/p0-offline-venv/bin/python -m pip --isolated install --no-index --find-links wheelhouse --require-hashes --only-binary=:all: --disable-pip-version-check -r p0/dependencies/requirements.txt
.tools/p0-offline-venv/bin/python -m pip --isolated check > .tools/p0-evidence/pip-check.txt
.tools/p0-offline-venv/bin/python scripts/p0_verify_dependencies.py > .tools/p0-evidence/dependency-smoke.json
```

同目录复测会拒绝覆盖；使用新的解包/证据目录，不删除前次失败证据。保存安装 stdout/stderr、命令退出码、host/依赖 JSON、断公网方法、候选包 SHA-256、执行时间和执行人。之后使用专用数据库凭证从目标机执行只读 DB 复核；完整迁移和网关调用属于后续阶段。

## 固定版本发现与后续责任

- FastAPI 0.163.0 使用 Starlette 1.10.0，并包含 opentelemetry-api 依赖。已读取安装包 applications.py 与 telemetry/_runtime.py：自动配置可受环境变量启用。为落实设计的离线运行约束，P1 应用工厂须显式设 `telemetry={"tracing": False, "metrics": False, "logs": False, "operation_spans": False, "auto_configure": False}`；预演脚本已如此设置。不安装 telemetry exporter/SDK extras。
- Uvicorn 0.54.0 的单 worker / proxy_headers=False 配置构造已验证；P3 仍需针对该版本验证真实信号和停止时序，不能用构造配置通过替代。
- SQLAlchemy 2.0.57 / asyncpg 0.32.0 目前仅完成适配器构造与 asyncpg 只读连接分别验证；异步事务、连接池、超时/取消和 SQLAlchemy 实际 PG 联调仍归后续实现。
- tzdata 2026.6 已验证禁用系统 TZPATH 后可加载 UTC、Asia/Shanghai；P1 需在应用生命周期正式固定来源。

以上为候选发现，未新增配置项，也未修改 80 项配置和验收判据。
