# POD 拉取接口文档

> 适用代码：`clients/`（CMC OneDrive、EMTC Google Drive、Westerman API）
> 说明：本文只描述「拉取 POD」链路上真正发出的外部接口调用（含认证），列出代码实际使用的请求参数与响应字段，并标注对应实现位置。响应中未被代码读取的字段不保证列全。

## 0. 总览

| 来源 | 协议 | 入口 | 关键实现 |
| --- | --- | --- | --- |
| CMC | Microsoft Graph REST（OneDrive） | `clients/cmc_onedrive/probe.py` | `RefreshingGraphSession`、`iter_collection`、`download_file` |
| EMTC | Google Drive API v3 | `clients/emtc_gdrive/probe.py` | `RefreshingGDriveSession`、`iter_items`、`get_download_request` |
| Westerman | Indicium HTTP API（另有 generic 兼容模式） | `clients/westerman_api/probe.py` | `_indicium_headers`、`_run_indicium`、`_parse_indicium_response` |
| Westerman 候选 | Odoo XML-RPC | `clients/westerman_api/candidates.py` | `_read_candidate_pickings` |

数据流：

```text
Odoo 候选查询（Westerman）→ 池 → get_order_info → download_document
CMC/EMTC：列目录 → 逐文件下载 → storage/pod/{来源}/… + _integrity/ manifest
```

三个来源的探测脚本都是 CLI 工具，由 `webapp/pipeline_runner.py` 编排调用。

---

## 1. CMC OneDrive（Microsoft Graph）

### 1.1 认证：OAuth2 刷新令牌

- **请求**：`POST https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token`

`tenant_id` 取自配置，默认 `common`（`build_token_url()` 在 `clients/cmc_onedrive/probe.py`）。

| 参数 | 位置 | 必填 | 说明 |
| --- | --- | --- | --- |
| `client_id` | form | 是 | 应用 ID |
| `refresh_token` | form | 是 | 长期刷新令牌 |
| `grant_type` | form | 是 | 固定 `refresh_token` |
| `scope` | form | 否 | 默认 `https://graph.microsoft.com/.default offline_access` |
| `client_secret` | form | 否 | 配置了才发送 |

- **响应**：`access_token`（缺失即报错）、`refresh_token`（返回则覆盖并写回配置文件，权限 600）。

触发时机：启动时 `force_refresh`；Graph 请求返回 401 时自动刷新并重放一次（`RefreshingGraphSession.get()`）。

### 1.2 列目录（列出 POD 文件）

三种配置模式（`iter_all_items()`）：

| 模式 | 请求 URL | 说明 |
| --- | --- | --- |
| `root_path` | `GET https://graph.microsoft.com/v1.0/drives/{drive_id}/root:{path}:/children` | 无 `drive_id` 时为 `me/drive/...` |
| `root_item_id` | `GET .../drives/{drive_id}/items/{item_id}/children` | 先取 item 元数据拿根目录名 |
| `shared_url` | `GET https://graph.microsoft.com/v1.0/shares/u!{base64url(shared_url)}/driveItem[/children]` | 公共 Vroom 链接改用 `https://api.onedrive.com/v1.0` 或 `https://my.microsoftpersonalcontent.com/_api/v2.1`，附加 `authKey` 查询参数 |

公共链接解析（`resolve_shared_access()`）：对 `1drv.ms` / `onedrive.live.com` 先跟随跳转拿落地 URL，再提取 `authkey`、`redeem`、`migratedtospo` 判断使用哪个 API 基址。

**请求头**：`Authorization: Bearer <access_token>`（公共 Vroom 链接不带，仅靠 `authKey`）。

**分页**：不分页参数。响应的 `@odata.nextLink` 原样作为下一页 URL 继续请求，直到字段消失。

**响应字段**（顶层 `value[]`，逐项读取）：

| 字段 | 用途 |
| --- | --- |
| `id`、`name` | 文件标识与落盘名 |
| `folder` | 存在即目录（递归入队） |
| `file.mimeType` | 文件类型 |
| `size`、`createdDateTime`、`lastModifiedDateTime` | 增量比对与 manifest |
| `@microsoft.graph.downloadUrl` | 临时下载直链（下载优先使用） |
| `webUrl` | 来源页面 URL |
| `parentReference.driveId / id / path` | 路径还原、跨盘 |
| `remoteItem.parentReference.driveId` | 共享盘 drive_id 解析 |
| `eTag`、`cTag` | 游标重启时的重放去重 |
| `@odata.nextLink` | 下一页游标 |

**重试与游标容错**（`_get_collection_page()` / `iter_collection()`）：

- 状态码 `408, 409, 429, 500, 502, 503, 504` 自动重试，最多 5 次；优先遵循 `Retry-After`，否则指数退避。
- 带 `$skiptoken` 的 400 视为可重试（个人盘续页偶发 400）。
- 重试后仍是带 `$skiptoken` 的 400：判定游标失效，从集合首页 URL 重启，最多 2 次，间隔 5s 起指数退避；重启后按 item 版本（eTag/cTag/时间/大小等）去重。
- 失败抛 `SafeRequestError`，错误消息不含请求 URL（token 不泄漏）。

### 1.3 单文件元数据

- **请求**：`GET https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}`（或 `me/drive/...`）
- **参数**：无查询参数；headers 同 1.2
- **响应**：同上单个 item 字段；用于 `--download-file-id` 直接下载

### 1.4 下载文件

按优先级选择 URL（`download_file()`）：

1. item 自带 `@microsoft.graph.downloadUrl` → 直接 GET 该临时直链（不带 headers）
2. `shared_url` 模式且为分享根文件 → `GET .../shares/{token}/driveItem/content`
3. 其它 → `GET .../drives/{drive_id}/items/{item_id}/content`

请求均为 `stream=True`，响应为**文件字节流**，边读边写并计算 SHA-256，登记 manifest（`download_and_record_file()`）。

### 1.5 CLI 参数（`clients/cmc_onedrive/probe.py:main`）

`--config`（必填）、`--limit`（默认 20，≤0 表示不限）、`--recursive`、`--download-first-file`、`--download-all-files`、`--download-file-id`、`--output-dir`、`--workers`（默认 1）、`--incremental`。

---

## 2. EMTC Google Drive（Drive API v3）

### 2.1 认证：OAuth2 刷新令牌

- **请求**：`POST https://oauth2.googleapis.com/token`（form）

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `client_id` | 是 | OAuth 客户端 ID |
| `client_secret` | 是 | OAuth 客户端密钥 |
| `refresh_token` | 是 | 长期刷新令牌 |
| `grant_type` | 是 | 固定 `refresh_token` |

- **响应**：`access_token`（缺失即报错）、`refresh_token`（返回则覆盖并写回配置）。
- 触发时机：启动时强制刷新；请求 401 时刷新并重放一次（`RefreshingGDriveSession.get()`）。

### 2.2 列文件

- **请求**：`GET https://www.googleapis.com/drive/v3/files`（`iter_items()`，按 BFS 逐层递归）

| 参数 | 值 / 说明 |
| --- | --- |
| `q` | `'{folder_id}' in parents and trashed = false` |
| `pageSize` | `min(limit, 100)`，不传 limit 时为 100 |
| `fields` | `nextPageToken, files(id, name, mimeType, size, createdTime, modifiedTime, webViewLink, webContentLink, parents, driveId)` |
| `supportsAllDrives` | `true` |
| `includeItemsFromAllDrives` | `true` |
| `corpora` | 配置了 `shared_drive_id` 时为 `drive` |
| `driveId` | 配置了 `shared_drive_id` 时填入 |
| `pageToken` | 翻页游标，取自上次响应的 `nextPageToken` |

**请求头**：`Authorization: Bearer <access_token>`。

**响应字段**：`files[]`（`id`、`name`、`mimeType`、`size`、`createdTime`、`modifiedTime`、`webViewLink`、`webContentLink`、`parents`、`driveId`）、`nextPageToken`。目录判定：`mimeType == application/vnd.google-apps.folder`。

### 2.3 单文件元数据

- **请求**：`GET https://www.googleapis.com/drive/v3/files/{file_id}`（`fetch_file_metadata()`）
- **参数**：`fields`（同 2.2 的单文件版）、`supportsAllDrives=true`
- **响应**：单个文件对象（同上字段）

### 2.4 下载文件

按 MIME 类型二选一（`get_download_request()`）：

| 场景 | 请求 | 参数 |
| --- | --- | --- |
| Google 原生文档（document/drawing/presentation/spreadsheet） | `GET /drive/v3/files/{file_id}/export` | `mimeType`（默认 `application/pdf`，可被 `--download-export-mime-type` 覆盖） |
| 普通文件 | `GET /drive/v3/files/{file_id}` | `alt=media`、`supportsAllDrives=true` |

`stream=True`，响应为文件字节流；导出模式下不校验远端大小。

### 2.5 CLI 参数（`clients/emtc_gdrive/probe.py:main`）

`--config`（必填）、`--limit`、`--recursive`、`--download-first-file`、`--download-all-files`、`--download-file-id`、`--download-export-mime-type`、`--output-dir`、`--workers`、`--incremental`。

---

## 3. Westerman API

### 3.1 通用请求头与响应包裹

`api_mode=indicium` 时所有请求带（`_indicium_headers()`）：

| Header | 值 |
| --- | --- |
| `{api_key_header}`（默认 `Authorization`） | `{api_key_prefix} {api_key}`（默认 `Bearer <token>`；已带前缀则不重复加） |
| `Accept` | `application/json` |
| `Content-Type` | `application/json` |

**响应包裹格式**（`_parse_indicium_response()`，两个接口通用）：

```json
[ { "status": "ok", "body": { ... } } ]
```

- 顶层必须是**非空 JSON 数组**，否则报错；
- `payload[0].status` 必须为 `ok`（大小写不敏感）；
- `payload[0].body` 必须是对象，业务字段都从 `body` 读取；
- 非 2xx 抛 `WestermanError`（带 `http_status`、`retryable`（429/5xx）、`auth_error`（401/403）标记）。

### 3.2 查询发货单 / 文档列表：`get_order_info`

- **请求**：`POST {base_url}{order_info_path}`，默认路径 `/get_order_info`，`allow_redirects=False`
- **请求体**（JSON）：

```json
{ "shipment_reference": "<发货引用/运单号>" }
```

- 单号来源：Westerman 池（池数据来自 Odoo 候选查询，见第 4 节）；CLI 可用 `--shipment-reference` 或 `--tracking-number` 直接指定。

**响应 `body` 字段**：

| 字段 | 类型 | 用途 |
| --- | --- | --- |
| `Shipment` | 对象 / 数组 | 发货单列表 |
| `Shipment[].shipment_reference` | string | 必须与请求一致，否则报错 |
| `Shipment[].external_id` | string | 记为 `tracking_number`（缺省用池中 tracking number） |
| `Shipment[].delivery_date` / `delivery_time` | string | 时间戳回退来源（时间缺失按当地 0 点，时区默认 `Europe/Amsterdam`） |
| `Shipment[].shipment_document` | 对象 / 数组 | 文档列表 |
| `document_id` | string | 文档唯一 ID（缺失则失败；用于去重与下载） |
| `document_file_name` | string | 原始文件名 |
| `document_type` | string | POD 判定：匹配 `pod_document_types`（默认 `["POD"]`，大小写不敏感）；无类型文档也进入下载候选，下载后再判 |
| `mime_type` | string | 文件类型 |
| `document_date_time` | string | 文档时间戳（优先级高于 delivery_date/time） |

### 3.3 下载文档：`download_document`

- **请求**：`GET {base_url}{download_document_path}`，默认路径 `/download_document`，`allow_redirects=False`
- **查询参数**：`document_id`（来自 3.2）
- **响应 `body` 字段**：

| 字段 | 用途 |
| --- | --- |
| `document_id` | 必须与请求一致，否则报错 |
| `document_type` | 必须属于 POD 类型，否则跳过该文档 |
| `document_file_name` | 落盘文件名（缺省 `document_{id}.pdf/.bin`） |
| `mime_type` | 决定扩展名与文件头校验 |
| `document_date_time` | 时间戳 |
| `content_base64` | base64 文档内容（缺失/非法/为空/超限均报错） |

**内容校验**：base64 严格解码 → 大小限制（`POD_SYNC_MAX_DOWNLOAD_BYTES`）→ 文件头校验（PDF `%PDF-`、PNG、JPEG、TIFF），不支持的 MIME/扩展名直接失败；落盘后校验 SHA-256 并写 manifest。

### 3.4 generic 兼容模式（`api_mode=generic`）

旧版单接口模式：

- **请求**：`{base_url}{lookup_path}`，方法由 `http_method` 决定（`GET` 或 `POST`）
- **参数**：GET 用 query / POST 用 JSON body，字段名 `tracking_param_name`（默认 `tracking_number`）
- **请求头**：`{api_key_header}: {api_key}`、`Accept: application/json`
- **响应**：
  - 二进制（`application/pdf`、`image/*`、`application/octet-stream`）→ 直接保存；
  - JSON → 按配置取 `base64_content_field`（base64 内容）、`download_url_field`（下载直链，主机需在白名单内）、`file_name_field`（文件名）。

### 3.5 CLI 参数（`clients/westerman_api/probe.py:main`）

`--config`（必填）、`--shipment-reference` / `--tracking-number`（二选一，必填）、`--download`、`--output-dir`。
批量模式由 `clients/westerman_api/sync.py` 从池领取任务后并发调用，等价参数为 `download=True`。

---

## 4. Odoo 候选查询（XML-RPC，Westerman 池的数据来源）

代码：`clients/westerman_api/candidates.py`，走 `POST {odoo.url}/xmlrpc/2/object` 的 `execute_kw`。

### 4.1 `stock.picking.search_read`（找需要拉 POD 的出库单）

| 参数 | 值 |
| --- | --- |
| domain | `picking_type_id.code = outgoing`、`carrier_id.delivery_type = {carrier_delivery_type}`（默认 `westman`）、`state in {candidate_states}`、`wms_status in {candidate_wms_statuses}`、`create_date >= now - candidate_lookback_days` |
| fields | `id, name, state, wms_status, create_date, write_date, carrier_tracking_ref` |
| kwargs | `offset`、`limit`（`candidate_page_size`，默认 500）、`order = create_date desc, id desc` |

响应：上述字段的行数组；分页直到取满 `candidate_scan_limit`（默认 5000）。

### 4.2 `stock.tracking.search_read`（过滤已推送过 POD 的单据）

| 参数 | 值 |
| --- | --- |
| domain | `[("picking_id", "in", <chunk>), ("pod_link", "!=", False)]` |
| fields | `picking_id`，`limit = len(chunk) * 10` |

---

## 5. 配置字段速查（与拉取相关，完整见 `config.example.json`）

| 配置段 | 字段 | 说明 |
| --- | --- | --- |
| `cmc_onedrive` | `access_token` / `tenant_id` / `client_id` / `client_secret` / `refresh_token` / `scope` | 认证 |
| | `drive_id` / `root_path` / `root_item_id` / `shared_url` | 列举模式（三选一） |
| | `warehouse` / `timeout_seconds` | 落盘目录名 / 请求超时 |
| `emtc_gdrive` | `access_token` / `client_id` / `client_secret` / `refresh_token` | 认证 |
| | `folder_id` / `shared_drive_id` / `warehouse` / `timeout_seconds` | 列举范围与超时 |
| `westerman_api` | `api_mode` / `base_url` / `order_info_path` / `download_document_path` | Indicium 接口 |
| | `api_key` / `api_key_header` / `api_key_prefix` / `proxy_url` | 认证与代理 |
| | `pod_document_types` / `document_timezone` | POD 判定与时间戳时区 |
| | `http_method` / `lookup_path` / `tracking_param_name` / `base64_content_field` / `download_url_field` / `file_name_field` | generic 模式 |
| `download` | `base_dir` / `verify_incremental_hash` | 落盘根目录与增量校验 |

---

## 6. 注意事项

- **令牌安全**：刷新成功的 `access_token` / `refresh_token` 会写回配置文件并设为 600 权限；日志与异常消息对 URL、`$skiptoken`、`tempauth` 等敏感值做脱敏。
- **限流与重试**：CMC 对 429/5xx 自动退避重试；Westerman 对 429/5xx 标记 `retryable` 后交由池重试；EMTC 仅 401 刷新重试。
- **完整性**：所有下载都写入 `storage/pod/_integrity/` manifest 并校验 SHA-256/大小，校验不通过禁止进入推送流程。
- **增量**：CMC/EMTC 支持 `--incremental`，按 manifest 中 `source_modified_time`（可选 hash）跳过未变化文件。
- **凭据占位符**：Westerman 会显式识别 `PASTE_*` / `CHANGE_ME` / `YOUR_TOKEN` / `EXAMPLE` 等占位符并报错；CMC/EMTC 只校验 token 或刷新凭据是否存在。
