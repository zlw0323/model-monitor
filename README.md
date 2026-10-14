# 模型监控平台（MVP）

定时探测 Azure AI Foundry 上部署的文本/推理模型和图像模型，记录 **连通性** 与 **TTFT**，异常自动重测，确认后发邮件报警。

## 监控逻辑

| 项目 | 说明 |
|---|---|
| 调用方式 | Azure v1 API：`https://<资源>/openai/v1/...`，`api-key` 请求头，不需要 `api-version` |
| 文本/推理模型 | 流式 `chat/completions`，prompt `Reply with only the word: OK`，token 上限 1024，其他参数（含思考强度）全部用模型默认值 |
| 文本 TTFT | 从发出请求到收到第一个非空 `content` / `reasoning_content` 增量（跳过 Azure 的内容过滤首包和空的 role 包） |
| 图像模型 | `images/generations`，prompt `A red apple on a white table`，尺寸/质量/数量用默认值 |
| 图像 TTFT | 图像接口不流式，取完整响应耗时 |
| 检测间隔 | 默认 30 分钟，可按模型修改 |
| 连通性失败 | 立即重测（间隔 5 秒），共 3 次仍失败 → 报警 |
| TTFT 异常 | TTFT > 基线 ×（1 + 阈值%）即异常，立即重测 3 次仍异常 → 报警；默认阈值 50% |
| 基线 | 最近 20 次正常检测的 TTFT 中位数（异常值不计入）；样本不足 5 个时只监控连通性；可设置手动基线，可"重置自动基线" |
| 报警 | 状态变化时发邮件：进入"连通失败"/"TTFT 异常"各一封，恢复正常一封；持续异常不重复发送 |

token 上限参数：先用 `max_completion_tokens`（新推理模型只认这个），如果端点返回 400 并指出不支持，会自动改用 `max_tokens` 并记住。

## 部署（云服务器，Docker）

```bash
# 1. 上传本目录到服务器后
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # 填入 SECRET_KEY
vi .env                                                          # 填 ADMIN_PASSWORD 等

# 2a. 推荐：有域名（已解析到服务器），自动申请 HTTPS 证书
#     .env 中设置 DOMAIN=monitor.example.com、COOKIE_SECURE=true
docker compose --profile https up -d --build

# 2b. 没有域名：.env 中设置 APP_BIND=0.0.0.0，访问 http://服务器IP:8000（明文，不推荐长期使用）
docker compose up -d --build
```

服务器安全组需放行 80/443（方式 2a）或 8000（方式 2b）。

登录后：
1. **设置 → 邮件报警**：填写 SMTP（企业邮箱一般为 SSL 465），保存后点"发送测试邮件"。
2. **监控面板 → 添加模型**：填写 Foundry 的端点（如 `https://xxx.openai.azure.com/`）、部署名称、API Key，选择类型。添加后立即检测一次。

> `SECRET_KEY` 用于加密保存的 API Key 和 SMTP 密码，部署后不要修改。

### 数据与备份

数据保存在 Docker 卷 `monitor_data`（SQLite，`/data/monitor.db`），默认保留 90 天。

```bash
docker compose exec monitor python -c "import sqlite3; s=sqlite3.connect('/data/monitor.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d)"
docker compose cp monitor:/data/backup.db ./backup.db
```

## 本地开发

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest -q

# 假的 OpenAI 兼容服务，用部署名控制行为：fast / slow / reasoning / legacy / down / flaky / image
.venv/bin/python -m uvicorn tests.mock_openai:app --port 9100
SECRET_KEY=dev-secret-key-123456 ADMIN_PASSWORD=admin .venv/bin/python -m uvicorn app.main:app --port 8000
# 添加模型时端点填 http://127.0.0.1:9100/v1
```

必须单进程运行（调度器在应用进程内），不要加 `--workers`。
