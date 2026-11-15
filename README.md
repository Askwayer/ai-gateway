# AI Gateway

当前是获批例外范围内的 **P1 本地基座**：Git/质量入口、80 项配置模型、空 Alembic 迁移骨架。没有业务表、HTTP 服务、鉴权或转发实现。P0/M0 未签收，不代表 P1/M1 或 A 验收通过。

## 本地环境

Python >=3.12.3,<3.13，依赖使用 uv 0.13.8 锁定。主项目运行依赖沿用 P0 候选精确版本；新增开发工具也记录在 uv.lock。项目不再依靠 `.tools/p0-candidate-venv` 运行 P1。

```bash
.tools/bin/uv sync --locked --python .venv/bin/python --no-python-downloads --cache-dir .tools/cache
.venv/bin/python -m app.config_check
.venv/bin/python scripts/check_quality.py
```

新检出仓库需先在批准的环境准备 Python 3.12 和固定版本 uv；`.tools`、虚拟环境和秘密不会进入 Git。Ubuntu 的系统 Python/venv 和离线安装仍按 P0 现场补验，不能以当前 macOS 安装成功代替。

`requirements.txt` 是主项目无开发组的带哈希导出；开发工具由 uv.lock 管理。P0 的 `p0/dependencies` 与 26 个 wheels 保留为当时运行依赖候选证据，不是新增开发工具的离线包，最终交付需重新验证锁/包闭包。

## 环境选择与配置

默认 `APP_ENV=development`，仅读取仓库根目录 `.env`。开发和 test 均强制使用 `ai_gateway_test` / `gateway_test_app` 及 `https://api.deepseek.com`。公司环境必须显式选择：

```bash
APP_ENV=company APP_ENV_FILE=.env.company .venv/bin/python -m app.config_check
```

该命令只校验配置，不联网。公司环境强制使用已选定 Fast 地址与 `ai_gateway` / `gateway_app`；缺文件、缺秘密、缺必要健康路径等都会失败，绝不加载开发 `.env` 补齐。现有公司文件的健康路径仍未实测/填写，因此目前失败是预期结果，不能擅自填入猜测值让它变绿。

- 选择器只来自进程环境或明确 CLI 参数；环境文件不能切换自身环境。相互冲突的参数会被拒绝。
- 只读取选定的一个文件，随后由同名进程环境变量覆盖；不会隐式读取系统中其他 `.env`，不执行 shell 或变量插值。公司环境若残留开发数据库/上游变量，会被环境边界检查拒绝；部署仍应清理不相关的环境变量。
- `.env` 中允许已有迁移/TEST/PRODUCTION/AI_FAST 辅助项，它们不是 80 项运行字段，也不会自动回退成规范上游参数。文件中的未知拼写、重复项和无值项拒绝；普通系统 PATH 等进程变量忽略。
- 布尔值仅 `true` / `false`；整数不接受小数或布尔；数值拒绝 NaN/Infinity。列表用 JSON 数组，例 `ALLOWED_MODELS=["deepseek-v4-flash"]`、`TRUSTED_PROXY_CIDRS=[]`。
- `UPSTREAM_CA_BUNDLE=system_trust_store` 为系统 CA；指定文件时相对路径以所选环境文件目录解析，必须能加载为有效 CA。时区只从锁定 Python tzdata 包读取。
- DATABASE_URL、ADMIN_API_KEY、UPSTREAM_API_KEY 使用 SecretStr，CLI 只显示字段/规则与状态；不要在日志里序列化原始配置输入或 Pydantic 的完整 errors()。

已实现结构/交叉预算校验；NTP、实际连接额度、最终上游协议、宿主停止时限、生产启动器单 worker 等仍需现场或后续模块验证。`app.config_check` 通过不等于服务 ready 或部署验收通过。

## 迁移骨架

`app.db.base.Base` 只有命名约定和空 metadata；`migrations/versions` 没有 revision。Alembic 从 MIGRATION_DATABASE_URL 取独立凭证，使用异步 SQLAlchemy / NullPool；不在 alembic.ini 写 URL。

```bash
.venv/bin/python -m alembic heads
.venv/bin/python -m alembic history
.venv/bin/python -m alembic upgrade head --sql
.venv/bin/python -m alembic current
```

前三项不需要连接 DB；`current` 才读取测试库状态。当前不执行在线 upgrade/stamp/autogenerate，不创建 Alembic 版本表或业务表。未来迁移需使用对应迁移角色，首版业务迁移还需从运行角色撤销 alembic_version 写权限。

迁移环境强制限 development/test、库名 ai_gateway_test、账号 gateway_test_migrator，并与运行连接的 host/port/database 一致；实际连接后再查询 current_database/current_user 校验身份。company 迁移即使离线 SQL 模式也会拒绝，不能用切换配置绕过本次授权范围。

## 质量入口与证据

`scripts/check_quality.py` 串行执行：Git/秘密排除检查、Ruff lint/format、mypy strict、配置/环境隔离/迁移骨架 pytest、设计/模板/计划校验、既有文档负例测试、P0 配置候选一致性检查。正常质量入口不连接外部服务、不读取真实上游、不执行数据库 DDL。

单独校验交接物：`.venv/bin/python scripts/build_p0_handoff.py --check`。这依赖被忽略的本机证据/wheels，不属于干净 Git 检出的必需质量命令。交接包是 P0 现场材料，不是 P1 应用发布包。

进度及验证见 [P1 本地基座执行记录](docs/P1-本地基座执行记录.md)。当前例外不扩展到其他业务功能；P0 现场待办见 [延期与例外记录](docs/P0-延期与P1例外记录.md)。
