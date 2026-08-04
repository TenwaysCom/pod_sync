# POD 同步系统项目交接文档

> 更新时间：2026-08-03  
> 生产地址：`https://pod.eleways.online`  
> Git：`git@github.com:TenwaysCom/pod_sync.git`  
> 生产目录：现有日志显示为 `/home/turbo/pod_sync`，接手时应到服务器确认

本文给产品、开发和运维共同使用。这里只说明接手系统真正需要知道的内容，不保存任何密码、token、API key 或私钥。

## 1. 系统做什么

系统处理 CMC、EMTC、Westerman 三家仓库/承运商的 POD（签收凭证）：

```text
获取 POD → 保存和校验文件 → 识别出库单/运单号
→ 检查 Odoo 唯一目标 → 写入 Odoo → 刷新对账
```

| 来源 | 获取方式 | 主要匹配依据 |
| --- | --- | --- |
| CMC | OneDrive | 文件名中的出库单号 |
| EMTC | Google Drive | tracking number |
| Westerman | Westerman API | Odoo 候选出库单和 shipment reference |

完整流程：

1. 先处理 Westerman 待拉取池，再同步 CMC、EMTC。
2. POD 下载到 `storage/pod/`，并登记完整性 manifest。
3. Phase 1A 识别出库单号或 tracking number，生成本轮 payload。
4. 推送前实时查询 Odoo，确认来源正确且只有一个目标。
5. 写入 Odoo `stock.tracking` 的 POD 字段。
6. Odoo 的 `tw_stock_tracking` 模块按条件汇总到 `stock.picking`。
7. 系统重新读取 Odoo，刷新总结对账。

重要：本系统实际写的是 `stock.tracking`，不是直接写 `stock.picking.x_studio_pod_link`。

## 2. 关键业务规则

### CMC、EMTC

- 只下载新增或发生变化的文件；
- 已下载且本地完整的文件会跳过；
- 文件缺失、大小或 hash 不一致时禁止推送；
- 单一来源失败时，其他来源仍可继续。

### Westerman

Westerman 先从 Odoo 找出需要继续查询 POD 的出库单，再放入 SQLite 池：

| 状态 | 含义 |
| --- | --- |
| `waiting_pod` | API 正常，但暂时没有 POD，后续自动再查 |
| `retry` | 网络、限流或临时错误，等待重试 |
| `downloaded` | POD 已下载，可进入识别和推送 |
| `completed` | Odoo 已完成，不再查询 |
| `expired` | 已不属于候选范围 |

候选刷新必须是最近且完整的 `complete`。如果是 `failed/truncated/stale`，Westerman 自动写入会关闭，但 CMC、EMTC 不受影响。单号 `probe.py` 只用于诊断，不能绕过池作为正式流程。

### Odoo 防错和幂等

- CMC、EMTC、Westerman 只能匹配各自 carrier 范围；
- tracking number 优先查 `stock.tracking`，未命中才 fallback 到 picking；
- 命中多条时不写，绝不能自动选第一条；
- 多来源对同一个目标发生冲突时转人工处理；
- Odoo 已有完全相同的 POD 和完成状态时跳过；
- URL 相同但状态不完整时补齐状态；新的有效本地 URL可以更新旧链接；
- 写入前必须实时检查 Odoo，不能只相信缓存。

原则：宁可暂时不写，也不能把 POD 写错出库单。

## 3. 日常怎么使用

主要页面：

| 页面 | 用途 |
| --- | --- |
| 定时任务 | 设置运行周期和执行步骤 |
| 任务与历史 | 查看每轮步骤、数字和原始日志 |
| POD 识别 | 查看 ready、无法识别和人工确认 |
| 总结对账 | 查看 Odoo 缺 POD、POD 找不到 Odoo等情况 |

定时任务已启用时，不需要每天分别点三个来源。日常只需要：

1. 查看最近完整流水线是否结束；
2. 检查三家来源有没有真实失败；
3. 看业务待处理数量是否突然增加；
4. 在总结对账处理未命中、多条命中和人工确认；
5. 抽查新记录能否在 Odoo 打开正确 POD。

### 怎么理解任务状态

| 状态 | 含义 |
| --- | --- |
| 成功 | 程序和业务均完成 |
| 成功，有待处理 | 程序正常，但有未找到、重复命中或人工确认 |
| 成功但有警告 | 主业务完成，报告等辅助步骤有警告 |
| 部分完成 | 有真实技术步骤失败，其他成功结果已保留 |
| 失败 | 关键步骤无法安全执行 |

例如：

```text
completed=35153
not_found=3929
ambiguous=642
needs_review=438
failed=0
```

这不是程序报错。35153 条已完成，5009 条属于业务待处理，任务应显示“成功，有待处理”，退出码为 0。只有 `failed>0`、`technical_failure_count>0`，或者出现 provider、文件、门禁、XML-RPC 异常时，才是真实技术失败。

## 4. 配置、运行和部署

真实配置是 `config.local.json`，不会提交 Git；模板是 `config.example.json`。

| 配置段 | 用途 |
| --- | --- |
| `pod_sync` | 生产域名和文件签名 |
| `download` | POD 存储目录和 hash 校验 |
| `cmc_onedrive` / `emtc_gdrive` | 云盘凭据和目录 |
| `westerman_api` | token、候选池和重试 |
| `odoo` | URL、DB、用户、API key、读取模式 |
| `odoo_reconcile` | Odoo 缓存和 carrier 映射 |

配置注意事项：

- `pod_sync.base_url` 必须是生产可访问域名；
- `download.base_dir` 不能随意修改，否则历史 POD 可能找不到；
- AWS 正式环境通常不使用本地 `127.0.0.1:7890` 代理；
- `odoo.read_backend` 当前推荐 `xmlrpc`；QueryDeluxe 64/65 仅保留回滚兼容；
- scheduler 配置在 `runtime/scheduler.json`，通常通过页面修改；
- 真实配置、token 和签名 URL不能放进 Git、issue 或普通聊天。

常用命令：

```bash
# 本地启动
cd /home/rick/pod_sync && ./start

# 完整流水线（默认包含真实 Odoo 推送）
venv/bin/python webapp/pipeline_runner.py --config config.local.json \
  --cmc-workers 5 --emtc-workers 5 --westerman-workers 3

# 只做下载、识别和报告
venv/bin/python webapp/pipeline_runner.py --config config.local.json \
  --skip-odoo-push

# 完整性检查
venv/bin/python ops/verify_sync_integrity.py --config config.local.json

# 测试
venv/bin/python -m unittest discover -s tests
venv/bin/python -m unittest discover -s phase1a/tests
```

生产部署：

1. 记录当前 commit，确认测试通过；
2. 备份 config、scheduler 和 Westerman pool；
3. 暂停 scheduler，等待正在写 Odoo 的任务结束；
4. 在 `/home/turbo/pod_sync` 拉取代码并安装 `requirements.lock`；
5. 使用生产现有的 systemd/supervisor/tmux/container 方式重启；
6. 检查页面、Odoo 连接、Westerman gate，并跑一轮安全预检；
7. 验证正常后恢复 scheduler。

不要通过清空 `runtime/` 或 `storage/` 解决部署问题。代码已更新但页面仍旧时，检查真实运行目录、PID、重启命令和反向代理 upstream。

## 5. 备份和常见故障

必须备份：

| 数据 | 默认位置 |
| --- | --- |
| 真实配置 | `config.local.json` |
| POD 和 manifest | `storage/pod/` |
| Westerman 池 | `runtime/westerman/pod_pull_pool.sqlite3` |
| 定时任务 | `runtime/scheduler.json` |

建议同时保留 `runtime/odoo_push/`、`runtime/jobs/` 和 `phase1a/out/`。`runtime/odoo_reconcile/` 可以重建。备份 SQLite 时应暂停写入或使用 SQLite backup，不能漏掉 WAL。

常见问题：

- Westerman 有 retry 但 `checked=0`：先看 `due_count`，通常只是没到 `next_check_at`；
- AWS 出现 `ProxyError`：检查 `proxy_url` 和服务环境代理变量；
- tracking 已有 POD，但 picking 没变：检查其他 tracking 和 Odoo `tw_stock_tracking` 汇总逻辑；
- tracking/picking 命中多条：交给 Odoo 数据负责人处理，不要改成取第一条；
- job 长期 running：看原始日志和 PID，重启前先确认是否正在写 Odoo；
- 报告失败但 POD 已写：报告不会回滚 Odoo，先核对 Odoo，再单独刷新报告。

如果怀疑发生错误匹配：立即关闭 scheduler 或 Odoo 推送，保留 job 日志、Phase payload、push history 和 tracking ID；修复后先 preflight，再小批量恢复。不要删除 POD、manifest 或 pool。

## 6. 接手时必须补齐

下面的信息放在公司内部受控文档中，只记录凭据保管位置，不记录凭据本身：

| 项目 | 必填内容 |
| --- | --- |
| AWS/SSH | 账号、Region、实例、堡垒机和密钥保管人 |
| 生产服务 | 服务管理器名称、重启命令、监听端口 |
| 网络 | Nginx/ALB、DNS、TLS 续期方式 |
| GitHub | 权限、发布审批人、当前生产 commit |
| 凭据 | CMC、EMTC、Westerman、Odoo、Web 密码的密码库位置 |
| 备份 | 位置、周期、保留时间和恢复负责人 |
| 监控 | 日志、磁盘、进程和任务失败告警接收人 |
| 联系人 | 三家来源、Odoo、产品和 AWS 负责人 |
| Odoo 模块 | `tw_stock_tracking` 生产版本和发布负责人 |

接手人应亲自完成：

- [ ] 登录服务器，找到真实进程、代码版本和重启方式；
- [ ] 打开控制台并解释主要页面；
- [ ] 运行完整性检查和一个已知 Westerman probe；
- [ ] 运行 Phase 1A，找到本轮 payload；
- [ ] 完成一次 Odoo 安全预检；
- [ ] 能区分“成功，有待处理”和真实失败；
- [ ] 暂停并恢复 scheduler；
- [ ] 完成一次代码更新、回滚和隔离恢复演练。

## 7. 接手开发需要知道

主要代码位置：

| 功能 | 位置 |
| --- | --- |
| 三家下载客户端 | `clients/` |
| 文件完整性 | `clients/common/` |
| 识别和 payload | `phase1a/` |
| 流水线、推送、任务和 scheduler | `webapp/` |
| 运维工具 | `ops/` |

当前已知风险：

1. runtime、SQLite 和文件锁是单机设计，多服务器部署前必须改造共享状态。
2. Odoo 推送仍会检查完整历史 payload；通用增量推送池尚未实现。
3. manifest、Phase 输出和 job 日志会持续增长，需要归档策略。
4. QueryDeluxe 兼容代码仍存在，确认 XML-RPC 长期稳定后再评估删除。
5. 生产服务管理、备份和告警不在 Git 中，必须由实际负责人完成交接。

后续修改同步、匹配或推送时，必须保留文件完整性、来源隔离、Westerman 门禁、Odoo 实时唯一性、幂等和本轮 payload 绑定。
