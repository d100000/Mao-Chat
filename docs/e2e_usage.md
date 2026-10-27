# 教员完整路径测试

新增 `npm run test:ui` 使用 `tests/e2e/product.mjs` 检查倾听、三层目标、行动确认、标题与归档、授权记忆、临时对话、来源展开、导出删除和移动端。`npm run test:ui:legacy` 保留既有认证、研究模式与管理后台回归。两者均使用隔离测试库与模型替身。

```bash
npm ci
npx playwright install chromium
E2E_BROWSER_CHANNEL=bundled npm run test:ui
```

下面说明旧回归的运行与真实供应商验收。

`tests/e2e/run.mjs` 使用 Playwright 启动独立无头 Chrome，并启动 `scripts/ui_test_server.py`。服务采用真实路由、认证、SQLite、会话和本地语料检索。模型回答与 Firecrawl 是明确标记的可控测试替身，不会发起外部请求。

## 运行

需要 Python 3、Node.js、Playwright 和本机 Google Chrome。Codex 桌面可通过 `load_workspace_dependencies` 找到内置 Node.js 和依赖目录，然后设置 `NODE_PATH` 为返回的 Node 包目录：

```bash
NODE_PATH=/path/to/node_modules node tests/e2e/run.mjs
```

脚本默认申请随机空闲端口。测试数据库位于独立临时目录，复制语料后会清除用户、管理员、密钥配置、会话、审计和网页记录；测试结束清除临时目录，不读取或写入 macOS Keychain。浏览器使用独立上下文，不复用日常浏览器账户。

报告和截图保存到 `tmp/ui-e2e/`。失败会返回非零退出码，并保存 `failure.png`：

- `report.json`：每条路径的结果和耗时，包含测试替身说明。
- 登录、问答、历史模式、管理后台的桌面和移动截图。

可用 `E2E_OUTPUT=/path/to/output` 改输出位置，`E2E_PYTHON=/path/to/python3` 改 Python。`E2E_BROWSER_CHANNEL` 默认为 `chrome`，设为 `bundled` 使用 Playwright 安装的 Chromium。

## 覆盖路径

- 普通账户注册、登录、错误密码、退出及重新登录。
- 研究问答、本地证据、跟进问题、历史会话恢复。
- 联网开关和候选证据状态。
- 历史日期必填、截止日期传递、历史模式禁止外部搜索。
- 会话恢复模式和截止日期；现代模式。
- 模型错误提示、重试、停止后继续。
- 同一浏览器切换账户时的本地记录隔离和 API 权限。
- 管理员登录、检查配置、保存、用户锁定即时撤销会话并拒绝正确密码登录、解锁后恢复访问、保存配置后继续问答、退出。
- 1440px 桌面、390px 移动及 320px 窄屏布局、抽屉开关以及浏览器未捕获脚本异常。

## 手动复查测试环境

```bash
python3 scripts/ui_test_server.py --port 8876
```

打开 `http://127.0.0.1:8876/auth` 注册普通测试用户。测试管理员为 `ui_admin`，密码 `ui-admin-password-123`。该账号仅存在于隔离测试数据库。服务控制台会明确输出 `provider: deterministic-test-double`，回答也有测试替身标记。

不要用这套服务验证远程模型/Firecrawl 的可用性；远程调用应另做独立 smoke test。可使用 `E2E_BASE_URL=http://127.0.0.1:8876` 让浏览器脚本连接已经运行的测试服务。

## 真实供应商验收

管理员后台保存有效的模型与 Firecrawl 配置后，可运行：

```bash
NODE_PATH=/path/to/node_modules node tests/e2e/live.mjs
```

该脚本启动 `scripts/live_test_server.py`，从现有安全存储读取凭据，使用真实模型和 Firecrawl，会消耗供应商额度。普通账户、会话和运行记录仍保存在独立临时数据库中；不会覆盖正式账户、配置或密钥。

覆盖注册登录、研究问答、多轮上下文、历史截止日期、现代建议、实际联网搜索、恢复与退出。默认每轮最多等待 180 秒、最多六步工具循环。报告和回答保存在 `tmp/live-browser/report.json`、`answers.json`，截图在同目录；不保存凭据。

单元测试中的管理员配置必须使用临时密钥文件和 Keychain 替身。不要在测试中向默认系统 Keychain service 写入占位密钥。

## 对话解惑验收

`tests/e2e/consult.mjs` 验证默认 `consult` 模式，使用 `scripts/live_test_server.py` 中的真实模型和隔离数据库：

```bash
NODE_PATH=/path/to/node_modules node tests/e2e/consult.mjs
```

场景先给出模糊的工作困惑，再补充两人团队、六周期限与预算限制；之后纠正目标并反馈一次尝试的结果，检验助手是否更新理解和建议。随后刷新恢复，继续制定本周实验，并检查旧研究模式仍可使用。

脚本检查实际工具调用、核验资料 ID 的可追溯引用、上下文传递、目标变化、可执行建议和窄屏布局，不把某个固定话术当成正确答案。澄清阶段可以不检索，综合分析与复盘建议必须有方法依据；没有历史事件对应时，不要求模型硬补故事。

报告、完整测试回答和截图保存在 `tmp/consult-browser/`。`CONSULT_E2E_OUTPUT` 可改输出目录，`CONSULT_E2E_BASE_URL` 可连接已启动的隔离真实服务。该测试会消耗供应商额度，不应作为无条件自动执行的单元测试。报告会明确标记真实供应商，凭据不进入源代码、报告或截图。

## 新产品路径的真实模型验收

```bash
npm run test:live:product
```

该命令明确启用真实服务调用，验证只想倾诉、区分长期方向与阶段目标、建议仅为待确认、用户采纳后缩短回答不改变状态，以及显式旧议题记忆、临时会话、反馈、导出和删除。报告与有限场景的真实回复存入被 Git 忽略的 `tmp/live-product-check/`，不提交账户或完整对话。
