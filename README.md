# 教员

以毛泽东原著、核验过的公开历史与研究者解释为思想依据，通过持续对话帮助用户梳理困惑、理解取舍、形成自己的判断，并根据真实反馈继续复盘。

用户可以直接讲眼前的问题，不需要先知道某句语录。回答区分历史事实、方法概括与现代建议；未知信息保持待确认，不用人物权威代替用户决定。

## 核心体验

- 倾听、澄清、分析与复盘：可选择只想说说、听一个做法或进一步查证。
- 分开长期方向、阶段目标与本轮诉求，支持自然语言纠正。
- 有出处的原则与方法解释，保留历史条件、适用限制及引用定位。
- 议题继续、行动状态、用户授权的记忆与临时对话。
- 账户隔离、议题管理、复制导出与删除；模型和 Firecrawl 配置由管理员管理。

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

打开 [教员登录页](http://127.0.0.1:8877/auth)，注册并登录即可使用。也可复制 `agent/config.example.json` 为被 Git 忽略的 `agent/config.json`，填写服务参数。环境变量优先；支持的本机环境还可使用 Keychain。

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
```

产品浏览器测试使用独立临时资料库、测试账户与确定性模型替身，不读写真实密钥。完整 OCR 集成测试在本机资产齐备时执行；公开克隆会明确跳过这部分可选集成用例。

真实服务验收会消耗所配置供应商额度，仅在明确需要时执行 `npm run test:live` 或 `npm run test:live:product`。流程通过不等于所有建议语义正确或已证明长期陪伴效果，仍需要对有限样例作内容审阅。

提交前可运行 `python3 scripts/check_release.py`，检查 Git 实际清单中的私有状态、文件体积与常见凭据格式。测试与发布说明见 [端到端测试](docs/e2e_usage.md)。
