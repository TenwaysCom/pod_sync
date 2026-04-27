# Phase 1A

这个目录负责 Phase 1A 的 POD 侧处理：

- 读取 manifest JSONL
- 解析 `picking_name / tracking_number`
- 生成 `matched / unmatched report`
- 生成给 Odoo webhook 的 payload
- 生成巡检 dashboard

## 目录

- `run_phase1a.py`
  - 入口脚本
- `phase1a_pod/`
  - 核心逻辑
- `examples/`
  - 示例配置、样例 manifest、样例 Odoo scope
- `docs/`
  - 部署文档和运行文档
- `tests/`
  - Phase 1A 测试

## 配置模式

当前支持两种方式：

1. 手工指定 manifest
2. 直接复用同步层配置自动推导 manifest

### 1. 手工指定 manifest

```bash
cd phase1a
python3 run_phase1a.py --config examples/phase1a_config.example.json
```

### 2. 复用同步层配置

先在仓库根目录准备：

```bash
cp config.example.json config.local.json
```

然后运行：

```bash
cd phase1a
python3 run_phase1a.py --config examples/phase1a_from_sync.example.json
```

## 关键输出

每次运行都会生成：

- `parsed_documents.json`
- `pod_backfill_matched_report.csv`
- `pod_backfill_unmatched_report.csv`
- `odoo_backfill_payload.json`
- `phase1a_summary.json`
- `phase1a_dashboard.html`

巡检入口：

- `out/<run_name>/phase1a_dashboard.html`
