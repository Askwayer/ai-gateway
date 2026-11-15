# 课题级 AI Gateway 项目开发详细设计书与验收基线

| 项目 | 内容 |
| --- | --- |
| 文档版本 | v1.6 |
| 编写日期 | 2026-11-15 |
| 适用对象 | 开发人员、部署运维人员、课题管理员、验收人员 |
| 技术方案 | FastAPI + SQLAlchemy 异步 ORM + asyncpg + PostgreSQL + Alembic |
| 部署场景 | 内网离线、小团队、单个本地 OpenAI 兼容推理服务 |
| 最终部署系统 | Ubuntu 24.04 LTS（用户已确认）；用户提供架构为 x86，暂按 x86_64/amd64 配置并由部署脚本校验；资源及进程管理方式待确认，尚未现场验证 |
| 当前状态 | 开发前设计稿；不是已实现功能或已完成测试的声明 |
| 修订来源 | 同步开发执行计划v1.1：补任务闭环、去重和依赖；36人日加6天缓冲；业务/配置/验收契约保持v1.5 |
| 工期批准 | 项目发起人（本会话用户）于2026-11-15明确批准：36人日基础工作量 + 最多6天风险缓冲，单人串行36～42工作日，外部等待另计；取代旧30～36估算 |
| M0前例外 | 同一批准人于2026-11-15批准例外并另行下达启动指令；仅实施P1本地基座，P0/M0未签收、完整P1/M1未完成；范围见 [延期与例外记录](./P0-延期与P1例外记录.md) |

## 1. 项目目标与验收原则

### 1.1 项目目标

建设一个位于课题调用方与本地大模型服务之间的轻量网关。管理员按课题分发独立 API Key，网关验证身份、限制并发、代理请求、采集上游报告的 Token 用量，并提供课题级查询和服务健康状态。

最终应形成以下闭环：

> 创建课题 → 发放 Key → 调用模型 → 查看进行中的请求 → 记录调用结果与 Token → 按课题查询 → 禁用或轮换 Key。

模型推理继续由上游负责；网关负责身份、请求入口和可解释的使用记录，不承诺提升模型生成速度。

### 1.2 三类指标的区别

1. **功能验收基线**：本文件中标为 A 编号的核心用例，必须全部通过。
2. **性能候选目标**：第 15 节给出可测量的初始目标。阶段 P0 冻结测试环境与最终目标后才成为性能验收基线；现在不是性能保证。
3. **预期内限制**：第 16 节明确的限制不构成缺陷，但必须能在接口或验收报告中看到对应状态，不能默默丢弃数据或将未知值当作零。

本项目的计量准确性定义为“与上游返回的合法 usage 数值一致”，不等同于独立验证模型实际计算量或建立商业计费账本。

### 1.3 默认范围假设

沿用原设计书的约束：首版无前端、无 Redis、无容器依赖、无多上游路由。新增并发检测/限制和上游健康检测。允许一个上游服务暴露少量模型名称，但不按模型选择不同地址。

假定调用者互相信任程度有限，课题 Key 仅能访问推理接口，不能使用管理接口或查询其他课题记录。管理员统一查询课题统计；首版不建设课题自助登录门户。

数据库服务器、推理服务与网络权限由现有环境提供。阶段 P0 核实环境；任何范围或验收目标调整，都记录在版本修订和验收基线表中。

## 2. 功能范围

### 2.1 首版必须实现

| 编号 | 功能 | 首版行为 |
| --- | --- | --- |
| F01 | 课题管理 | 创建、查询、重命名、启停；使用稳定 ID 关联历史数据 |
| F02 | Key 分发 | 创建多个课题 Key、设置过期时间、禁用；仅创建时返回明文 |
| F03 | Key 鉴权 | 验证 Key 哈希、有效状态、课题状态和有效期 |
| F04 | 非流式代理 | 代理 `/v1/chat/completions`，保留业务响应结构 |
| F05 | SSE 代理 | 逐块转发、不整包缓冲，处理断开、超时和异常结束 |
| F06 | Token 统计 | 记录输入/输出/总 Token 与 usage 状态，未知值保留 NULL |
| F07 | 请求记录 | 课题、Key、模型、结果、耗时、首个有效 SSE 事件时间、追踪 ID |
| F08 | 并发检测 | 查看全局、课题、Key 的在途数与拒绝计数 |
| F09 | 并发限制 | 全局 + 课题 + Key 三层上限；超限立即返回 429，无等待队列 |
| F10 | 健康检测 | 网关存活、数据库就绪、上游可达与可推理状态分开展示 |
| F11 | 用量查询 | 时间范围内按课题/Key/模型/日期汇总，支持分页请求明细 |
| F12 | 管理保护 | 独立管理员凭证；课题 Key 无管理权限 |
| F13 | 离线交付 | 离线依赖包、配置样例、迁移、启动/停止/备份恢复说明 |
| F14 | 验收证据 | 模拟上游、自动化核心用例、压测报告和真实上游联调记录 |

### 2.2 首版不包含

- 多实例协调、多 worker 共享并发计数、跨节点精确限流。
- 多上游负载均衡、自动切换、请求自动重试、供应商协议转换。
- 金额计费、充值、余额、账单、课题业务 TPM/RPM 配额与按月 Token 硬限制；§11.3 的入口防洪泛令牌桶属于基础保护，不是课题计费配额。
- Web 管理界面、SSO、复杂 RBAC、邮件或即时消息告警。
- RAG、向量数据库、Prompt 管理、内容审核。
- `/v1/responses`、Embeddings、音频、图像、文件上传、WebSocket 接口。
- 推理引擎内部 GPU 利用率、KV Cache、真实生成槽位的采集或控制。
- 请求结果缓存、客户端重试去重、断点续传和进程崩溃后的流恢复。

### 2.3 协议兼容范围

首版以文本 Chat Completions 的非流式与 SSE 流式调用为验收核心。对 `messages`、`model`、`stream` 做必要校验；工具调用等 JSON 字段允许透传，但实际能力由上游决定，未列入联调矩阵的能力不宣称已验证。

提供 `/v1/models` 返回配置白名单中的模型元数据，不从未知外部地址动态拉取。未支持的接口返回 404。协议兼容不等于对所有自称 OpenAI 兼容的引擎做无限兼容。

## 3. 系统架构与技术选型

### 3.1 部署关系

```text
课题客户端 / SDK / curl
        │ Authorization: Bearer <课题 Key>
        ▼
FastAPI 网关（单实例、单 Uvicorn worker）
  ├─ 鉴权与课题解析
  ├─ 全局 / 课题 / Key 并发门控
  ├─ 请求登记与结果结算 ────────── PostgreSQL
  ├─ HTTPX 共享连接池 ─────────── 本地 LLM 兼容服务
  ├─ 健康探测任务 ─────────────── 数据库 / 上游
  └─ /admin/* 管理与查询接口
        ▲
管理员脚本 / curl（独立管理员 Key）
```

部署中不要求 Docker、Redis、Kubernetes。离线不代表不需要数据库安装权限、端口开放和进程管理；这些是交付前置条件。

### 3.2 技术组件

| 层级 | 选型 | 用途与约束 |
| --- | --- | --- |
| Python | 3.12 系列，`>=3.12.3,<3.13` | 目标使用 Ubuntu 24.04 系统 Python 创建 venv；P0 记录实际 Ubuntu 包修订号并验证目标平台 wheels |
| API | FastAPI + Uvicorn | 异步 HTTP 服务；生产 `--workers 1`，禁用 reload |
| 参数/配置 | Pydantic + pydantic-settings | 配置启动校验、请求与管理接口 DTO |
| HTTP | HTTPX AsyncClient | lifespan 创建和销毁；连接池共享，上游响应逐请求关闭 |
| ORM | SQLAlchemy 2.x AsyncEngine / AsyncSession | 使用显式异步数据库访问，版本以锁文件为准 |
| 驱动 | asyncpg | 连接串使用 `postgresql+asyncpg://` |
| 数据库 | PostgreSQL | 已有本地预检记录为18.7（不是本次重新联通验证）；P0复核最终版本及兼容性，不将旧候选16/17写成实际版本 |
| 迁移 | Alembic | 审核后执行迁移；不在每次启动时自动修改表结构 |
| 异步清理 | AnyIO / asyncio | 有时限的取消屏蔽、后台任务生命周期与并发锁 |
| 测试 | pytest、pytest-asyncio/AnyIO、HTTPX | 单元测试、真实 PostgreSQL 集成测试、网络 SSE 测试 |
| 质量检查 | Ruff + 类型检查工具 | 格式、静态检查及核心模块类型检查 |

这是为本项目选择的异步组合，不是 FastAPI 官方全栈模板的原样配置。所有版本在 P0 联调后锁定，交付包括依赖锁、离线安装清单与 SHA-256 校验文件。

### 3.3 数据库与 HTTP 生命周期

- AsyncEngine 和 sessionmaker 为进程共享对象；每个独立数据库操作使用自己的 AsyncSession，禁止多个并发任务共享一个 Session。[S1]
- 鉴权、请求登记、结束结算使用短事务；等待上游生成期间不持有数据库事务或占用数据库连接。
- HTTPX 的共享客户端只在应用退出时关闭；每条上游响应必须在正常结束、异常、取消时关闭。使用上下文管理器时，其生命周期必须覆盖流消费全过程。[S2]
- 请求结束清理使用有界取消屏蔽；完成后继续传播取消异常，不把取消转成普通成功。[S3]
- 后台任务统一由 lifespan 管理，退出时停止调度并等待有界清理。

## 4. 模块划分与建议目录

```text
ai-gateway/
├── app/
│   ├── main.py                    # app 工厂与 lifespan
│   ├── server.py                  # 单 worker 启动与停止信号适配
│   ├── api/
│   │   ├── inference.py           # chat/completions、models
│   │   ├── health.py              # live、ready
│   │   └── admin/                 # 课题、Key、用量、运行状态
│   ├── core/                      # 配置、凭证、日志、错误、时钟
│   ├── db/                        # engine、session、模型
│   ├── schemas/                   # 管理 DTO、统计响应
│   ├── repositories/              # SQL 查询与持久化
│   ├── services/
│   │   ├── authentication.py
│   │   ├── key_management.py
│   │   ├── concurrency.py
│   │   ├── accounting.py
│   │   ├── health_monitor.py
│   │   └── usage_query.py
│   └── upstream/
│       ├── client.py              # HTTP 转发、连接和超时
│       └── sse.py                 # 有界增量解析，旁路采集 usage
├── alembic/versions/
├── tests/{unit,integration,e2e}/
├── scripts/                       # 初始化、探测、验收、压测、归档
├── docs/
├── pyproject.toml
├── alembic.ini
├── .env.example
└── README.md
```

目录是实现建议，可在保持模块责任不变的前提下调整。推理路由只编排流程，统计 SQL 不放入路由；HTTP/SSE 资源清理集中在可测试的组件中。

## 5. 核心请求流程与失败语义

### 5.1 非流式调用

1. 分配网关生成的 `request_id`，执行进程/IP 入口保护并限制请求体大小，再解析 JSON。入口拒绝不访问数据库。
2. 验证课题 Key、课题状态、过期时间；验证模型白名单和必要参数。
3. 根据最近的上游健康缓存判断是否允许探测性/普通请求，遵循第 10 节规则。
4. 在同一个进程锁内检查并增加全局、课题、Key 在途数，得到唯一 admission token。
5. 向数据库提交 `started` 请求记录。登记未成功，则释放名额、返回 503，绝不调用上游。
6. 使用配置中的上游地址和上游凭证发送请求。课题 Key 不发给上游。
7. 接收响应并提取合法 usage；非流式响应设置大小限制，避免无限缓冲。
8. 使用独立短事务将记录更新为终态。更新失败时按第 8 节有界重试并保留待核查记录。
9. 返回上游业务响应或网关错误，关闭上游响应并释放名额。

非流式正常路径在返回结果前尝试结算。上游已经成功但结算失败时仍交付成功结果，避免诱导客户端重复生成；此前提交的 `started` 记录保留，运行状态将计量标记为降级。

### 5.2 流式调用

前五步与非流式一致。先完成上游连接并检查响应状态，再向客户端发送 SSE 响应头。

- 上游在 SSE 开始前返回错误：按普通 HTTP 错误处理，不能包装成 200 流。
- 上游成功：原始响应字节逐块转发，同时送入独立的有界 SSE 解析器采集 usage。
- 正常结束需要观察约定的终止标志；首版 Chat Completions 默认 `[DONE]`。其他上游行为必须在 P0 兼容记录中明确，不自动把 EOF 等同于成功。
- 客户端断开、上游断流、超时均进入统一结束处理；关闭上游连接、结算记录、释放并发名额。
- 不缓存整个输出，不按 SSE 事件个数推算 Token。

网关响应设置 `Content-Type: text/event-stream`、`Cache-Control: no-cache`、`X-Accel-Buffering: no`。这些响应头不能代替反向代理配置；若有反向代理，必须验证其缓冲和超时配置。

### 5.3 断开与取消的处理

采用一个覆盖整个推理 ASGI 调用的请求监督器，不依赖 endpoint 返回 StreamingResponse 后才启动的监听。处理方法如下：

1. 请求体有界读取完成后，由监督器独占原始 ASGI `receive`。如读取请求体时已经断开，直接取消，不能启动上游调用。
2. 在同一 AnyIO task group 内启动 disconnect watcher 与工作任务。watcher 等待 `http.disconnect`，收到后取消工作任务的 cancel scope；工作任务覆盖鉴权后处理、上游 `send(..., stream=True)` 等待响应头、读首块及后续 ASGI `send`。
3. 流式返回使用自有受控 ASGI Response；它直接发送 response.start/body，复用监督器，不再启动 Starlette 内置 disconnect listener。普通响应发送也在监督器中完成。未知路由/请求体读取阶段仍由框架正常处理。
4. 工作任务先完成时取消 watcher；断开先发生时停止发送并关闭上游。发送抛出断连类异常也走同一收尾入口；竞争只允许一次状态结算和名额释放。
5. 取消在 AnyIO 有界 shield 中清理，完成后重新传播取消；不能吞掉取消并返回 completed。不得同时调用 `request.is_disconnected()` 或让另一个组件读取同一 receive。[S3]

监督器由覆盖响应发送的自有 ASGI 组件实现，不使用生命周期已经结束的 endpoint 局部 task group 把生成器交给外部消费。P3 用真实 TCP 分别测试等待响应头、等待首事件、流中、慢客户端、发送异常和正常结束；内存 ASGI 测试不足以验证断开。

清理预算自停止业务读写时开始：上游关闭受 `UPSTREAM_CLOSE_TIMEOUT` 限制；结算尝试次数、单次时限、退避分别引用 `SETTLEMENT_MAX_ATTEMPTS`、`SETTLEMENT_ATTEMPT_TIMEOUT`、`SETTLEMENT_RETRY_BACKOFF`（单次含取连接、事务与回滚），整体受 `CLEANUP_TIMEOUT` 单调截止时间约束。子预算总和及余量按 §11.1 公式校验。每一步取自身时限与剩余总预算的较小值，预算不足则跳过重试。外层 finally 执行无 I/O 的一次性名额释放，数据库失败不能阻止释放。[S2][S3]

A13 的 `CLEANUP_TIMEOUT` 目标适用于健康验收主机，不是对操作系统完全失去调度或进程被 SIGKILL 的实时保证。发现不可取消驱动或关闭超时需标记降级并丢弃连接，不得无限等待。

首条非流式切片的传输实现补充：仅包装Response的关闭不足以约束httpcore在读异常/取消时自行执行的关闭。默认HTTP/1.1传输采用底层TCP abort处置：需要物理关闭时直接调用asyncio Transport.abort，不等待异步关闭或TLS close_notify，让httpcore继续移除请求及失效连接；健康keep-alive仍复用。该策略不新增预算参数，也不重建整个共享池或中断其他请求。实际socket释放由事件循环后续回调完成，回归须同时证明对端EOF/复位、池项清空、结算/名额释放及后续恢复；不能只断言关闭协程退出。HTTPX 0.28.1、httpcore 1.0.9、AnyIO 4.16.0的适配边界集中在app/upstream_transport.py，依赖升级须复验；运行限定asyncio事件循环。此验证不等同于P3客户端断连监督器或目标Ubuntu现场验收。

### 5.4 HTTP 错误与完成结果

| 情况 | 未发送响应头时 | 已开始 SSE 时 | 内部 outcome |
| --- | --- | --- | --- |
| 凭证缺失/无效/过期/禁用 | 401 | 不适用 | 不生成课题用量记录 |
| 课题 Key 调管理接口 | 403 | 不适用 | 管理拒绝事件 |
| 参数或模型不允许 | 400；请求体超限 413 | 不适用 | 请求校验拒绝 |
| 超过任一并发上限 | 429 + Retry-After | 不适用 | rejected |
| 上游健康门控拒绝 | 503 | 不适用 | rejected |
| 鉴权/登记数据库不可用 | 503 | 不适用 | 日志与运行计数，不伪造已落库记录 |
| 上游连接/协议错误 | 502 | 中断流，不伪造成功结束标志 | upstream_error |
| 上游等待或总时限超时 | 504 | 中断流 | timeout |
| 上游明确返回 4xx/5xx | 保留状态码，转为统一脱敏错误信封，source=upstream | 若错误发生于流中，记录并终止 | upstream_error |
| 客户端断开 | 无法保证再送达 HTTP 响应 | 停止写入并清理 | client_cancelled |
| 处理完成 | 上游正常状态码 | 正常 SSE 结束 | completed |

所有可构造的 HTTP 错误统一为 `{"error":{"type":"...","code":"...","message":"...","source":"gateway"},"request_id":"..."}`。网关注册 FastAPI RequestValidationError、Starlette HTTPException 和未处理异常处理器：输入校验（包括框架默认 422）统一转成 400，404/405/500 也使用此信封；405 保留 Allow 头。响应模型校验失败是内部 500，不是客户端 400。[S6]

上游 4xx/5xx 保留状态码，但错误体只提取允许的 type/code 与脱敏 message，补充 `source=upstream` 和 `upstream_status`；非 JSON 或不安全错误正文改为通用消息，不直接透传。上游错误的网关 code 固定 `upstream_http_error`，原上游 code 存可选 `upstream_code`。上游成功业务体保持原契约；统一错误信封是明确的错误路径改写。

错误字段契约（均位于 error 对象）：type 为网关稳定类别，取 invalid_request_error、authentication_error、permission_error、conflict_error、rate_limit_error、service_unavailable_error、upstream_error、timeout_error、internal_error；分别对应输入/路由、鉴权、权限、资源冲突409、429、本地503、上游错误、网关504、内部500。上游错误统一 type=upstream_error，原上游 type 写入可选 upstream_type，避免覆盖网关类别。upstream_status 为原上游 HTTP 整数状态码，仅 source=upstream 的 HTTP 错误必填；upstream_code/upstream_type 为可选的脱敏字符串，缺失或不安全则省略。它们只允许 ASCII 字母、数字、下划线、点和连字符，最多64字符；整数code（不含布尔值）可转十进制字符串，其他类型丢弃；message 使用网关模板，不复制任意上游正文。source=gateway 时不生成这三个上游字段。请求记录继续使用 upstream_status_code 存 HTTP 状态，不与响应 error.upstream_status 混淆。

网关入口桶 429 与业务并发 429 使用 `CONCURRENCY_RETRY_AFTER`；auth_capacity_exceeded、admin_capacity_exceeded、database_unavailable、shutting_down、upstream_unhealthy 等可构造的网关 503（含 ready）统一带 `SERVICE_UNAVAILABLE_RETRY_AFTER` 指定的整数秒 Retry-After。auth 容量拒绝归 runtime.ingress，本版没有单独的 ingress 503 错误码。上游 429/503 只按 §6.1 的允许列表保留其 Retry-After，不用网关值覆盖。该头仅建议等待，不保证恢复，也不授权客户端自动重试已产生副作用的请求。

所有到达应用、且尚能发送响应的成功/错误/SSE/429/404/405 响应均包含网关生成的 UUID 头 `X-Request-ID`。覆盖而不信任客户端同名头；上游同名头不得覆盖它。错误另带 `X-Error-Source: gateway|upstream`，与 JSON source 一致。上游 401 因而可与本地凭证错误区分。服务器在进入应用前拒绝的非法 HTTP、连接已断开或已发出的流，不保证追加响应头/错误体。

| HTTP | error.code | source | 场景 |
| --- | --- | --- | --- |
| 400 | invalid_request / invalid_model / query_limit_exceeded | gateway | 输入校验、模型或查询边界 |
| 401 | invalid_api_key | gateway | 缺失、未知、过期、禁用凭证 |
| 403 | permission_denied | gateway | 课题凭证访问管理接口等权限错误 |
| 404 / 405 | not_found / method_not_allowed | gateway | 路由、资源不存在或方法不允许 |
| 409 | project_code_conflict | gateway | 创建课题的code已存在，不自动重试创建 |
| 413 | request_too_large | gateway | 请求体超限 |
| 429 | ingress_rate_limited / concurrency_limit_exceeded | gateway | 入口/IP 保护或业务三层并发限制；带 Retry-After |
| 503 | auth_capacity_exceeded / admin_capacity_exceeded / database_unavailable / shutting_down | gateway | 鉴权/管理门控、数据库错误或排空 |
| 503 | upstream_unhealthy | gateway | 本地健康门控拒绝 |
| 502 | upstream_connection_error / upstream_protocol_error | gateway | 网关访问上游连接/编码/响应格式错误 |
| 504 | upstream_timeout / request_timeout | gateway | 上游等待或总时限 |
| 500 | internal_error | gateway | 未处理异常，外部不返回栈信息 |
| 上游原 4xx/5xx | upstream_http_error | upstream | 上游明确返回错误，非网关鉴权失败 |

错误内容不回显原始验证 input、秘密、内部地址和连接串。请求头/错误格式在 A39 同时检查。

SSE 已发出 200 后不能改成 502/504；因此 `response_status_code=200` 与 `outcome=timeout` 可以同时出现，统计成功率必须使用 outcome，不能只数 HTTP 200。

`completed` 表示网关观察到上游按协议完成且处理链未发现错误，不是客户端已经完整接收或使用结果的证明。网络发送成功也不等于客户端业务已处理成功。首版不实现客户端确认协议。

## 6. API 详细设计

### 6.1 推理与探针接口

| 方法与路径 | 鉴权 | 功能 |
| --- | --- | --- |
| POST `/v1/chat/completions` | 课题 Key | 非流式/流式 Chat Completions |
| GET `/v1/models` | 课题 Key | 配置中的可用模型目录；不是实时健康保证 |
| GET `/health/live` | 无 | 进程可响应；存活返回 200 |
| GET `/health/ready` | 无 | 配置、迁移版本、数据库及关闭状态满足服务条件时 200，否则 503 |

`/health/ready` 不发起模型生成，也不因单次上游推理失败而指示网关进程应重启；它读取后台数据库检查缓存（周期引用DB_READY_CHECK_INTERVAL，过期引用DB_READY_STALE_AFTER），启动未检查、缓存失败/过期或排空均503。因此数据库刚断开到下一次检查之间允许短暂旧状态，不宣称瞬时一致；业务鉴权仍真实查库。上游业务可用性由管理健康接口展示，公开探针只返回最少信息。

上游 URL 固定在配置中；拒绝客户端传入的 `base_url` 等路由控制字段，不跟随重定向。响应 `X-Request-ID` 的语义见 §5.4，适用于本节接口。

请求头允许列表：Content-Type（网关固定 application/json）、Accept（按模式设置）、Authorization（配置的上游凭证或省略）、Accept-Encoding（固定 identity）、X-Request-ID（网关生成）、X-Gateway-Request-Source（网关设置 business/health_probe）。忽略客户端同名来源标记。Host/Content-Length 由 HTTPX 重算，不转发客户端 Cookie、Connection、Transfer-Encoding、Forwarded/X-Forwarded-* 等。

首版采用 identity 编码策略：先检查上游 Content-Encoding 为空或 identity，流消费明确使用 `aiter_raw()`，不调用自动内容解压的 `aiter_bytes()`。若上游无视 identity 返回 gzip/br 等编码，则在发下游响应头前关闭并返回 502 upstream_protocol_error；不把压缩字节喂给 SSE 解析器。非流式也按此策略有界读取原始体。HTTP 传输分块由 HTTPX/ASGI 各自处理，raw 不代表复制 chunked 帧。[S7]

上游响应头允许列表：Content-Type（校验为模式兼容类型）以及错误时合法的 Retry-After（整数 0～3600 秒；其他值丢弃）。不复制 Content-Encoding（identity 已规范化）、Transfer-Encoding、Content-Length、Connection、Keep-Alive、Trailer、Upgrade、Set-Cookie、上游 X-Request-ID；其他头默认剥离。网关自己设置 X-Request-ID、错误来源头和 SSE 的缓存/禁缓冲头。非流式最终长度由框架生成，SSE 不设置 Content-Length。原上游请求标识仅在允许时写内部 upstream_request_id。

Content-Type的具体契约如下；这是首版兼容范围，媒体类型名/参数名不区分大小写，按语法解析而非整串相等比较；重复charset参数视为非法，不任意选择其中一个。[S13]

| 路径 | 接受/处理 | 失败与断言 |
| --- | --- | --- |
| 客户端带JSON体的推理/管理请求 | 单一application/json；允许省略charset或显式UTF-8（含引号/大小写变体），其他语法合法的参数忽略；正文严格UTF-8 JSON | 缺失/重复Content-Type、非法语法、text/plain、application/*+json或非UTF-8 charset：400 invalid_request，计validation，未调用上游；本版不另引入415 |
| 上游成功非流式 | 单一application/json，同样允许可选UTF-8；有界解码JSON且为预期对象，响应输出规范application/json | 头缺失/重复/非法、类型或charset不匹配、JSON格式错误：502 upstream_protocol_error，source=gateway；已有business调用仍计已接纳失败 |
| 上游成功流式 | 单一text/event-stream；可选UTF-8，其他合法参数忽略；下游规范text/event-stream | 相同头异常在下游response.start之前返回502 upstream_protocol_error；200 application/json不冒充SSE；后续流内解析错误按既定流错误/usage规则处理 |
| 上游4xx/5xx | 先按状态走错误路径；仅当头满足JSON契约且有界正文可解析时提取允许字段，否则使用通用脱敏消息 | identity编码且可完整有界读取时保留原HTTP状态、source=upstream、code=upstream_http_error、upstream_status；例如401 text/html不因Content-Type变502；不得转发HTML或原错误正文 |

上游错误的编码/大小/读超时仍受现有安全边界约束，超限/编码非法为502、读取超时为504，source=gateway；不承诺在无法安全读取时保留原错误体。无请求体的GET不要求客户端Content-Type。A31/A39/A40须覆盖表中接受与拒绝分支，并区分“网关准入前未调用”与“上游已返回但协议不符”。

### 6.2 管理接口

所有 `/admin/*` 需要独立管理员 Key。无凭证或未知凭证返回 401；可识别的课题 Key 返回 403。管理员 Key 不作为课题身份调用推理。

| 方法与路径 | 请求主要字段 | 行为 |
| --- | --- | --- |
| POST `/admin/projects` | code、name、max_concurrency | 创建课题，返回 201 |
| GET `/admin/projects` | cursor、limit、active | 分页查询课题 |
| PATCH `/admin/projects/{id}` | name、is_active、max_concurrency | 修改课题；code 和 id 不变 |
| POST `/admin/projects/{id}/keys` | name、expires_at、max_concurrency | 创建 Key，201 响应中仅此一次返回明文 |
| GET `/admin/projects/{id}/keys` | cursor、limit | 只返回 ID、显示前缀、状态和限制等 |
| PATCH `/admin/keys/{id}` | is_active、expires_at、max_concurrency | 启停或修改；不返回明文 |
| GET `/admin/usage` | from、to、project_id、key_id、model、group_by、timezone | 聚合查询 |
| GET `/admin/requests` | from、to、project_id、key_id、outcome、cursor、limit | 请求明细，不含输入/输出正文 |
| GET `/admin/runtime` | 无 | 在途数、上限、拒绝数、结算失败计数、进程启动时间 |
| GET `/admin/health/upstream` | 无 | 最近探测状态、失败原因类别、最后成功时间、数据是否过期 |
| GET `/admin/health/history` | from、to、cursor、limit | 探测历史 |
| GET `/admin/audit-logs` | from、to、action、resource_id、cursor、limit | 只读脱敏管理审计，不含 Key 明文/哈希 |

首版不提供物理删除课题/Key；禁用保留历史关联。Key 轮换采用“发新 Key → 切换客户端 → 禁用旧 Key”，避免引入独立轮换状态机。

管理闭环实现契约：列表返回 `{data: [...], next_cursor: string|null}`，按 `(created_at DESC,id DESC)` 参数化游标分页，读取limit+1判定下一页；limit使用既有默认/最大配置，active仅接受true/false，重复或未知查询参数、非法游标返回400。PATCH必须包含至少一个允许字段，禁止变更id/code/project_id/key_hash；max_concurrency为正INTEGER范围值，不接受布尔/显式NULL。expires_at要求带时区的ISO时间，PATCH中NULL表示清除到期日。查询不返回Key明文或哈希，管理成功响应均带Cache-Control: no-store。

撤销在本闭环中指PATCH旧Key的is_active=false；可按启停接口重新启用，但不会清除已有revoked_at。所有已注册管理路由先执行管理员鉴权依赖，再校验UUID路径及请求参数；管理员身份直接恒定时间校验；其他凭证通过鉴权DB通道仅识别Key哈希，已知Key（包括禁用/过期）访问管理返回403，未知Key为401。管理SQL另占ADMIN_DB_MAX_INFLIGHT通道，容量满立即503 admin_capacity_exceeded并带既有Retry-After；SQL操作与退出清理共享DB_OPERATION_TIMEOUT截止时间，清理同时不超过既有CLEANUP_TIMEOUT；显式调用连接close而不使用会等待屏蔽清理的连接上下文退出。关闭/回滚到期或取消时，对该连接同步硬失效并终止asyncpg连接、归还池记录，再释放通道；不全池dispose，不留后台close任务。清理看门狗与超时共享单调截止时间，在受支持的驱动和健康调度环境下，已获得连接后的SQL/提交及清理使用同一调用截止时间，较短的结算单次/总预算优先，不为清理重新增加完整预算。取消继续传播。提交确认后清理失败也返回503；提交期间断连的结果可能未知，不自动重试写入，调用者需查询确认，503不等同于业务必然回滚。审计action固定project.create/project.update/key.create/key.update，actor=admin，仅写允许字段的before/after值。此处不提前实现P5审计查询或P2入口桶。


创建接口不承诺幂等：客户端超时后不可无条件重试。Key 创建响应丢失时无法找回明文，可查询记录后禁用旧 Key 并重建；不通过日志找回密钥。

### 6.3 请求与响应示例

创建课题：

```json
{"code":"topic-a","name":"课题 A","max_concurrency":4}
```

创建 Key：

```json
{"name":"实验脚本","expires_at":"2027-01-01T00:00:00+08:00","max_concurrency":2}
```

创建成功响应示意，实际密钥由 CSPRNG 生成：

```json
{
  "id":"<uuid>",
  "project_id":"<uuid>",
  "key":"sk-gw-<随机密钥，仅本次显示>",
  "key_prefix":"sk-gw-xxxxxx",
  "is_active":true,
  "max_concurrency":2,
  "expires_at":"2026-12-31T16:00:00Z"
}
```

推理调用：

```json
{
  "model":"local-model",
  "messages":[{"role":"user","content":"你好"}],
  "stream":true,
  "stream_options":{"include_usage":true}
}
```

管理分页默认/上限引用PAGINATION_DEFAULT_LIMIT/PAGINATION_MAX_LIMIT；统计跨度引用USAGE_MAX_RANGE_DAYS。from/to 必须是带时区的 ISO 8601 时间，采用 `[from,to)` 半开区间。内部存 UTC，展示默认 Asia/Shanghai；按日分桶先按指定时区计算日期边界。timezone 只接受 §11.1 的 IANA 白名单，非法值 400。请求明细固定按 (started_at, request_id) 降序，审计按 (created_at,id) 降序；没有可选排序参数。管理查询未知参数（含 sort）返回 400，不静默忽略。

### 6.4 统计响应口径

```json
{
  "from":"2026-11-01T00:00:00+08:00",
  "to":"2026-12-01T00:00:00+08:00",
  "timezone":"Asia/Shanghai",
  "items":[{
    "project_id":"<uuid>",
    "request_count":100,
    "completed_count":92,
    "failed_count":3,
    "cancelled_count":2,
    "pending_count":1,
    "unknown_count":2,
    "rejected_count":5,
    "usage_reported_count":90,
    "usage_partial_count":1,
    "usage_missing_count":6,
    "usage_invalid_count":2,
    "usage_pending_count":1,
    "usage_coverage":0.909091,
    "all_reported_usage":{"request_count":90,"prompt_tokens_sum":12000,"completion_tokens_sum":6000,"total_tokens_sum":18000},
    "completed_usage":{"request_count":88,"prompt_tokens_sum":11800,"completion_tokens_sum":5900,"total_tokens_sum":17700}
  }],
  "as_of":"2026-11-14T01:00:00Z"
}
```

- `request_count` = 已登记且获得并发名额的请求数，包括实际转发前失败的请求；它是网关接纳次数，不等同于上游已执行次数。
- `request_count = completed + failed + cancelled + pending + unknown`；failed 包含 timeout、upstream_error、gateway_error。
- `rejected_count` 独立展示，不计入 request_count。无法鉴权归属的 401、参数校验失败、登记失败不进入课题账表，仅进入全局运行计数/脱敏日志。
- usage 分类计数之和等于 request_count。已终结请求的完整 usage 覆盖率 = reported / (request_count - pending_count)；分母为零返回 NULL。
- `all_reported_usage` 的三个 Token sum 累计所有 `usage_status=reported` 的已接纳请求，包括 cancelled/timeout 等非成功结果；它表示所有已知完整报告消耗。
- `completed_usage` 仅累计 `usage_status=reported AND outcome=completed`，是前者的子集，两者不可相加。各对象的 request_count 为纳入该对象的请求数。partial 数据仅在明细展示，不混入两组 Token sum；A29 使用失败但 reported 的固定数据验证此口径。
- 当没有任何完整 usage 时，Token sum 返回 NULL；合法 usage 的零值必须保留为 0。已知用量不能被描述为包含缺失记录的完整实际消耗。
- 分组维度仅允许 project、key、model、day，最多组合两个，使用白名单构建查询；聚合结果设最大返回行数与截断/分页约定，禁止默默丢组。首版超USAGE_MAX_GROUPS组返回400，提示缩小范围。
- 同一统计响应在同一 SQL 快照中计算各计数和总量；多次查询期间仍有请求完成，数值可以正常变化。

### 6.5 日桶聚合 SQL 方案

选择边界表 range join。应用使用锁定的 tzdata，按请求 timezone 枚举与 [from,to) 相交的本地日期，分别把当天及次日本地午夜转成 UTC；首尾裁剪到 from/to，得到等长数组 day_labels/date[]、starts/timestamptz[]、ends/timestamptz[]。逐行校验 start<end、边界相接且无重叠，并覆盖完整查询区间；按本地日期递增，不能在 UTC 上机械累加24小时。边界行数受 `USAGE_MAX_RANGE_DAYS` 推导的上界约束（本版边界行上限为USAGE_MAX_RANGE_DAYS+2，为首尾部分日和夏令时切换留余量；超限返回400，不截断）；无数据日不补虚拟记录，items 可为空。

以下为 project+day 的 SQL 结构示意，绑定参数使用 SQLAlchemy text/bindparam，不拼接用户输入；所有 §6.4 计数和两组usage的请求数与三字段聚合放在同一SELECT内，示例仅展示部分列：

```sql
WITH bounds AS (
    SELECT * FROM unnest(
        CAST(:day_labels AS date[]),
        CAST(:starts AS timestamptz[]),
        CAST(:ends AS timestamptz[])
    ) AS b(day, utc_start, utc_end)
)
SELECT r.project_id, b.day,
       count(*) FILTER (WHERE r.admitted) AS request_count,
       count(*) FILTER (WHERE NOT r.admitted) AS rejected_count,
       sum(r.total_tokens) FILTER (
           WHERE r.admitted AND r.usage_status = 'reported'
       ) AS all_reported_total_tokens,
       sum(r.total_tokens) FILTER (
           WHERE r.admitted AND r.usage_status = 'reported'
             AND r.outcome = 'completed'
       ) AS completed_total_tokens
FROM request_logs AS r
JOIN bounds AS b ON r.started_at >= b.utc_start AND r.started_at < b.utc_end
WHERE r.started_at >= :from_utc AND r.started_at < :to_utc
GROUP BY r.project_id, b.day
ORDER BY r.project_id, b.day
LIMIT :group_limit_plus_one;
```

可选 project/key/model 等值过滤仅在存在该参数时追加绑定谓词；字段与 GROUP BY 从固定映射选择。没有 day 时不关联边界表。结果读取 `USAGE_MAX_GROUPS+1` 行判溢出，超限返回400；该 LIMIT 不保证数据库只计算这些组，仍受查询硬截止约束。日期标签由应用传入，数据库不另做 AT TIME ZONE/date_trunc，避免第二套时区规则。[S9]

P6 EXPLAIN 矩阵覆盖 project+day、model+day 的边界表 join，以及不带 day 的 key+model；各测有/无维度过滤、UTC/Asia/Shanghai、整日/首尾半日/跨日、稀疏/倾斜数据、冷热缓存。记录实际边界行数、扫描行数/loops、聚合方式、排序/临时文件、BUFFERS、SQL和checkout耗时。A29验证边界互斥及与独立参考聚合一致；不强制 planner 采用某种索引，也不把此示例当作已通过性能实测。

## 7. 数据模型

### 7.1 通用约定

主键采用 UUID；时间采用 `TIMESTAMPTZ`，统一 UTC 存储。Token 和累计耗时采用 BIGINT，非负约束。枚举使用字符串 + CHECK 约束，方便迁移。外键禁止级联删除历史数据。

首版五张业务表：projects、api_keys、request_logs、health_checks、admin_audit_logs；另有 Alembic 版本表。配置中的单上游不单独建资源管理表。

### 7.2 projects

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| id | UUID PK | 稳定课题标识 |
| code | VARCHAR(64) UNIQUE NOT NULL | 稳定业务编号，创建后不可改 |
| name | VARCHAR(128) NOT NULL | 显示名称，可改 |
| is_active | BOOLEAN NOT NULL | 默认 true |
| max_concurrency | INTEGER > 0 | 默认引用DEFAULT_PROJECT_MAX_CONCURRENCY；不允许NULL/负数代表特殊含义 |
| created_at / updated_at | TIMESTAMPTZ | 创建、更新时间 |

课题停用后新鉴权请求拒绝，已接纳请求继续完成。管理变更与鉴权并发时，以该次鉴权查询观察到的已提交状态为准，不追杀已经通过鉴权的请求。

### 7.3 api_keys

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| id | UUID PK | Key 身份 |
| project_id | UUID FK NOT NULL | 课题归属，不允许迁移到其他课题 |
| name | VARCHAR(128) | 用途说明 |
| key_hash | CHAR(64) UNIQUE NOT NULL | 随机 Key 的 SHA-256 十六进制值 |
| key_prefix | VARCHAR(24) | 仅显示少量前缀帮助辨识，不能据此鉴权 |
| is_active | BOOLEAN | 默认 true |
| expires_at | TIMESTAMPTZ NULL | NULL 表示无固定过期日 |
| max_concurrency | INTEGER > 0 | 默认引用DEFAULT_KEY_MAX_CONCURRENCY |
| created_at / updated_at / revoked_at | TIMESTAMPTZ | 审计相关时间，revoked_at 可空 |

索引：key_hash 唯一索引；(project_id, created_at, id) 查询索引。Key 由至少 32 字节密码学随机数生成，不使用课题名称、时间戳或短口令构造。高熵随机 Token 可用 SHA-256 校验；此设计不适用于人类口令。

首版鉴权每次读数据库，不缓存启停/权限状态；简化撤销生效语义。设置最小权限数据库账号，日志和查询响应不返回 key_hash。

### 7.4 request_logs

| 字段组 | 字段 | 说明 |
| --- | --- | --- |
| 身份 | request_id UUID PK、project_id FK、api_key_id FK | request_id 同时用于追踪与幂等结算 |
| 上游 | upstream_id、model、upstream_request_id NULL | upstream_id 首版固定；上游 ID 来自允许读取的响应头 |
| 模式 | is_stream BOOLEAN、admitted BOOLEAN | 是否流式、是否接纳 |
| 时序 | started_at、upstream_started_at NULL、finished_at NULL | started_at为登记时刻；rejected使用本节同一DB语句时刻；上游开始时刻是发送尝试时间，非对方收到证明 |
| 性能 | duration_ms NULL、first_event_ms NULL | 单调时钟计算；首事件不等同于精确首 Token |
| 状态 | outcome、error_code NULL | started/completed/client_cancelled/timeout/upstream_error/gateway_error/unknown/rejected |
| HTTP | upstream_status_code NULL、response_status_code NULL | 与最终 outcome 分离 |
| 用量 | prompt_tokens NULL、completion_tokens NULL、total_tokens NULL | 上游报告值；缺失字段不强行补零 |
| 用量状态 | usage_status、usage_source、usage_missing_reason NULL | 见第 8 节；无 usage 时 source=none |
| 校验 | usage_validation_error NULL | 非负、字段缺失、总量不一致等类别，不存原始正文 |
| 恢复 | process_run_id UUID、settlement_attempts、updated_at | 定位未终结请求和崩溃恢复 |
| 限流 | rejection_scope NULL | global/project/key/upstream_unhealthy；仅持久化这四种值；其他入口拒绝仅运行计数 |

约束：rejected记录admitted=false、usage_status=not_applicable，直接INSERT终态，不经过started。该INSERT显式以同一个数据库statement_timestamp()赋值started_at=finished_at=updated_at；started_at在这里表示“拒绝记录登记时刻”，不是HTTP到达或准入时刻。upstream_started_at、duration_ms、first_event_ms和全部Token列均为NULL，不能用0伪装实际推理耗时。rejection_scope只允许§9.3的四类持久化拒绝。started记录finished_at=NULL、usage_status=pending；其他终态须填finished_at，unknown的finished_at是确认未知的时间，duration_ms仍NULL。

迁移CHECK约束固定rejected的上述空值/相等关系，started_at/updated_at为NOT NULL，finished_at仅started可空；拒绝INSERT失败时不补造记录，按最终原因记runtime计数。拒绝统计、日桶、分页及保留清理统一使用started_at；跨午夜才执行INSERT的拒绝归INSERT所在日期。A20/A29分别验证字段与半开边界；不宣称数据库登记时刻等于内存准入拒绝决定时刻。

rejected的HTTP/错误字段必须与拒绝范围成组匹配，不能只分别检查枚举：

| rejection_scope | response_status_code | error_code | upstream_status_code |
| --- | --- | --- | --- |
| global / project / key | 429 | concurrency_limit_exceeded | NULL |
| upstream_unhealthy | 503 | upstream_unhealthy | NULL |

outcome为NOT NULL；迁移增加名为ck_rejected_http_contract的CHECK：当outcome='rejected'时，upstream_status_code必须IS NULL，且上表组合条件必须IS TRUE。显式IS TRUE避免SQL三值逻辑让response_status_code/error_code/rejection_scope中的NULL通过CHECK。A20验证两类合法INSERT及错误状态/错误码/错配scope/上游状态非空/必填字段NULL等非法INSERT；上述响应状态是网关拟返回值，不证明客户端已接收。auth_capacity_exceeded、admin_capacity_exceeded、database_unavailable和shutting_down等不属于持久化rejected允许值；拒绝日志执行失败仍按既定runtime路径处理，不制造另一条503拒绝账。

索引：

- (project_id, started_at, request_id)：课题统计/明细。
- (api_key_id, started_at, request_id)：Key 查询。
- (started_at, request_id)：全局时间范围和归档。
- (process_run_id, started_at) WHERE outcome='started'：未完成恢复。
- (model, started_at)：支持模型过滤后时间范围扫描；不能保证无 model 过滤的 model+day 聚合走此索引。
- P6 必须保存 `EXPLAIN (ANALYZE, BUFFERS)`：project+day、model+day、key+model，分别有/无过滤，并记录数据倾斜、冷热缓存及索引体积。覆盖大部分表时顺序扫描可能是合理计划，验收看实测时间和资源，不以“用了索引”为通过条件。若超过§15.2“统计查询”冻结目标，先优化查询/索引并复测；仍不满足须走基线变更，不自动免测。

不存 Prompt、模型回答、完整请求体、完整 SSE 或 Authorization。课题历史展示当前名称；历史归属始终按 ID，重命名不会拆分统计。

`duration_ms` 从接纳请求到观察到终止结果计算，不包含之后的结算清理；`first_event_ms` 从同一起点到首个非注释、非空 SSE data 事件计算。两者使用单调时钟，持久化时间戳只用于时间范围查询。首事件可能是角色信息，不能对外标作精确 TTFT。

### 7.5 health_checks

id UUID、upstream_id、probe_kind（reachability/inference）、status（healthy/degraded/unhealthy/skipped）、checked_at、latency_ms、http_status NULL、error_code NULL、model NULL。

索引 (upstream_id, checked_at, id)。只存脱敏错误类别与必要描述；不存探测凭证和完整模型响应。探测用量不计入课题账表，注明实际可能消耗上游推理资源。

### 7.6 admin_audit_logs

id UUID、actor（首版为 admin）、action、resource_type、resource_id、request_id、created_at、changes JSONB。

changes 仅包含允许的变更字段与脱敏前后值，不含 Key 明文/哈希或上游密钥。课题和 Key 的管理变更与审计写入同一个事务；审计失败则变更回滚。

审计索引：(created_at DESC,id DESC)、(action,created_at DESC,id DESC)、(resource_id,created_at DESC,id DESC)。按 (created_at,id) 做参数化降序游标；同时过滤 action/resource_id 时保留剩余谓词并实测计划，首版不增加 changes 的 GIN 索引。A35包含相同created_at跨页、各过滤条件、脱敏与权限检查；P6附审计查询计划和索引体积。

### 7.7 迁移与保留

- 首个迁移建立表、外键、CHECK 与索引；后续变更生成迁移后人工审查，不能把自动生成结果直接视为正确。
- 生产迁移显式运行 `alembic upgrade head`，启动时检查当前 revision 是否符合应用要求。异步环境的 Alembic 接入按官方 cookbook 实现。[S4]
- 请求明细、健康历史和管理审计保留期分别引用REQUEST_LOG_RETENTION_DAYS、HEALTH_LOG_RETENTION_DAYS、AUDIT_LOG_RETENTION_DAYS。
- 首版提供显式归档/清理脚本，默认 dry-run；不自动删除数据。清理前备份，清理后的时间范围不再承诺完整历史汇总，接口返回可用数据保留起点说明。
- 首版不建设额外日报汇总表；大数据量时根据查询性能再引入，避免明细和汇总双写不一致。

## 8. Token 计量、结算与异常恢复

### 8.1 usage 状态

| usage_status | 判定 | 主汇总行为 |
| --- | --- | --- |
| pending | 请求已登记、尚未终结 | 不计 Token；单列进行中 |
| reported | 上游提供完整且合法的 prompt/completion/total 三字段 | 原值计入汇总 |
| partial | 只提供部分合法字段 | 保留已知字段，主汇总不纳入；明细展示 |
| missing | 没有收到可用 usage | 三字段 NULL，记录原因 |
| invalid | 字段类型、非负性、总量或重复终结 usage 校验失败 | 主汇总不纳入，保存合法字段和错误类别 |
| not_applicable | 请求被网关拒绝且未接纳 | 不计请求接纳数及 Token |

`usage_source` 为 upstream_json / upstream_sse / none。禁止以字符数、SSE 事件数或请求 max_tokens 参数代替实际 usage。

`missing_reason` 建议值：upstream_not_provided、client_cancelled_before_usage、upstream_interrupted、timeout_before_usage、process_interrupted、settlement_lost。结果状态与 usage 状态独立：流失败前若已收到可信的完整 usage，仍可保留 reported，但不会因此把请求结果改成 completed。

Token 校验要求整数且非负，不接受布尔值冒充整数。默认校验 total=prompt+completion；若上游的字段语义不同，必须在 P0 明确映射规则并形成测试，不默默修改原始值。细分字段如缓存 Token、reasoning Token 首版不独立累计，避免与总量重复相加。

### 8.2 流式 usage 能力开关

配置 `UPSTREAM_STREAM_USAGE_MODE=include_usage|unsupported`，由 P0 实测决定：

- include_usage：网关在发送上游时合并 `stream_options.include_usage=true`，保留其他 stream_options 字段；这是明确允许的请求体改写。
- unsupported：不向不支持该参数的引擎强加它；客户端显式携带该参数时按已冻结兼容约定处理，不能请求失败后自动重发。
- 无论模式如何，旁路解析实际返回的 usage。正常结束却缺少 usage 记 missing，不伪造 reported。

验收时提供两条路径：模拟上游必须覆盖有 usage/无 usage；真实上游按其已确认能力验收。真实服务不支持 usage 时，“Token 可统计”的交付范围必须写明为非流式和其他实际支持的路径。

### 8.3 SSE 增量解析

- 解析器跨 HTTP chunk 保留未完成行/事件，支持 UTF-8 字节拆分、LF/CRLF、多 data 行、注释和空行事件分隔。
- 不把网络分块等同于 SSE 事件；不把 SSE 事件等同于模型 Token。
- 原始字节转发与解析分离，不为采集 usage 改写正常内容；Content-Length 等不适用头不复制。
- SSE单事件解析缓冲上限引用SSE_EVENT_PARSE_MAX_BYTES。超限后停止该事件的计量解析并记录 invalid，仍在整体安全边界内继续转发；最终也不恢复成“完整计量”。
- `[DONE]` 表示完成信号，若其前发生显式错误事件，则 outcome 保持错误。usage 可位于 choices 为空的终结事件，不能丢弃。
- 默认只接受一次完整终结 usage；相同重复值不重复累计，不同重复值标记 invalid。任何分块都不会触发多次数据库累计。

### 8.4 转发前登记与幂等结束

对每个获得并发名额的请求，先提交一条 started 记录，再开始上游发送。最终更新采用 `WHERE request_id=:id AND outcome='started'` 的条件更新；同一请求重复清理不会生成多条记录，也不会重复累加 Token。

终态结算是更新一条原始记录，聚合查询从原始记录计算，不在请求路径中给累计总表加数。完成请求与重启恢复使用互斥的条件更新语义。

正常结算次数/单次时限/退避引用 §5.3 与 §11.1，共享 `CLEANUP_TIMEOUT` 截止时间，每次尝试前检查剩余时间。通用 `DB_OPERATION_TIMEOUT` 不覆盖更短的专用结算时限。数据库恢复前无法落地的结果只保留 started 和脱敏故障日志，不宣称持久化完成。客户端响应不因已产生的结果结算失败而自动重试上游。

### 8.5 崩溃与数据库故障

| 场景 | 设计行为 | 可保证与不可保证 |
| --- | --- | --- |
| 鉴权前数据库故障 | 拒绝新请求 503 | 不绕过鉴权 |
| 登记提交失败 | 不转发，释放名额，503 | 不产生未登记的正常新上游调用；提交结果不确定时可能留下多余 started 记录 |
| 流进行中数据库故障 | 已接纳流继续，有界尝试结算 | 记录已存在；结束状态/Token 可能无法补全 |
| 进程崩溃或强制终止 | 原 started 留存，重启后恢复为 unknown | 保留接纳痕迹；不恢复完整响应和缺失 Token |
| 上游已生成但网络断开 | 关闭网关连接并记录异常 | 不保证上游计算立即停止或其最终用量可获知 |
| 客户端自行重试 | 视为新 request_id、新调用 | 不提供业务幂等，可能重复消耗 |

每次启动产生 `process_run_id`。运维必须确认旧网关进程已退出后启动唯一实例；启动恢复将旧 run 中的 started 标为 unknown。运行中的结算失败记录由后台扫描任务检查：仅处理本 run、已经不在活动请求注册表中的 started，宽限RECOVERY_GRACE_PERIOD后标记unknown。不得仅以请求年龄判定超时并改写仍活动的记录。

不支持滚动双实例运行；不能在旧进程仍可能处理请求时执行恢复。单 worker/单实例是验收部署条件，并非由进程内锁提供的分布式保证。

## 9. 并发检测与限制

### 9.1 定义

在途请求 = 获得 admission token 后，尚未完成响应处理与有界清理的请求。流式请求保持名额直到流结束/取消并完成清理，不能在返回 StreamingResponse 对象时释放。

初始值分别引用GLOBAL_MAX_CONCURRENCY、DEFAULT_PROJECT_MAX_CONCURRENCY、DEFAULT_KEY_MAX_CONCURRENCY。它们只是保守起点；真实上游容量通过 P0/P6 验证后调整。上限不承诺与上游 GPU 能力一致。

### 9.2 原子准入

1. 每次鉴权读取该课题和 Key 的已提交限制。
2. 获取进程内短时锁，依次检查全局、课题、Key 计数；该锁内不执行数据库、网络或其他长等待。
3. 任一达到上限，所有计数不变，返回拒绝范围；通过则三项一起加一，并登记唯一 token。
4. 最终释放只执行一次，三项一起减一；计数不得出现负数。

拒绝返回 429，`error.code=concurrency_limit_exceeded`，Retry-After引用CONCURRENCY_RETRY_AFTER，仅是建议重试等待，不是可用容量承诺。无队列，避免大量长连接堆积。

超限/健康门控拒绝若身份已明确，尝试写 admitted=false 的 rejected 记录；若数据库不可用，记录失败写入全局运行指标，响应转为 503，避免声称拒绝日志已落库。所有拒绝均不得发送上游业务推理请求；背景健康探测按独立来源计数。

### 9.3 监测输出

`/admin/runtime` 至少返回：

- process_run_id、started_at、uptime_seconds、accepting_requests。
- 全局 active、limit，以及按 project_id、key_id 分组的 active/limit。
- 自本次启动以来的 admitted_total、completed_total、cancelled_total、failed_total。
- `rejected_total`（对象）固定包含 global、project、key、upstream_unhealthy、database、authentication、validation、ingress、permission、routing、shutdown。单请求按最终拒绝原因只增加一类；原拟全局拒绝但拒绝日志DB操作失败改503只计database；尚未执行DB就因鉴权/拒绝通道满改auth_capacity_exceeded则只计ingress。
- global/project/key/upstream_unhealthy 与 request_logs.rejection_scope 同名，对已成功落库拒绝可对账。其余分类仅内存/脱敏日志，不是 request_logs 的枚举；database 只指接纳登记前失败，结算失败另计 accounting_write_failures_total。
- authentication 对应网关 401（不含上游 401），validation 对应 400/413，ingress 对应入口桶或鉴权并发门控，permission 对应 403，routing 对应 404/405，shutdown 对应排空 503。上游 4xx/5xx 属已接纳请求 failed_total，不进 rejected_total。
- `auth_inflight`、`ingress_bucket_entries`、`probe_active`、`business_active`、`global_active`；global_active=business_active+probe_active。接纳登记失败只计 database，内存 admitted_total 与数据库 request_count 都以成功提交登记为起点；物理准入后的临时计数与该累计量分开。
- accounting_write_failures_total、accounting_degraded、pending_settlements。
- 健康探测任务存活、最近运行时间及被跳过次数。

内存累计数在重启后清零；数据库历史统计不清零。接口明确 `counter_scope=process_lifetime`，禁止混同为历史累计量。所有读数用快照返回，空闲 Key 计数可移除，避免内存无限增长。

### 9.4 限制变更与部署约束

课题/Key 上限通过 PATCH 动态修改；降低不取消已接纳请求，例如当前 4 个、上限改成 2，则暂停新接纳直到在途数低于 2。Key 禁用同样只阻止后续鉴权。

全局上限只来自启动配置，变更需先排空退出，再改配置启动；首版不添加测试后门。A15 在无在途请求的干净测试实例中以 GLOBAL_MAX_CONCURRENCY=3 启动，关闭背景探测并清空测试 fixture 后才计数；A19 只验证课题/Key 动态降低，不把全局限额当成热更新。

默认启动脚本固定单 worker、单实例；不使用 Gunicorn 多 worker，也不使用生产 reload。Uvicorn 的通用连接限制不能替代按课题计数。扩展为多实例属于下一阶段架构变更，需重新设计共享计数、恢复与验收。

## 10. 健康检测与可用性策略

### 10.1 健康层次

| 层次 | 方法 | 能证明什么 | 不能证明什么 |
| --- | --- | --- | --- |
| 网关存活 | `/health/live` | 进程能处理探针 | 数据库、上游正常 |
| 网关就绪 | `/health/ready` | 配置/迁移满足要求、数据库可用、未关闭 | 模型具备良好生成质量 |
| 上游可达 | 配置的轻量 GET 探针 | 服务端点响应 | 所有模型可正常生成 |
| 上游可推理 | 固定短文本 Chat Completions | 被测模型实际完成一次最小推理 | 所有模型/长上下文都正常 |

不能默认所有 OpenAI 兼容服务都具有相同 `/health`。P0 确认 `UPSTREAM_HEALTH_PATH`；可采用引擎官方健康路径或实际支持的 `/v1/models`，并标明只表示可达性。

### 10.2 调度与状态

- 可达性探测周期/时限引用HEALTH_INTERVAL/HEALTH_TIMEOUT；按计划开始时刻调度，任务不重叠，错过轮次不并行追赶。
- 推理探测周期/总时限引用INFERENCE_PROBE_INTERVAL/INFERENCE_PROBE_TIMEOUT；固定Prompt及max_tokens分别引用INFERENCE_PROBE_PROMPT/INFERENCE_PROBE_MAX_TOKENS，P0实测冻结。
- 连续失败达到HEALTH_FAILURE_THRESHOLD判unhealthy；连续成功达到HEALTH_RECOVERY_THRESHOLD恢复healthy；未达到失败阈值时标degraded。
- 缓存超过HEALTH_STALE_AFTER未更新判stale；首次启动为 unknown。接口必须展示状态、时间和失败原因，不能把旧 healthy 永久保留。
- 可推理状态独立保存；探测失败提示 degraded，不自动声称所有模型不可用。最近推理探测超过INFERENCE_PROBE_STALE_AFTER未成功执行则标stale。
- 推理探测最多一条在途，与业务共用全局上限；仅在同一准入锁内确认 `global_active <= GLOBAL_MAX_CONCURRENCY - PROBE_MIN_SPARE_SLOTS` 才占用1槽；启动后余量至少为 PROBE_MIN_SPARE_SLOTS-1，默认值见配置表。已有探测或空槽不足则 skipped；预留数大于全局上限时允许配置但推理探测始终 skipped。不进入课题/Key计数。可达性GET不占推理名额。
- 这是低优先启动策略，不能保证完全不挤占后来业务：探测先启动后仍占槽，后续业务达到共享上限会 429。首版不抢占已开始的探测，也不允许突破上游总上限。A24 必须测试“先业务后探测”和“先探测后业务”；需要零探测竞争的场景关闭推理探测并显示 inference=disabled，可达性检测继续运行。

### 10.3 请求门控

只有可达性状态为已确认 unhealthy 且缓存新鲜时，对新业务请求返回 503 `upstream_unhealthy`。healthy/degraded/unknown/stale 允许带正常超时尝试业务请求，并在运行状态中显示健康信息不足。

此策略避免启动时尚未探测和探测任务偶发延迟导致永久拒绝；不提供复杂熔断半开算法。可达性探测即使业务被拒绝也继续运行，确保恢复检测可发生。

若某些引擎轻量探针不可靠，P0 可关闭门控但保留检测与展示，验收基线需明确 `HEALTH_GATE_ENABLED` 的最终值。默认验收启用门控；关闭时对应门控用例须通过范围变更记录处理，不能直接跳过。

### 10.4 默认检测时效的预估

在探测正常调度、网络故障持续且操作受HEALTH_TIMEOUT约束时，发现/恢复预算分别至少覆盖HEALTH_INTERVAL×对应阈值+HEALTH_TIMEOUT，并预留调度余量；实际候选上限只定义于§15.2“健康发现/恢复”行。实际按单调时钟测量，包含调度间隔；若主机过载或事件循环阻塞，必须记录偏差并修复或重新评估基线。

### 10.5 探测与模型用量

探测不走课题 Key，不计入课题用量，也不伪造某个课题。但推理探测可能产生真实上游消耗，在 health_checks 中标识来源。用户查询的课题总量因此不等于上游服务的所有请求总量。

模拟上游必须按 (endpoint, X-Gateway-Request-Source) 分别计 business、health_probe 与 unknown，并记录 request_id。来源头只能由网关生成，客户端同名头被覆盖，不能作为安全凭证。所有“上游收到 0 次”断言均指 business，不包含健康探测；并发/性能隔离 fixture 禁用探测，健康专项 fixture 开启并验证分类。缺少来源的请求不得默认为 business 或被忽略。

## 11. 配置、超时与安全边界

### 11.1 配置唯一清单

本节是配置项名称及初始值的唯一来源；正文数字为说明，必须与本表同步。除课题/Key 的已持久化限额外，配置均在启动时读取，修改需重启。模板中的配置候选列由本表生成；实际冻结值由验收方填写在独立验收记录中。

<!-- CONFIG_BEGIN -->
| 配置 | 初始值/约定 | 说明 |
| --- | --- | --- |
| DATABASE_URL | postgresql+asyncpg://... | 必填秘密；首版直连 PostgreSQL，不经 PgBouncer 事务池 |
| ADMIN_API_KEY | 必填高熵随机值 | 不与课题/上游 Key 共用；变更需重启 |
| UPSTREAM_BASE_URL | 必填内网固定地址 | 禁止客户端覆盖与重定向 |
| UPSTREAM_API_KEY | 空 | 上游允许无认证时可空 |
| UPSTREAM_CA_BUNDLE | 系统信任库 | 内部 CA 可配置本地文件；不得默认禁用 TLS 校验 |
| ALLOWED_MODELS | 必填模型列表 | P0 实测冻结 |
| UPSTREAM_STREAM_USAGE_MODE | include_usage | include_usage 或 unsupported，P0 实测 |
| GLOBAL_MAX_CONCURRENCY | 16 | 业务与推理探测共享总上限；仅重启生效 |
| DEFAULT_PROJECT_MAX_CONCURRENCY | 4 | 仅新建课题默认值；已有记录通过 PATCH 修改 |
| DEFAULT_KEY_MAX_CONCURRENCY | 2 | 仅新建 Key 默认值 |
| CONCURRENCY_RETRY_AFTER | 1 秒 | 入口/并发429建议等待，不保证下一次可用 |
| SERVICE_UNAVAILABLE_RETRY_AFTER | 1 秒 | 网关可构造503统一Retry-After；上游错误不套用 |
| HTTP_CONNECT_TIMEOUT | 5 秒 | 上游连接建立 |
| HTTP_POOL_TIMEOUT | 2 秒 | 取上游连接 |
| HTTP_WRITE_TIMEOUT | 10 秒 | 发送请求体 |
| HTTP_READ_IDLE_TIMEOUT | 60 秒 | 相邻读取空闲时间 |
| REQUEST_TOTAL_TIMEOUT | 300 秒 | 业务总时限；不含随后清理 |
| CLEANUP_TIMEOUT | 5 秒 | 统一有界清理截止时间 |
| UPSTREAM_CLOSE_TIMEOUT | 0.5 秒 | 属于清理总预算 |
| SETTLEMENT_MAX_ATTEMPTS | 2 | 总尝试次数，含首次 |
| SETTLEMENT_ATTEMPT_TIMEOUT | 1.5 秒 | 含取连接、事务、回滚；属于清理预算 |
| SETTLEMENT_RETRY_BACKOFF | 0.1 秒 | 两次尝试之间仅一次 |
| DB_OPERATION_TIMEOUT | 2 秒 | 普通鉴权/登记操作；结算另有更短时限 |
| DB_QUERY_TIMEOUT | 3 秒 | 管理聚合操作硬截止；不代替§15.2统计查询性能目标 |
| DB_POOL_SIZE | 16 | 常驻池上限；实际连接按需建立 |
| DB_MAX_OVERFLOW | 20 | 总连接上限由DB_POOL_SIZE加本项计算；需与DB预算一致 |
| AUTH_DB_MAX_INFLIGHT | 16 | 鉴权及拒绝记录DB通道；无等待，满则503并带Retry-After |
| ADMIN_DB_MAX_INFLIGHT | 2 | 管理查询/写入通道；满则 503 database_unavailable |
| BACKGROUND_DB_MAX_INFLIGHT | 2 | 健康/恢复等后台 DB 操作；就绪探针读取缓存 |
| DB_READY_CHECK_INTERVAL | 5 秒 | 后台检查 DB 状态，公开 ready 不每次查库 |
| DB_READY_STALE_AFTER | 15 秒 | 缓存过期 ready=503 |
| HTTP_MAX_CONNECTIONS | 32 | 共享客户端连接池含业务与可达性探测余量 |
| REQUEST_BODY_MAX_BYTES | 1048576 | 1 MiB |
| NONSTREAM_RESPONSE_MAX_BYTES | 16777216 | 16 MiB；也限制上游错误体 |
| SSE_EVENT_PARSE_MAX_BYTES | 262144 | 256 KiB；超限标 invalid |
| APP_DRAIN_TIMEOUT | 20 秒 | 收到首次停止信号开始计时，到期取消在途请求 |
| UVICORN_GRACEFUL_TIMEOUT | 30 秒 | Uvicorn 等待任务上限，晚于排空+清理截止 |
| BACKGROUND_SHUTDOWN_TIMEOUT | 3 秒 | 关闭后台任务和共享资源的整体预算 |
| HOST_STOP_TIMEOUT | 40 秒 | 宿主停止配置，不是 FastAPI 自动应用的值 |
| SHUTDOWN_SAFETY_MARGIN | 5 秒 | 应用到Uvicorn、Uvicorn到宿主各自至少留出的余量 |
| HEALTH_REACHABILITY_ENABLED | true | 可达性检测开关；生产关闭需记录范围变更 |
| UPSTREAM_HEALTH_PATH | 启用时P0必填，禁用时可空 | 可达性启用时必填，禁用时可空；非空时必须为同一上游的合法相对路径 |
| HEALTH_GATE_ENABLED | true | 依赖可达性检测；检测关闭时必须为 false |
| HEALTH_INTERVAL | 30 秒 | 可达性轮次间隔，不重叠 |
| HEALTH_TIMEOUT | 3 秒 | 一次可达性探测总时限 |
| HEALTH_FAILURE_THRESHOLD | 3 | 连续失败判 unhealthy |
| HEALTH_RECOVERY_THRESHOLD | 2 | 连续成功恢复 healthy |
| HEALTH_STALE_AFTER | 90 秒 | 最近可达性检查过期 |
| INFERENCE_PROBE_ENABLED | true | 隔离用例可关闭；关闭时 inference=disabled |
| INFERENCE_PROBE_MODEL | P0 必填 | 来自 ALLOWED_MODELS；启用探测时必填 |
| INFERENCE_PROBE_PROMPT | Reply OK. | 固定非秘密短文本 |
| INFERENCE_PROBE_MAX_TOKENS | 1 | 上游不接受时 P0 冻结替代值 |
| INFERENCE_PROBE_INTERVAL | 300 秒 | 推理探测轮次间隔 |
| INFERENCE_PROBE_TIMEOUT | 30 秒 | 一次探测总时限 |
| INFERENCE_PROBE_STALE_AFTER | 600 秒 | 最近实际执行结果过期；skipped 不刷新成功时间 |
| PROBE_MIN_SPARE_SLOTS | 2 | 启动探测前至少空闲槽数，不保证后续业务无竞争 |
| PAGINATION_DEFAULT_LIMIT | 50 | 管理列表默认 |
| PAGINATION_MAX_LIMIT | 200 | 超限返回 400，不静默截断 |
| USAGE_MAX_RANGE_DAYS | 31 | 按时间范围最大跨度验证 |
| USAGE_MAX_GROUPS | 1000 | 查询至上限+1 判断溢出，超限 400 |
| USAGE_MAX_GROUP_DIMENSIONS | 2 | 仅 project/key/model/day 白名单 |
| DEFAULT_TIMEZONE | Asia/Shanghai | UTC 存储，响应默认分桶时区 |
| ALLOWED_TIMEZONES | UTC,Asia/Shanghai | 明确 IANA 白名单，扩展需 P0 验证 |
| TZDATA_SOURCE | locked_python_package | 固定 tzdata 版本；应用禁用系统 tzdb 搜索以统一规则 |
| REQUEST_LOG_RETENTION_DAYS | 180 | 显式归档清理脚本，默认 dry-run |
| HEALTH_LOG_RETENTION_DAYS | 30 | 显式归档清理脚本 |
| AUDIT_LOG_RETENTION_DAYS | 365 | 显式归档清理脚本 |
| LOG_MAX_BYTES | 104857600 | 100 MiB 单文件 |
| LOG_RETENTION_DAYS | 7 | 结构化日志保留 |
| RECOVERY_SCAN_INTERVAL | 30 秒 | 扫描本 run 非活动 started |
| RECOVERY_GRACE_PERIOD | 60 秒 | 非活动记录宽限后才 unknown |
| INGRESS_GLOBAL_RATE | 20 次/秒 | 受保护 API 进程级令牌桶，进入 DB 前 |
| INGRESS_GLOBAL_BURST | 40 | 允许短时突发 |
| INGRESS_IP_RATE | 5 次/秒 | 每来源 IP 令牌桶，对所有凭证状态一视同仁 |
| INGRESS_IP_BURST | 20 | 同IP突发容量；须容纳额定合法并发首请求 |
| INGRESS_IP_MAX_ENTRIES | 4096 | 固定内存上限；表满且无过期项，新 IP 返回 429 |
| INGRESS_IP_IDLE_TTL | 120 秒 | 回收不活跃桶；时钟用单调时间 |
| TRUSTED_PROXY_CIDRS | 空 | 默认 socket peer 为来源；只信任明确列出的代理 |
| CLOCK_MAX_OFFSET | 1 秒 | P0/运维校时前提，不是请求路径 NTP 查询 |
| DEPENDENCY_LOCK_TOOL | uv | 锁 uv.lock 并导出带 hashes 依赖清单；工具版本 P0 固定 |
<!-- CONFIG_END -->

启动校验必须覆盖：正数/非负与枚举范围；分页默认不大于上限；DEFAULT_TIMEZONE 属于 ALLOWED_TIMEZONES；探测模型在白名单中；HEALTH_GATE_ENABLED 依赖可达性探测启用；上游协议/CA/路径合法。

启动必须拒绝：PROBE_MIN_SPARE_SLOTS不是整数或小于1；通道/池/限额不是正整数（DB_MAX_OVERFLOW允许0）；SETTLEMENT_MAX_ATTEMPTS不是正整数；超时/余量非正，或退避为负；两个Retry-After不是1～3600的整数秒。额定首请求容量要求 AUTH_DB_MAX_INFLIGHT、INGRESS_GLOBAL_BURST、INGRESS_IP_BURST 均不小于 GLOBAL_MAX_CONCURRENCY；HTTP_MAX_CONNECTIONS 至少为 GLOBAL_MAX_CONCURRENCY+1。不同取舍须显式修订容量契约，不允许通过关闭启动校验实现测试 profile。

预算约束：`UPSTREAM_CLOSE_TIMEOUT + SETTLEMENT_MAX_ATTEMPTS × SETTLEMENT_ATTEMPT_TIMEOUT + (SETTLEMENT_MAX_ATTEMPTS-1) × SETTLEMENT_RETRY_BACKOFF < CLEANUP_TIMEOUT`；`APP_DRAIN_TIMEOUT + CLEANUP_TIMEOUT + SHUTDOWN_SAFETY_MARGIN <= UVICORN_GRACEFUL_TIMEOUT`；`UVICORN_GRACEFUL_TIMEOUT + BACKGROUND_SHUTDOWN_TIMEOUT + SHUTDOWN_SAFETY_MARGIN <= HOST_STOP_TIMEOUT`。SETTLEMENT_ATTEMPT_TIMEOUT不得大于DB_OPERATION_TIMEOUT。宿主实际时限需要部署脚本/进程管理器单独配置并取证，不仅校验环境变量。

数据库不是每条流长期占一条连接；本版以业务登记/结算不超过 GLOBAL_MAX_CONCURRENCY、鉴权/拒绝不超过 AUTH_DB_MAX_INFLIGHT、管理不超过 ADMIN_DB_MAX_INFLIGHT、后台不超过 BACKGROUND_DB_MAX_INFLIGHT 为保守预算，池总上限必须不小于四者之和。各通道必须先取得进程内门控，单个操作最多一个连接；先归还连接再释放通道，禁止嵌套持有两个通道。通过鉴权即释放鉴权通道，之后才申请业务名额和登记；业务拒绝日志重新尝试鉴权/拒绝通道，满则转auth_capacity_exceeded并仅计ingress。增加任一容量必须同时审查数据库max_connections、其他应用占用、管理保留连接和本应用连接授权，不能自动扩池。

WP-DB-02实现使用独立Core连接/事务，不跨请求共享会话。鉴权、登记/标记/结算、管理和ready统一显式连接清理；调用者通过上下文传递单调截止时间，截止保护在取连接前安装，覆盖池内等待、建连、驱动初始化、pre-ping及其取消回滚；已关联物理连接到期仅终止该连接，并中断仍在等待的回滚，不对整池执行abort；连接交付后继续按同一截止时间保护SQL/提交/清理；嵌套操作不延长父截止时间。取连接期间对所属驱动的SQLAlchemy硬失效使用同步terminate，避免断连重试中的异步关闭被二次取消打断而漏还池记录；其他连接仍走原路径，驱动回滚抛错不能覆盖已发生的调用者取消。ready先占后台通道，满额不排队、不访问DB；成功、失败、超时和取消均在连接退出后释放通道。记录connect/checkout/checkin/reset/invalidate/close事件计数、实际当前/峰值池占用（pool.checkedout，包含建连/pre-ping），另列successful_checkouts_active表示已触发checkout且尚未checkin的记录数；两者不得混用。不保留SQL、凭证或无界历史；重复失效回调按事件次数计数，不冒充唯一物理连接数。观察值仅供内部验证，/admin/runtime归后续工作包。应用全局shutdown排空与引擎关闭仍由生命周期/停止工作包收口。

P6报告包含DB checkout等待、SQL时长、通道拒绝数、峰值连接数，以及GLOBAL_MAX_CONCURRENCY条同时登记/结算突发。池大小只控制资源上界，额外耗时目标引用§15.2实测；不能由扩池推出性能已达标。

总时限覆盖连接上游之前和传输期间，HTTPX read timeout 不是请求总时限。完整超时参数在 P0 固定；较短的结算专用时限优先于普通 DB_OPERATION_TIMEOUT。


配置加载职责补充：只读取显式选定的一个环境文件，缺失或非法时失败，不跨环境补齐；进程同名变量可覆盖文件，部署需清理残留变量。上游地址、库名、账号属于部署值，不在运行代码中写死。文件仅使用规范运行字段与 APP_ENV / MIGRATION_DATABASE_URL，不接受 TEST_* / PRODUCTION_* / AI_FAST_* 兼容别名。迁移独立加载凭证，要求与运行连接同目标、不同角色，并在真实连接后核对配置声明的数据库和迁移用户。当前“DeepSeek + 专用测试库”的阶段限制由唯一配置 CLI `app.config_check --profile local-development` 在开发连接/迁移前检查；`scripts/check_development.py` 只保留兼容 wrapper。company 迁移由 `load_migration_config` 强制拒绝，包括完整配置、在线与 `--sql` 离线模式，且在创建 engine 前生效。`--profile company` 仅检查运行配置；以后放开 company 迁移需另行明确授权并修改门禁。配置解析通过 `read_environment_file` 公开入口共享，80 项运行 schema 仍只在 Settings 中维护。该调整不改变 P0 现场签收或正式库操作授权。

#### 11.1.1 完整启动校验清单

以下类型清单覆盖CONFIG源表每个字段且只出现一次。配置加载后、创建连接/启动后台任务前执行结构与交叉校验；日志只给字段名/规则ID和脱敏原因，不输出原值。P0评审清单并冻结候选值，P1实现实际Pydantic校验，A42逐条负例验证；文档脚本只校验可静态判断的候选值、覆盖率和预算，不能替代文件可读性、真实秘密或现场资源取证。

<!-- VALIDATION_BEGIN -->
| 类型 | 配置清单（逗号分隔） | 启动规则 |
| --- | --- | --- |
| address | DATABASE_URL,UPSTREAM_BASE_URL,UPSTREAM_HEALTH_PATH | DB必须postgresql+asyncpg且具备host/port/db；上游仅http/https且无userinfo/query/fragment；健康路径须单斜线开头、无主机/query/fragment/CRLF。健康路径可达性启用时必填，禁用时可空；其余字段必填；非空路径无论开关状态均校验；端口1～65535。 |
| secret | ADMIN_API_KEY,UPSTREAM_API_KEY | ADMIN_API_KEY由至少32字节CSPRNG输出编码，运行配置至少43个无空白ASCII字符；UPSTREAM_API_KEY允许空，两者拒绝CR/LF且不回显值。管理员凭证不能与非空上游凭证相同；随机性来源由P0取证，不能靠长度证明熵。 |
| file | UPSTREAM_CA_BUNDLE | 系统信任库或存在可读的CA文件，加载失败拒绝启动；不接受关闭TLS验证作为替代。 |
| model_text | ALLOWED_MODELS,INFERENCE_PROBE_MODEL,INFERENCE_PROBE_PROMPT | ALLOWED_MODELS非空去重列表；启用推理探测时MODEL属于白名单且PROMPT非空；禁用时允许MODEL为空。所有文本拒绝控制字符，标识不接受空白值。 |
| enum | UPSTREAM_STREAM_USAGE_MODE,TZDATA_SOURCE,DEPENDENCY_LOCK_TOOL | UPSTREAM_STREAM_USAGE_MODE仅include_usage/unsupported；TZDATA_SOURCE固定locked_python_package；DEPENDENCY_LOCK_TOOL固定uv。 |
| positive_int | GLOBAL_MAX_CONCURRENCY,DEFAULT_PROJECT_MAX_CONCURRENCY,DEFAULT_KEY_MAX_CONCURRENCY,CONCURRENCY_RETRY_AFTER,SERVICE_UNAVAILABLE_RETRY_AFTER,SETTLEMENT_MAX_ATTEMPTS,DB_POOL_SIZE,AUTH_DB_MAX_INFLIGHT,ADMIN_DB_MAX_INFLIGHT,BACKGROUND_DB_MAX_INFLIGHT,HTTP_MAX_CONNECTIONS,REQUEST_BODY_MAX_BYTES,NONSTREAM_RESPONSE_MAX_BYTES,SSE_EVENT_PARSE_MAX_BYTES,HEALTH_FAILURE_THRESHOLD,HEALTH_RECOVERY_THRESHOLD,INFERENCE_PROBE_MAX_TOKENS,PROBE_MIN_SPARE_SLOTS,PAGINATION_DEFAULT_LIMIT,PAGINATION_MAX_LIMIT,USAGE_MAX_RANGE_DAYS,USAGE_MAX_GROUPS,USAGE_MAX_GROUP_DIMENSIONS,REQUEST_LOG_RETENTION_DAYS,HEALTH_LOG_RETENTION_DAYS,AUDIT_LOG_RETENTION_DAYS,LOG_MAX_BYTES,LOG_RETENTION_DAYS,INGRESS_GLOBAL_BURST,INGRESS_IP_BURST,INGRESS_IP_MAX_ENTRIES | 严格正整数，不接受布尔/小数/非有限值；计数/字节/保留天数/分页均不得用0表示无限。Retry-After另限定1～3600；USAGE_MAX_GROUP_DIMENSIONS在1～2。 |
| positive_number | HTTP_CONNECT_TIMEOUT,HTTP_POOL_TIMEOUT,HTTP_WRITE_TIMEOUT,HTTP_READ_IDLE_TIMEOUT,REQUEST_TOTAL_TIMEOUT,CLEANUP_TIMEOUT,UPSTREAM_CLOSE_TIMEOUT,SETTLEMENT_ATTEMPT_TIMEOUT,DB_OPERATION_TIMEOUT,DB_QUERY_TIMEOUT,DB_READY_CHECK_INTERVAL,DB_READY_STALE_AFTER,APP_DRAIN_TIMEOUT,UVICORN_GRACEFUL_TIMEOUT,BACKGROUND_SHUTDOWN_TIMEOUT,HOST_STOP_TIMEOUT,SHUTDOWN_SAFETY_MARGIN,HEALTH_INTERVAL,HEALTH_TIMEOUT,HEALTH_STALE_AFTER,INFERENCE_PROBE_INTERVAL,INFERENCE_PROBE_TIMEOUT,INFERENCE_PROBE_STALE_AFTER,RECOVERY_SCAN_INTERVAL,RECOVERY_GRACE_PERIOD,INGRESS_GLOBAL_RATE,INGRESS_IP_RATE,INGRESS_IP_IDLE_TTL,CLOCK_MAX_OFFSET | 有限数且>0；禁止NaN/Infinity/布尔值，关闭功能用开关不能用零时限。 |
| nonnegative_number | SETTLEMENT_RETRY_BACKOFF | 有限数且>=0，不接受布尔；纳入清理总预算。 |
| nonnegative_int | DB_MAX_OVERFLOW | 整数且>=0；禁止SQLAlchemy的-1无限overflow。 |
| boolean | HEALTH_REACHABILITY_ENABLED,HEALTH_GATE_ENABLED,INFERENCE_PROBE_ENABLED | 仅解析规范true/false；非法字符串拒绝，不按非空字符串当真。 |
| timezone | DEFAULT_TIMEZONE,ALLOWED_TIMEZONES | 白名单非空且无重复，默认值必须属于其中；每个值必须能由锁定tzdata加载；首版只允许UTC/Asia/Shanghai，扩展先修订基线。 |
| cidr | TRUSTED_PROXY_CIDRS | 允许空列表；每项为合法规范化IP/CIDR，拒绝*及/0全网信任；IPv4映射IPv6统一规范化；启动器确认禁用服务器自动代理头解析。 |
<!-- VALIDATION_END -->

交叉约束除本节清理/停止/通道池/首请求预算外，还包括：分页默认<=最大；HEALTH_STALE_AFTER>HEALTH_INTERVAL+HEALTH_TIMEOUT；DB_READY_STALE_AFTER>DB_READY_CHECK_INTERVAL+DB_OPERATION_TIMEOUT；INFERENCE_PROBE_STALE_AFTER>INFERENCE_PROBE_INTERVAL+INFERENCE_PROBE_TIMEOUT；RECOVERY_GRACE_PERIOD>=CLEANUP_TIMEOUT；可达性门控依赖检测启用；启用探测的模型/路径/Prompt必须有效。禁用探测也保留数值合法性检查，不允许负值潜伏到下次启用。

部署门槛另列P0/A42证据：单worker且reload关闭、无第二实例；迁移revision匹配；数据库分配给网关的连接预算覆盖池上限并为其他应用/运维留量；HOST_STOP_TIMEOUT对应真实进程管理配置；锁定依赖及tzdata可导入；代理信任链与CLOCK_MAX_OFFSET达标。数据库暂不可用时ready不得为真，任何启动错误均不能回落到不鉴权/不限流模式。P0占位值（例如P0必填/必填模型列表）禁止进入实际运行配置。

### 11.2 安全与数据最小化

- 管理 Key 通过 Authorization 发送，不使用 URL 查询参数；恒定时间比较，配置来源限制文件权限。
- 课题 Key 明文仅创建响应可见；响应设置 `Cache-Control: no-store`，应用日志、错误跟踪和审计不记录正文。
- 管理接口与推理接口明确分离鉴权逻辑，不允许管理员凭证意外被转发。
- 参数化 SQL；查询分组使用允许列表，明细排序固定，拒绝任意字段/SQL 片段。
- HTTPX 默认关闭非预期环境代理继承（如配置 `trust_env=False`），避免内网请求经外部代理；内部 CA 使用显式配置，不默认关闭 TLS 校验。
- 服务若跨不可信网络传输，应使用 HTTPS 或受控内部入口；网关本身不负责签发证书。离线环境仍需保护 Bearer 凭证。
- 默认禁用开放 CORS；Swagger UI 如启用则仅内网管理员使用，并打包静态资源，避免运行时 CDN 请求；也可在生产关闭文档页。
- 安装包、运行和探针不需要访问公网；无默认遥测或在线模型目录下载。

### 11.3 未认证入口保护

全部受保护接口（推理与管理）在查库前执行进程级和来源 IP 令牌桶，使用单调时钟、短锁原子判断；同时通过才消耗令牌。按 §11.1 默认速率运行，耗尽返回 429 ingress_rate_limited 和 Retry-After，增加 rejected_total.ingress，不访问数据库、不建课题账表。合法请求同样受限，不能绕过入口保护；内网 NAT 共用 IP 时需 P0 调整容量。

桶数量上限和闲置回收时间引用 `INGRESS_IP_MAX_ENTRIES`、`INGRESS_IP_IDLE_TTL`；表满无过期项时新来源也返回429。公开live/ready不走鉴权DB，ready只读后台缓存。

通过桶后，鉴权/拒绝落库通道最多 `AUTH_DB_MAX_INFLIGHT` 条并发；满时立即返回503 auth_capacity_exceeded、计ingress且带 §5.4 的Retry-After，不排队。获通道后的DB故障/截止返回503 database_unavailable、计database，不能把容量拒绝与DB执行失败混为一类。首版不加否定凭证缓存。默认通道数与同IP突发容量支持额定合法首请求（A44），不承诺洪泛/慢DB期间所有合法请求都可获准；A41验证有界拒绝及恢复。

反向代理信任链：交付启动器显式设置 Uvicorn proxy_headers=False（等效 --no-proxy-headers），保留原始socket peer；仅网关入口适配器解析X-Forwarded-For，禁止框架和应用二次解析。peer不在 `TRUSTED_PROXY_CIDRS` 时忽略所有转发头；在信任表内时，从最右侧peer向左剥除连续可信代理，取第一个不可信IP为来源。无该IP、缺失头或链格式非法时保守使用原始peer共享桶，不取任意最左值；不解析Forwarded/X-Real-IP作为替代。边缘代理必须覆盖外来XFF并写入真实peer，中间代理仅追加真实peer；不允许信任全网CIDR。规范化IPv4映射IPv6地址，客户端无法通过等价文本绕过桶。P0冻结代理拓扑、CIDR、头处理规则、Uvicorn实参和socket peer/最终client_ip证据，A41覆盖直连伪造、可信单跳/多跳、非可信中间跳及NAT共享来源；直连部署也记录空信任表。涉及的Uvicorn开关以锁定版本核对。[S10]

### 11.4 时钟、时区、驱动与离线锁定

- 网关与数据库主机由运维接入内网 NTP/chrony（或等效校时），P0记录时钟偏差不超过CLOCK_MAX_OFFSET；离线不要求公网 NTP。到期判断统一使用数据库 `statement_timestamp()`，请求存储时间同源；耗时/退避/TTL 使用单调时钟。时钟跳变会影响绝对过期/时间范围，须在故障记录中说明。
- 白名单只允许 UTC、Asia/Shanghai，增加时区需基线修订。应用固定 Python tzdata 包并使用该包的 ZoneInfo 数据（禁用系统 TZPATH 搜索）；日期桶由应用计算 UTC 边界传给 SQL，避免 PostgreSQL 与 Python tzdb 版本不一致。P0 记录 tzdata 版本，A29 验证边界。
- 首版数据库连接契约为直连 PostgreSQL。PgBouncer 事务池不在本版支持范围，不能只关闭 asyncpg 缓存就宣称兼容；发现现场必须使用代理时，P0 单独验证版本、prepared statement 行为与连接回收并修订契约。[S8]
- 依赖使用 uv 锁定，固定 uv 本身版本；联网构建环境依据 uv.lock 导出带哈希清单、按目标 OS/CPU/Python 收集 wheels，离线使用固定清单和 `--no-index --find-links ... --require-hashes` 安装。锁文件包含 tzdata；具体导出命令在 P0 用固定工具版本验证，不在现场动态解析新版本。

### 11.5 Python 环境与目标平台基线

- 最终目标为 Ubuntu 24.04 LTS；用户所述 x86 暂解释为 64 位 `x86_64`（Ubuntu 包架构 `amd64`）。部署时核验 `uname -m` 和 `dpkg --print-architecture`，与此不符须重新确认构建目标。
- Ubuntu 24.04 默认 Python 为 3.12；本次核对官方 noble-updates 的 python3.12 包为 `3.12.3-1ubuntu0.17`，不是 3.12.10。发行版包修订号与上游 Python 补丁版本分别记录，以目标机实际安装结果为准：[Ubuntu 包信息](https://packages.ubuntu.com/noble-updates/python3.12)。项目 `.python-version` 为 `3.12`，`pyproject.toml` 限定 `>=3.12.3,<3.13`。
- 当前开发环境为 macOS ARM64、独立 `.venv`、CPython 3.12.16。目标 Ubuntu 使用 `/usr/bin/python3` 创建自己的 `.venv`；不复制本机 `.venv`、`.tools` 或 macOS wheels。虚拟环境须在目标平台重建：[Python venv 文档](https://docs.python.org/3.12/library/venv.html)。
- 目标机需预装 `python3.12-venv`（离线环境由系统包源或配套离线 deb 包提供），在项目根目录运行 `bash scripts/bootstrap_ubuntu_venv.sh`。脚本验证 OS、架构及 Python 版本，创建或检查 `.venv`；遇到不兼容的已有环境退出，不覆盖已有目录。该脚本仅准备解释器环境，不安装项目依赖、不修改系统 Python。
- 当前依赖准备阶段使用 uv 0.13.8；`uv.lock` 与带哈希的 `requirements.txt` 仅锁定已引入的 asyncpg 0.32.0，尚非完整应用交付依赖。后续加入 FastAPI、SQLAlchemy、Alembic、tzdata 等时同步更新两份锁定文件。导出命令（项目根目录执行，指定现有解释器与项目内缓存，禁用下载及联网解析）：`.tools/bin/uv export --format requirements.txt --no-dev --no-emit-project --locked --python .venv/bin/python --no-python-downloads --cache-dir .tools/cache --offline -o requirements.txt`。若锁文件过期则失败，先在授权联网构建环境更新锁，不在导出时去掉--locked补救；Ubuntu构建机需准备本机可运行的同版本uv。
- 目标平台 wheelhouse 准备完成后，安装命令为 `.venv/bin/python -m pip install --no-index --find-links wheelhouse --require-hashes -r requirements.txt`。P0 在 Ubuntu x86_64 上验证安装及导入，并记录 `python3 --version`、`dpkg-query -W python3.12`、架构和依赖版本。当前仅完成本机环境及锁文件检查，尚未完成目标平台离线安装验收。

## 12. 日志、运行维护与交付

### 12.1 日志内容

结构化日志包含时间、级别、request_id、project_id/key_id、model、outcome、错误类别、耗时。默认不包含输入/输出正文、完整 Header、URL 凭证和原始上游错误栈内容。

业务请求一条开始事件、一条结束或结算失败事件；日志与数据库是两种证据。数据库失败时日志用于排查，但首版不提供从日志自动补账功能，不将日志视为可靠计费队列。

日志轮转按LOG_MAX_BYTES/LOG_RETENTION_DAYS配置，防止撑满磁盘。磁盘不足应有清晰错误和运行状态，不能靠吞掉异常维持“全部正常”。

### 12.2 启动与停止

交付 README 给出目标系统上验证过的命令，流程为：

1. 准备 Python 虚拟环境、PostgreSQL 数据库与专用账号。
2. 离线安装锁定依赖，配置 `.env` 或环境变量。
3. 备份已有数据库，执行 Alembic 迁移。
4. 确认无旧网关实例存活；启动时完成旧 started 记录恢复。
5. 使用项目交付入口 `python -m app.server` 启动；该入口固定单worker、关闭Uvicorn自动代理头解析，按 `UVICORN_GRACEFUL_TIMEOUT` 配置退出等待并安装提前排空的停止信号适配器。入口将在P1/P3实现，不能用普通uvicorn CLI替代后仍宣称通过A33。
6. 验证 live/ready、上游健康、管理员权限与一条真实调用。

停止时序从收到首次 SIGTERM/SIGINT 的 t0 开始：

1. 项目启动器对固定版本Uvicorn Server停止信号钩子做薄适配：同步标记accepting_requests=false、ready=503，停止新探测；将已有监督器取消截止设为t0+APP_DRAIN_TIMEOUT，再调用原始退出逻辑关闭监听。注册表和排空检查在单事件循环原子执行。
2. 到t0+APP_DRAIN_TIMEOUT取消剩余任务，并行执行至多CLEANUP_TIMEOUT的清理，目标在两项之和前释放名额。取消原因shutdown对应outcome=gateway_error、error_code=shutting_down，不冒充客户端断开。
3. Uvicorn等待、后台关闭和宿主强制终止分别引用UVICORN_GRACEFUL_TIMEOUT、BACKGROUND_SHUTDOWN_TIMEOUT和HOST_STOP_TIMEOUT；按 §11.1 留出SHUTDOWN_SAFETY_MARGIN。A33直接发送SIGTERM并记录全部时间点，不以手动调用清理函数代替。
4. lifespan shutdown可能晚于Uvicorn等待请求，不能作为排空计时的唯一触发点。信号适配器与固定Uvicorn版本一起测试；升级时重新验证。[S5]

Uvicorn 的 graceful timeout 是等待任务后触发取消的时限，并不等同于宿主 SIGKILL，不能断言同值必然截断所有清理；本方案仍保留明确层级余量以消除时序竞争。再次强制停止、SIGKILL、宿主失调度或失电不保证完整清理，按 unknown 恢复处理。长流可能被取消，不承诺无损升级。

无需 root 的部署可使用用户态进程管理或平台现有管理服务；具体方式在 P0 冻结，不能把 nohup 当作可靠重启管理的完整方案。

### 12.3 备份与恢复

提供 `pg_dump`/恢复说明及一套备份恢复演练。建议每日备份，业务允许的 RPO 候选 24 小时、RTO 候选 2 小时；这依赖运维计划和数据规模，不属于网关代码单独保证。

验收至少在隔离数据库恢复一次，并校验课题/Key/请求表记录数及指定时间范围统计一致。数据库备份不包含外部 ADMIN_API_KEY/UPSTREAM_API_KEY，秘密配置须单独安全保管。

### 12.4 交付清单

- 项目源码、依赖锁文件、迁移文件、配置样例。
- 与目标 OS/CPU/Python 匹配的离线 wheel 包或批准的内部包源说明、校验和。
- README、部署手册、接口说明、运维与备份恢复说明。
- 模拟上游与确定性测试数据，真实上游兼容性记录。
- 自动化测试结果、性能报告、问题清单、已知限制。
- 本设计书定版文件和填写后的验收记录。

## 13. 分阶段开发计划与里程碑

### 13.1 工期估算口径

按一名熟悉Python Web的开发者全职串行投入，重估基线为36人日，加6天风险缓冲，总计 **36～42个工作日（约8～9个工作周）**；外部等待单列，不作为已完成工作。D1从正式开工及环境具备执行条件计算，不是日历日期承诺。

配套[开发执行计划v1.1](./AI-Gateway-开发执行计划-v1.1.md)是工作包与依赖的执行依据。旧执行计划的30人日中最终验收只有0.5天；本次恢复P6为5天，其他任务补漏/去重净增1.5人日，形成36人日。局部测试、集成故障、容量和最终验收均有显式工作包，不能把正常验收转嫁给风险缓冲。M0/M2/M3后依实际成本复核，不保证候选估算必然命中。

| 阶段 | 工作日 | 投入 | 主要工作 | 交付物 | 里程碑/退出条件 |
| --- | --- | --- | --- | --- | --- |
| P0 环境与协议验证 | D1–D4 | 4 天 | 环境/协议/代理原型/候选依赖预演及冻结 | 见执行计划对应工作包及证据 | M0：环境输入与方法签收，不要求未实现网关过A41/A44 |
| P1 工程基座与身份 | D5–D11 | 7 天 | Git/质量入口、配置/迁移、ready/启动器、两类鉴权、管理审计写入、最小模拟器 | 见执行计划对应工作包及证据 | M1：管理建课题发Key与真实鉴权组件通过，迁移可重建、ready真实反映DB |
| P2 非流式闭环 | D12–D17 | 6 天 | 入口保护/准入、登记/结算、上游适配与非流式编排 | 见执行计划对应工作包及证据 | M2：登记先于上游、确定usage落库对账；断开完整收尾待M3 |
| P3 监督器与流式故障收尾 | D18–D24 | 7 天 | 先过TCP原型门禁，再完成SSE/统一收尾、恢复/信号与故障回归 | 见执行计划对应工作包及证据 | M3：真实TCP各断开/退出路径无泄漏，usage明确，崩溃恢复可追溯 |
| P4 并发与容量验收 | D25–D27 | 3 天 | 在已实现闭环上执行A41/A44及三层并发、动态限制和runtime验收 | 见执行计划对应工作包及证据 | M4：默认合法首请求无误拦，故障有界拒绝可恢复，专项偏差完整 |
| P5 健康/查询与运维完善 | D28–D31 | 4 天 | 上游健康、统计/日桶/审计读取、备份清理工具和早期EXPLAIN | 见执行计划对应工作包及证据 | M5：故障恢复及查询边界正确，运维工具可重复执行 |
| P6 联调压测与交付 | D32–D36 | 5 天 | 真实兼容/全量回归、性能/持续运行、最终离线恢复和签收 | 见执行计划对应工作包及证据 | M6：全部核心/性能/兼容/离线证据通过并签收 |
| 风险缓冲 | D37–D42 | 0～6 天 | 实际缺陷、内网依赖及兼容风险处理；不预支给计划内验证 | 修复/复测与实际消耗 | 不默认用于新增功能 |

### 13.2 里程碑、前置工作与验证边界

- M0：环境、直连上游、代理回显原型、候选依赖离线预演与配置来源签收。P0不要求未实现网关通过A41/A44；正式入口/启动校验在P1/P2落实，容量实测在P4。
- M1：工程/迁移/真实DB通道/ready/生产启动器骨架可用；管理员与课题Key鉴权、课题Key管理和原子审计写入可演示；最小模拟上游支持后续测试。
- M2：先提交started再转发，登记失败不调用上游，确定usage逐条落库且不串账；非流式编排有集成证据。完整响应发送取消语义在M3统一收尾后回归。
- M3：P3先通过单receive真实TCP监督器原型，再扩SSE与故障模式；正式断开/超时/信号处理无泄漏，旧run/非活动记录恢复可追溯。原型失败停止P3扩展并重估。
- M4：在完整请求闭环上验证三层限额/动态限制、runtime、A44合法冷/热首请求及A41故障/来源容量；默认与实验profile分开。
- M5：上游健康迟滞/恢复/共享槽位、日桶/分页/审计读取、备份恢复和保留清理工具完成；早期EXPLAIN提前暴露计划风险。
- M6：发布候选全量回归、性能/持续运行、真实上游兼容与最终Ubuntu离线安装/恢复通过，交付证据归档签收。

P0配置冻结依赖环境、协议、代理和候选依赖预演全部结果；现场未确定项不得写成已验证。P1前置最小模拟器/ready/启动骨架；P2的基础计量/登记协议不依赖SSE；P3只实现一次恢复和信号逻辑，P6复测而不重写。模块编号与里程碑编号由执行计划明确区分。

工作包Done表示其局部交付和测试完成，不代表关联A用例全部通过。进入P6只需P0～P5交付、发布候选和脚本/环境就绪，不要求P6的最终证据事先存在。持续运行按照冻结时长实际执行，设置/分析的人日与机器运行小时分别记录；本阶段为跨工作日机器运行预留窗口，故障后的完整复测纳入实际成本。

本次只调整执行组织/估算和已有数据库版本记录，不删减A用例，不放宽配置或性能判据。范围变更需单独修订设计/模板，阶段演示不能替代最终验收。

## 14. 验收测试设计

### 14.1 测试分层与证据

1. 单元测试：Token 校验、时间边界、并发原子操作、SSE 分块解析和状态转换。
2. 集成测试：真实 PostgreSQL 上的迁移、约束、事务、聚合与条件更新；不能仅用 SQLite 替代。
3. 网络端到端测试：独立 Uvicorn 与可控上游进程，通过 TCP 验证慢流、断开、异常和资源释放。
4. 真实上游联调：验证实际模型名、usage、健康路径、工具参数透传边界和实际首字时间。
5. 离线交付测试：断开公网情况下安装、迁移、启动、管理、调用、查询和备份恢复。

模拟上游提供以下模式：确定usage的JSON；事件间隔与数量按§15.2“即时流式交付”行的SSE；无 usage；UTF-8/行边界任意拆分；重复/非法 usage；4xx/5xx；长首块等待；半途断流；慢读写与无限等待。每次按 endpoint 与网关生成的来源标记记录上游 business/health_probe/unknown 次数；确认网关在准入前返回的429/401/503未增加business，上游原401/429/503属于已发送业务需计数，不将背景探测混作业务。并发/性能隔离 fixture 启动时关闭探测，健康专项单独启用，未知来源必须报错。

每个用例保存命令或自动化测试 ID、配置、期望值、实际结果、request_id、必要 SQL 查询与脱敏日志位置。真实上游未知 Token 数不能替代模拟上游确定值测试。

功能用例节流与测试profile约定：

- default-protection：使用 §11.1 冻结入口桶/鉴权通道/池配置；仅按用例关闭探测（同时关闭其健康门控）或设置课题/Key测试数据，所有偏差必须列明。A44及A41的默认速率/鉴权/DB/伪造头轮次必须用此profile，禁止提高入口/鉴权限额后宣称默认保护通过；A41桶表填满专项单独按§14.1.3登记实验profile。
- functional-paced：A05/A08/A13/A18及普通功能序列使用统一调度器，包含创建fixture、管理查询、runtime轮询在内，同IP总到达率不超过 `min(INGRESS_IP_RATE, INGRESS_GLOBAL_RATE) × 0.8`，非并发专项不突发；不降低请求数量。连续用例先等通道/业务在途归零并按burst/rate补足令牌，再开始计数；通过真实等待补令牌，不在生产增加重置桶后门。断开重试每次等清理完成，已接纳并不等于仅建立TCP；A13须证明已完成登记/到达目标等待阶段。
- 并发专项按用例屏障发送，并提前检查各层槽位、桶令牌及测试数据容量。A15/A16/A17分别只触发指定层，避免同Key限额误挡全局测试；测试过程记录具体error.code/rejection_scope，任何非预期429/503都判失败，不自动重试到通过。
- 独立实验profile可调整限额/压测到达率，但须在模板§2登记配置差异、理由、用例及恢复验证，不得替代默认容量与保护验收。所有profile保持相同鉴权/准入实现和启动校验，不允许绕过保护分支。

limit-isolation是A15/A24的固定专项profile：仅将GLOBAL_MAX_CONCURRENCY设为用例要求的小值，保持入口桶/鉴权/池参数为冻结值；探测与门控开关按用例，课题/Key容量确保只触发被测层。与default-protection分开登记，恢复原配置并重启后再执行A41/A44，不借专项实例证明默认额定容量。

#### 14.1.1 A44 首次业务负载与热池判定

“冷池轮”严格定义为cold-start-business：新网关进程、创建fixture使用独立测试连接并在启动前完成；应用只执行正常迁移版本检查、恢复和ready所需后台SQL，尚无业务/管理负载、无主动建满池。t0发业务屏障前，通过SQLAlchemy pool事件记录本进程池标识、累计connect/close、checked_out和物理连接存活数C0，要求C0<GLOBAL_MAX_CONCURRENCY，所有启动操作已归还连接。保留从建池起的事件日志以证明无预热；不能只凭“刚重启”或总连接数推断。正常ready产生的连接允许存在，不声称C0=0。[S12]

若启动检查已使C0达到全局额度，本轮不满足冷池判据，应报告准备条件失败；不能在t0前偷偷dispose或停后台再称默认冷启动。P0冻结正常启动路径与C0取证方法，P4执行后填写实际C0、冷池阶段新建/关闭连接数及DB checkout延迟，连接建立超时计失败，不排除冷池失败样本。

热池轮使用同一池；在计时前通过测试夹具同时checkout并持有min(GLOBAL_MAX_CONCURRENCY,DB_POOL_SIZE)条连接，确认不同物理连接后全部归还；入口令牌补足再发同样屏障。预热不得改变池/鉴权配置，不经生产HTTP后门；t0记录可复用连接数达到该预热目标。自然失效/重连须记录，目标未满足先标准备失败。两轮运行期间正常DB后台任务继续，只有上游探测按profile关闭；观察数据通过测试进程事件通道输出，不用高频管理查询消耗同IP令牌。结束同时验证业务、鉴权和checkout归零。

#### 14.1.2 A41 故障注入机制

仅在tests下的网络集成夹具通过app工厂依赖注入包装鉴权repository，不改生产限流、超时或清理代码。生产入口没有注入环境变量/管理路由/请求头；正式启动器不加载测试模块。夹具按request_id白名单作用于指定鉴权调用，不影响ready、管理SQL或后台任务；注入清单和夹具版本进入验收证据。

1. 通道饱和：包装点位于取得AUTH_DB_MAX_INFLIGHT门控后、取得数据库连接前，并已进入DB_OPERATION_TIMEOUT截止域。匹配请求await可取消的Event，逐条发“已到达屏障”通知；控制器只在全部门控已占用且最早截止尚有余量时发送一个已预留入口令牌的额外请求，要求503 auth_capacity_exceeded且该request_id无checkout/SQL事件。若屏障没在截止前建成，标夹具准备失败，不能拿自然超时后的结果冒充容量拒绝。
2. 数据库执行超时：另起干净轮次，在取得真实会话后、同一个普通DB操作截止域内先执行参数绑定的SELECT pg_sleep(:delay)，delay大于DB_OPERATION_TIMEOUT；这是仅测试使用的前置SQL，若睡眠未超时则继续原鉴权查询；本用例预期在睡眠期间触发超时，原鉴权查询不执行，并核对该request_id的SQL事件证明这一点。要求超时503 database_unavailable、取消/回滚或失效丢弃可见，最终归还通道/连接。无新请求并在健康DB恢复后，于CLEANUP_TIMEOUT内检查无测试request_id关联的活动SQL；超时未取消则失败。不以只在Python里sleep替代驱动取消验收。
3. 对照与恢复：正常快速鉴权、桶耗尽、通道满、SQL超时分别独立轮次；记录令牌、auth_inflight、checkout/SQL、最终code和拒绝计数增量。每轮finally释放屏障、取消并等待测试任务、移除包装，异常清理仅针对夹具自己的连接；停止发送、待资源归零和令牌补足后，合法Key成功、禁用Key401。

通道屏障阶段不持有DB连接，因此不能用它证明物理连接耗尽；数据库连接上限用真实SQL延迟轮次及pool事件验证。测试控制通道为进程间测试夹具通道，不占网关HTTP入口令牌。A41记录两种机制各自证据，不能仅写“慢DB已测试”。

#### 14.1.3 A41 来源IP与桶容量专项

默认保护轮次保持TRUSTED_PROXY_CIDRS为空：同一真实socket peer发送不同伪造XFF，必须解析成同一个client_ip并命中同一个桶。它证明伪造头不可绕过，不能证明创建了多个来源桶。

桶容量专项使用实验profile（bucket-capacity），N=INGRESS_IP_MAX_ENTRIES保持冻结值，构造N+1个实际可区分来源。优先在目标Ubuntu隔离测试机枚举并预验证可bind、可连通的不同回环源地址，客户端显式绑定local_address；逐条核对socket peer与最终client_ip，信任表仍为空。不能仅改变端口、XFF或IPv4/IPv6等价文本充当不同IP。若目标环境无法提供足够源地址，使用隔离测试代理：仅信任其准确peer地址/CIDR，代理覆盖外来XFF并由受控测试进程写入预生成的不重复地址，服务只允许测试流量进入；报告明确这些是代理合成来源，不冒充真实终端IP。代理方案需在模板§2登记TRUSTED_PROXY_CIDRS及拓扑偏差。

还必须登记入口总速率偏差：默认速率在桶TTL内未必能填满N个桶，不能因旧桶已过期回收而宣称通过“满表拒绝”。专项保持N、INGRESS_IP_IDLE_TTL、鉴权/DB池参数不变，选择受控发包速率R使(N+1)/R小于TTL的一半，并配置INGRESS_GLOBAL_RATE大于R（留调度余量）；记录实际总耗时与首桶年龄，必须在TTL的一半以内完成N+1来源阶段，否则本轮准备失败。使用进程间观察通道采样，避免额外管理来源占桶；不得通过直接填字典或关闭回收任务替代网络容量测试。

从干净实例开始，各新来源发一条受保护请求，确认前N个不同client_ip分别创建桶且entries=N；即使后续鉴权拒绝，也要证明没有入口速率/表满拒绝。第N+1个新来源有全局令牌时必须429 ingress_rate_limited、无DB访问、entries仍为N；已有来源在其令牌充足时仍可通过入口。记录request_id、来源映射、桶项数、全局/IP令牌及DB事件，排除“因总速率耗尽而429”的假阳性。

停止流量后，等待单调时钟超过INGRESS_IP_IDLE_TTL并观察实际回收，再从新来源访问，证明可创建桶。专项结束恢复原始速率与信任表、重启干净实例，复测默认伪造XFF和合法请求恢复。默认保护轮次与专项证据分别归档，任一缺失均不能标A41通过；实验结果不证明默认吞吐能力。P0冻结方案及偏差，P4/A41执行前验证实际来源地址与负载生成器能力。

### 14.2 核心验收矩阵

<!-- CASES_BEGIN -->
| 编号 | 验收项 | 测试方法与通过标准 |
| --- | --- | --- |
| A01 | 干净安装与迁移 | 空 PostgreSQL 执行 upgrade head 成功，应用 ready；重复执行无额外变更 |
| A02 | 鉴权正确 | 无 Key、随机 Key、过期 Key、禁用 Key 各返回 401；模拟上游 business 次数为 0（探测独立计数） |
| A03 | 管理权限隔离 | 无管理员凭证 401，课题 Key 403；正确管理员 Key 能创建课题与 Key |
| A04 | 密钥只显示一次 | 创建响应有明文，后续查询/数据库/日志/审计无完整明文；数据库仅存哈希和显示前缀 |
| A05 | 课题归属 | A/B 各发固定数量请求，分组计数与 request_id 明细一致，无串账 |
| A06 | 启停与过期 | 管理变更提交后的新鉴权生效；已经接纳的流仍按原契约完成 |
| A07 | 非流式正常转发 | 模拟上游成功业务 JSON 与返回结果语义一致、状态码正确，包含 §5.4 定义的 X-Request-ID |
| A08 | 非流式 usage | 100 次固定 usage 调用，逐条三字段及总和精确匹配；无重复记录 |
| A09 | usage 缺失/异常 | 完全缺失记missing；只有部分字段且所有已提供字段合法记partial；任一负值/非法类型（含布尔）/总量不一致记invalid，优先于partial；三类均不混入完整Token总和；全部合法零值记reported且总和为0 |
| A10 | 流式即时性 | 按§15.2“即时流式交付”的事件数、间隔和首事件阈值执行，首事件早于结束；DB 的 first_event_ms 非负且≤duration_ms，受控时钟单测精确等于首个有效 data 事件时间差，注释不触发计时 |
| A11 | SSE 分块 | UTF-8/CRLF/多 data 行/usage 空 choices/分块边界测试通过；转发字节完整且不重复计数 |
| A12 | 流式 usage | 正常结束且 usage 完整时记录精确；无 usage 仍 completed+missing |
| A13 | 断开释放 | functional-paced下确认已登记并分别到达等待响应头/首事件/流中，再真实TCP断开各20次；健康环境在CLEANUP_TIMEOUT内名额归零、连接关闭且记录终态；只有一个receive所有者；未接纳或入口拒绝的尝试不算完成次数 |
| A14 | 超时与断流 | 可控上游延迟/断流，响应头前 502/504，流中记录正确 outcome；不伪造 DONE |
| A15 | 全局并发限制 | limit-isolation profile登记全局/探测偏差；无旧在途的干净实例以全局上限 3 启动且关闭探测，fixture 清零；保持三条业务流，第四条429，business 在途不超过3；配置变更不作为热更新测试 |
| A16 | 课题并发限制 | A 两个 Key 共用课题上限 2，第三个请求被拒绝；B 在全局有余量时可调用 |
| A17 | Key 并发限制 | 同一 Key 上限 2，第三个拒绝；一条结束后可再接纳 |
| A18 | 释放幂等 | 正常/异常/取消/重复结束回调后计数无负数、无泄漏、无重复日志；循环 100 轮通过 |
| A19 | 降低限额 | 课题/Key 通过 PATCH 从高限额降低，不中断已接纳请求；新鉴权按新限制执行；全局重启变更另按A15准备 |
| A20 | 拒绝统计 | 已归属限流/健康拒绝持久化scope为global/project/key/upstream_unhealthy，不进接纳数与Token，business增量0；按§7.4核对rejected时间相等、空值、日桶归属及response_status_code/upstream_status_code/error_code与scope组合；尝试非法组合或NULL应被CHECK拒绝；拒绝日志DB执行失败仅计runtime.database并503，通道满未执行DB则auth_capacity_exceeded仅计ingress，不双计 |
| A21 | 存活与就绪 | 数据库断开时live200，后台检查失败后ready503（DB_READY_CHECK_INTERVAL+DB_OPERATION_TIMEOUT内）；停掉检查超过DB_READY_STALE_AFTER亦503且带Retry-After；恢复并成功检查后ready恢复，无秘密 |
| A22 | 上游故障发现 | 持续故障在冻结窗口内成为 unhealthy；门控启用时新请求 503 |
| A23 | 健康恢复与过期 | 连续成功后解除拒绝；探测停止后显示 stale，不永久显示旧 healthy |
| A24 | 推理探测隔离 | limit-isolation profile登记全局/探测偏差；探测记录 history 不计课题；G=3时业务active=2则 skipped；探测先占1槽后允许2条业务、第三条429；全局总在途≤3；探测结束业务恢复；分别核对来源计数 |
| A25 | 登记失败不调用 | 注入登记失败/DB失联，503、上游 business 次数0、无名额残留；health_probe独立计数不污染结论 |
| A26 | 结束结算失败 | 上游成功后阻断数据库，结果按契约交付、无上游自动重试；started 可追踪，恢复后标 unknown |
| A27 | 崩溃恢复 | 已登记流期间强制终止唯一网关，确认进程退出再重启；旧 started 为 unknown，Token 不伪造，仍可查到请求 |
| A28 | 结算幂等 | 同 request_id 多次结算，只保留一条终态记录，聚合不重复 |
| A29 | 聚合边界 | 按§6.5核对边界表join与独立参考聚合；[from,to)、整日/半日/跨日、无数据、白名单时区、重命名、NULL/零值；completed及cancelled/timeout的reported分别核对all_reported_usage与completed_usage；任一记录只入一个日桶；加入跨午夜rejected登记样本，started_at所在半开区间计拒绝；拒绝未知时区 |
| A30 | 分页稳定性 | 同 started_at 多条数据用 (started_at,request_id) 游标分页，在静态数据集中无遗漏重复 |
| A31 | HTTP 边界与凭证 | 模型白名单/请求体上限生效；课题 Key 未传上游；错误消息和日志不泄漏秘密 |
| A32 | 离线运行 | 无公网访问条件下依赖安装、迁移、启动及核心调用成功；无远程 CDN/遥测依赖 |
| A33 | 优雅关闭 | 交付启动器直接SIGTERM：t0拒绝新接纳，APP_DRAIN_TIMEOUT到期取消；健康环境在其加CLEANUP_TIMEOUT之和内释放；Uvicorn/后台/宿主时限按§11.1，核对真实进程管理配置与Retry-After、ready及shutdown outcome；无提前信号适配不得通过 |
| A34 | 备份恢复 | 隔离库恢复成功，指定表记录数和确定时间范围聚合与备份时一致 |
| A35 | 管理变更审计 | 课题/Key变更与审计原子提交，审计失败回滚；管理员 audit-logs 可读脱敏分页，课题Key拒绝，SQL与API一致，无敏感字段 |
| A36 | 查询保护 | 非法 group_by、维度超限、超时间范围、超分组数、未知查询参数返回统一400；请求明细固定排序；禁止截断后冒充完整统计 |
| A37 | 模型目录 | 有效课题Key访问/v1/models返回配置白名单，未知/禁用Key401；不请求上游、不暴露秘密，目录不被误作实时健康结果 |
| A38 | 运行状态契约 | 逐类制造拒绝核对runtime字段、互斥分类及counter_scope；global_active=business_active+probe_active，正常/异常结束归零；进程重启计数清零而DB历史不变 |
| A39 | 错误信封与追踪 | 覆盖400/401/403/404/405/409/413/429/500/502/503/504、框架校验和上游401/429/503；核对§5.4的type/code/source/upstream_status/upstream_code/upstream_type类型/脱敏，Retry-After来源和值，X-Request-ID与X-Error-Source，405 Allow；客户端/上游ID无法覆盖，SSE成功头含ID |
| A40 | HTTP转发边界 | identity流按拼接后的业务字节序列一致，不要求HTTP chunk分界一致；上游gzip/br在下游发头前502；客户端Cookie不转发、上游Set-Cookie/Content-Encoding/Transfer-Encoding不复制；允许服务器重分帧；来源头不可伪造；逐项执行§6.1 Content-Type矩阵，含参数/大小写、缺失/重复、成功SSE类型不符、401 HTML错误保留状态 |
| A41 | 入口保护与恢复 | 默认保护轮次使用default-protection，按§14.1.2注入；桶容量专项另用§14.1.3实验profile并记录配置偏差/来源IP证据：快DB未知Key为401，桶耗尽为429，桶项≤INGRESS_IP_MAX_ENTRIES；慢DB屏障占满AUTH_DB_MAX_INFLIGHT后另发有令牌请求应503 auth_capacity_exceeded且无DB访问；已获通道操作超过DB_OPERATION_TIMEOUT为503 database_unavailable；auth_inflight不超上限，取消/超时后通道归还；总DB连接不超池上限。另按§11.3测信任链伪造/NAT；停洪泛、撤延迟、通道归零并补足令牌后合法请求恢复，禁用Key仍401；逐类核对计数和Retry-After |
| A42 | 配置与环境冻结 | 逐项执行§11.1.1类型/交叉/现场检查及负例；非法预算/枚举/时区/连接配额启动失败；校时、直连PG、tzdata与uv版本和离线哈希有证据；冻结变更同步文档 |
| A43 | 文档交叉校验 | check_docs.py及test_check_docs.py通过；隔离副本改变用例/配置/性能/追踪、非法探测/连接/停止预算、正文阈值和模板§5/§8重复阈值或结论范围必须失败；已填模板拒绝同步 |
| A44 | 合法并发首请求 | default-protection的入口/鉴权/池参数不变，探测关闭；干净健康DB与模拟上游、同IP桶充满，G=GLOBAL_MAX_CONCURRENCY个有效请求屏障同时发送；按课题/Key上限分配足够身份，默认4课题各2Key每Key2请求；屏障上游保持G条在途后统一释放，无429/503、business调用恰G、计量逐条正确、结束计数归零；按§14.1.1判定cold-start-business与热池，各执行一轮；C0/启动SQL/池事件缺证或不满足前置条件不能通过，记录auth_inflight/DB checkout及连接峰值；此轮不套低到达率延迟目标 |
<!-- CASES_END -->

测试时限引用配置名，性能指标引用§15.2的冻结行；不得在验收失败后只修改报告中的期望值。

### 14.3 验收结论规则

- §14.2核心用例全部通过，真实上游兼容记录完整，交付资料齐备，才满足功能验收；用例总数由源表校验，不另抄编号范围。
- 安全泄漏、课题串账、并发名额泄漏、已知完整 usage 统计错误、正常 SSE 被整包缓冲，均为阻断验收的问题。
- 已知限制必须与实际行为一致；例如允许缺失 usage，不允许将其填为 0 或显示“完整计量”。
- 性能按 P0 冻结的环境与指标测试；未冻结或缺少证据只能标“待验证”，不能给出“已通过”。
- 发现缺陷后修复并回归受影响的核心用例，保留问题及复测记录。范围变更须更新版本，不通过静默删除用例达成验收。

### 14.4 功能到验收追踪矩阵

以下 F 编号来自 §2.1，A 编号来自 §14.2；新增/删除用例必须同步本表。性能条目在 §15.2 单独冻结，不以核心功能测试替代。

<!-- TRACE_BEGIN -->
| 功能 | 验收用例 |
| --- | --- |
| F01 | A01,A05,A06,A29,A35 |
| F02 | A02,A04,A06,A35 |
| F03 | A02,A03,A06,A25,A31,A41,A44 |
| F04 | A07,A08,A14,A31,A37,A39,A40 |
| F05 | A10,A11,A12,A13,A14,A33,A40 |
| F06 | A08,A09,A11,A12,A26,A28,A29 |
| F07 | A05,A10,A13,A14,A26,A27,A28,A30,A33 |
| F08 | A18,A20,A38 |
| F09 | A15,A16,A17,A18,A19,A20,A24,A44 |
| F10 | A21,A22,A23,A24 |
| F11 | A05,A29,A30,A36 |
| F12 | A03,A04,A31,A35,A39,A41 |
| F13 | A01,A32,A33,A34,A42 |
| F14 | A01,A10,A15,A29,A32,A34,A42,A43,A44 |
<!-- TRACE_END -->

### 14.5 自动化文档校验

设计书是配置候选、验收名称、性能目标与追踪关系的唯一源。`python3 scripts/check_docs.py` 检查模板对应块完全一致、编号唯一且完整、F→A覆盖、配置预算算术、正文/模板引用规则、阶段工期、表格列数与代码块闭合；退出非零即文档检查失败。定版前必跑，未来 CI 可直接运行此命令，无需数据库或第三方依赖。

修改设计源表后运行 `python3 scripts/check_docs.py --sync-template`，只生成模板中标记的配置/用例/性能/追踪块，不改手写说明。该模式拒绝覆盖已填实际结果的模板；正式执行先复制模板为单独的验收报告，禁止拿已填写报告作为生成目标。`--docs-dir` 用于隔离副本验证失败路径。当前默认检查v1.6及开发执行计划v1.1；--version可指定v1.2～v1.5复核历史基线。正文清理/停止预算及模板§5/§8不再重复阈值，脚本拒绝这些区域新增数字时限或结论编号范围；A10/A13/A21/A33引用源表和配置名。运行 `python3 scripts/test_check_docs.py` 验证失败路径及同步保护。该检查不代替网关启动校验或人工语义审查。

## 15. 最终效果预估与性能验证

### 15.1 可预期实现的功能效果

| 用户可见效果 | 交付预期 | 证据 |
| --- | --- | --- |
| 按课题独立发 Key | 每课题可多个 Key，独立禁用/过期/限制 | 管理 API 演示与 A02–A06 |
| 使用常见 SDK 调本地模型 | 配置网关 base_url 与课题 Key 后调用已支持 Chat Completions | 真实上游联调 |
| 查询可信用量 | 输入/输出/总 Token 与上游合法 usage 一致，同时显示缺失比例 | A08、A09、A12、A29 |
| 发现和限制拥塞 | 可查看三层在途数，超限返回明确 429 | A15–A20 |
| 发现模型服务异常 | 显示可达/可推理状态、时间、失败原因及恢复 | A21–A24 |
| 中断仍可追溯 | 正常断开记录 cancelled；崩溃或结算丢失标 unknown | A13、A26、A27 |
| 内网独立运行 | 无公网依赖，通过脚本完成管理和运维 | A32–A34 |

“已接纳记录可追溯”依赖登记提交后的 PostgreSQL 数据仍存在且可恢复；数据库本身数据丢失不在网关进程的保证内。

### 15.2 性能候选基线

候选参考环境：网关 4 vCPU/8 GiB RAM，PostgreSQL 独立或同网段主机、SSD、低时延内网，单 worker；真实 LLM/GPU 独立部署。P0 记录实际 CPU、内存、系统、网络 RTT、数据库位置和版本，不能把候选环境当作现场事实。

<!-- PERF_BEGIN -->
| 指标 | 初始目标（待 P0 冻结） | 测量方法 |
| --- | --- | --- |
| 常规业务并发 | 1～16 条在途请求 | 真实上游在安全容量内验证，最终上限取实测值 |
| 非流式网关额外耗时 | P95 差值 ≤ 30ms | 同一负载生成器分别直连/经网关访问固定 50ms 模拟上游，每组 ≥1000 次、到达率4次/秒、并发上限4；记录两组分布，不冒充逐请求纯开销 |
| SSE 首事件额外耗时 | P95 差值 ≤ 30ms | 固定 SSE 模拟上游、到达率4次/秒、并发上限16，直连/经网关使用相同负载 |
| 即时流式交付 | 首事件到客户端 ≤500ms，且明显早于约 2s 流结束 | 100ms 周期、20 事件测试，网络按基线 |
| 额定负载稳定性 | 非故障/非拒绝用例中网关自身错误为0；计数和完整usage记录全匹配 | 关闭探测、足够课题/Key；每流10s，初始2条/秒在首流结束前达到16在途，此后闭环逐条补位，目标在途16，含轮询总到达率≤4次/秒；首次达到目标后测至少30分钟，另测探测竞争 |
| 断开资源回收 | 健康DB/模拟上游条件下≤CLEANUP_TIMEOUT | 断开后检查并发、连接与记录终态 |
| 健康发现/恢复 | 故障 ≤100s；恢复 ≤70s | 按第 10 节默认探测间隔持续注入故障 |
| 统计查询 | 100万条合成日志、USAGE_MAX_RANGE_DAYS范围、≤USAGE_MAX_GROUPS分组，P95≤1s | 执行§6.5边界表join矩阵及审计查询；交付EXPLAIN (ANALYZE, BUFFERS)、冷热缓存与DB checkout等待 |
| 持续运行 | 8 小时无崩溃、名额泄漏、连接数持续增长 | 模拟或真实负载；预热后比较稳定窗口，RSS 不持续单调增长，允许缓存/GC 波动 |
<!-- PERF_END -->

如果真实上游只支持 4 个并发，则生产限额应设为 4 或更低；不能为了达到网关的 16 并发候选目标压垮推理服务。网关自身能力可用模拟上游隔离测试。

不承诺“FastAPI 单实例必然安全承载 200～300 并发”。吞吐与响应大小、数据库写入、流持续时间、客户端速度、主机和上游容量都相关。

上述低延迟测试有明确到达率。稳定性测试以闭环目标在途补位，响应/清理/补位之间允许短暂低于目标；须报告时间加权平均、最小值、P5/P95、峰值和补位延迟，不宣称每个采样点恒为目标。负载生成器根据性能表推导预热时间小于单流时长，稳态补位速率约为目标在途除以单流时长，并为管理轮询保留额度。若实测从未达到目标或负载生成器持续落后，本轮无效，不能以降低在途掩盖。A44健康额定首请求不允许429/503；只有A41的超容量/故障场景允许按契约拒绝。实验profile不能冒充默认配置结果。

### 15.3 Token 统计效果预估

| 请求类型 | 可验收的结论 | 不应承诺的结论 |
| --- | --- | --- |
| 非流式，合法完整 usage | 与上游报告值逐条一致 | 上游报告一定等于硬件实际消耗 |
| 流式正常结束，完整 usage | 完整采集并一致入库 | 任意上游都支持该字段 |
| 上游不提供 usage | 次数、耗时、结果可记录；Token 缺失可见 | 自动恢复精确 Token |
| 中途断开 | 保留已收到的 usage 或明确 missing | 仍能拿到上游最终总量 |
| 崩溃/结算失败 | 已登记请求保留并标 unknown | 每条请求都能最终补齐准确数值 |

不使用“行业准确率 93%～99%”作为验收指标。报告同时给出：完整 usage 条数/覆盖率、partial/missing/invalid 数量、已知 Token 总和、unknown 请求数；覆盖率低不等于字段采集算法本身算错。

### 15.4 数据容量预估方法

不预设每条日志固定大小。P6 用 10 万条代表性数据测量 `pg_total_relation_size`（含索引），计算实际每条平均存储量，再估算：

`保留期空间 ≈ 日均已接纳与已记录拒绝请求数 × 保留天数 × 实测每条字节数 × 1.5～2 运维余量`。

健康记录按每日秒数除以HEALTH_INTERVAL估算轮次数，再加实际推理探测。备份、WAL 和审计另算。压测日志不能不加区分地混入生产课题数据。

## 16. 风险、已知限制与应对

| 风险/限制 | 影响 | 应对与责任边界 |
| --- | --- | --- |
| 上游流式 usage 不支持 | Token 覆盖率降低 | P0 验证、保留缺失状态，接受次数统计；不无依据估算 |
| 上游兼容接口行为差异 | 某参数/事件不兼容 | 冻结引擎版本与协议矩阵，不声称无限兼容 |
| 客户端取消无法停止 GPU 推理 | 上游实际资源仍被占用 | 关闭连接、记录边界；真实取消能力由引擎验证 |
| 进程/机器崩溃 | 长流中断、usage 缺失 | 登记先行、unknown 恢复、备份，不保证无损 |
| PostgreSQL 故障 | 新请求拒绝、现有请求结算降级 | fail-closed 新准入，保留记录，运行状态可见 |
| 多 worker 误部署 | 并发限制被放大、恢复冲突 | 固定部署脚本与检查清单，只验收单实例 |
| 自研ASGI监督器与信号适配 | receive竞争、send异常先于disconnect、重复取消或版本升级可能导致泄漏/错误终态 | P3先完成真实TCP竞态原型；单一receive所有者、一次性收尾、锁定服务器/框架版本；A13/A18/A33覆盖，升级重跑，未通过阻断M3 [S11] |
| 先知晓上游健康不等于请求必成功 | 探测与真实调用间有状态变化 | 每次调用仍有超时和错误处理 |
| 离线依赖缺包或平台不匹配 | 安装失败 | P0 在目标环境验证 wheel，交付前断网重装 |
| 真实性能低于候选目标 | 阶段延期或需降额 | P0 冻结环境，P6 压测，以证据调整而非预设保证 |
| 慢客户端/超长请求 | 长期占槽、延迟增长 | 请求大小、总时限、无排队策略；允许明确拒绝 |
| 运营期数据长期增长 | 查询变慢、磁盘不足 | 索引、保留策略、显式归档与容量报告 |

上述限制必须出现在最终 README 与验收报告中。出现未说明的真实数据丢失、秘密泄漏或课题串账，不能归入“预期内限制”。

## 17. 验收执行顺序与签收资料

### 17.1 演示脚本顺序

1. 在验收环境记录版本、配置与数据基线，确认单实例、单 worker。
2. 创建课题 A/B，各发一个 Key，保存创建响应中的秘密到受控位置。
3. 非流式调用固定数据，检查上游/客户端/数据库三方 usage 一致。
4. 完整 SSE、无 usage SSE、断开 SSE 各执行一次，展示状态与缺失口径。
5. 制造并发超限，展示 429、计数、上游收到次数以及释放后恢复。
6. 制造上游故障和恢复，展示状态转换、503 门控和探测隔离。
7. 制造数据库登记/结算失败和进程崩溃，展示拒绝或 unknown 记录。
8. 查询课题和时区边界统计，禁用 Key 并验证立即影响新鉴权。
9. 执行性能与持续运行测试，附原始数据，避免只展示截图结论。
10. 执行离线安装与备份恢复，核对交付清单和未解决问题。

### 17.2 基线签收表

| 项目 | 冻结值/记录位置 |
| --- | --- |
| 设计书版本与修订 | v1.6，正式验收前填写定版修订号 |
| Git commit / 发布版本 | 待开发交付时填写 |
| Python / 依赖锁 / PostgreSQL | P0 填写 |
| 上游引擎、版本、模型、参数 | P0 填写 |
| stream usage 模式与健康路径 | P0 填写 |
| 单实例进程管理方式 | P0 填写 |
| 并发、超时、请求大小 | P0 初定、P6 复核，变更需记录 |
| 性能环境与目标 | P0 冻结第 15 节基线 |
| 核心用例结果 | §14.2全部用例逐项附证据 |
| 风险和范围变更 | 无则填写“无”，不得留空 |
| 开发方 / 验收方 / 日期 | 正式验收时填写 |

配套《验收记录模板》用于执行记录。模板中的空白代表尚未测试，不能被解释为默认通过。

## 18. 参考依据与阅读入口

以下是实现参考，不代表要整体引入这些项目；本文件的超时、限额、阶段和指标是针对当前场景的设计决定。原 v1.0 来自用户提供的粘贴设计文本，未作为仓库文件归档；本项目可复查的历史版本从 docs 中保留的 v1.1 开始，不声称存在 v1.0 文件。

参考项目目录是学习材料集合，不代表每个项目都被选为实现底座。RuoYi-FastAPI 可参考管理分层/SQLAlchemy；FBA 可参考异步会话与工程分层；New API 可参考Key与用量产品流程。前两者整体后台对首版偏重，New API为不同语言，三者均不直接导入业务代码。本轮不修改参考项目，也不把目录存在视为通过代码审计。

- [RuoYi-FastAPI](https://github.com/insistence/RuoYi-FastAPI)：对应本地 `参考项目/RuoYi-FastAPI/`。
- [FastAPI Best Architecture](https://github.com/fastapi-practices/fastapi-best-architecture)：对应 `参考项目/fastapi-best-architecture/`。
- [New API](https://github.com/QuantumNous/new-api)：对应 `参考项目/new-api/`。

- [S1] [SQLAlchemy 异步 ORM 文档](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)：AsyncEngine、AsyncSession、并发任务中的会话使用。
- [S2] [HTTPX 异步与流式响应](https://www.python-httpx.org/async/)：客户端连接复用、流式转发和 response 关闭责任。
- [S3] [AnyIO 取消与超时](https://anyio.readthedocs.io/en/stable/cancellation.html)：取消传播、清理期间屏蔽与有界超时。
- [S4] [Alembic Cookbook](https://alembic.sqlalchemy.org/en/latest/cookbook.html)：异步数据库连接的迁移接入方法。
- [S5] [Uvicorn Server 源码](https://github.com/encode/uvicorn/blob/master/uvicorn/server.py)：等待请求、任务取消与 lifespan shutdown 的顺序；实际按锁定版本验证信号适配。
- [S6] [FastAPI 错误处理](https://fastapi.tiangolo.com/tutorial/handling-errors/)：覆盖默认请求校验与HTTP异常处理器。
- [S7] [HTTPX 流式响应](https://www.python-httpx.org/quickstart/#streaming-responses)：raw读取与自动内容解码的区别。
- [S8] [SQLAlchemy asyncpg 方言](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#asyncpg)：prepared statements/连接池兼容边界。
- [FastAPI 官方全栈模板](https://github.com/fastapi/full-stack-fastapi-template)：参考配置、工程组织与测试，不原样采用其同步数据库方案。
- [LiteLLM 虚拟 Key](https://docs.litellm.ai/docs/proxy/virtual_keys)：参考课题/团队 Key 分发思路。
- [LiteLLM 健康检测](https://docs.litellm.ai/docs/proxy/health)：参考进程就绪与模型可用性分离。

- [S9] [PostgreSQL数组函数](https://www.postgresql.org/docs/current/functions-array.html)：参数化unnest构造等长边界列，采用本设计的区间join。
- [S10] [Uvicorn设置](https://uvicorn.dev/settings/)：proxy-headers及可信来源配置，锁定版本复核。
- [S11] [ASGI HTTP规范](https://asgi.readthedocs.io/en/stable/specs/www.html)：disconnect和send异常可能竞态，需单一监督器处理。

- [S12] [SQLAlchemy连接池](https://docs.sqlalchemy.org/en/20/core/pooling.html)：按需建连及pool事件；本项目冷/热池判据是测试约定。
- [S13] [HTTP媒体类型语义](https://www.rfc-editor.org/rfc/rfc9110.html#section-8.3)：类型/参数解析依据；首版接受列表按§6.1限定。

## 19. 版本修订记录

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| v1.0 | 原始输入文档 | FastAPI + MySQL 最小课题分发网关，约 12 日计划 |
| v1.1 | 2026-11-14 | 改为 PostgreSQL 异步方案；五张业务表；补充三层并发、健康检测、计量状态机、失败恢复、24～30 工作日计划及 A01–A36 验收用例 |
| v1.2 | 2026-11-14 | 处理26条审查意见；明确20/5/30/40s退出链路、两次结算预算、探测竞争与来源、统一错误/追踪、配置清单、入口保护及连接预算；新增A37–A43、F→A矩阵与自动同步校验 |
| v1.3 | 2026-11-14 | 处理本轮8项意见；调整默认入口/鉴权/连接容量、增加A44，固定测试profile/代理信任链/错误字段/日桶SQL/审计索引，扩大交叉校验和失败路径测试；工期重估28～34工作日 |
| v1.4 | 2026-11-14 | 明确拒绝时间、完整启动检查、冷池与故障注入契约，收敛正文阈值、Content-Type、秘密填写及导出命令；P0增至4天，总计30～36工作日 |
| v1.5 | 2026-11-15 | 补全专项profile、条件健康路径、pg_sleep超时语义、rejected HTTP约束与A41多来源容量测试；保持功能范围和工期不变 |
| v1.6 | 2026-11-15 | 同步执行计划v1.1，36人日加6天缓冲，补请求闭环与工程前提、拆实现/验证责任、去重依赖；P6保留5天；业务/验收范围不变 |

## 20. v1.2 历史审查意见处理与文档检查记录

本节保留上一版本处理记录，其中容量、用例数和命令结果仅描述v1.2当时状态；当前基线以v1.6正文及§24为准。

### 20.1 逐项处理

下表表示设计修订已落地，不表示对应网关功能已经实现或运行验收通过。

| 审查编号 | 处理结果 | 对应位置/证据要求 |
| --- | --- | --- |
| 1 | 分离应用排空、清理、Uvicorn与宿主时限，采用20/5/30/40s，增加提前信号适配 | §12.2、A33；纠正“Uvicorn超时必然等于强杀”的过强表述 |
| 2 | 最多两次结算，每次1.5s，关闭0.5s、退避0.1s，总子预算3.6s | §5.3、§8.4、配置预算自动校验 |
| 3 | 统一持久化scope四类；database仅运行计数 | §7.4、§9.3、A20/A38 |
| 4 | 增加authentication/validation等明确运行拒绝分类 | §9.3、A38；按最终原因单次计数 |
| 5 | 模拟上游按endpoint与business/health_probe/unknown来源分别计数 | §10.5、§14.1、A02/A20/A25 |
| 6 | 探测开始前预留两个空槽，明确仍可能与后到业务竞争；补反向测试 | §10.2、A24；不再承诺完全不挤占 |
| 7 | 校验异常统一400；404/405/500处理器、上游错误来源与错误码表 | §5.4、A39 |
| 8 | 定义X-Request-ID及成功/错误/SSE覆盖范围 | §5.4、§6.1、A07/A39 |
| 9 | 配置项集中为78项唯一清单，并生成验收模板候选列 | §11.1、模板§2、check_docs.py |
| 10 | 覆盖等待响应头的单一receive监督器与task group取消策略 | §5.3、A13 |
| 11 | 明确aiter_raw、identity编码和请求/响应头允许列表 | §6.1、A40 |
| 12 | 全局限额只在重启生效，A15使用干净fixture；热更新仅课题/Key | §9.4、A15/A19 |
| 13 | 给出短事务通道与默认24连接预算，增加DB等待分解 | §11.1、§15.2；不以扩池替代实测 |
| 14 | 进程/IP有界桶、4条鉴权DB门控、可信代理与恢复语义 | §11.3、A41 |
| 15 | 分离all_reported_usage与completed_usage，后者为前者子集 | §6.4、A29 |
| 16 | 增加只读脱敏audit-logs管理接口 | §6.2、A35 |
| 17 | 增加模型时间索引及多种聚合EXPLAIN交付要求 | §7.4、§15.2；不强制错误的索引计划 |
| 18 | 补内网校时、DB时钟比较、IANA白名单、锁定tzdata/uv、首版直连PG限制 | §11.4、P0退出清单、A42 |
| 19 | 模板交付清单补设计书定版和填写后的验收记录 | 模板§6 |
| 20 | 模板候选表和实际测量表均补SSE首事件≤500ms | 模板§2/§5、A10 |
| 21 | 模板结论补真实上游兼容性验收 | 模板§8 |
| 22 | A36改为实际接口的group_by/时间/分组数/未知参数边界 | §6.3、A36 |
| 23 | 从设计生成模板四个区块，自动检查漂移及拒绝覆盖已填写区块 | §14.5、scripts/check_docs.py、A43 |
| 24 | 缺陷表默认“无”，明确仅为尚未登记，状态“未核实” | 模板§7；不暗示验收通过 |
| 25 | 增加F→A映射、模型目录/runtime/first_event_ms验收 | §14.4、A10/A37/A38，覆盖全部43用例 |
| 26 | 补三个参考项目用途与不整体采用的理由，说明v1.0为粘贴输入 | §18；v1.1仍保留为历史文件 |

### 20.2 本次文档验证

2026-11-14 执行 `python3 scripts/check_docs.py`：通过。确认78项配置、43项用例、14项功能追踪、9项性能目标在设计书和模板之间一致，清理/停止/连接池预算检查通过，Markdown表格及代码块结构正常。

使用临时副本验证以下六种错误均被脚本以非零退出码拒绝：用例名称漂移、配置候选值漂移、性能目标漂移、引用不存在的用例、结算预算超限、尝试覆盖已填“通过”及证据的模板。另验证未填写模板中的名称漂移可自动同步修复，手写区块不变。

以上是文档工具验证，不是A01–A42的网关功能执行结果。正式验收模板保持全部“未执行”；下一开发入口仍是P0环境和协议确认。

## 21. v1.3 历史审查意见处理记录

本节工期、用例和检查结果仅描述v1.3；当前改动见§22。

| 本轮意见 | 修订与验收证据 |
| --- | --- |
| 1 功能节流/profile | §14.1统一到达率、前置补令牌与非预期拒绝判失败；模板§2记录偏差 |
| 2 鉴权首请求容量 | 提高AUTH_DB_MAX_INFLIGHT和INGRESS_IP_BURST，同步DB池预算；§5.4 Retry-After；A44冷/热池合法首请求 |
| 3 代理信任链 | §11.3固定唯一解析者和右向左信任规则，P0退出门槛、模板§1和A41取证 |
| 4 重复数字漂移 | 关键正文改为配置/指标引用；模板§5/§8移除重复阈值/编号范围，check_docs和失败路径测试检查 |
| 5 日桶SQL | §6.5选定UTC边界表join、同快照聚合及EXPLAIN矩阵；A29验证互斥边界 |
| 6 配置与错误字段 | §11.1增加正整数/探测余量/通道池/首请求突发/HTTP/停止预算检查；§5.4字段与Retry-After契约；A39/A42 |
| 7 用例/负载/审计 | A09非法字段优先、A40按字节拼接、A41慢DB分支；§15.2可达到的预热与在途分布；§7.6审计索引 |
| 8 风险与工期 | §16监督器风险，P3原型门槛；§13调整28天基线加6天缓冲，不静默删减核心验收 |

上一轮只修订设计、模板与文档工具；所有网关功能仍待P0及后续实施。模板结果保持“未执行”。文档工具验证：`python3 scripts/check_docs.py`通过，80项配置、44项核心用例、14项功能追踪、9项性能指标一致；`--version v1.2`历史检查通过。`python3 scripts/test_check_docs.py`的7组测试通过，包含29种错误注入、同步不覆盖已填记录、手写内容保留及幂等同步。错误注入覆盖生成块漂移、探测/通道/连接/停止预算、正文与模板阈值、结论范围、阶段工期和无效用例映射；所有操作均在临时副本进行。此结果仅证明文档工具检查有效，不表示网关已实现或功能验收通过。

## 22. v1.4 历史审查摘要处理与冻结边界

本节保留v1.4处理和检查记录；当前增量见§23。

| 审查项 | 本次约定 / 后续证据 |
| --- | --- |
| P1 rejected时间 | §7.4同语句时间与NULL约束；A20/A29验证拒绝统计和跨日 |
| P2-3 启动校验 | §11.1.1逐字段类型、交叉和现场门槛；P1实现、A42负例 |
| P2-4 A44冷池 | §14.1.1首次业务负载定义、C0与池事件；P0冻结方法，P4执行 |
| P2-1 重复数字 | 正文改引配置/性能表，扩大文档校验；示例payload、固定专项fixture及历史记录不当作默认值 |
| P2-5 Content-Type | §6.1区分客户端、上游成功/错误及状态优先级，A31/A39/A40取证 |
| P2-6 A41注入 | §14.1.2测试专用依赖包装、可取消屏障和真实pg_sleep，区分lane/DB超时 |
| P3 导出/秘密/排期/profile | §11.5固定离线导出命令；模板秘密行只记录来源；P0扩为4天；A15标limit-isolation |

本轮依据用户提供的审查摘要修订，没有假定已取得完整审查报告。上述设计歧义已明确为实现契约；P0冻结仍需现场取证和签收，不因文档校验通过自动完成。网关用例保持未执行。

本轮文档验证：80项配置均进入启动类型清单，44项用例/14项映射/9项性能指标与模板同步；当前及历史版本交叉检查通过。文档工具10组测试通过，涵盖新增数值/枚举/健康预算、冷池/注入/拒绝时间引用、秘密来源保护和正文重复阈值的失败路径。离线导出使用现有解释器和项目缓存成功，临时导出文件的依赖及哈希与现有requirements.txt一致；未下载新解释器、未修改依赖锁或数据库。这些结果不替代网关启动、网络故障或Ubuntu目标机的实测。

## 23. v1.5 历史修订与检查记录

本节保留旧版本工期与工具结果；当前排期以§13及§24为准。

| 本轮意见 | 处理与证据要求 |
| --- | --- |
| 1 profile漏项 | 模板§2加入limit-isolation，A24标明profile；脚本检查模板列表包含四类profile且A24保留专项标记 |
| 2 健康路径条件 | 配置表与启动清单统一“可达性启用时必填，禁用时可空”，非空路径始终校验；同步模板 |
| 3 睡眠超时分支 | §14.1.2明确本轮在pg_sleep期间超时，原鉴权SQL不执行；未超时才继续原查询 |
| 4 rejected HTTP约束 | §7.4增加scope/status/error组合及上游状态NULL的CHECK设计，显式IS TRUE阻止NULL绕过；A20增加正反例断言 |
| 5 桶容量来源构造 | §14.1.3定义回环bind或隔离代理的N+1来源、实际peer证据、速率/TTL前置条件和恢复；模板登记专项偏差，不能用伪造XFF替代多来源 |

2026-11-15文档交叉检查通过：80项配置、44项验收用例、14项功能追踪、9项性能指标一致；11组文档工具测试通过，新增9种遗漏/矛盾注入均被拒绝，历史v1.2/v1.3/v1.4仍可单独检查。当前脚本只验证文档约定和同步，不证明迁移CHECK、IP来源或数据库超时已经实测。模板所有用例保持未执行，本轮未修改数据库或部署配置；功能范围与30～36工作日估算不变。

## 24. v1.6 执行计划同步说明

生成执行计划v1.1，历史执行计划v1.0及设计v1.5保留。80项配置、44项用例、14项追踪和9项性能源表不变；增加计划校验覆盖工作包估算、阶段日期、依赖图和A责任。本次只产出计划/文档工具，未创建业务工程、连接数据库或部署；P0仍待正式执行。

2026-11-15验证：当前文档和执行计划交叉检查通过，45个工作包合计36人日；14组工具测试通过。与v1.5的五个契约源块逐字比对一致，历史版本检查保留；新增计划失败路径检查不会覆盖手写计划或已填写验收记录。
