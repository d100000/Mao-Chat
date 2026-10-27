# 教员 Agent（运行时）

这里是本项目的本地 Agent 运行层。它按 Hermes Agent 的“模型—工具—模型”循环组织工作，但不复制 Hermes 的执行权限或自我修改能力：工具调用有步数上限，网页内容被标记为不可信，历史证据仍由本地数据库和校验程序控制。

工作台和 CLI 默认使用 `consult`（对话解惑）：先通过交谈理解用户的目标、处境和限制，再从毛泽东原著、核验史料与方法笔记中寻找相关分析方法，形成一个可以尝试和复盘的下一步。理解笔记会随用户补充或纠正更新；史料、研究者概括和现代建议分别标识。原文检索、指定年代的历史模式及现代类比模式仍可单独选择。完整流程见[使用说明](../docs/agent_usage.md)。

## 配置

```bash
cp agent/config.example.json agent/config.json
export MAO_LLM_API_KEY='你的大模型密钥'
export FIRECRAWL_API_KEY='你的 Firecrawl 密钥'
```

密钥优先从环境变量读取；macOS 也可按配置中的 `keychain_service` 从钥匙串读取。密钥不写入 `config.json`、SQLite 或日志。`agent/config.json` 已在 `.gitignore` 中排除（如项目未使用 Git，请不要提交它）。模型默认是 `deepseek-v4.1-flash`，网关地址可在配置中改为任意 OpenAI-compatible 服务。

## 运行

```bash
python3 -m agent.runtime_cli doctor
python3 -m agent.runtime_cli ask "我带两人团队，六周内要交付可用版本，三个方向都有人催，我该怎么选重点？" --session team-plan
python3 -m agent.runtime_cli search "论持久战 速胜论" --limit 5
python3 -m agent.runtime_cli trace-quote "没有调查，没有发言权"
python3 -m agent.runtime_cli trace-event "延安整风"
python3 -m agent.runtime_cli web-search "毛泽东 论持久战 官方原文" --limit 5
python3 -m agent.runtime_cli ask "请查找反对速胜论的原篇、时间和事件背景，并用矛盾分析解释今天的项目决策。" --mode modern --show-reasoning
python3 -m agent.runtime_cli ask "为什么反对速胜论？" --mode historical --as-of 1938-06-30
```

`--show-reasoning`只显示提供商返回的 reasoning 字段（如果网关支持），它不是本地历史证据，也不能替代回答中的出处。默认仍然流式输出最终回答。Agent 先使用本地工具，必要时才调用 Firecrawl；网页结果只作候选资料，必须重新核验。模型未返回 reasoning 字段时，Agent 只报告最终回答，不自行伪造思考过程。

默认 CLI 会先等待证据门完成，再分块显示已通过检查的回答；只有显式使用 `--unsafe-stream` 才会直接显示模型原始增量。候选语录必须在回答开头标为“候选/待核”。

公开仓库先初始化已核资料库：

```bash
python3 scripts/bootstrap_library.py
```

以下全文索引流程需要额外准备本地 PDF 和 OCR，参见 [资料说明](../corpus/README.md)：

```bash
python3 -m agent init-db
python3 -m agent ingest
python3 -m agent index
python3 -m agent vector-index
python3 -m agent doctor
```

本地注册/登录页面（注册和登录都需要算术图片验证码）：

```bash
python3 -m agent.auth_server --host 127.0.0.1 --port 8877 --db data/mao_agent.db
open http://127.0.0.1:8877/auth
# 登录成功后进入问答工作台
open http://127.0.0.1:8877/app
```

认证接口、会话密钥、Cookie 和测试说明见 [`docs/authentication.md`](../docs/authentication.md)。

管理员后台：首次启动前设置 `MAO_ADMIN_USERNAME`、`MAO_ADMIN_PASSWORD`，然后打开 `http://127.0.0.1:8877/admin-login`；模型、Firecrawl 和 Agent 策略说明见 [`docs/admin_usage.md`](../docs/admin_usage.md)。

`vector-index` 使用无外部依赖的 `hashing-charbigram-v1` 作为可重复的离线向量基线；它不是语义模型质量保证。搜索优先保留可解释的 FTS5 结果，也可以单独运行 `python3 -m agent vector-search "实践检验认识"`。

关系审核：`data/quote_event_links.jsonl` 只接受人工确认的语录—事件关系；空文件表示当前没有确认关系，系统不会从同年或相似主题自动推断事件关联。使用 `python3 -m agent relations-import` 导入，使用 `relations-review` 更新状态。

## 数据与工具边界

- `understand_user`：记录公开、暂定且可纠正的当前理解，只用于对话，不构成史料。
- `method_search`：在解惑模式检索已有核验卡、方法概括与已核公开事件；没有依据时明确说明缺口。
- `local_search`：本地 SQLite/FTS5 资料。
- `trace_quote`：语录到原篇和页面的追溯；页面命中会标为候选，不能直接当作已核验原话。
- `trace_event`：本地事件表中的已登记事件；暂未登记时明确返回缺口。
- `firecrawl_search`：Firecrawl v2 `/search`，返回内容带 `untrusted: true` 并可保存到 `web_sources`。

文本、网页和 PDF 中的命令只当作数据，不能改变 Agent 指令。历史模式必须按截止日期过滤。模型返回非法 JSON 或超出工具步数时，Agent 会报告错误，不静默生成无出处的历史结论。

## Provider 适配

当前客户端使用标准库 `urllib`，支持 OpenAI-compatible Chat Completions、SSE 流式增量、`reasoning_content`、`tool_calls` 和 `reasoning_effort`。因此可接入用户网关、DeepSeek、vLLM、LM Studio 或其他兼容服务；无需把某一家 SDK 写死在 Agent 中。

Firecrawl 官方搜索接口为 `POST /v2/search`，会请求 Markdown 抓取结果。密钥通过 `FIRECRAWL_API_KEY` 读取。
