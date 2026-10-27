# 网页版深度代码复审

复审范围：`web/index.html`、`agent/auth_server.py` 的工作台路由、SSE 传输、会话恢复、认证边界，以及前端静态契约测试。

## 修复的问题

| 优先级 | 问题 | 修复 |
|---|---|---|
| P1 | 点击历史会话只切换了 ID，页面没有恢复内容 | 增加 `/api/agent/session`，按用户前缀读取并裁剪会话；前端同时保留本地恢复兜底 |
| P1 | 生成按钮显示停止状态但不能真正停止请求 | 使用 `AbortController`，停止按钮中断 fetch，恢复输入状态 |
| P1 | 移动端证据栏被媒体查询永久隐藏 | 改为可打开的右侧抽屉，并支持 Escape 关闭 |
| P1 | 桌面端隐藏证据栏后仍保留空白网格列 | 增加 `workspace.evidence-hidden` 单列布局 |
| P1 | 历史模式不填截止日期时才在服务端失败 | 前端发送前拦截并聚焦日期控件，后端继续做严格日期校验 |
| P1 | SSE 工具结果可能携带过大的网页正文 | 服务端对 SSE 展示结果截断，内部审计仍保留完整工具记录 |
| P1 | 多个生成请求可能同时消耗模型资源 | 服务端加入 4 个并发槽位，超限返回 429，并在 finally 中释放 |
| P1 | 会话提示词可无限增长 | 服务端最多取 24 条历史消息，每条最多 12000 字 |
| P2 | 模型错误仍可能被记录成成功运行 | 错误事件以 `error` 状态写入运行审计 |
| P2 | 键盘用户缺少状态反馈 | 增加 `aria-live`、`aria-pressed`、`aria-expanded`、`aria-label` 和 Escape 行为 |

## 验证

```text
agent/tests       32 passed
tests             35 passed
agent/benchmarks   3 passed
auth_server专项    5 passed
Node --check       passed
```

前端专项测试覆盖页面元素与 API 契约、响应式和 reduced-motion 规则、历史/SSE/AbortController 保护；后端专项覆盖未登录、非法日期、会话隔离、SSE 事件、工具截断和并发限流。

当前页面仍使用轻量 SVG 验证码和本地单进程服务，适合本机 Agent 工作台。公网部署需要 HTTPS、反向代理并发控制和持久化密钥管理。
