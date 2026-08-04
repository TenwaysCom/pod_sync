# pod_sync

`pod_sync` 是一个独立的 POD 同步与 Phase 1A 处理仓库。

## 核心文档

- [POD 同步系统项目交接文档](docs/HANDOVER.md)

当前仓库只保留 4 类内容：

- 客户端入口与共享同步逻辑
- 运维巡检命令
- Phase 1A 匹配、报表和 Odoo payload 生成
- 最小测试与部署 / 运行文档

它刻意不包含：

- 真实 `config.json`
- 真实 token
- 本地下载文件
- 旧的实验输出目录
- Odoo 模块代码

## 目录

- `config.example.json`
  - 脱敏配置样例
- `clients/`
  - 按客户 / 来源拆开的同步入口
- `clients/common/`
  - 共享下载、manifest、完整性逻辑
- `ops/`
  - 完整性校验和状态检查命令
- `webapp/`
  - FastAPI 管理界面，负责页面触发同步、巡检和 Phase 1A
- `phase1a/`
  - Phase 1A 匹配、报表、dashboard
- `tests/`
  - 同步层测试
- `storage/pod/`
  - 默认本地存储根目录

## 本地配置

不要直接把真实配置提交进仓库。

推荐做法：

```bash
cp config.example.json config.local.json
```

然后把真实参数写到 `config.local.json`。
正式运行统一使用 `config.local.json`，这个文件默认不纳入 git。

当前建议使用这些配置段名：

- `cmc_onedrive`
- `emtc_gdrive`
- `westerman_api`

当前默认存储根目录是相对路径：

```json
{
  "download": {
    "base_dir": "./storage/pod"
  }
}
```

这意味着下载文件、manifest 和完整性报告默认都写到仓库内，不需要额外准备 `/data/backup/pod`。

## 快速开始

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.local.json
```

先验证云端访问：

```bash
python3 clients/cmc_onedrive/probe.py --config config.local.json --limit 1
python3 clients/emtc_gdrive/probe.py --config config.local.json --limit 1
```

Westerman 正式同步由 Odoo 候选清单和本地待拉取池驱动：

```bash
python3 clients/westerman_api/sync.py --config config.local.json --limit 100 --workers 3
```

池状态保存在 `runtime/westerman/pod_pull_pool.sqlite3`，有附件才下载到
`storage/pod/Westerman`；暂时无附件的出库单会按计划重查。
完整流水线会先执行 Westerman，再执行 CMC/EMTC，因此其他云盘故障不会阻止
Westerman 本轮候选搜索和下载。

### Odoo 普通接口读取模式

QueryDeluxe 64/65 可以按阶段替换为普通 Odoo XML-RPC。唯一的模式开关是：

```json
{
  "odoo": {
    "read_backend": "shadow"
  }
}
```

- `querydeluxe`：保持原行为，适合随时回滚。
- `shadow`：QueryDeluxe 仍是权威数据，同时刷新并对比 XML-RPC 缓存。
- `xmlrpc`：完全从 XML-RPC 缓存生成 POD 索引和对账数据，不访问 Query 64/65。

XML-RPC 缓存只保留 `delivery_type=spare/cmc/westman` 的出库单，以及这些
出库单关联的 `stock.tracking`（包含尚未写入 POD 的记录）。首次是全量构建，之后按 `write_date`
增量刷新，每 7 天自动全量校准一次。缓存路径默认为
`runtime/odoo_reconcile/odoo_reconcile.sqlite3`。

总结对账中的“命中其他承运商”也只在这三家目标承运商之间交叉检查；其他
delivery type 按本次业务范围排除，不再为了这个状态加载全部 Odoo 出库单。

生产切换建议先运行 `shadow` 并检查任务日志中的 `shadow_status`，确认稳定后再改为
`xmlrpc`。需要回滚时只需改回 `querydeluxe`，无需删除缓存。

### Odoo 增量推送池

完整流水线默认把 Phase 1A 的全量结果刷新到
`runtime/odoo_push/push_pool.sqlite3`，但不会再把全部历史记录逐条请求 Odoo：

- 首次部署先用上面的 XML-RPC 对账缓存批量校验历史目标；
- 只有新增文件、目标 URL 变化、状态不完整或到期复核项才进入逐条写入；
- 未找到项默认 24 小时后批量复核，歧义项默认 7 天后复核；
- 已完成项每 7 天通过一次全量缓存重新审计，防止 Odoo 外部修改长期漂移；
- 写入前仍执行原有的承运商范围、唯一性和当前值检查，任务中断后可安全重试。

不需要手工初始化：升级后的第一次完整流水线会自动创建池并完成首次校验。任务详情页会分别显示“本轮批量校验”和“本轮逐条请求”。紧急回退到旧推送方式时，可以在 `config.local.json` 设置：

网页“Odoo 推送”的预览也使用同一批量缓存，确认写入后使用同一增量池，因此不会再先预检 4 万条、随后又逐条重放 4 万条。

```json
{
  "odoo_push": {
    "incremental_pool_enabled": false
  }
}
```

再看 Phase 1A：

```bash
cd phase1a
python3 run_phase1a.py --config examples/phase1a_from_sync.example.json
```

## Phase 1A

Phase 1A 文档入口：

- [phase1a/README.md](/home/rick/pod_sync/phase1a/README.md)
- [DEPLOYMENT.md](/home/rick/pod_sync/phase1a/docs/DEPLOYMENT.md)
- [RUNBOOK.md](/home/rick/pod_sync/phase1a/docs/RUNBOOK.md)
- [clients/README.md](/home/rick/pod_sync/clients/README.md)
- [webapp/README.md](/home/rick/pod_sync/webapp/README.md)

## Web Console

安装依赖后可以直接启动 FastAPI 管理界面：

```bash
export POD_SYNC_ADMIN_PASSWORD='CHANGE_ME'
export POD_SYNC_SECRET_KEY='CHANGE_ME_RANDOM_32_BYTES'
python3 -m uvicorn webapp.app:app --host 0.0.0.0 --port 8080
```

页面里当前支持：

- 触发 `CMC / EMTC / Westerman` 同步入口
- 触发 `verify_sync_integrity` 和 `check_sync_status`
- 触发 `Phase 1A`
- 查看任务日志
- 直接在网页内打开最新 `Phase 1A dashboard`

安全注意：

- 不再提供默认后台密码；必须设置 `POD_SYNC_ADMIN_PASSWORD`，或在 `config.local.json` 设置 `admin_password`。
- 生产环境建议同时设置稳定的 `POD_SYNC_SECRET_KEY`，并通过 HTTPS 反向代理访问。
- 登录默认每个来源 IP 在 10 分钟内最多失败 5 次；可用 `POD_SYNC_LOGIN_RATE_LIMIT_MAX` 和 `POD_SYNC_LOGIN_RATE_LIMIT_WINDOW` 调整。
- `config.local.json` 包含 token/API key，建议权限为 `600`。
- 如果需要把本地 POD 文件链接写回 Odoo，请配置 `pod_sync.base_url`；系统只会写入 `/storage/files/...` 本地链接，不会写入 OneDrive/Google Drive 源链。`pod_sync.file_url_secret` 是可选项，配置后会要求签名 token；不配置时本地文件链接公开可读。
- 默认单文件下载上限是 200MB，可用 `POD_SYNC_MAX_DOWNLOAD_BYTES` 调整，设为 `0` 表示关闭限制。
- 后台任务默认最多并发 2 个、单任务最长 6 小时；可用 `POD_SYNC_MAX_CONCURRENT_JOBS` 和 `POD_SYNC_JOB_TIMEOUT_SECONDS` 调整。

复现部署时可以使用 `requirements.lock` 固定当前已验证的完整依赖版本。依赖漏洞扫描建议在装有 `pip-audit` 的环境中执行：

```bash
python3 -m pip_audit -r requirements.lock
```

Phase 1A 的主要产物：

- `matched report`
- `unmatched report`
- `odoo_backfill_payload.json`
- `phase1a_dashboard.html`

## 测试

同步层测试：

```bash
python3 -m unittest tests/test_sync_integrity.py
python3 -m unittest tests/test_drive_downloads.py
```

Phase 1A 测试：

```bash
python3 -m unittest discover -s phase1a/tests
```
