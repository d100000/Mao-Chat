# 最终代码复审：本地账户与验证码

复审范围包括 `agent/auth.py`、`agent/auth_server.py`、认证页面、SQLite 认证表、认证单元测试，以及原有 Agent、知识库和基准测试。

## 已完成的功能

- 用户注册、大小写不敏感的用户名校验、密码长度校验和重复用户名处理。
- PBKDF2-HMAC-SHA256 密码派生、随机盐、未知用户 dummy KDF。
- 算术验证码以 SVG 图片返回，算式不出现在 JSON 的明文 `prompt` 或数据库中；答案只保存 HMAC 指纹。
- 滑块目标区绘制在 SVG 图片内，精确目标和容差不出现在 JSON；客户端 nonce 只返回一次，数据库只保存 HMAC 指纹。
- 验证码 TTL、最大失败次数、一次性消费和原子条件更新；并发提交同一验证码最多成功一次。
- 登录会话使用随机 token，数据库只保存 HMAC 指纹，支持 HttpOnly/SameSite Cookie、Bearer API、注销、过期和清理。
- 默认在数据库旁生成 `0600` 的 `.auth-secret`，重启服务后会话仍可按 TTL 验证；数据库、WAL、SHM 文件也收紧到 `0600`。
- HTTP 层有同源检查、请求体上限、挑战与认证请求的每 IP 窗口限速、10 秒 socket 超时、CSP、nosniff、无日志输出和非回环地址的 Secure Cookie。
- 登录后的 `/app` 工作台使用认证后的 `/api/agent/stream` SSE 接口，按用户前缀恢复会话并把工具结果、回答和审计记录写回本地。

## 复审结论

没有发现会绕过算术题、滑块题或会话校验的 P0 缺陷。此前发现的验证码重放竞态、滑块目标泄露、服务重启后密钥失效、WAL 文件权限和挑战接口无限创建问题已处理。

仍保留两个使用边界：这是面向本机的轻量验证，不是对抗专业打码服务的强 CAPTCHA；若绑定非回环地址，应使用 HTTPS、共享 `MAO_AUTH_SECRET`，并在反向代理或进程管理器中增加并发上限。HTTP 层已经提供每 IP 限速，直接调用 `AuthService` 的 Python API 时应由调用方负责请求级限速。

## 测试结果

```text
agent/tests       35 passed
tests             39 passed
agent/benchmarks   3 passed
认证专项（auth + auth_server）14 passed
前端专项（契约、响应式、SSE/历史/AbortController、Node 检查）4 passed
管理员专项（管理员登录、密钥脱敏、配置重载、HTTP 权限）3 passed
py_compile        passed
```

认证专项覆盖注册、登录、重复用户名、错误密码、算术题图片解析、滑块错误和成功、过期、错误次数耗尽、重放、并发一次性消费、密码/答案/nonce/token 不落明文、登录锁定、注销、会话重启持久化、HTTP Cookie、同源拒绝、坏 JSON、SVG 路由、挑战限速、工作台 SSE 路由和权限模式。

启动和 API 使用说明见 [`docs/authentication.md`](../docs/authentication.md)。
