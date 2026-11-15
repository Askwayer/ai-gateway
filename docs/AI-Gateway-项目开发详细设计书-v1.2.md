# 课题级 AI Gateway 项目开发详细设计书与验收基线

| 项目 | 内容 |
| --- | --- |
| 文档版本 | v1.2 |
| 编写日期 | 2026-11-14 |
| 适用对象 | 开发人员、部署运维人员、课题管理员、验收人员 |
| 技术方案 | FastAPI + SQLAlchemy 异步 ORM + asyncpg + PostgreSQL + Alembic |
| 部署场景 | 内网离线、小团队、单个本地 OpenAI 兼容推理服务 |
| 最终部署系统 | Ubuntu 24.04 LTS（用户已确认）；用户提供架构为 x86，暂按 x86_64/amd64 配置并由部署脚本校验；资源及进程管理方式待确认，尚未现场验证 |
| 当前状态 | 开发前设计稿；不是已实现功能或已完成测试的声明 |
| 修订来源 | 在 v1.1 基础上处理 26 条审查意见；v1.1 文件保留为历史版本 |

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
| 数据库 | PostgreSQL | 候选 16 或 17 的可用维护版本；P0 冻结具体版本 |
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

清理预算自停止业务读写时开始：上游关闭最多 0.5s；结算总尝试最多 2 次、每次最多 1.5s（含取连接、事务与回滚），仅一次 0.1s 退避；上述最大合计 3.6s，留 1.4s 给调度和释放，整体仍受 5s 单调截止时间约束。每一步取自身时限与剩余总预算的较小值，预算不足则跳过重试。外层 finally 执行无 I/O 的一次性名额释放，数据库失败不能阻止释放。[S2][S3]

A13 的 5s 目标适用于健康验收主机，不是对操作系统完全失去调度或进程被 SIGKILL 的实时保证。发现不可取消驱动或关闭超时需标记降级并丢弃连接，不得无限等待。

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

所有到达应用、且尚能发送响应的成功/错误/SSE/429/404/405 响应均包含网关生成的 UUID 头 `X-Request-ID`。覆盖而不信任客户端同名头；上游同名头不得覆盖它。错误另带 `X-Error-Source: gateway|upstream`，与 JSON source 一致。上游 401 因而可与本地凭证错误区分。服务器在进入应用前拒绝的非法 HTTP、连接已断开或已发出的流，不保证追加响应头/错误体。

| HTTP | error.code | source | 场景 |
| --- | --- | --- | --- |
| 400 | invalid_request / invalid_model / query_limit_exceeded | gateway | 输入校验、模型或查询边界 |
| 401 | invalid_api_key | gateway | 缺失、未知、过期、禁用凭证 |
| 403 | permission_denied | gateway | 课题凭证访问管理接口等权限错误 |
| 404 / 405 | not_found / method_not_allowed | gateway | 路由、资源不存在或方法不允许 |
| 413 | request_too_large | gateway | 请求体超限 |
| 429 | ingress_rate_limited / concurrency_limit_exceeded | gateway | 入口/IP 保护或业务三层并发限制；带 Retry-After |
| 503 | auth_capacity_exceeded / database_unavailable / shutting_down | gateway | 鉴权门控、数据库错误或排空 |
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

`/health/ready` 不发起模型生成，也不因单次上游推理失败而指示网关进程应重启；它读取后台数据库检查缓存（每5s检查、超15s为stale），启动未检查、缓存失败/过期或排空均503。因此数据库刚断开到下一次检查之间允许短暂旧状态，不宣称瞬时一致；业务鉴权仍真实查库。上游业务可用性由管理健康接口展示，公开探针只返回最少信息。

上游 URL 固定在配置中；拒绝客户端传入的 `base_url` 等路由控制字段，不跟随重定向。响应 `X-Request-ID` 的语义见 §5.4，适用于本节接口。

请求头允许列表：Content-Type（网关固定 application/json）、Accept（按模式设置）、Authorization（配置的上游凭证或省略）、Accept-Encoding（固定 identity）、X-Request-ID（网关生成）、X-Gateway-Request-Source（网关设置 business/health_probe）。忽略客户端同名来源标记。Host/Content-Length 由 HTTPX 重算，不转发客户端 Cookie、Connection、Transfer-Encoding、Forwarded/X-Forwarded-* 等。

首版采用 identity 编码策略：先检查上游 Content-Encoding 为空或 identity，流消费明确使用 `aiter_raw()`，不调用自动内容解压的 `aiter_bytes()`。若上游无视 identity 返回 gzip/br 等编码，则在发下游响应头前关闭并返回 502 upstream_protocol_error；不把压缩字节喂给 SSE 解析器。非流式也按此策略有界读取原始体。HTTP 传输分块由 HTTPX/ASGI 各自处理，raw 不代表复制 chunked 帧。[S7]

上游响应头允许列表：Content-Type（校验为模式兼容类型）以及错误时合法的 Retry-After（整数 0～3600 秒；其他值丢弃）。不复制 Content-Encoding（identity 已规范化）、Transfer-Encoding、Content-Length、Connection、Keep-Alive、Trailer、Upgrade、Set-Cookie、上游 X-Request-ID；其他头默认剥离。网关自己设置 X-Request-ID、错误来源头和 SSE 的缓存/禁缓冲头。非流式最终长度由框架生成，SSE 不设置 Content-Length。原上游请求标识仅在允许时写内部 upstream_request_id。

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

管理分页默认 50、上限 200；统计默认最大范围 31 天，可配置提高。from/to 必须是带时区的 ISO 8601 时间，采用 `[from,to)` 半开区间。内部存 UTC，展示默认 Asia/Shanghai；按日分桶先按指定时区计算日期边界。timezone 只接受 §11.1 的 IANA 白名单，非法值 400。请求明细固定按 (started_at, request_id) 降序，审计按 (created_at,id) 降序；没有可选排序参数。管理查询未知参数（含 sort）返回 400，不静默忽略。

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
- 分组维度仅允许 project、key、model、day，最多组合两个，使用白名单构建查询；聚合结果设最大返回行数与截断/分页约定，禁止默默丢组。首版超 1000 组返回 400，提示缩小范围。
- 同一统计响应在同一 SQL 快照中计算各计数和总量；多次查询期间仍有请求完成，数值可以正常变化。

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
| max_concurrency | INTEGER > 0 | 默认 4；不允许 NULL/负数代表特殊含义 |
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
| max_concurrency | INTEGER > 0 | 默认 2 |
| created_at / updated_at / revoked_at | TIMESTAMPTZ | 审计相关时间，revoked_at 可空 |

索引：key_hash 唯一索引；(project_id, created_at, id) 查询索引。Key 由至少 32 字节密码学随机数生成，不使用课题名称、时间戳或短口令构造。高熵随机 Token 可用 SHA-256 校验；此设计不适用于人类口令。

首版鉴权每次读数据库，不缓存启停/权限状态；简化撤销生效语义。设置最小权限数据库账号，日志和查询响应不返回 key_hash。

### 7.4 request_logs

| 字段组 | 字段 | 说明 |
| --- | --- | --- |
| 身份 | request_id UUID PK、project_id FK、api_key_id FK | request_id 同时用于追踪与幂等结算 |
| 上游 | upstream_id、model、upstream_request_id NULL | upstream_id 首版固定；上游 ID 来自允许读取的响应头 |
| 模式 | is_stream BOOLEAN、admitted BOOLEAN | 是否流式、是否接纳 |
| 时序 | started_at、upstream_started_at NULL、finished_at NULL | started_at 为登记时刻；上游开始时刻是发送尝试时间，非对方收到的证明 |
| 性能 | duration_ms NULL、first_event_ms NULL | 单调时钟计算；首事件不等同于精确首 Token |
| 状态 | outcome、error_code NULL | started/completed/client_cancelled/timeout/upstream_error/gateway_error/unknown/rejected |
| HTTP | upstream_status_code NULL、response_status_code NULL | 与最终 outcome 分离 |
| 用量 | prompt_tokens NULL、completion_tokens NULL、total_tokens NULL | 上游报告值；缺失字段不强行补零 |
| 用量状态 | usage_status、usage_source、usage_missing_reason NULL | 见第 8 节；无 usage 时 source=none |
| 校验 | usage_validation_error NULL | 非负、字段缺失、总量不一致等类别，不存原始正文 |
| 恢复 | process_run_id UUID、settlement_attempts、updated_at | 定位未终结请求和崩溃恢复 |
| 限流 | rejection_scope NULL | global/project/key/upstream_unhealthy；仅持久化这四种值；其他入口拒绝仅运行计数 |

约束：rejected 记录 admitted=false、usage_status=not_applicable；started 记录 finished_at=NULL、usage_status=pending；终态完成需填 finished_at，unknown 的 finished_at 是确认未知的时间，不冒充真实请求结束时刻，duration_ms 保持 NULL。

索引：

- (project_id, started_at, request_id)：课题统计/明细。
- (api_key_id, started_at, request_id)：Key 查询。
- (started_at, request_id)：全局时间范围和归档。
- (process_run_id, started_at) WHERE outcome='started'：未完成恢复。
- (model, started_at)：支持模型过滤后时间范围扫描；不能保证无 model 过滤的 model+day 聚合走此索引。
- P6 必须保存 `EXPLAIN (ANALYZE, BUFFERS)`：project+day、model+day、key+model，分别有/无过滤，并记录数据倾斜、冷热缓存及索引体积。覆盖大部分表时顺序扫描可能是合理计划，验收看实测时间和资源，不以“用了索引”为通过条件。若超过 1s，先优化查询/索引并复测；仍不满足须走基线变更，不自动免测。

不存 Prompt、模型回答、完整请求体、完整 SSE 或 Authorization。课题历史展示当前名称；历史归属始终按 ID，重命名不会拆分统计。

`duration_ms` 从接纳请求到观察到终止结果计算，不包含之后的结算清理；`first_event_ms` 从同一起点到首个非注释、非空 SSE data 事件计算。两者使用单调时钟，持久化时间戳只用于时间范围查询。首事件可能是角色信息，不能对外标作精确 TTFT。

### 7.5 health_checks

id UUID、upstream_id、probe_kind（reachability/inference）、status（healthy/degraded/unhealthy/skipped）、checked_at、latency_ms、http_status NULL、error_code NULL、model NULL。

索引 (upstream_id, checked_at, id)。只存脱敏错误类别与必要描述；不存探测凭证和完整模型响应。探测用量不计入课题账表，注明实际可能消耗上游推理资源。

### 7.6 admin_audit_logs

id UUID、actor（首版为 admin）、action、resource_type、resource_id、request_id、created_at、changes JSONB。

changes 仅包含允许的变更字段与脱敏前后值，不含 Key 明文/哈希或上游密钥。课题和 Key 的管理变更与审计写入同一个事务；审计失败则变更回滚。

### 7.7 迁移与保留

- 首个迁移建立表、外键、CHECK 与索引；后续变更生成迁移后人工审查，不能把自动生成结果直接视为正确。
- 生产迁移显式运行 `alembic upgrade head`，启动时检查当前 revision 是否符合应用要求。异步环境的 Alembic 接入按官方 cookbook 实现。[S4]
- 请求明细默认保留 180 天、健康历史 30 天、管理审计 365 天，均为可配置建议。
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
- SSE 单事件解析缓冲初值上限 256 KiB。超限后停止该事件的计量解析并记录 invalid，仍在整体安全边界内继续转发；最终也不恢复成“完整计量”。
- `[DONE]` 表示完成信号，若其前发生显式错误事件，则 outcome 保持错误。usage 可位于 choices 为空的终结事件，不能丢弃。
- 默认只接受一次完整终结 usage；相同重复值不重复累计，不同重复值标记 invalid。任何分块都不会触发多次数据库累计。

### 8.4 转发前登记与幂等结束

对每个获得并发名额的请求，先提交一条 started 记录，再开始上游发送。最终更新采用 `WHERE request_id=:id AND outcome='started'` 的条件更新；同一请求重复清理不会生成多条记录，也不会重复累加 Token。

终态结算是更新一条原始记录，聚合查询从原始记录计算，不在请求路径中给累计总表加数。完成请求与重启恢复使用互斥的条件更新语义。

正常结算总尝试最多 2 次（首次 + 最多 1 次重试），每次 ≤1.5s，仅一次 0.1s 退避；共享 §5.3 的 5s 清理截止时间，每次尝试前检查剩余时间。通用 DB_OPERATION_TIMEOUT=2s 不覆盖此更短的专用结算时限。数据库恢复前无法落地的结果只保留 started 和脱敏故障日志，不宣称持久化完成。客户端响应不因已产生的结果结算失败而自动重试上游。

### 8.5 崩溃与数据库故障

| 场景 | 设计行为 | 可保证与不可保证 |
| --- | --- | --- |
| 鉴权前数据库故障 | 拒绝新请求 503 | 不绕过鉴权 |
| 登记提交失败 | 不转发，释放名额，503 | 不产生未登记的正常新上游调用；提交结果不确定时可能留下多余 started 记录 |
| 流进行中数据库故障 | 已接纳流继续，有界尝试结算 | 记录已存在；结束状态/Token 可能无法补全 |
| 进程崩溃或强制终止 | 原 started 留存，重启后恢复为 unknown | 保留接纳痕迹；不恢复完整响应和缺失 Token |
| 上游已生成但网络断开 | 关闭网关连接并记录异常 | 不保证上游计算立即停止或其最终用量可获知 |
| 客户端自行重试 | 视为新 request_id、新调用 | 不提供业务幂等，可能重复消耗 |

每次启动产生 `process_run_id`。运维必须确认旧网关进程已退出后启动唯一实例；启动恢复将旧 run 中的 started 标为 unknown。运行中的结算失败记录由后台扫描任务检查：仅处理本 run、已经不在活动请求注册表中的 started，宽限至少 60 秒后标记 unknown。不得仅以请求年龄判定超时并改写仍活动的记录。

不支持滚动双实例运行；不能在旧进程仍可能处理请求时执行恢复。单 worker/单实例是验收部署条件，并非由进程内锁提供的分布式保证。

## 9. 并发检测与限制

### 9.1 定义

在途请求 = 获得 admission token 后，尚未完成响应处理与有界清理的请求。流式请求保持名额直到流结束/取消并完成清理，不能在返回 StreamingResponse 对象时释放。

建议初始值：全局 16、每课题 4、每 Key 2。它们只是保守起点；真实上游容量通过 P0/P6 验证后调整。上限不承诺与上游 GPU 能力一致。

### 9.2 原子准入

1. 每次鉴权读取该课题和 Key 的已提交限制。
2. 获取进程内短时锁，依次检查全局、课题、Key 计数；该锁内不执行数据库、网络或其他长等待。
3. 任一达到上限，所有计数不变，返回拒绝范围；通过则三项一起加一，并登记唯一 token。
4. 最终释放只执行一次，三项一起减一；计数不得出现负数。

拒绝返回 429，`error.code=concurrency_limit_exceeded`，Retry-After 初值 1 秒，仅是建议重试等待，不是可用容量承诺。无队列，避免大量长连接堆积。

超限/健康门控拒绝若身份已明确，尝试写 admitted=false 的 rejected 记录；若数据库不可用，记录失败写入全局运行指标，响应转为 503，避免声称拒绝日志已落库。所有拒绝均不得发送上游业务推理请求；背景健康探测按独立来源计数。

### 9.3 监测输出

`/admin/runtime` 至少返回：

- process_run_id、started_at、uptime_seconds、accepting_requests。
- 全局 active、limit，以及按 project_id、key_id 分组的 active/limit。
- 自本次启动以来的 admitted_total、completed_total、cancelled_total、failed_total。
- `rejected_total`（对象）固定包含 global、project、key、upstream_unhealthy、database、authentication、validation、ingress、permission、routing、shutdown。单请求按最终拒绝原因只增加一类；例如原拟全局拒绝但拒绝日志落库失败改为 503，则只计 database。
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

- 可达性探测每 30 秒一次，单次超时 3 秒，任务不重叠。
- 最小推理探测每 300 秒一次，单次总超时 30 秒，固定短 Prompt，候选 max_tokens=1；上游不接受时在 P0 调整并记录。
- 连续 3 次可达性失败判 unhealthy；连续 2 次可达性成功恢复 healthy。1～2 次失败为 degraded。
- 缓存超过 90 秒未更新判 stale；首次启动为 unknown。接口必须展示状态、时间和失败原因，不能把旧 healthy 永久保留。
- 可推理状态独立保存；探测失败提示 degraded，不自动声称所有模型不可用。最近推理探测超过 600 秒未成功执行则标 stale。
- 推理探测最多一条在途，与业务共用全局上限；仅在同一准入锁内确认 `global_active <= GLOBAL_MAX_CONCURRENCY - PROBE_MIN_SPARE_SLOTS`（默认预留 2 个空槽）才占用 1 槽，启动后至少剩 1 槽。全局上限 <2、已有探测或空槽不足则 skipped。不进入课题/Key 计数。可达性 GET 不占推理名额。
- 这是低优先启动策略，不能保证完全不挤占后来业务：探测先启动后仍占槽，后续业务达到共享上限会 429。首版不抢占已开始的探测，也不允许突破上游总上限。A24 必须测试“先业务后探测”和“先探测后业务”；需要零探测竞争的场景关闭推理探测并显示 inference=disabled，可达性检测继续运行。

### 10.3 请求门控

只有可达性状态为已确认 unhealthy 且缓存新鲜时，对新业务请求返回 503 `upstream_unhealthy`。healthy/degraded/unknown/stale 允许带正常超时尝试业务请求，并在运行状态中显示健康信息不足。

此策略避免启动时尚未探测和探测任务偶发延迟导致永久拒绝；不提供复杂熔断半开算法。可达性探测即使业务被拒绝也继续运行，确保恢复检测可发生。

若某些引擎轻量探针不可靠，P0 可关闭门控但保留检测与展示，验收基线需明确 `HEALTH_GATE_ENABLED` 的最终值。默认验收启用门控；关闭时对应门控用例须通过范围变更记录处理，不能直接跳过。

### 10.4 默认检测时效的预估

在探测任务正常、网络故障持续且单次请求最长 3 秒的条件下，从故障发生到连续三次失败确认，候选验收上限为 100 秒；恢复确认候选上限 70 秒。实际按单调时钟测量，包含调度间隔；若主机过载或事件循环阻塞，必须记录偏差并修复或重新评估基线。

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
| CONCURRENCY_RETRY_AFTER | 1 秒 | 429 建议等待，不保证下一次可用 |
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
| DB_QUERY_TIMEOUT | 3 秒 | 管理聚合操作硬截止；不代替 P95≤1s 性能目标 |
| DB_POOL_SIZE | 16 | 常驻池上限；实际连接按需建立 |
| DB_MAX_OVERFLOW | 8 | 总连接上限 24；需与 DB 预算一致 |
| AUTH_DB_MAX_INFLIGHT | 4 | 鉴权及拒绝记录 DB 通道；无等待，满则 503 |
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
| HEALTH_REACHABILITY_ENABLED | true | 可达性检测开关；生产关闭需记录范围变更 |
| UPSTREAM_HEALTH_PATH | P0 必填 | 同一上游下的相对路径，不能含其他主机 |
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
| INGRESS_IP_BURST | 10 | 同 IP 突发容量 |
| INGRESS_IP_MAX_ENTRIES | 4096 | 固定内存上限；表满且无过期项，新 IP 返回 429 |
| INGRESS_IP_IDLE_TTL | 120 秒 | 回收不活跃桶；时钟用单调时间 |
| TRUSTED_PROXY_CIDRS | 空 | 默认 socket peer 为来源；只信任明确列出的代理 |
| CLOCK_MAX_OFFSET | 1 秒 | P0/运维校时前提，不是请求路径 NTP 查询 |
| DEPENDENCY_LOCK_TOOL | uv | 锁 uv.lock 并导出带 hashes 依赖清单；工具版本 P0 固定 |
<!-- CONFIG_END -->

启动校验必须覆盖：正数/非负与枚举范围；分页默认不大于上限；DEFAULT_TIMEZONE 属于 ALLOWED_TIMEZONES；探测模型在白名单中；HEALTH_GATE_ENABLED 依赖可达性探测启用；上游协议/CA/路径合法。

预算约束：`UPSTREAM_CLOSE_TIMEOUT + SETTLEMENT_MAX_ATTEMPTS × SETTLEMENT_ATTEMPT_TIMEOUT + (SETTLEMENT_MAX_ATTEMPTS-1) × SETTLEMENT_RETRY_BACKOFF < CLEANUP_TIMEOUT`，默认 3.6s < 5s。`APP_DRAIN_TIMEOUT + CLEANUP_TIMEOUT + 5s 调度余量 <= UVICORN_GRACEFUL_TIMEOUT`；`UVICORN_GRACEFUL_TIMEOUT + BACKGROUND_SHUTDOWN_TIMEOUT + 5s 余量 <= HOST_STOP_TIMEOUT`。宿主时限需要部署脚本/进程管理器单独配置并取证。

数据库不是每条流长期占一条连接；连接需求由短事务到达速率和占用时长决定，不能仅凭业务并发数判断性能。不过为避免默认配置的通道互相挤压，本版采用可解释的保守容量：业务登记/结算最多 G 个、鉴权/拒绝 4 个、管理 2 个、后台 2 个，所以池总上限要求 `DB_POOL_SIZE+DB_MAX_OVERFLOW >= G+4+2+2`，默认 24。各通道必须先取得进程内门控；鉴权/拒绝共用通道，不额外无限排队。提高 G 必须同时审查数据库 max_connections、其他应用占用与连接预算，不能自动无限扩池。

P6 测试报告增加 DB checkout 等待、SQL 时长、通道拒绝数、峰值连接数，以及 16 条同时登记/结算突发。池大小只能控制资源上界，不能保证额外 P95≤30ms；该目标仍以实测为准。查询性能超过目标时提供计划和优化证据。

总时限覆盖连接上游之前和传输期间，HTTPX read timeout 不是请求总时限。完整超时参数在 P0 固定；较短的结算专用时限优先于普通 DB_OPERATION_TIMEOUT。

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

桶数量固定上限 4096，定期清理闲置 120s 的桶；表满无过期项时新来源也返回 429，不无界创建状态。默认不信任 X-Forwarded-For；受信代理在入口适配层按明确信任链解析，P0 验证伪造头无法换 IP 绕过保护。公开 live/ready 不走鉴权 DB，ready 只读后台缓存，保护接口状态查询不会额外逐次打数据库。

通过桶后，鉴权/拒绝落库通道最多 4 条并发；满时返回 503 auth_capacity_exceeded、计 ingress，其他凭证状态待获得通道后再判断。首版不加否定凭证缓存，从而保留禁用/启用状态每次查库的语义。入口保护是单实例有界保护，不能保证被持续压满时所有合法流量都能获准，更不等同网络 DDoS 防护；A41 验证无无界内存/DB 并发增长及停止洪泛后的恢复。

### 11.4 时钟、时区、驱动与离线锁定

- 网关与数据库主机由运维接入内网 NTP/chrony（或等效校时），P0 记录时钟偏差 ≤1s；离线不要求公网 NTP。到期判断统一使用数据库 `statement_timestamp()`，请求存储时间同源；耗时/退避/TTL 使用单调时钟。时钟跳变会影响绝对过期/时间范围，须在故障记录中说明。
- 白名单只允许 UTC、Asia/Shanghai，增加时区需基线修订。应用固定 Python tzdata 包并使用该包的 ZoneInfo 数据（禁用系统 TZPATH 搜索）；日期桶由应用计算 UTC 边界传给 SQL，避免 PostgreSQL 与 Python tzdb 版本不一致。P0 记录 tzdata 版本，A29 验证边界。
- 首版数据库连接契约为直连 PostgreSQL。PgBouncer 事务池不在本版支持范围，不能只关闭 asyncpg 缓存就宣称兼容；发现现场必须使用代理时，P0 单独验证版本、prepared statement 行为与连接回收并修订契约。[S8]
- 依赖使用 uv 锁定，固定 uv 本身版本；联网构建环境依据 uv.lock 导出带哈希清单、按目标 OS/CPU/Python 收集 wheels，离线使用固定清单和 `--no-index --find-links ... --require-hashes` 安装。锁文件包含 tzdata；具体导出命令在 P0 用固定工具版本验证，不在现场动态解析新版本。

### 11.5 Python 环境与目标平台基线

- 最终目标为 Ubuntu 24.04 LTS；用户所述 x86 暂解释为 64 位 `x86_64`（Ubuntu 包架构 `amd64`）。部署时核验 `uname -m` 和 `dpkg --print-architecture`，与此不符须重新确认构建目标。
- Ubuntu 24.04 默认 Python 为 3.12；本次核对官方 noble-updates 的 python3.12 包为 `3.12.3-1ubuntu0.17`，不是 3.12.10。发行版包修订号与上游 Python 补丁版本分别记录，以目标机实际安装结果为准：[Ubuntu 包信息](https://packages.ubuntu.com/noble-updates/python3.12)。项目 `.python-version` 为 `3.12`，`pyproject.toml` 限定 `>=3.12.3,<3.13`。
- 当前开发环境为 macOS ARM64、独立 `.venv`、CPython 3.12.16。目标 Ubuntu 使用 `/usr/bin/python3` 创建自己的 `.venv`；不复制本机 `.venv`、`.tools` 或 macOS wheels。虚拟环境须在目标平台重建：[Python venv 文档](https://docs.python.org/3.12/library/venv.html)。
- 目标机需预装 `python3.12-venv`（离线环境由系统包源或配套离线 deb 包提供），在项目根目录运行 `bash scripts/bootstrap_ubuntu_venv.sh`。脚本验证 OS、架构及 Python 版本，创建或检查 `.venv`；遇到不兼容的已有环境退出，不覆盖已有目录。该脚本仅准备解释器环境，不安装项目依赖、不修改系统 Python。
- 当前依赖准备阶段使用 uv 0.13.8；`uv.lock` 与带哈希的 `requirements.txt` 仅锁定已引入的 asyncpg 0.32.0，尚非完整应用交付依赖。后续加入 FastAPI、SQLAlchemy、Alembic、tzdata 等时同步更新两份锁定文件。导出命令：`.tools/bin/uv export --format requirements.txt --no-dev --no-emit-project --locked -o requirements.txt`。
- 目标平台 wheelhouse 准备完成后，安装命令为 `.venv/bin/python -m pip install --no-index --find-links wheelhouse --require-hashes -r requirements.txt`。P0 在 Ubuntu x86_64 上验证安装及导入，并记录 `python3 --version`、`dpkg-query -W python3.12`、架构和依赖版本。当前仅完成本机环境及锁文件检查，尚未完成目标平台离线安装验收。

## 12. 日志、运行维护与交付

### 12.1 日志内容

结构化日志包含时间、级别、request_id、project_id/key_id、model、outcome、错误类别、耗时。默认不包含输入/输出正文、完整 Header、URL 凭证和原始上游错误栈内容。

业务请求一条开始事件、一条结束或结算失败事件；日志与数据库是两种证据。数据库失败时日志用于排查，但首版不提供从日志自动补账功能，不将日志视为可靠计费队列。

日志轮转按大小/天数配置，建议单文件 100 MiB、保留 7 天，防止撑满磁盘。磁盘不足应有清晰错误和运行状态，不能靠吞掉异常维持“全部正常”。

### 12.2 启动与停止

交付 README 给出目标系统上验证过的命令，流程为：

1. 准备 Python 虚拟环境、PostgreSQL 数据库与专用账号。
2. 离线安装锁定依赖，配置 `.env` 或环境变量。
3. 备份已有数据库，执行 Alembic 迁移。
4. 确认无旧网关实例存活；启动时完成旧 started 记录恢复。
5. 使用项目交付入口 `python -m app.server` 启动；该入口固定单 worker 并配置 Uvicorn graceful timeout=30s、安装提前排空的停止信号适配器。入口将在 P1/P3 实现，不能用普通 uvicorn CLI 替代后仍宣称通过 A33。
6. 验证 live/ready、上游健康、管理员权限与一条真实调用。

停止时序从收到首次 SIGTERM/SIGINT 的 t0 开始：

1. 项目启动器对固定版本 Uvicorn Server 的停止信号钩子做薄适配：同步标记 accepting_requests=false、ready=503，停止新探测；将已有请求监督器的取消截止时间设为 t0+20s，随后调用 Uvicorn 原始退出逻辑关闭监听。注册表与排空检查在单事件循环原子执行，避免漏掉刚接纳的请求。
2. 业务任务在 20s 排空窗口内可自然结束；到 t0+20s 统一取消剩余任务，各任务并行执行最多 5s 清理，目标 t0+25s 前释放名额。请求取消原因区分 shutdown，内部 outcome=gateway_error、error_code=shutting_down，不冒充客户端断开。
3. Uvicorn graceful timeout=30s，比应用排空+清理多 5s；资源关闭整体预算 3s，宿主进程管理器的强制停止时限设为至少 40s。A33 直接发 SIGTERM 并记录各时间点，不通过仅手动调用清理函数代替。
4. ASGI lifespan shutdown 可能发生在 Uvicorn 等待请求之后，不能把它作为开始 20s 排空计时的唯一触发点。信号适配器与固定 Uvicorn 版本一起测试；升级时重新验证。[S5]

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

以下按一名熟悉 Python Web 的开发者、每天可投入完整开发时间、上游/数据库及时可用估算。基线 24 个工作日，加 6 个工作日联调与缺陷缓冲，总计约 **24～30 个工作日（5～6 周）**。碎片化投入应按实际可用工时重新排期。

相比原先约 12 天的最小转发器，本版新增并发门控、健康任务、持久化状态机、故障测试、离线包和验收报告，因此不沿用原估算。工作日 D1 从环境具备条件且开发正式开始计算，不是日历日期承诺。

| 阶段 | 工作日 | 投入 | 主要工作 | 交付物 | 里程碑/退出条件 |
| --- | --- | --- | --- | --- | --- |
| P0 环境与协议验证 | D1–D2 | 2 天 | 核对 OS/Python/PG/端口、直连与时钟、uv/tzdata/离线包；测试 usage/取消/健康路径并冻结配置、连接预算与指标 | 环境基线、兼容矩阵、依赖版本、验收参数表 | M0：可直接访问数据库与上游，流式 usage 能力已确认 |
| P1 工程骨架与身份 | D3–D5 | 3 天 | app/lifespan、异步 DB、Alembic、课题与 Key、独立管理员鉴权、审计 | 可启动工程、首版迁移、管理 API 测试 | M1：创建课题→发 Key→鉴权通过；密钥不泄漏，迁移可重建 |
| P2 非流式闭环 | D6–D9 | 4 天 | HTTP 转发、登记/结算、usage 校验、错误映射、用量查询基础 | 非流式端到端、真实 DB 集成测试 | M2：返回正确响应，课题用量与上游一致，错误不串账 |
| P3 流式与故障收尾 | D10–D14 | 5 天 | SSE 旁路解析、取消、读/总超时、资源清理、unknown 恢复、流式统计 | 分块/断连/故障测试与示例 | M3：正常流即时转发，断开不泄漏，缺失值明确，重复清理不重复计量 |
| P4 并发与运行监测 | D15–D17 | 3 天 | 三层准入、一次性释放、动态限制、运行状态、超限拒绝 | 并发冲突及流式占槽测试 | M4：并发不突破配置，拒绝不调用上游，各出口恢复计数 |
| P5 健康检测与查询完善 | D18–D20 | 3 天 | 分层探针、迟滞/恢复、门控、历史、聚合/分页/时区、管理审计完善 | 健康故障演示、聚合正确性报告 | M5：故障/恢复按窗口可见，跨课题和跨日统计正确 |
| P6 联调、压测与交付 | D21–D24 | 4 天 | 真服务联调、压测、持续运行、离线安装、恢复演练、文档与验收 | 交付包、测试/压测/验收记录 | M6：全部核心用例通过，候选性能转为实测结论，限制明确 |
| 风险缓冲 | D25–D30 | 0～6 天 | 针对实际缺陷、内网依赖、上游兼容问题处理 | 修复与回归证据 | 不默认用于新增功能 |

### 13.2 每阶段演示

- M0：展示上游原始非流/流式输出与健康端点结果，确认 usage 是否存在。
- M1：创建课题 A/B，发两个 Key，错误 Key 401，课题 Key 无管理权限。
- M2：课题 A 连续调用 3 次，查到 3 条记录和确定的 Token 总量，B 不变。
- M3：一个完整 SSE，一个无 usage SSE，一个中断 SSE，展示三种状态；关进程重启后留下 unknown。
- M4：设置 Key 上限 2，同时保持两条流，第三条 429；关闭一条后新请求可接纳。
- M5：暂停模拟上游，观察 unhealthy/503；恢复上游，观察恢复；探测不混入课题账表。
- M6：按第 14 节从干净数据库执行验收，并在离线目标环境完成部署演示。

M2 是最早可展示业务价值的节点；只有到 M6 才能宣称完成本文件全部范围。阶段演示通过不等于最终验收通过。

P0 退出检查新增：§11.1 配置逐项冻结（秘密不写验收记录）；内网校时与 IANA/tzdata 版本；直连 PostgreSQL/无不兼容事务代理；uv 导出和离线重装试验；DB 24 连接预算与授权；健康来源分类及共享槽位策略；20/5/30/40s 停止链路可部署性。P1 固定错误信封、入口令牌桶、请求 ID 和 runtime 字段契约；P3 验证 receive 单一所有者、raw/identity 转发与信号适配器；P6 交付 EXPLAIN 和数据库等待分解。上述工作纳入原风险缓冲，若目标环境强制 PgBouncer 或新增协议，则重新估工期。

## 14. 验收测试设计

### 14.1 测试分层与证据

1. 单元测试：Token 校验、时间边界、并发原子操作、SSE 分块解析和状态转换。
2. 集成测试：真实 PostgreSQL 上的迁移、约束、事务、聚合与条件更新；不能仅用 SQLite 替代。
3. 网络端到端测试：独立 Uvicorn 与可控上游进程，通过 TCP 验证慢流、断开、异常和资源释放。
4. 真实上游联调：验证实际模型名、usage、健康路径、工具参数透传边界和实际首字时间。
5. 离线交付测试：断开公网情况下安装、迁移、启动、管理、调用、查询和备份恢复。

模拟上游提供以下模式：确定 usage 的 JSON；每 100ms 一个事件、共 20 个事件的 SSE；无 usage；UTF-8/行边界任意拆分；重复/非法 usage；4xx/5xx；长首块等待；半途断流；慢读写与无限等待。每次按 endpoint 与网关生成的来源标记记录上游 business/health_probe/unknown 次数；确认 429/401/503 未增加 business，不将背景探测混作业务。并发/性能隔离 fixture 启动时关闭探测，健康专项单独启用，未知来源必须报错。

每个用例保存命令或自动化测试 ID、配置、期望值、实际结果、request_id、必要 SQL 查询与脱敏日志位置。真实上游未知 Token 数不能替代模拟上游确定值测试。

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
| A09 | usage 缺失/异常 | 缺失记 missing、部分记 partial、负值/非法类型/总量不一致记 invalid；不混入完整 Token 总和 |
| A10 | 流式即时性 | 20 个间隔 100ms 的事件，客户端首事件≤500ms 且早于结束；DB 的 first_event_ms 非负且≤duration_ms，受控时钟单测精确等于首个有效 data 事件时间差，注释不触发计时 |
| A11 | SSE 分块 | UTF-8/CRLF/多 data 行/usage 空 choices/分块边界测试通过；转发字节完整且不重复计数 |
| A12 | 流式 usage | 正常结束且 usage 完整时记录精确；无 usage 仍 completed+missing |
| A13 | 断开释放 | 等待上游响应头、等待首事件和流中分别真实 TCP 断开各 20 次；健康环境下≤5s 名额归零、连接关闭且记录终态；验证只有一个 receive 所有者 |
| A14 | 超时与断流 | 可控上游延迟/断流，响应头前 502/504，流中记录正确 outcome；不伪造 DONE |
| A15 | 全局并发限制 | 无旧在途的干净实例以全局上限 3 启动且关闭探测，fixture 清零；保持三条业务流，第四条429，business 在途不超过3；配置变更不作为热更新测试 |
| A16 | 课题并发限制 | A 两个 Key 共用课题上限 2，第三个请求被拒绝；B 在全局有余量时可调用 |
| A17 | Key 并发限制 | 同一 Key 上限 2，第三个拒绝；一条结束后可再接纳 |
| A18 | 释放幂等 | 正常/异常/取消/重复结束回调后计数无负数、无泄漏、无重复日志；循环 100 轮通过 |
| A19 | 降低限额 | 课题/Key 通过 PATCH 从高限额降低，不中断已接纳请求；新鉴权按新限制执行；全局重启变更另按A15准备 |
| A20 | 拒绝统计 | 已归属限流/健康拒绝持久化 scope 为 global/project/key/upstream_unhealthy，不进接纳数与Token，business 调用增量0；拒绝落库失败仅增加 runtime.database 并503，不双计 |
| A21 | 存活与就绪 | 数据库断开时live200，后台检查失败后ready503（默认5s周期+2s操作时限）；停掉后台检查超过15s亦503；恢复并成功检查后ready恢复，无秘密 |
| A22 | 上游故障发现 | 持续故障在冻结窗口内成为 unhealthy；门控启用时新请求 503 |
| A23 | 健康恢复与过期 | 连续成功后解除拒绝；探测停止后显示 stale，不永久显示旧 healthy |
| A24 | 推理探测隔离 | 探测记录 history 不计课题；G=3时业务active=2则 skipped；探测先占1槽后允许2条业务、第三条429；全局总在途≤3；探测结束业务恢复；分别核对来源计数 |
| A25 | 登记失败不调用 | 注入登记失败/DB失联，503、上游 business 次数0、无名额残留；health_probe独立计数不污染结论 |
| A26 | 结束结算失败 | 上游成功后阻断数据库，结果按契约交付、无上游自动重试；started 可追踪，恢复后标 unknown |
| A27 | 崩溃恢复 | 已登记流期间强制终止唯一网关，确认进程退出再重启；旧 started 为 unknown，Token 不伪造，仍可查到请求 |
| A28 | 结算幂等 | 同 request_id 多次结算，只保留一条终态记录，聚合不重复 |
| A29 | 聚合边界 | 固定数据验证 [from,to)、白名单时区跨日、重命名、NULL/零值；同时放入 completed+reported 与 cancelled/timeout+reported，核对 all_reported_usage 和 completed_usage 子集及计数；拒绝未知时区 |
| A30 | 分页稳定性 | 同 started_at 多条数据用 (started_at,request_id) 游标分页，在静态数据集中无遗漏重复 |
| A31 | HTTP 边界与凭证 | 模型白名单/请求体上限生效；课题 Key 未传上游；错误消息和日志不泄漏秘密 |
| A32 | 离线运行 | 无公网访问条件下依赖安装、迁移、启动及核心调用成功；无远程 CDN/遥测依赖 |
| A33 | 优雅关闭 | 通过交付启动器运行并直接SIGTERM：t0拒绝新接纳、20s取消剩余任务、健康环境25s内释放，Uvicorn30s后备、宿主40s；验证ready与shutdown outcome，缺少提前信号适配不得通过 |
| A34 | 备份恢复 | 隔离库恢复成功，指定表记录数和确定时间范围聚合与备份时一致 |
| A35 | 管理变更审计 | 课题/Key变更与审计原子提交，审计失败回滚；管理员 audit-logs 可读脱敏分页，课题Key拒绝，SQL与API一致，无敏感字段 |
| A36 | 查询保护 | 非法 group_by、维度超限、超时间范围、超分组数、未知查询参数返回统一400；请求明细固定排序；禁止截断后冒充完整统计 |
| A37 | 模型目录 | 有效课题Key访问/v1/models返回配置白名单，未知/禁用Key401；不请求上游、不暴露秘密，目录不被误作实时健康结果 |
| A38 | 运行状态契约 | 逐类制造拒绝核对runtime字段、互斥分类及counter_scope；global_active=business_active+probe_active，正常/异常结束归零；进程重启计数清零而DB历史不变 |
| A39 | 错误信封与追踪 | 覆盖400/401/403/404/405/413/429/500/502/503/504、框架校验和上游401/429；source、code、X-Request-ID与X-Error-Source一致，405 Allow保留，客户端/上游ID无法覆盖；SSE成功头也含ID |
| A40 | HTTP转发边界 | 模拟identity字节逐块一致；上游gzip/br在发下游头前502；不复制上游Cookie/Set-Cookie/Content-Encoding/Transfer-Encoding，允许ASGI服务器自行选择下游传输分帧；验证来源头不可伪造、非法Content-Type被拒绝 |
| A41 | 入口保护与恢复 | 洪泛未知Key及伪造转发头，验证进程/IP桶、最大4096项、鉴权DB≤4；入口拒绝无DB访问；停止洪泛并补充令牌后合法请求恢复，已禁用Key不因缓存继续有效 |
| A42 | 配置与环境冻结 | 逐项核对§11.1与P0表；非法预算/枚举/时区/连接配额启动失败；校时、直连PG、tzdata与uv版本和离线哈希有证据；冻结变更同步文档 |
| A43 | 文档交叉校验 | 运行python3 scripts/check_docs.py成功；临时副本改变用例名/配置值/性能值/追踪映射时必须失败，模板不得默认填写通过 |
<!-- CASES_END -->

测试中的“5 秒内”等默认数字在 P0 如调整，必须同步更新本表和性能基线；不能在验收失败后只修改报告中的期望值。

### 14.3 验收结论规则

- A01–A43 全部通过，真实上游兼容记录完整，交付资料齐备，才满足功能验收。
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
| F03 | A02,A03,A06,A25,A31,A41 |
| F04 | A07,A08,A14,A31,A37,A39,A40 |
| F05 | A10,A11,A12,A13,A14,A33,A40 |
| F06 | A08,A09,A11,A12,A26,A28,A29 |
| F07 | A05,A10,A13,A14,A26,A27,A28,A30,A33 |
| F08 | A18,A20,A38 |
| F09 | A15,A16,A17,A18,A19,A20,A24 |
| F10 | A21,A22,A23,A24 |
| F11 | A05,A29,A30,A36 |
| F12 | A03,A04,A31,A35,A39,A41 |
| F13 | A01,A32,A33,A34,A42 |
| F14 | A01,A10,A15,A29,A32,A34,A42,A43 |
<!-- TRACE_END -->

### 14.5 自动化文档校验

设计书是配置候选、验收名称、性能目标与追踪关系的唯一源。`python3 scripts/check_docs.py` 检查模板对应块完全一致、编号唯一且完整、F→A覆盖、配置预算算术、表格列数与代码块闭合；退出非零即文档检查失败。定版前必跑，未来 CI 可直接运行此命令，无需数据库或第三方依赖。

修改设计源表后运行 `python3 scripts/check_docs.py --sync-template`，只生成模板中标记的配置/用例/性能/追踪块，不改手写说明。该模式拒绝覆盖已填实际结果的模板；正式执行先复制模板为单独的验收报告，禁止拿已填写报告作为生成目标。`--docs-dir` 用于隔离副本验证失败路径。

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
| 额定负载稳定性 | 非故障/非拒绝用例中网关自身错误为 0；计数和完整 usage 记录全匹配 | 关闭探测，模拟每条流持续10s，按1条/秒渐增至16在途后维持至少30分钟，补位到达率不超4次/秒；另测探测竞争，分别报告 |
| 断开资源回收 | 健康 DB/模拟上游条件下 ≤5s | 断开后检查并发、连接与记录终态 |
| 健康发现/恢复 | 故障 ≤100s；恢复 ≤70s | 按第 10 节默认探测间隔持续注入故障 |
| 统计查询 | 100 万条合成日志、31 天范围、≤1000 分组，P95 ≤1s | 覆盖 project+day/model+day/key+model 的过滤与全范围查询；交付 EXPLAIN (ANALYZE, BUFFERS)、冷热缓存与DB checkout等待 |
| 持续运行 | 8 小时无崩溃、名额泄漏、连接数持续增长 | 模拟或真实负载；预热后比较稳定窗口，RSS 不持续单调增长，允许缓存/GC 波动 |
<!-- PERF_END -->

如果真实上游只支持 4 个并发，则生产限额应设为 4 或更低；不能为了达到网关的 16 并发候选目标压垮推理服务。网关自身能力可用模拟上游隔离测试。

不承诺“FastAPI 单实例必然安全承载 200～300 并发”。吞吐与响应大小、数据库写入、流持续时间、客户端速度、主机和上游容量都相关。

上述候选低延迟测试有明确到达率，不能推导为无限制吞吐承诺。16条长流稳态用错峰到达避免将入口保护误判为代理故障；过载/瞬时16条鉴权突发另做保护测试，允许按契约503/429但必须计数并确认恢复。若要提高吞吐压测的入口/鉴权限额，须另列配置与连接预算报告，不能拿放宽保护后的结果冒充默认配置。

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

健康记录按 30 秒可达性探测约 2880 条/日，再加实际推理探测。备份、WAL 和审计另算。压测日志不能不加区分地混入生产课题数据。

## 16. 风险、已知限制与应对

| 风险/限制 | 影响 | 应对与责任边界 |
| --- | --- | --- |
| 上游流式 usage 不支持 | Token 覆盖率降低 | P0 验证、保留缺失状态，接受次数统计；不无依据估算 |
| 上游兼容接口行为差异 | 某参数/事件不兼容 | 冻结引擎版本与协议矩阵，不声称无限兼容 |
| 客户端取消无法停止 GPU 推理 | 上游实际资源仍被占用 | 关闭连接、记录边界；真实取消能力由引擎验证 |
| 进程/机器崩溃 | 长流中断、usage 缺失 | 登记先行、unknown 恢复、备份，不保证无损 |
| PostgreSQL 故障 | 新请求拒绝、现有请求结算降级 | fail-closed 新准入，保留记录，运行状态可见 |
| 多 worker 误部署 | 并发限制被放大、恢复冲突 | 固定部署脚本与检查清单，只验收单实例 |
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
| 设计书版本与修订 | v1.2，正式验收前填写定版修订号 |
| Git commit / 发布版本 | 待开发交付时填写 |
| Python / 依赖锁 / PostgreSQL | P0 填写 |
| 上游引擎、版本、模型、参数 | P0 填写 |
| stream usage 模式与健康路径 | P0 填写 |
| 单实例进程管理方式 | P0 填写 |
| 并发、超时、请求大小 | P0 初定、P6 复核，变更需记录 |
| 性能环境与目标 | P0 冻结第 15 节基线 |
| 核心用例结果 | A01–A43 逐项附证据 |
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

## 19. 版本修订记录

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| v1.0 | 原始输入文档 | FastAPI + MySQL 最小课题分发网关，约 12 日计划 |
| v1.1 | 2026-11-14 | 改为 PostgreSQL 异步方案；五张业务表；补充三层并发、健康检测、计量状态机、失败恢复、24～30 工作日计划及 A01–A36 验收用例 |
| v1.2 | 2026-11-14 | 处理26条审查意见；明确20/5/30/40s退出链路、两次结算预算、探测竞争与来源、统一错误/追踪、配置清单、入口保护及连接预算；新增A37–A43、F→A矩阵与自动同步校验 |

## 20. v1.2 审查意见处理与文档检查记录

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
