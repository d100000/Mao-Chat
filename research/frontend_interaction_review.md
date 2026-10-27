# AI 史料工作台前端交互方案

## 参考模式

- ChatGPT 把搜索和语音放在对话输入区附近，继续追问沿用同一会话，并用流式文本保持即时反馈。参考：[ChatGPT 搜索说明](https://help.openai.com/en/articles/9237897-conducting-your-searches-on-search)、[ChatGPT Voice](https://help.openai.com/en/articles/20001274)。
- Claude Artifacts 把较长的工作产物放在对话旁侧，使回答和可复用结果同时可见。参考：[Claude Artifacts](https://support.claude.com/en/articles/17153992-what-are-artifacts-and-how-do-i-use-them)。
- Perplexity 把来源、当前答案和后续追问连接起来，适合研究型任务。参考：[Perplexity Pro Search](https://www.perplexity.ai/help-center/en/articles/10352903-what-is-pro-search)。

## 本项目的取舍

页面没有复刻通用的蓝紫渐变 AI 首页，而是采用“墨色工作台 + 宣纸背景 + 朱砂操作色”的史料方向：左侧是可恢复的会话列表，中间是连续对话，右侧是一条带节点的证据线。右侧来源卡片固定呈现来源、页码、审核状态和摘录，减少用户在回答与出处之间来回切换的成本。

交互状态包括：新建对话、最近会话恢复、研究/历史/现代模式、历史截止日期、联网搜索开关、语音输入、SSE 流式回答、工具进度提示、回答失败提示、证据来源/时间/说明标签页、证据栏折叠和移动端侧栏抽屉。历史模式缺少截止日期时由后端拒绝请求，登录失效时回到 `/auth?next=/app`。

动效保持克制：首次内容和消息使用短距离上移，生成中使用三个点的微动效，工具检索使用状态点和文字进度，来源卡片按时间线顺序进入；`prefers-reduced-motion` 会关闭过渡和动画。页面没有使用会干扰阅读的持续背景动画。

## 文件与接口

- 页面：`web/index.html`
- 页面服务：`GET /app`
- 流式问答：`POST /api/agent/stream`
- 工作台会话按 `user_id + thread_id` 保存，回答完成后写入本地 Agent 会话目录和审计表。
