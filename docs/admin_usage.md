# 教员管理员后台

启动服务后打开：

```text
http://127.0.0.1:8765/admin-login
```

管理员登录同样需要算术图片验证码。首次创建管理员时，在启动服务前设置：

```bash
export MAO_ADMIN_USERNAME=admin
export MAO_ADMIN_PASSWORD='请使用至少八位的强密码'
python3 -m agent.auth_server --host 127.0.0.1 --port 8765 --db data/mao_agent.db
```

首次启动成功后可移除这两个环境变量。管理员密码使用与普通账户相同的 PBKDF2-HMAC-SHA256 方案；管理员会话使用独立的 HttpOnly Cookie `mao_admin_session`。

后台可配置：

- OpenAI-compatible 模型地址、模型名、温度、最大输出、超时、推理强度、流式和思考开关
- Firecrawl 地址、搜索数量上限
- Agent 最大工具步数、本地检索数量和网页检索数量
- 普通用户状态查看与锁定/解锁

首次没有数据库覆盖配置时，默认模型为 `deepseek-v4.1-flash`，OpenAI-compatible 地址为 `https://api.bob-api.com/v1`，Firecrawl 地址为 `https://api.firecrawl.dev/v2`；这些默认值来自现有 `agent/config.json`，管理员保存后会写入 `app_settings`。

API 密钥只接受新值，不在页面或 API 中回显。macOS 优先写入 Keychain；没有可用 Keychain 时写入数据库旁的 `0600` `.provider-secrets` 文件。普通配置写入 SQLite 的 `app_settings` 表。保存配置后，后续新对话会重载 Agent 配置。

管理员 API：

```text
GET  /api/admin/me
POST /api/admin/login
POST /api/admin/logout
GET  /api/admin/config
PUT  /api/admin/config
POST /api/admin/config/test
GET  /api/admin/users
GET  /api/admin/overview
POST /api/admin/users/{user_id}/lock
POST /api/admin/users/{user_id}/unlock
```

服务默认只监听本机。绑定非回环地址时必须使用 HTTPS，并在反向代理中限制并发和访问来源。
