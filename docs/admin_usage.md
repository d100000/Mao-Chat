# 教员管理员后台

启动服务后打开：

```text
http://127.0.0.1:8765/admin-login
```

管理员登录需要算术图片验证码；公网部署还强制要求TOTP动态码。首次创建管理员时，在启动服务前设置：

```bash
export MAO_ADMIN_USERNAME=admin
export MAO_ADMIN_PASSWORD='请使用至少八位的强密码'
python3 -m agent.auth_server --host 127.0.0.1 --port 8765 --db data/mao_agent.db
```

首次启动成功后可移除这两个环境变量。管理员密码使用与普通账户相同的 PBKDF2-HMAC-SHA256 方案；管理员会话使用独立的 HttpOnly Cookie `mao_admin_session`。

首次本地创建管理员后，在受信终端登记TOTP，再公开服务。命令只在本机显示一次登记材料，请直接保存到验证器，勿提交或转发：

```sh
python3 -m agent.auth --db data/mao_agent.db --enroll-admin-totp admin
```

财务写操作要求最近5分钟内验证，过期时在操作表单填写新的动态码。相同动态码不能重放，也不会延长原管理员会话的有效期。

后台可配置：

- OpenAI-compatible 模型地址、模型名、温度、最大输出、超时、推理强度、流式和思考开关
- Firecrawl 地址、搜索数量上限
- Agent 最大工具步数、本地检索数量和网页检索数量
- 普通用户状态查看与锁定/解锁
- 四类token单价的草稿预览、版本发布和停用，平台每日现金费用上限
- 风险事件、财务审计、未知用量对账、退款、积分调账与大额独立审批

计费和生产启用步骤见 [积分与安全部署说明](credits-and-security.md)。后台不提供购买、充值、转账或提现。

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

## 质量与响应体验

后台运行状态展示最近30天持久议题的完整答复与首个有效内容P50/P95、自愿反馈分布及供应商用量报告覆盖。状态提示不算有效内容；临时对话、首次失败或取消不进入持久统计。未报告用量和未配置费用换算时保留未知，不据此估造账单或长期效果。

审计保留运行代码、提示词、知识版本、实际采用资料指纹、调用数和工具计时。管理员接口 `/api/admin/quality?days=30` 仅返回聚合，不提供私人原文或反馈说明。用户可通过议题导出复制自己的反馈，删除时清理相应记录。
