# Phase 1A Runbook

This runbook covers the normal operating flow:

1. verify cloud access
2. run sync probes or downloads
3. verify manifests and integrity
4. run Phase 1A
5. inspect dashboard output

## Start

```bash
cd /srv/pod_sync
source .venv/bin/activate
```

## 1. Cloud Access Checks

### OneDrive

```bash
python3 clients/cmc_onedrive/probe.py --config config.local.json --limit 1
```

### Google Drive

```bash
python3 clients/emtc_gdrive/probe.py --config config.local.json --limit 1
```

## 2. Sync Operations

### OneDrive first-file download

```bash
python3 clients/cmc_onedrive/probe.py --config config.local.json --limit 20 --download-first-file
```

Expected output locations:

- `storage/pod/CMC/...`
- `storage/pod/_integrity/CMC/onedrive.jsonl`

### Google Drive first-file download

```bash
python3 clients/emtc_gdrive/probe.py --config config.local.json --limit 20 --download-first-file
```

Expected output locations:

- `storage/pod/EMTC/...`
- `storage/pod/_integrity/EMTC/gdrive.jsonl`

### API download

```bash
python3 clients/westerman_api/probe.py --config config.local.json --shipment-reference WDB2B/OUT/01358 --download
```

Expected output locations:

- `storage/pod/Westerman/{shipment reference}/{document id}_{sha256 prefix}_{file name}`
- `storage/pod/_integrity/Westerman/westerman_api.jsonl`

单票命令用于原子排障。正式运行由 Odoo 候选清单驱动待拉取池：

```bash
python3 clients/westerman_api/sync.py \
  --config config.local.json \
  --limit 100 \
  --workers 3
```

池位于 `runtime/westerman/pod_pull_pool.sqlite3`。正常的无附件响应会显示为
`waiting_pod`，不是失败；下载结果会写入上面的正式 storage 和 integrity manifest，
随后可直接进入 Phase 1A。Westerman 是显式 opt-in 来源：新旧 Scheduler 配置均
默认关闭该步骤，并且 `westerman_api.enabled` 必须明确设为 `true` 才会运行；
缺失该配置段不会自动启用。
候选扫描触及安全上限时按降级处理，不清理旧池状态，也不允许本轮 Westerman
写入 Odoo；扫描顺序优先最新出库单，避免新单长期饥饿。
候选刷新结果会持久化在池中，只有最近状态为 `complete` 且更新时间不超过
`candidate_refresh_max_age_hours` 才允许 Phase/Odoo gate 放行；默认门槛为 48
小时。`running`、`failed`、`truncated`、`skipped`、`unknown`、缺失/损坏池以及
过期的 `complete` 均 fail closed。时间按带时区的 ISO-8601 值换算为 UTC；时间戳
缺失、格式错误或无法安全解析时同样 fail closed。
Phase 1A 会按池状态过滤 Westerman manifest，只有至少一个有效 POD 已通过完整性
校验并处于待推送状态的记录可进入 payload；同票其他附件失败（包括可重试或鉴权
失败）会单独标记，不丢弃已经落盘的有效 POD。鉴权失败仍会熔断本批后续供应商
请求并报告降级；候选刷新为 `complete` 时，逐票池门禁仍可放行有效 POD。候选刷新
或池门禁失败时只隔离 Westerman，CMC/EMTC
仍继续识别和推送；执行 Odoo mutation 前还会使用相同的新鲜度门槛再次核验池
为 `downloaded/pending`，因此旧 payload、缺失池、不可读池或过期池都不能
绕过限制。

常见结果：

- `downloaded`：POD 已落盘，后续不再访问 Westerman。
- `waiting_pod`：暂未提供 POD，默认 24 小时后重查。
- `retry`：429、5xx 或网络异常，等待退避重试。
- `failed`：文档或响应无法安全处理，默认按 `failed_requeue_hours=24` 冷却后重试
  （允许范围 1..720 小时）。
- `completed`：Odoo 的 `stock.tracking` 已有 POD，是终态。
- `expired`：出库单已离开完整 Odoo 候选范围，不再查询或自动推送。

Westerman 文档内无时区时间按 `document_timezone=Europe/Amsterdam` 解释；该值必须
是有效 IANA 时区名，无法解析时不应猜测时间。

紧急停用时关闭 Scheduler 的 `Westerman API 同步` 步骤，或将
`westerman_api.enabled` 设为 `false`；不会删除池或已下载文件。

## 3. Integrity Checks

### Full verification

```bash
python3 ops/verify_sync_integrity.py --config config.local.json --source all
```

### Quick verification

```bash
python3 ops/verify_sync_integrity.py --config config.local.json --source all --skip-sha256
```

### Status summary

```bash
python3 ops/check_sync_status.py --config config.local.json --quick
```

## 4. Run Phase 1A

### Reuse sync config

```bash
cd phase1a
python3 run_phase1a.py --config examples/phase1a_from_sync.example.json
```

This reads:

- `../../config.local.json`
- manifests under `../storage/pod/_integrity/...`

### Use example manifests

```bash
cd phase1a
python3 run_phase1a.py --config examples/phase1a_config.example.json
```

### Use custom config

```bash
cd phase1a
cp examples/phase1a_from_sync.example.json phase1a_live.json
python3 run_phase1a.py --config phase1a_live.json
```

## 5. Main Outputs

Each Phase 1A run produces:

- `parsed_documents.json`
- `pod_backfill_matched_report.csv`
- `pod_backfill_unmatched_report.csv`
- `odoo_backfill_payload.json`
- `phase1a_summary.json`
- `phase1a_dashboard.html`

Most important files:

1. `pod_backfill_matched_report.csv`
2. `pod_backfill_unmatched_report.csv`
3. `odoo_backfill_payload.json`
4. `phase1a_dashboard.html`

## 6. Inspection

Open:

- `phase1a/out/<run_name>/phase1a_dashboard.html`

Focus on:

1. `Source Health`
2. `Anomalies`
3. `Matched Payload Groups`
4. `Document Explorer`

## 7. Common Issues

### Phase 1A returns 0 documents

Usually one of these:

- sync was not actually downloaded
- manifest files do not exist under `storage/pod/_integrity/...`
- `config.local.json` path is wrong

Check:

```bash
find storage/pod/_integrity -type f
```

### Unmatched documents exist but Odoo should have records

Check the Odoo scope file:

- `picking_name`
- `tracking_numbers`
- eligibility / scope flags

### Change output directory

Update only the Phase 1A config:

- `output_dir`

Do not change sync storage unless you intentionally want a different local storage root.
