# 课题级 AI Gateway 项目开发详细设计书与验收基线

| 项目 | 内容 |
| --- | --- |
| 文档版本 | v1.1 |
| 编写日期 | 2026-11-14 |
| 适用对象 | 开发人员、部署运维人员、课题管理员、验收人员 |
| 技术方案 | FastAPI + SQLAlchemy 异步 ORM + asyncpg + PostgreSQL + Alembic |
| 部署场景 | 内网离线、小团队、单个本地 OpenAI 兼容推理服务 |
| 当前状态 | 开发前设计稿；不是已实现功能或已完成测试的声明 |
| 与 v1.0 的关系 | 替换 MySQL 方案，补充并发控制、健康检测、故障记录、阶段计划和验收方法 |

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
- 金额计费、充值、余额、账单、TPM/RPM 配额与按月 Token 硬限制。
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
| Python | 候选 3.12 或 3.13 | P0 按内网系统及 wheel 可用性冻结具体版本 |
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

1. 分配网关生成的 `request_id`，限制请求体大小，解析 JSON。
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

不能仅依靠在读到下一块数据时轮询断开，因为上游可能长时间不输出。实现需要覆盖等待响应头、等待首块和流消费期间的断开，由框架取消机制或单一 ASGI disconnect 监听器触发清理。

同一 ASGI receive 通道只允许一个明确的消费方；不同时让多个轮询器抢读请求消息。请求体读取完成后再建立断开监听。最终实现方式在 P3 通过真实 TCP 断开测试验证，不能仅用内存 ASGI 测试替代。

清理顺序：停止继续读写 → 有界关闭上游响应 → 有界结算 → 在外层 finally 释放 admission token。取消屏蔽总预算初始为 5 秒；每次资源关闭和数据库操作还需有独立超时。即使结算失败也必须释放名额。[S2][S3]

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
| 上游明确返回 4xx/5xx | 保留状态码与允许的业务错误体 | 若错误发生于流中，记录并终止 | upstream_error |
| 客户端断开 | 无法保证再送达 HTTP 响应 | 停止写入并清理 | client_cancelled |
| 处理完成 | 上游正常状态码 | 正常 SSE 结束 | completed |

自有错误结构统一为 `{"error":{"type":"...","code":"...","message":"..."},"request_id":"..."}`。错误消息不包含内部地址、上游凭证和数据库连接串。

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

`/health/ready` 不发起模型生成，也不因单次上游推理失败而指示网关进程应重启；上游业务可用性由管理健康接口展示。公开探针只返回最少信息。

上游 URL 固定在配置中；拒绝客户端传入的 `base_url` 等路由控制字段，不允许重定向到未知地址。上游请求头采用允许列表，替换 Authorization，移除 Host、Connection、Transfer-Encoding 等不应透传的头。

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

管理分页默认 50、上限 200；统计默认最大范围 31 天，可配置提高。from/to 必须是带时区的 ISO 8601 时间，采用 `[from,to)` 半开区间。内部存 UTC，展示默认 Asia/Shanghai；按日分桶先按指定时区计算日期边界。

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
    "prompt_tokens_sum":12000,
    "completion_tokens_sum":6000,
    "total_tokens_sum":18000
  }],
  "as_of":"2026-11-14T01:00:00Z"
}
```

- `request_count` = 已登记且获得并发名额的请求数，包括实际转发前失败的请求；它是网关接纳次数，不等同于上游已执行次数。
- `request_count = completed + failed + cancelled + pending + unknown`；failed 包含 timeout、upstream_error、gateway_error。
- `rejected_count` 独立展示，不计入 request_count。无法鉴权归属的 401、参数校验失败、登记失败不进入课题账表，仅进入全局运行计数/脱敏日志。
- usage 分类计数之和等于 request_count。已终结请求的完整 usage 覆盖率 = reported / (request_count - pending_count)；分母为零返回 NULL。
- 三个 Token sum 仅累计 `usage_status=reported` 的对应字段，保证总量的口径一致；partial 数据在明细中可见，不混入主汇总。
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
| 限流 | rejection_scope NULL | global/project/key/upstream_unhealthy |

约束：rejected 记录 admitted=false、usage_status=not_applicable；started 记录 finished_at=NULL、usage_status=pending；终态完成需填 finished_at，unknown 的 finished_at 是确认未知的时间，不冒充真实请求结束时刻，duration_ms 保持 NULL。

索引：

- (project_id, started_at, request_id)：课题统计/明细。
- (api_key_id, started_at, request_id)：Key 查询。
- (started_at, request_id)：全局时间范围和归档。
- (process_run_id, started_at) WHERE outcome='started'：未完成恢复。
- 模型索引仅在查询计划证明必要时增加，避免每条日志维护过多索引。

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

正常结算首次失败后最多重试 2 次，退避候选 100ms/300ms，全部受 5 秒清理总预算约束。数据库恢复前无法落地的结果只保留 started 和脱敏故障日志，不宣称持久化完成。客户端响应不因已产生的结果结算失败而自动重试上游。

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

超限/健康门控拒绝若身份已明确，尝试写 admitted=false 的 rejected 记录；若数据库不可用，记录失败写入全局运行指标，响应转为 503，避免声称拒绝日志已落库。所有拒绝均不得发送上游推理请求。

### 9.3 监测输出

`/admin/runtime` 至少返回：

- process_run_id、started_at、uptime_seconds、accepting_requests。
- 全局 active、limit，以及按 project_id、key_id 分组的 active/limit。
- 自本次启动以来的 admitted_total、completed_total、cancelled_total、failed_total。
- rejected_total 按 global/project/key/health/database 分类。
- accounting_write_failures_total、accounting_degraded、pending_settlements。
- 健康探测任务存活、最近运行时间及被跳过次数。

内存累计数在重启后清零；数据库历史统计不清零。接口明确 `counter_scope=process_lifetime`，禁止混同为历史累计量。所有读数用快照返回，空闲 Key 计数可移除，避免内存无限增长。

### 9.4 限制变更与部署约束

降低上限不取消已接纳请求；例如当前 4 个、上限改成 2，则暂停新接纳，直到在途数低于 2。Key 禁用同样只阻止后续鉴权。

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
- 推理探测最多一条在途，并占用全局并发名额；全局无空闲名额就 skipped，不挤占用户请求、不进入课题/Key 计数。可达性 GET 不占推理并发名额。

### 10.3 请求门控

只有可达性状态为已确认 unhealthy 且缓存新鲜时，对新业务请求返回 503 `upstream_unhealthy`。healthy/degraded/unknown/stale 允许带正常超时尝试业务请求，并在运行状态中显示健康信息不足。

此策略避免启动时尚未探测和探测任务偶发延迟导致永久拒绝；不提供复杂熔断半开算法。可达性探测即使业务被拒绝也继续运行，确保恢复检测可发生。

若某些引擎轻量探针不可靠，P0 可关闭门控但保留检测与展示，验收基线需明确 `health_gate_enabled` 的最终值。默认验收启用门控；关闭时对应门控用例须通过范围变更记录处理，不能直接跳过。

### 10.4 默认检测时效的预估

在探测任务正常、网络故障持续且单次请求最长 3 秒的条件下，从故障发生到连续三次失败确认，候选验收上限为 100 秒；恢复确认候选上限 70 秒。实际按单调时钟测量，包含调度间隔；若主机过载或事件循环阻塞，必须记录偏差并修复或重新评估基线。

### 10.5 探测与模型用量

探测不走课题 Key，不计入课题用量，也不伪造某个课题。但推理探测可能产生真实上游消耗，在 health_checks 中标识来源。用户查询的课题总量因此不等于上游服务的所有请求总量。

## 11. 配置、超时与安全边界

### 11.1 初始配置建议

| 配置 | 初始值/约定 | 说明 |
| --- | --- | --- |
| DATABASE_URL | `postgresql+asyncpg://...` | 由环境提供，不写入仓库 |
| ADMIN_API_KEY | 独立高熵随机值 | 不与课题/上游 Key 共用；首版轮换需重启 |
| UPSTREAM_BASE_URL | 内网固定地址 | 启动校验协议和地址；不跟随重定向 |
| UPSTREAM_API_KEY | 可选秘密 | 只用于上游，默认不继承客户端 Authorization |
| ALLOWED_MODELS | 显式模型列表 | 不允许任意路由名称 |
| GLOBAL_MAX_CONCURRENCY | 16 | P0/P6 调整 |
| HTTP_CONNECT_TIMEOUT | 5 秒 | 连接建立 |
| HTTP_POOL_TIMEOUT | 2 秒 | 获取上游连接 |
| HTTP_WRITE_TIMEOUT | 10 秒 | 请求体发送 |
| HTTP_READ_IDLE_TIMEOUT | 60 秒 | 等待响应/相邻网络读取的空闲上限 |
| REQUEST_TOTAL_TIMEOUT | 300 秒 | 包括流传输/慢客户端等待的业务总时限，清理另计 |
| CLEANUP_TIMEOUT | 5 秒 | 取消屏蔽与结束清理总预算 |
| DB_OPERATION_TIMEOUT | 2 秒 | 单次鉴权/登记/结算操作，含等待连接 |
| DB_POOL_SIZE / MAX_OVERFLOW | 5 / 5 | 初始小连接池，不按每条 SSE 常驻连接 |
| HTTP_MAX_CONNECTIONS | 32 | 高于业务并发，加探针余量；需随并发配置校验 |
| REQUEST_BODY_MAX_BYTES | 1 MiB | 文本首版，不支持无限大请求 |
| NONSTREAM_RESPONSE_MAX_BYTES | 16 MiB | 超限返回 502 并记录错误 |
| SSE_EVENT_PARSE_MAX_BYTES | 256 KiB | 解析缓冲边界 |
| GRACEFUL_SHUTDOWN_TIMEOUT | 30 秒 | 到期取消在途任务后有界清理 |
| DEFAULT_TIMEZONE | Asia/Shanghai | 存储仍为 UTC |

总时限必须覆盖连接上游之前和数据传输期间；HTTPX read timeout 不是整条请求的总时限。长推理或慢客户端会触发限制，不能无限占槽。最终参数需要结合上游首字延迟调整并冻结。

### 11.2 安全与数据最小化

- 管理 Key 通过 Authorization 发送，不使用 URL 查询参数；恒定时间比较，配置来源限制文件权限。
- 课题 Key 明文仅创建响应可见；响应设置 `Cache-Control: no-store`，应用日志、错误跟踪和审计不记录正文。
- 管理接口与推理接口明确分离鉴权逻辑，不允许管理员凭证意外被转发。
- 参数化 SQL；查询分组和排序使用允许列表，拒绝任意字段/SQL 片段。
- HTTPX 默认关闭非预期环境代理继承（如配置 `trust_env=False`），避免内网请求经外部代理；内部 CA 使用显式配置，不默认关闭 TLS 校验。
- 服务若跨不可信网络传输，应使用 HTTPS 或受控内部入口；网关本身不负责签发证书。离线环境仍需保护 Bearer 凭证。
- 默认禁用开放 CORS；Swagger UI 如启用则仅内网管理员使用，并打包静态资源，避免运行时 CDN 请求；也可在生产关闭文档页。
- 安装包、运行和探针不需要访问公网；无默认遥测或在线模型目录下载。

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
5. 使用 `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --timeout-graceful-shutdown 30` 启动。
6. 验证 live/ready、上游健康、管理员权限与一条真实调用。

默认关闭新流量接纳后进入优雅退出，允许在途请求在 30 秒窗口完成，到期取消。30 秒后仍未完成的长请求允许被中断，不算支持无损升级。状态更新/连接关闭清理预算需与宿主进程强制终止时限留出余量。

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
| P0 环境与协议验证 | D1–D2 | 2 天 | 核对 OS/Python/PG/端口，直连上游测试流/非流 usage、取消、健康路径；冻结范围与指标 | 环境基线、兼容矩阵、依赖版本、验收参数表 | M0：可直接访问数据库与上游，流式 usage 能力已确认 |
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

## 14. 验收测试设计

### 14.1 测试分层与证据

1. 单元测试：Token 校验、时间边界、并发原子操作、SSE 分块解析和状态转换。
2. 集成测试：真实 PostgreSQL 上的迁移、约束、事务、聚合与条件更新；不能仅用 SQLite 替代。
3. 网络端到端测试：独立 Uvicorn 与可控上游进程，通过 TCP 验证慢流、断开、异常和资源释放。
4. 真实上游联调：验证实际模型名、usage、健康路径、工具参数透传边界和实际首字时间。
5. 离线交付测试：断开公网情况下安装、迁移、启动、管理、调用、查询和备份恢复。

模拟上游提供以下模式：确定 usage 的 JSON；每 100ms 一个事件、共 20 个事件的 SSE；无 usage；UTF-8/行边界任意拆分；重复/非法 usage；4xx/5xx；长首块等待；半途断流；慢读写与无限等待。每次测试记录上游实际收到的请求次数，用于确认 429/401/503 没有偷偷转发。

每个用例保存命令或自动化测试 ID、配置、期望值、实际结果、request_id、必要 SQL 查询与脱敏日志位置。真实上游未知 Token 数不能替代模拟上游确定值测试。

### 14.2 核心验收矩阵

| 编号 | 验收项 | 测试方法与通过标准 |
| --- | --- | --- |
| A01 | 干净安装与迁移 | 空 PostgreSQL 执行 upgrade head 成功，应用 ready；重复执行无额外变更 |
| A02 | 鉴权正确 | 无 Key、随机 Key、过期 Key、禁用 Key 各返回 401；模拟上游收到次数为 0 |
| A03 | 管理权限隔离 | 无管理员凭证 401，课题 Key 403；正确管理员 Key 能创建课题与 Key |
| A04 | 密钥只显示一次 | 创建响应有明文，后续查询/数据库/日志/审计无完整明文；数据库仅存哈希和显示前缀 |
| A05 | 课题归属 | A/B 各发固定数量请求，分组计数与 request_id 明细一致，无串账 |
| A06 | 启停与过期 | 管理变更提交后的新鉴权生效；已经接纳的流仍按原契约完成 |
| A07 | 非流式正常转发 | 模拟上游业务 JSON 与返回结果语义一致，状态码正确，包含 request_id 响应头 |
| A08 | 非流式 usage | 100 次固定 usage 调用，逐条三字段及总和精确匹配；无重复记录 |
| A09 | usage 缺失/异常 | 缺失记 missing、部分记 partial、负值/非法类型/总量不一致记 invalid；不混入完整 Token 总和 |
| A10 | 流式即时性 | 20 个间隔 100ms 的事件，客户端在上游结束前收到首个事件；候选首事件延迟见性能基线 |
| A11 | SSE 分块 | UTF-8/CRLF/多 data 行/usage 空 choices/分块边界测试通过；转发字节完整且不重复计数 |
| A12 | 流式 usage | 正常结束且 usage 完整时记录精确；无 usage 仍 completed+missing |
| A13 | 断开释放 | 首事件前和流中分别断开各 20 次；健康测试环境下 5 秒内名额归零、上游网关连接关闭、记录终态 |
| A14 | 超时与断流 | 可控上游延迟/断流，响应头前 502/504，流中记录正确 outcome；不伪造 DONE |
| A15 | 全局并发限制 | 模拟上游保持流，将全局上限设为 3，第四个请求 429，上游同时业务请求不超过 3 |
| A16 | 课题并发限制 | A 两个 Key 共用课题上限 2，第三个请求被拒绝；B 在全局有余量时可调用 |
| A17 | Key 并发限制 | 同一 Key 上限 2，第三个拒绝；一条结束后可再接纳 |
| A18 | 释放幂等 | 正常/异常/取消/重复结束回调后计数无负数、无泄漏、无重复日志；循环 100 轮通过 |
| A19 | 降低限额 | 从高限额降低，不中断已接纳请求；新请求按新限制执行 |
| A20 | 拒绝统计 | 已归属的限流/健康拒绝记 rejected；不进入 request_count 和 Token 总量；无上游调用 |
| A21 | 存活与就绪 | 数据库断开时 live 200、ready 503；恢复后 ready 恢复；公开接口无秘密 |
| A22 | 上游故障发现 | 持续故障在冻结窗口内成为 unhealthy；门控启用时新请求 503 |
| A23 | 健康恢复与过期 | 连续成功后解除拒绝；探测停止后显示 stale，不永久显示旧 healthy |
| A24 | 推理探测隔离 | 探测能验证被测模型并记录 history，不增加课题用量，全局无名额时 skipped |
| A25 | 登记失败不调用 | 注入登记失败/DB 失联，503、上游收到次数 0、无并发名额残留 |
| A26 | 结束结算失败 | 上游成功后阻断数据库，结果按契约交付、无上游自动重试；started 可追踪，恢复后标 unknown |
| A27 | 崩溃恢复 | 已登记流期间强制终止唯一网关，确认进程退出再重启；旧 started 为 unknown，Token 不伪造，仍可查到请求 |
| A28 | 结算幂等 | 同 request_id 多次结算，只保留一条终态记录，聚合不重复 |
| A29 | 聚合边界 | 使用固定时间数据验证 [from,to)、上海时区跨日、课题重命名、空集合/NULL/零值和全部状态计数 |
| A30 | 分页稳定性 | 同 started_at 多条数据用 (started_at,request_id) 游标分页，在静态数据集中无遗漏重复 |
| A31 | HTTP 边界与凭证 | 模型白名单/请求体上限生效；课题 Key 未传上游；错误消息和日志不泄漏秘密 |
| A32 | 离线运行 | 无公网访问条件下依赖安装、迁移、启动及核心调用成功；无远程 CDN/遥测依赖 |
| A33 | 优雅关闭 | 停止接纳，短请求在窗口内完成；长流到期取消并清理，重启能处理残留记录 |
| A34 | 备份恢复 | 隔离库恢复成功，指定表记录数和确定时间范围聚合与备份时一致 |
| A35 | 管理变更审计 | 课题/Key 变更与审计原子提交；审计失败时变更回滚；敏感字段未落审计 |
| A36 | 查询保护 | 超大时间范围/分组结果/非法排序被拒绝或受约定限制，不返回部分结果冒充完整 |

测试中的“5 秒内”等默认数字在 P0 如调整，必须同步更新本表和性能基线；不能在验收失败后只修改报告中的期望值。

### 14.3 验收结论规则

- A01–A36 全部通过，真实上游兼容记录完整，交付资料齐备，才满足功能验收。
- 安全泄漏、课题串账、并发名额泄漏、已知完整 usage 统计错误、正常 SSE 被整包缓冲，均为阻断验收的问题。
- 已知限制必须与实际行为一致；例如允许缺失 usage，不允许将其填为 0 或显示“完整计量”。
- 性能按 P0 冻结的环境与指标测试；未冻结或缺少证据只能标“待验证”，不能给出“已通过”。
- 发现缺陷后修复并回归受影响的核心用例，保留问题及复测记录。范围变更须更新版本，不通过静默删除用例达成验收。

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

| 指标 | 初始目标（待 P0 冻结） | 测量方法 |
| --- | --- | --- |
| 常规业务并发 | 1～16 条在途请求 | 真实上游在安全容量内验证，最终上限取实测值 |
| 非流式网关额外耗时 | P95 差值 ≤ 30ms | 同一负载生成器分别直连/经网关访问固定 50ms 模拟上游，每组 ≥1000 次；记录两组分布，不冒充逐请求纯开销 |
| SSE 首事件额外耗时 | P95 差值 ≤ 30ms | 固定 SSE 模拟上游、相同并发下直连/经网关对比 |
| 即时流式交付 | 首事件到客户端 ≤500ms，且明显早于约 2s 流结束 | 100ms 周期、20 事件测试，网络按基线 |
| 额定负载稳定性 | 非故障/非拒绝用例中网关自身错误为 0；计数和完整 usage 记录全匹配 | 16 并发固定模拟上游，至少 30 分钟；排除注入故障但必须分别报告 |
| 断开资源回收 | 健康 DB/模拟上游条件下 ≤5s | 断开后检查并发、连接与记录终态 |
| 健康发现/恢复 | 故障 ≤100s；恢复 ≤70s | 按第 10 节默认探测间隔持续注入故障 |
| 统计查询 | 100 万条合成日志、31 天范围、≤1000 分组，P95 ≤1s | 说明课题/模型分布、缓存冷热、执行次数与执行计划 |
| 持续运行 | 8 小时无崩溃、名额泄漏、连接数持续增长 | 模拟或真实负载；预热后比较稳定窗口，RSS 不持续单调增长，允许缓存/GC 波动 |

如果真实上游只支持 4 个并发，则生产限额应设为 4 或更低；不能为了达到网关的 16 并发候选目标压垮推理服务。网关自身能力可用模拟上游隔离测试。

不承诺“FastAPI 单实例必然安全承载 200～300 并发”。吞吐与响应大小、数据库写入、流持续时间、客户端速度、主机和上游容量都相关。

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
| 设计书版本与修订 | v1.1，正式验收前填写定版修订号 |
| Git commit / 发布版本 | 待开发交付时填写 |
| Python / 依赖锁 / PostgreSQL | P0 填写 |
| 上游引擎、版本、模型、参数 | P0 填写 |
| stream usage 模式与健康路径 | P0 填写 |
| 单实例进程管理方式 | P0 填写 |
| 并发、超时、请求大小 | P0 初定、P6 复核，变更需记录 |
| 性能环境与目标 | P0 冻结第 15 节基线 |
| 核心用例结果 | A01–A36 逐项附证据 |
| 风险和范围变更 | 无则填写“无”，不得留空 |
| 开发方 / 验收方 / 日期 | 正式验收时填写 |

配套《验收记录模板》用于执行记录。模板中的空白代表尚未测试，不能被解释为默认通过。

## 18. 参考依据与阅读入口

以下是实现参考，不代表要整体引入这些项目；本文件的超时、限额、阶段和指标是针对当前场景的设计决定。

- [S1] [SQLAlchemy 异步 ORM 文档](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)：AsyncEngine、AsyncSession、并发任务中的会话使用。
- [S2] [HTTPX 异步与流式响应](https://www.python-httpx.org/async/)：客户端连接复用、流式转发和 response 关闭责任。
- [S3] [AnyIO 取消与超时](https://anyio.readthedocs.io/en/stable/cancellation.html)：取消传播、清理期间屏蔽与有界超时。
- [S4] [Alembic Cookbook](https://alembic.sqlalchemy.org/en/latest/cookbook.html)：异步数据库连接的迁移接入方法。
- [FastAPI 官方全栈模板](https://github.com/fastapi/full-stack-fastapi-template)：参考配置、工程组织与测试，不原样采用其同步数据库方案。
- [LiteLLM 虚拟 Key](https://docs.litellm.ai/docs/proxy/virtual_keys)：参考课题/团队 Key 分发思路。
- [LiteLLM 健康检测](https://docs.litellm.ai/docs/proxy/health)：参考进程就绪与模型可用性分离。

## 19. 版本修订记录

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| v1.0 | 原始输入文档 | FastAPI + MySQL 最小课题分发网关，约 12 日计划 |
| v1.1 | 2026-11-14 | 改为 PostgreSQL 异步方案；五张业务表；补充三层并发、健康检测、计量状态机、失败恢复、24～30 工作日计划及 A01–A36 验收用例 |
