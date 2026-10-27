# 教员

以毛泽东原著、核验过的公开历史与研究者解释为思想依据，通过持续对话帮助用户梳理困惑、理解取舍、形成自己的判断，并根据真实反馈继续复盘。

用户可以直接讲眼前的问题，不需要先知道某句语录。回答区分历史事实、方法概括与现代建议；未知信息保持待确认，不用人物权威代替用户决定。

## 核心体验

- 原话摘录、用户确认与更正历史；停用的信息不会因旧摘要再次进入当前理解。
- 倾听、澄清、分析与复盘：可选择只想说说、听一个做法或进一步查证。
- 分开长期方向、阶段目标与本轮诉求，支持自然语言纠正。
- 有出处的原则与方法解释，保留历史条件、适用限制及引用定位。
- 议题继续、行动状态、用户授权的记忆与临时对话。
- 账户隔离、议题管理、复制导出与删除；模型和 Firecrawl 配置由管理员管理。
- 免登录访客对话、一次性体验积分、账户每日积分与注册后记录归入。
- 服务端报价、冻结和实际用量结算；后台管理价格版本、费用预算、退款和审计。

本轮实现对照见 [方案落地记录](docs/plan-implementation.md)，试用流程见 [成人试用执行材料](docs/pilot-study.md)。

具体交互与边界见 [使用说明](docs/agent_usage.md)，设计依据见 [完整产品优化方案](docs/教员产品优化方案.docx)。实现采用本地资料检索、受限工具调用与回答复核；模型通过兼容 Chat Completions 的服务接入。

## 本机启动

需要 Python 3.9 或更新版本；应用运行层使用 Python 标准库。Node.js 22 或更新版本仅用于前端行为和浏览器测试。

```sh
git clone https://github.com/d100000/Mao-Chat.git
cd Mao-Chat
python3 scripts/bootstrap_library.py
```

资料初始化使用仓库内的已核记录，不需要私人 PDF、原账户数据库或 API 密钥。首次初始化后，按使用的模型服务设置：

```sh
export MAO_LLM_BASE_URL='https://your-provider.example/v1'
export MAO_LLM_MODEL='your-model'
export MAO_LLM_API_KEY='your-key'
# 需要网页搜索时再设置
export FIRECRAWL_API_KEY='your-firecrawl-key'
python3 -m agent.auth_server --host 127.0.0.1 --port 8877
```

打开 [教员工作台](http://127.0.0.1:8877/app)，无需登录即可进入。首次使用赠送积分前需要完成服务端人机验证；注册后每天获得100积分，当天剩余在北京时间零点失效。100积分对应0.10元，实际按模型用量扣除。也可复制 `agent/config.example.json` 为被 Git 忽略的 `agent/config.json`，填写服务参数。环境变量优先；支持的本机环境还可使用 Keychain。

正式对话还需要配置 Turnstile、已核验的模型价格版本和平台每日费用上限；联网搜索另需搜索费用上界。缺少配置会明确提示并停止外部调用。配置、计费规则和恢复流程见 [积分与安全部署说明](docs/credits-and-security.md)。

首次需要管理员后台时，在启动前设置自己的 `MAO_ADMIN_USERNAME` 和 `MAO_ADMIN_PASSWORD`，访问 `/admin-login`。密钥的读取与保存方式见 [管理说明](docs/admin_usage.md)。不要把 `.env`、本机配置、账户数据库、会话或密钥提交到仓库。

## 资料与数据

公开库包含37条核验资料及其公开出处和范围，原文全文是可选本地资产。使用 `--refresh-library` 可更新既有库中的资料表，默认不会静默替换已有库。完整 OCR 和 PDF 的准备方式见 [全文资料说明](corpus/README.md)。

临时对话表示本应用不保存该次会话历史，内容仍由所配置的模型及已开启的搜索服务处理。记忆需要用户授权，用户可查看、停用或删除。使用前应核对所选服务商的数据处理安排。

当前启动方式面向本机使用。公开提供持续情感互动前，需要结合实际部署落实相应的数据、服务运营与评估条件；设计文档给出了分期范围。

## 测试

```sh
python3 -m unittest discover -q
python3 -m compileall -q agent scripts tests
npm ci
npx playwright install chromium
E2E_BROWSER_CHANNEL=bundled npm run test:ui
E2E_BROWSER_CHANNEL=bundled npm run test:ui:plan
E2E_BROWSER_CHANNEL=bundled npm run test:ui:legacy
E2E_BROWSER_CHANNEL=bundled npm run test:ui:credits
E2E_BROWSER_CHANNEL=bundled npm run test:ui:billing
E2E_BROWSER_CHANNEL=bundled npm run test:ui:billing-http
```

产品浏览器测试使用独立临时资料库、测试账户与确定性模型替身，不读写真实密钥。`test:ui:billing-http`通过真实HTTP、认证和SQLite验证草稿恢复、退款幂等、双人审批、模型/搜索费用对账、账本重建及申诉复核；需要时可用`E2E_PYTHON`指定Python解释器。完整 OCR 集成测试在本机资产齐备时执行；公开克隆会明确跳过这部分可选集成用例。

真实服务验收会消耗所配置供应商额度，仅在明确需要时执行 `npm run test:live` 或 `npm run test:live:product`。流程通过不等于所有建议语义正确或已证明长期陪伴效果，仍需要对有限样例作内容审阅。

提交前可运行 `python3 scripts/check_release.py`，检查 Git 实际清单中的私有状态、文件体积与常见凭据格式。测试与发布说明见 [端到端测试](docs/e2e_usage.md)。
