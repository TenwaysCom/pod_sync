# Web Console

`webapp/` 提供一个基于 FastAPI 的管理界面，用来把当前 CLI 操作收进网页里。

## 当前能力

- 页面触发 `CMC OneDrive` 同步
- 页面触发 `EMTC Google Drive` 同步
- 页面触发 `Westerman API` 查询 / 下载
- 页面触发完整性校验和状态检查
- 页面触发 `Phase 1A`
- 查看最近任务、命令、日志
- 直接内嵌查看最新 `Phase 1A dashboard`

## 启动

```bash
export POD_SYNC_ADMIN_PASSWORD='CHANGE_ME'
export POD_SYNC_SECRET_KEY='CHANGE_ME_RANDOM_32_BYTES'
python3 -m uvicorn webapp.app:app --host 0.0.0.0 --port 8080
```

然后打开：

- `http://127.0.0.1:8080`

## 设计边界

- 页面只允许白名单操作，不允许执行任意 shell 命令
- 任务状态存文件，位置在 `runtime/jobs/`
- 不引入数据库
- 不改写现有同步核心逻辑，仍然复用现有 CLI 入口
- 后台 POST 操作要求登录和 CSRF 校验
- `/storage/files/...` 在未配置有效 `pod_sync.file_url_secret` 时公开可读；配置有效 secret 后要求登录态或签名链接访问
