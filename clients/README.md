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
  - Westerman 的 API 入口

当前 `config.local.json` 也按同样的键命名：

- `cmc_onedrive`
- `emtc_gdrive`
- `westerman_api`

## 约定

- 每个客户端目录保留自己的 `probe.py` 入口
- 客户端私有 OAuth / token 获取脚本放在自己的目录里
- 跨客户端可复用的代码只放 `common/`
- 运维检查命令统一放根目录下的 `ops/`

这样后面如果新增例如 `foo_s3`、`bar_sharepoint`，直接新建：

```text
clients/
  foo_s3/
    probe.py
  bar_sharepoint/
    probe.py
```
