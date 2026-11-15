# AI Gateway

当前已完成非流式切片并实现**管理闭环**：管理员建课题、发Key、查询/修改/禁用、原子审计，以及健康检查、单上游转发、请求登记与幂等结算。管理闭环按代码、文档、证据拆分交付，提交版本见[证据索引](docs/P1-管理闭环-证据索引-20261115.json)。开发使用DeepSeek和专用测试库。P0/M0、完整P1/P2尚未签收，现场验证继续待办。

EX-P0-DB-01数据库基础本地范围已验证，按代码、文档、证据拆分交付；提交与验证见[数据库证据索引](docs/P1-数据库基础收口-证据索引-20261115.json)，状态见[数据库基础记录](docs/P1-数据库基础收口-执行记录.md)。上列管理索引为9dbd62e历史快照。

## 环境与检查

Python >=3.12.3,<3.13，uv 0.13.8，运行/开发依赖已锁定。真实秘密只在被忽略的本地环境文件中。

```bash
.tools/bin/uv sync --locked --python .venv/bin/python --no-python-downloads --cache-dir .tools/cache
.venv/bin/python -m app.config_check --profile local-development
.venv/bin/python scripts/check_quality.py
```

完整质量入口运行283项本地pytest、Ruff、mypy及14组文档测试，不连接真实数据库或外部上游。传输测试会启动127.0.0.1临时TCP/TLS监听器；需要允许回环连接，并提供本机openssl命令生成一次性测试证书。证书/私钥仅写测试临时目录。Ubuntu离线安装、最终平台资源与上游能力不能由macOS本地结果替代。

## 配置和迁移边界

`app/core/settings.py` 是唯一80项运行schema；本切片未修改字段、默认值或校验。`read_environment_file` 是运行/迁移共用的公开解析入口。默认development/test读取`.env`；company显式读取`.env.company`，文件缺失/非法时失败，不跨环境补齐。进程同名变量可覆盖文件，部署必须清理残留变量。

唯一配置CLI为 `python -m app.config_check --profile local-development|company`；不指定profile只检查通用配置。旧 `scripts/check_development.py` 仅为兼容wrapper。公司profile可验证运行配置，但**company在线与离线迁移均硬阻断**。通用运行加载器不写死服务地址、库名或账号；开发profile限定DeepSeek和测试身份。

运行文件仅接受规范字段及APP_ENV/MIGRATION_DATABASE_URL；TEST_*、PRODUCTION_*、AI_FAST_*别名已移除。迁移连接与运行连接必须同host/port/database、不同账号；真实连接后再次核对声明身份。秘密不进入日志或提交。

本轮测试库已从`0001_nonstream_slice`增量升级至`0002_admin_management`，包含projects、api_keys、request_logs、health_checks、admin_audit_logs五张业务表及Alembic版本表。health_checks仅结构，尚无探测任务。运行角色不能DDL或写版本表。允许本轮新增upgrade，不允许在线downgrade或清空数据库；正式库不在授权内。

## 本地运行

本切片未实现后台上游探测，必须显式关闭现有三个开关，其他配置保持原值。单worker运行，不启用reload或代理头解析：

```bash
HEALTH_REACHABILITY_ENABLED=false HEALTH_GATE_ENABLED=false INFERENCE_PROBE_ENABLED=false \
  .venv/bin/python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --workers 1 --loop asyncio --no-proxy-headers
```

- `GET /health/live`：进程可响应。
- `GET /health/ready`：数据库/迁移缓存正常且未过期；不代表上游健康。
- `POST /v1/chat/completions`：课题Bearer Key、允许的model、文本messages，stream必须省略或false。管理员Key不能用于推理。

管理员使用独立Bearer Key访问POST/GET `/admin/projects`、PATCH `/admin/projects/{id}`、POST/GET `/admin/projects/{id}/keys`、PATCH `/admin/keys/{id}`。创建Key只在201响应返回一次明文；PATCH is_active=false禁用旧Key。轮换按发新Key、切换客户端、禁用旧Key执行。课题Key可访问 `/v1/models` 和推理接口，不能调用管理接口。

管理列表返回data/next_cursor；审计只写入数据库，审计查询API仍留待P5。以下验证脚本仅生成临时课题/Key，结束后清理自己的数据，不留下可复用测试密钥。

## 端到端复验（显式联网）

```bash
# 测试库 + 模拟上游，校验鉴权、登记失败、幂等、超时与真实DB对账
.venv/bin/python scripts/verify_nonstream_slice.py --report .tools/p1-slice-new-run.json
# 管理闭环、审计CHECK回滚及关闭/回滚卡住故障，无真实模型请求
.venv/bin/python scripts/verify_admin_slice.py --report .tools/p1-admin-new-run.json
# 管理脚本仅从已知0001升级至0002时需要已批准的 --migrate；旧非流脚本加 --deepseek 才调用真实上游
```

报告路径必须未存在。脚本先核对测试库身份及预期表/revision，遇非预期状态停止；只清理本轮夹具数据，不清库、不降级。首次真实DeepSeek已验证35输入+8输出=43 Token，响应与数据库一致。

切片未包含SSE、多模型、审计查询、统计、入口桶、上游探测、unknown恢复及真实TCP断连监督器，不能据此部署生产或签收全量A用例。

最新管理记录见 [管理闭环执行记录](docs/P1-管理闭环-执行记录.md)；已提交的上一批见 [纵向切片执行记录](docs/P1-首条非流式纵向切片-执行记录.md)，历史基座见 [P1基座记录](docs/P1-本地基座执行记录.md)，授权与P0现场待办见 [例外记录](docs/P0-延期与P1例外记录.md)。P0交接物用 `python scripts/build_p0_handoff.py --check` 验证，依赖本机忽略的证据/wheels，不是应用发布包。

上一批非流式代码、文档与证据已分别提交；该历史版本的标识、源文件哈希及质量证据见 [非流式切片证据索引](docs/P1-非流式切片-证据索引-20261115.json)。

数据库基础隔离复验（显式创建随机命名临时库及专属角色，结束仅删除本轮资源；使用本地`.env.db-admin`，不修改现有测试库）：

```bash
.venv/bin/python scripts/verify_database_foundation.py --create-disposable --report .tools/p1-db-new-run.json
```

该夹具验证同一PostgreSQL集群上的空库重建；历史0001引用预先存在的gateway_test_app角色，不代表全新集群安装已验证。Ubuntu现场及完整生产生命周期另行验证。

隔离夹具会在DDL前将资源计划写入`.tools/db-disposable/`（目录0700、journal 0600，不含密码）；角色创建与归属标记同事务，建库确认丢失时以已标记的专属owner核验归属。清理前先有界关闭创建连接，再使用新连接按journal中PID及backend_start确认原后台会话已退出，随后比对集群标识、角色标记及数据库owner。仅socket关闭不能认定DDL结束；后台仍存在或归属不符时拒绝删除及标记已清理。异常退出后使用对应journal恢复，禁止按前缀批量删库：

```bash
.venv/bin/python scripts/verify_database_foundation.py --recover-journal .tools/db-disposable/gw_verify_<本轮标识>.json --report .tools/p1-db-recovery-new.json
```

报告必须使用新路径。恢复仅处理journal中精确列出的资源，可重复执行；未清理成功的journal必须保留并复核，不能把脚本退出当作资源已回收。

新journal为v2并记录创建会话身份；旧v1缺少该身份，恢复命令会拒绝自动回收，需要人工核对，不通过猜测PID或仅查询资源暂时不存在来补写已清理。
