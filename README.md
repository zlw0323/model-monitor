# 模型监控平台（MVP）

定时探测 Azure AI Foundry 上部署的文本/推理模型和图像模型，记录 **连通性** 与 **TTFT**，异常自动重测，确认后发邮件报警。

## 监控逻辑

| 项目 | 说明 |
|---|---|
| 调用方式 | Azure v1 API：`https://<资源>/openai/v1/...`，`api-key` 请求头，不需要 `api-version` |
| 文本/推理模型 | 流式 `chat/completions`，prompt `Reply with only the word: OK`，token 上限 1024，其他参数（含思考强度）全部用模型默认值 |
| 文本 TTFT | 从发出请求到收到第一个非空 `content` / `reasoning_content` 增量（跳过 Azure 的内容过滤首包和空的 role 包） |
| 图像模型 | `images/generations`，prompt `A red apple on a white table`，固定 `quality=low`、`size=1024x1024`（默认的 auto 会让模型每次自选质量和尺寸，耗时在 20~70 秒间跳动，无法比较），`output_format=jpeg`、`output_compression=50`（返回数据从约 1.7MB 降到约 0.2MB，耗时和费用不变；模型不支持时自动去掉这两个参数重试），其他参数用默认值 |
| 图像 TTFT | 图像接口不流式，取完整响应耗时 |
| 检测间隔 | 默认 30 分钟，可按模型修改 |
| 错峰调度 | 每个模型在检测间隔内有固定的时间点（按模型 ID 均匀分布），100 个模型 / 30 分钟约每 18 秒开始一个；平台重启后各自按时间点补测，不会全部同时开始。新添加或修改端点的模型立即检测 |
| 并发限制 | 文本、图像、同一 Azure 资源分别限制同时检测数（默认 4 / 2 / 3，设置页可改），超出时显示"排队中" |
| 干扰标记 | 每次检测记录同时进行的检测数；与图像检测重叠的样本标记为"受干扰"，不计入基线 |
| 连通性失败 | 立即重测（间隔 5 秒），共 3 次仍失败 → 报警 |
| TTFT 异常 | TTFT > 基线 ×（1 + 阈值%）即异常，立即重测 3 次仍异常 → 报警；默认阈值 50%。TTFT 异常的重测为**独占重测**：等其他检测结束、暂停新检测后单独测量，报警只依据无干扰的数据 |
| 基线 | 最近 20 次正常且未受干扰的检测 TTFT 中位数（异常值不计入）；样本不足 5 个时只监控连通性；可设置手动基线，可"重置自动基线" |
| 报警 | 状态变化时同时发送**邮件**和**飞书群机器人**消息：进入"连通失败"/"TTFT 异常"各一条，恢复正常一条；持续异常不重复发送。两个渠道互相独立，任一未配置或失败不影响另一个，发送结果分别记录在"报警记录"页（可单条删除、多选删除、清空） |

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
2. **设置 → 飞书群机器人报警**：在飞书群"设置 → 群机器人"中添加自定义机器人，开启"签名校验"，把 Webhook 地址和签名密钥填入并保存，点"发送测试消息"。如果机器人开启了 IP 白名单，需要加入服务器公网 IP。
3. **监控面板 → 添加模型**：填写 Foundry 的端点（如 `https://xxx.openai.azure.com/`）、部署名称、API Key，选择类型。添加后立即检测一次。

> `SECRET_KEY` 用于加密保存的 API Key、SMTP 密码、飞书 Webhook 和签名密钥，部署后不要修改。
> 飞书 Webhook 地址等同于发消息的凭证，只在平台设置页填写，不要写进代码或提交到仓库。

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
# 同时提供假的飞书机器人：Webhook 填 http://127.0.0.1:9100/open-apis/bot/v2/hook/test，签名密钥 test-secret，
# 收到的消息可在 http://127.0.0.1:9100/feishu-messages 查看
.venv/bin/python -m uvicorn tests.mock_openai:app --port 9100
SECRET_KEY=dev-secret-key-123456 ADMIN_PASSWORD=admin .venv/bin/python -m uvicorn app.main:app --port 8000
# 添加模型时端点填 http://127.0.0.1:9100/v1
```

必须单进程运行（调度器在应用进程内），不要加 `--workers`。
