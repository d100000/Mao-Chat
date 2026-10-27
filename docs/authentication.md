# 教员本地账户认证

`agent.auth.AuthService` 提供账户、验证码和会话的标准库实现，`agent.auth_server` 提供只监听本机的注册/登录页面。

## 启动

```bash
python3 -m agent.auth_server --host 127.0.0.1 --port 8765 --db data/mao_agent.db
open http://127.0.0.1:8765/auth
# 登录成功后打开
open http://127.0.0.1:8765/app
```

注册和登录都需要算术题验证码。登录成功后，浏览器只通过 `HttpOnly`、`SameSite=Lax` Cookie 保存会话；JSON 响应不会返回原始会话 token。

页面通过同源 JSON 接口 `POST /api/auth/captcha` 获取算术题挑战；注册和登录提交到 `/api/auth/register`、`/api/auth/login`。退出使用 `POST /api/auth/logout`，状态查询使用 `GET /api/auth/me`。也可用 `GET /auth/captcha.svg` 获取一张独立的算术题 SVG。

登录后的工作台位于 `/app`。它通过 `POST /api/agent/stream` 接收 `question`、`mode`、`as_of` 和 `thread_id`，以 SSE 返回状态、工具结果、回答片段和完成事件；会话历史按用户前缀保存到本地 Agent 会话目录。

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

注册和登录使用一次性挑战；`verify_math` 可用于需要分步验证的客户端。会话由 `validate_session(token)` 检查，退出用 `logout(token)`，成功登录返回的 token 只在内存中出现一次。

## 测试

```bash
python3 -m unittest agent.tests.test_auth agent.tests.test_auth_server -v
```
