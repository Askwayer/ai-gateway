# P0 本地准备交接

本包用于现场补验，未包含网关业务实现，不代表 M0 已签收。目标：Ubuntu 24.04 x86_64 / 系统 Python 3.12；首版选定公司 Fast，客户端直连网关，无 Nginx。

当前交接版本r2：开发阶段始终使用DeepSeek与测试库，公司Fast只在公司环境联调/部署。M0阶段签收仍未完成。工期36人日+最多6天缓冲已由项目发起人于2026-11-15批准；用户已另行指令实施P1本地基座，未开始A验收、未完成整个P1。详见docs/P0-延期与P1例外记录.md。此包只交付P0补验材料，不包含P1应用源码或新增开发工具；应用需从主仓库取得。

旧无r2后缀包是历史快照。包内SOURCE_INDEX.json记录源清单，SHA256SUMS覆盖包内全部文件；外部docs/P0-本地收尾-证据索引-20261115.json记录当前源及包哈希。该包为项目内可分发材料（可能含内部地址），不含受控秘密；不要将可分发理解成可公开上传。

包内只有源码、候选配置、脱敏证据及锁定 Linux wheels，不包含真实 `.env`、`.env.company`、数据库或 API 密钥。真实公司配置应由受控渠道另行放到目标目录，权限 0600；也可以参考 `p0/company.env.example` 填写，不能把占位符当真实凭证运行。

从包根目录执行 `sha256sum -c SHA256SUMS`。操作系统 python3.12-venv 等前置条件应先由现场准备。按照 `p0/dependencies/README.md` 的 Ubuntu 断网安装步骤建立 `.tools/p0-offline-venv`，保存 host、安装和依赖导入证据。该 README 的 macOS 构建命令只供原开发机复现，现场不需要 macOS 工具。

随后在目标机运行（使用新的输出文件名，保留失败证据）：

```bash
.tools/p0-offline-venv/bin/python scripts/p0_direct_echo.py --output .tools/p0-evidence/direct-echo-target.json
.tools/p0-offline-venv/bin/python scripts/p0_check_company_upstream.py --output .tools/p0-evidence/company-models-target.json
.tools/p0-offline-venv/bin/python scripts/p0_preflight.py database --env-file .env.company --output .tools/p0-evidence/company-database-target.json
.tools/p0-offline-venv/bin/python scripts/p0_config_candidates.py --check
```

数据库预检优先读取进程 DATABASE_URL，只有未设置时才读取 --env-file；现场须确认没有旧的进程变量覆盖公司文件。公司连通性脚本显式读取 `.env.company`，仅发一次认证 GET /v1/models、禁用环境代理和重定向、没有推理调用。它不依赖 APP_ENV_FILE；主仓库P1配置CLI已实现显式环境选择，但不在此P0工具包内。

`p0_direct_echo.py` 仅监听本机回环，运行完关闭；六个直连检查与两个额外回环来源可行性结果分别记录。不要用六项通过掩盖多来源失败。原型不是生产入口，不支持可信代理链解析。

最终还需取得真实内网上游非流/SSE usage、探测参数、健康路径及取消/资源回收证据；补齐时钟/网络/资源/宿主停止预算，落实 A41 来源方案，再完成配置冻结与签收。详见 `docs/P0-直连原型与现场补验.md` 和 `docs/P0-配置候选清单.md`。
