# 最终完整测试报告

测试日期：2026-10-26

## 本地自动化测试

```text
agent/tests       35 passed
tests             39 passed
agent/benchmarks   3 passed
Python py_compile  passed
web/index.html     Node --check passed
web/admin.html     Node --check passed
web/admin-login.html Node --check passed
```

覆盖范围包括普通用户注册/登录、算术验证码、滑块验证码、管理员登录、管理员会话、配置脱敏、配置重载、用户锁定、HTTP 权限、SSE、会话恢复、并发限制、前端响应式契约和 JavaScript 语法。

## 真实服务烟测

### Firecrawl

```text
查询：毛泽东 论持久战 官方原文
结果：通过
返回结果：1
untrusted：true
```

网页结果已按设计标记为不可信候选资料。

### LLM 网关

```text
模型：deepseek-v4.1-flash
网关：https://api.bob-api.com/v1
结果：失败
HTTP：401 Unauthorized
服务端消息：Invalid token
```

这表示当前服务端读取到的 LLM token 已失效、错误或不再被网关接受。代码流程、请求格式和本地 Agent 测试均通过；更新管理员后台的模型密钥后即可重新进行真实问答烟测。

管理员入口：`http://127.0.0.1:8765/admin-login`。密钥不会在页面、配置 API 或报告中回显。
