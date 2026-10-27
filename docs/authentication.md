# 教员身份与账户认证

`agent.auth.AuthService` 提供账户、验证码和会话的标准库实现，`agent.auth_server` 默认只监听本机，工作台允许访客使用。部署配置见 [积分与安全](credits-and-security.md)。

## 启动

```bash
python3 -m agent.auth_server --host 127.0.0.1 --port 8765 --db data/mao_agent.db
open http://127.0.0.1:8765/auth
# 也可直接以访客进入
open http://127.0.0.1:8765/app
```

普通账户注册和登录使用服务端验证的人机挑战。验证结果绑定当前身份、操作ID和参数摘要；前端声称验证成功不能获得积分或会话。登录成功后，浏览器只通过 `HttpOnly`、`SameSite=Lax` Cookie 保存会话；JSON 响应不会返回原始会话 token。

页面先调用 `POST /api/guest/bootstrap` 建立独立的访客内容、试用资格和钱包能力。受保护操作返回 `challenge_required` 后，通过 `POST /api/security/verify` 交换短期证明；带相同操作ID和内容重试原操作。注册和登录提交到 `/api/auth/register`、`/api/auth/login`。退出使用 `POST /api/auth/logout`，状态查询使用 `GET /api/auth/me`。旧算术挑战仅保留给管理员入口及受信本地Python兼容接口，不能替代普通HTTP账户的人机验证。

工作台位于 `/app`。先经 `/api/points/quote` 获取并确认报价，再通过 `POST /api/agent/stream` 提交原参数、`quote_id` 和 `client_request_id`，以 SSE 返回受理、状态、回答片段及结算。访客与账户使用不同的服务端存储身份；IP仅参与风险判断，不能据此获取会话。

服务对验证码和认证提交按客户端地址做短窗口限速，并拒绝带有非本服务 `Origin` 的请求。默认回环地址使用 HTTP；绑定非回环地址时会标记会话 Cookie 为 `Secure`，应通过 HTTPS 反向代理访问。

## 数据与密钥

认证表会在首次创建 `AuthService` 时加入现有 SQLite 数据库：`users`、`captcha_challenges`、`auth_sessions`、`auth_audit` 和 `auth_rate_limits`。数据库只保存密码验证材料、验证码答案和会话 token 的 HMAC 指纹。

服务默认在数据库旁创建权限为 `0600` 的 `.auth-secret` 侧车文件，因此重启后会话仍可按 TTL 使用；部署到多进程环境时，所有进程应共享同一个稳定密钥。也可以在启动服务前显式设置一个仅当前用户可读的值：

```bash
export MAO_AUTH_SECRET='use-a-long-random-local-secret'
python3 -m agent.auth_server --db data/mao_agent.db
```

不要把该值提交到仓库或写入日志。`AuthService.cleanup()` 可定期清理过期会话、已消费验证码和旧限速窗口。

默认命令只绑定 `127.0.0.1`。如果改为非回环地址，服务会给会话 Cookie 加 `Secure`，因此必须通过 HTTPS 提供页面；生产部署还应在代理层限制并发和保留周期。

管理员配置见 [`docs/admin_usage.md`](admin_usage.md)，普通用户登录不会获得管理员接口权限。

## Python API

```python
from agent.auth import AuthService

auth = AuthService("data/mao_agent.db")
math = auth.create_math_challenge()
result = auth.register(
    "alice", "a-long-password",
    math_challenge_id=math["challenge_id"], math_answer="...",
)
```

`MAO_AUTH_CAPTCHA_PEPPER` 仍可作为兼容的环境变量名，但新部署建议使用 `MAO_AUTH_SECRET`。

上面的Python例子是受信本机接口的兼容用法。HTTP客户端必须走强人机验证；不能提交 `require_captcha=false` 跳过。会话由 `validate_session(token)` 检查，退出用 `logout(token)`，成功登录返回的 token 只在内存中出现一次。公网管理员还必须完成TOTP，财务写操作要求最近5分钟内重新验证；本地登记命令见 [管理员说明](admin_usage.md)。

## 测试

```bash
python3 -m unittest agent.tests.test_auth agent.tests.test_auth_server -v
```
