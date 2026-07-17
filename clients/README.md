# Clients Layout

客户端同步入口按来源单独放目录，后面新增别的客户时继续往这里加，不再混放到一个 `scripts/` 目录里。

## 目录规则

- `common/`
  - 所有客户端共享的下载、manifest、完整性辅助逻辑
- `cmc_onedrive/`
  - CMC 的 OneDrive 入口
- `emtc_gdrive/`
  - EMTC 的 Google Drive 入口
- `westerman_api/`
  - Westerman Indicium API 入口
  - 先按 shipment reference 调用 `get_order_info`，再按 document ID 调用
    `download_document`，仅保存配置的 POD 文档类型

当前 `config.local.json` 也按同样的键命名：

- `cmc_onedrive`
- `emtc_gdrive`
- `westerman_api`

## 约定

- 每个客户端目录保留自己的 `probe.py` 入口
- 客户端私有 OAuth / token 获取脚本放在自己的目录里
- 跨客户端可复用的代码只放 `common/`
- 运维检查命令统一放根目录下的 `ops/`

Westerman 单票 POD 下载示例：

```bash
python3 clients/westerman_api/probe.py \
  --config config.local.json \
  --shipment-reference WDB2B/OUT/01358 \
  --download
```

正式 token 只放在不提交的 `config.local.json` 中。`--tracking-number` 仍作为旧配置的兼容参数保留。
下载文件使用 document ID 和内容 SHA256 前缀命名；同一内容重跑不会重复追加 manifest，内容变化也不会覆盖旧文件。

Westerman 正式批量入口使用 SQLite 待拉取池：

```bash
python3 clients/westerman_api/sync.py \
  --config config.local.json \
  --limit 100 \
  --workers 3
```

每次运行先从 Odoo 只读发现 `delivery_type=westman` 且尚无
`stock.tracking.pod_link` 的出库单，再原子认领到期项。查询成功但供应商暂时没有
POD 的记录进入 `waiting_pod`，默认 24 小时后再查；下载成功进入 `downloaded`
且不再请求供应商；Odoo 已存在 POD 时进入 `completed`；完整候选刷新后已离开业务
范围的记录进入 `expired`。扫描触及上限时不会执行过期清理，避免截断结果造成误判。
同时本轮标记为降级并禁止 Westerman Odoo 写入；扫描按最新出库单优先，避免新单
长期饥饿。若持续截断，应在不超过 50000 的范围内提高 `candidate_scan_limit`，或
缩短 `candidate_lookback_days`。429、5xx 和网络错误按退避时间重试，401/403 会
熔断本批后续请求。

默认池文件是 `runtime/westerman/pod_pull_pool.sqlite3`。`--limit 0` 使用配置中的
`pool_batch_limit`（默认 100），不会无界拉取。`proxy_url` 只作用于 Westerman
HTTP 会话，不影响 Odoo、CMC 或 EMTC。
在完整流水线中 Westerman 排在 CMC/EMTC 之前，避免云盘来源硬失败导致它本轮
没有机会轮询。

Phase 1A 只接收池中 `downloaded/pending` 的 Westerman manifest；`retry`、
`failed`、`expired` 和已在 Odoo 终结的记录不会进入自动推送。若本轮 Odoo 候选
刷新失败，流水线仍处理 CMC/EMTC，但会阻止本轮所有 Westerman Odoo 写入。
最近一次候选刷新状态也持久化在池中，只有 `complete` 才放行；`running`、
`failed`、`truncated`、`skipped` 或尚无刷新记录时，后续手工 Phase 和旧 payload
同样被拦截。
Odoo 真正写入前会再次读取池状态，旧 payload 也不能绕过这道限制；池文件缺失
或不可读时拒绝 Westerman 写入。单票含多个附件时，如已有有效 POD 落盘但另一个
附件失败（包括可重试或鉴权失败），有效 POD 保持 `downloaded/pending`，当前流水线
即可继续推送；附件级失败通过 `partial_document_failure_count` 保留告警。鉴权失败
仍会熔断本批后续供应商请求并将任务标记为降级；只要候选刷新为 `complete`，已验证
POD 仍由逐票池门禁放行。通用“单条推送”入口
拒绝 Westerman，必须通过上述
池驱动 payload 写入。批量 Odoo push 使用跨进程锁串行执行，避免两个任务并发
查到“尚无 stock.tracking”后重复创建记录。

这样后面如果新增例如 `foo_s3`、`bar_sharepoint`，直接新建：

```text
clients/
  foo_s3/
    probe.py
  bar_sharepoint/
    probe.py
```
