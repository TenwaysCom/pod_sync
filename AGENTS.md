# AGENTS.md

## 项目概述

`pod_sync` 是 POD（Proof of Delivery）同步系统。从云盘拉取签收单到本地，与 Odoo 出库单匹配，最终把 POD 链接推送回 Odoo。

## 目录结构

- `clients/` — 云盘同步客户端（CMC OneDrive、EMTC Google Drive、Westerman API），共享逻辑在 `clients/common/`
- `webapp/` — FastAPI 管理界面，入口 `webapp.app:app`
- `phase1a/` — POD 文件名解析、Odoo 匹配、backfill payload 生成
- `ops/` — 运维脚本（审计、完整性校验、报表刷新）
- `storage/pod/` — 本地 POD 文件存储 + `_integrity/` 完整性清单
- `runtime/` — 运行时数据（调度器状态、任务日志、Odoo 快照、推送历史）

## 数据流转

1. `clients/*/probe.py` 下载 POD 文件 → `storage/pod/{来源}/{路径}`
2. `phase1a/run_phase1a.py` 提取标识符、匹配 Odoo → `phase1a/out/{批次}/odoo_backfill_payload.json`
3. `webapp/pipeline_runner.py` 编排全流程：同步 → Phase 1A → 推送 → 报表
4. `webapp/odoo_push.py` 通过 XML-RPC 写入 Odoo 的 `stock.tracking` 表（不是 `stock.picking.x_studio_pod_link`）
5. `webapp/odoo_pod_compare.py` 对比 POD 标识符与 Odoo 出库单数据

## 常用命令

```bash
# 启动 Web 控制台
./start                                    # 默认端口 8123
python3 -m uvicorn webapp.app:app --host 0.0.0.0 --port 8080

# 运行完整流水线
python3 webapp/pipeline_runner.py --config config.local.json --cmc-workers 5 --emtc-workers 5

# 单独探测某个来源
python3 clients/cmc_onedrive/probe.py --config config.local.json --limit 1

# Phase 1A 解析 + 匹配
cd phase1a && python3 run_phase1a.py --config examples/phase1a_from_sync.example.json

# 审计/修复 Odoo 中已推送的远端源 URL（改为本地 URL）
python3 ops/audit_source_pod_links.py --config config.local.json --repair

# 完整性校验
python3 ops/verify_sync_integrity.py --config config.local.json

# 测试
python3 -m unittest tests/test_sync_integrity.py
python3 -m unittest tests/test_webapp_odoo_push.py
python3 -m unittest discover -s phase1a/tests
```

## 配置

- 本地配置：`config.local.json`（不提交到 git），模板：`config.example.json`
- 关键配置段：`cmc_onedrive`、`emtc_gdrive`、`westerman_api`、`odoo`、`pod_sync`、`download`、`querydeluxe`
- `pod_sync.base_url` 必须配置，才能生成指向本地 storage 的 URL
- `pod_sync.file_url_secret` 可选。不配时 storage 文件公开可读，生成的 URL 不带签名 token

## Odoo 推送

- 写的是 **`stock.tracking`** 表（`tw_stock_tracking` 模块），不是 `stock.picking.x_studio_pod_link`
- `get_local_file_url()` 生成 `{base_url}/storage/files/{编码路径}`。只有 `base_url` 缺失或本地路径不在 `base_dir` 下时才回退到源 URL
- **去重逻辑**：只有现有 URL **完全一致** 时才跳过。URL 变了（远端变本地）会触发 UPDATE 覆盖
- 修改已推送的旧记录：运行 `ops/audit_source_pod_links.py --repair` 或重跑一次 pipeline

## 匹配与对比

- **tracking_number 匹配优先级**：先查 `stock.tracking`（通过 `runtime/odoo_push/odoo_pod_index.json`）→ 未命中再查 `stock.picking.carrier_tracking_ref`
- **跨承运商命中**：已合并到 `matched`，不再有独立的"命中其他承运商"分类
- `has_stock_tracking_pod` 字段：交叉引用 `runtime/stock_tracking_pod_picking_ids.json` 标记已推送记录
- `runtime/odoo_push/odoo_pod_index.json`：stock.tracking 的 tracking_number → picking_name 索引

## 注意事项

- **无登录认证**：auth 中间件已改为直接放行，所有请求无需登录、无需 CSRF
- **调度器保存**：`tick()` 全程持 `self._lock` 读写，防止与用户手动保存竞态覆盖
- **CMC OneDrive 翻页 400**：`_get_collection_page` 对带 skiptoken 的 400 错误重试最多 5 次。大文件夹永久性编码错误仍可能失败
- **Phase 1A 模式**：`parse_only` 只提取标识符不匹配；`scope_match` 需要配置 `odoo_scope_path` 才会生成推送 payload
- **多 worker**：`./start` 脚本通过 `runtime/webapp_secret` 设置 `POD_SYNC_SECRET_KEY` 保证多 worker 密钥一致。直接 `uvicorn --workers N` 时文件持久化在 `runtime/.session_secret`
- **文件上传限制**：默认单文件 200MB，可用 `POD_SYNC_MAX_DOWNLOAD_BYTES` 调整（0=不限制）
- **任务并发**：默认最多 2 个、单任务最长 6 小时，可用 `POD_SYNC_MAX_CONCURRENT_JOBS` 和 `POD_SYNC_JOB_TIMEOUT_SECONDS` 调整
- **任务日志保留**：默认保留最近 30 天；每个任务结束后自动清理更早的已结束任务，也可在“任务与历史”页手动选择保留天数。可用 `POD_SYNC_JOB_RETENTION_DAYS` 调整，设为 `0` 关闭自动清理
