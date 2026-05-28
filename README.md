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
