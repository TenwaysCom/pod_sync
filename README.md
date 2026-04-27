# pod_sync

`pod_sync` 是一个独立的 POD 同步与 Phase 1A 处理仓库。

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
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.local.json
```

先验证云端访问：

```bash
python3 clients/cmc_onedrive/probe.py --config config.local.json --limit 1
python3 clients/emtc_gdrive/probe.py --config config.local.json --limit 1
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
