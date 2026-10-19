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
| 报警 | 同时发送**邮件**和**飞书群机器人**消息，两个渠道互相独立，发送结果分别记录在"报警记录"页（可单条删除、多选删除、清空） |
| 报警去重 | 每个模型的一次异常是一个"事件"：开始时报警一次；同一错误（数字、ID 等变化部分忽略，如 429 的"Retry after N seconds"）反复出现只计数不再报警；**报错内容变成本次事件中没报过的错误**时报一次"异常变化"；连续 2 轮检测正常（可设置）才发"恢复"通知，并附异常持续时间和期间异常轮数。第一轮正常后 5 分钟内自动复测确认，避免模型时好时坏时反复"恢复/报警" |
| 报警合并 | 新报警先等待 60 秒（可设置，期间又有新报警则继续等，最长 5 分钟），然后合并成一条消息，按资源分组，如"资源 xxx：连通失败 25 个（均为 认证失败（HTTP 401））"并列出每个模型 |
| 联动检测 | 某模型出现资源级错误（401/403 认证失败、无法连接、超时、5xx）时，立即检测同一资源下的其他模型，让它们的报警进入同一条消息；同一资源 30 分钟内最多联动一次。429、404 等单个部署的问题不联动 |
| 监控端断网 | 检测出现无法连接、超时、连接中断、502/503/504 时，先访问与模型无关的参考地址（默认 Cloudflare、Microsoft、百度，设置页可改，最多 5 个；同时失败的多个检测共用一次检查，且只采用失败之后做的检查结果）：任意一个能访问 → 监控服务器网络正常，按模型故障处理；全部不可访问 → 判定为**监控端自身断网**：只发一条"监控端网络中断"报警，不改变模型状态、不重测、不报模型故障，暂停定时检测，期间结果标记为"监控端断网"、不计入可用率和趋势图；每 30 秒检查一次网络，恢复后发"监控端网络已恢复"（含中断时长和未计入的检测次数），各模型回到自己的错峰时间点继续检测。断网状态保存在数据库中，平台重启后不会重复报警 |

token 上限参数：先用 `max_completion_tokens`（新推理模型只认这个），如果端点返回 400 并指出不支持，会自动改用 `max_tokens` 并记住。

## 资源与模型管理

- **Azure 资源**（"资源"页）：一个资源 = 端点 + API Key，该资源下的所有部署共用。更换 Key 只改一处，该资源下的模型会在 2 分钟内陆续用新 Key 检测；端点改到其他主机必须重新填写 Key。资源下还有模型时不能删除。
- **模型**：选择所属资源，填写部署名称和类型。同一资源下"部署名称 + 类型"不能重复。极少数不走 v1 API 的模型可在"高级"里填写完整请求 URL（必须与资源端点同一主机）。
- **批量导入**（监控面板 → 批量导入）：从 Excel 复制整张表粘贴，或选择 CSV 文件（UTF-8 或 Excel 默认的 GBK 均可）。第一行为表头，必填列"资源、类型、部署名称"，可选列"名称、检测间隔、预警阈值、手动基线、启用"，可下载模板。先预览再导入：重复的行自动跳过，有错误的行不导入。导入的模型在 5 分钟内陆续开始首次检测。表格中不包含 API Key。
- **监控面板**：列表视图，可按状态（点击顶部统计）、类型、资源筛选，可搜索、"只看异常"、点击表头排序；每行带最近趋势图。勾选多个模型可批量立即检测、启用、停用、删除。筛选条件会记住。
- 旧版本（每个模型单独填端点和 Key）升级时会自动按"端点 + Key"合并成资源，历史数据保留，并在数据库旁生成 `monitor.db.pre-resources.bak` 备份。

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
3. **资源 → 添加资源**：填写 Foundry 资源的端点（如 `https://xxx.openai.azure.com/`）和 API Key。
4. **监控面板 → 添加模型 / 批量导入**：选择资源，填写部署名称和类型。单个添加的模型立即检测一次。

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

# 假的 OpenAI 兼容服务，用部署名控制行为：fast / slow / reasoning / legacy / down / flaky / image / throttled（429）
# API Key 填 bad-key 时所有部署返回 401（模拟整个资源 Key 错误）
# 同时提供假的飞书机器人：Webhook 填 http://127.0.0.1:9100/open-apis/bot/v2/hook/test，签名密钥 test-secret，
# 收到的消息可在 http://127.0.0.1:9100/feishu-messages 查看
.venv/bin/python -m uvicorn tests.mock_openai:app --port 9100
SECRET_KEY=dev-secret-key-123456 ADMIN_PASSWORD=admin .venv/bin/python -m uvicorn app.main:app --port 8000
# 添加资源时端点填 http://127.0.0.1:9100/v1（Key 任意），再添加模型
```

必须单进程运行（调度器在应用进程内），不要加 `--workers`。
