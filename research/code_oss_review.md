# 本地 Agent 代码与开源项目 Review

评审日期：2026-10-26

## 结论

当前项目已经是一个可运行的“本地史料检索 + 可配置 LLM + bounded tool loop”原型，基础检索和历史截止点做得比较扎实。1,200 题离线检索评测通过，但这证明的是检索层和边界层，不是 1,200 次自然语言回答都正确。

最需要优先补的不是再增加工具，而是把“回答必须经过出处/逐字资格检查”接入正常 `ask` 路径，并把语录—原篇—事件关系真正写入数据库。当前 `quote_event_links` 表是空的，`sessions`、`messages`、`citations`、`llm_runs` 也仍为空；这些表目前只是 schema 预留。

## 本地代码发现

### P1：回答校验没有接入正常输出

`agent/runtime.py` 有 `structured_draft()`，但普通 `Agent.ask()` 直接返回模型自然语言，没有调用 `persona.py check/record` 或等价的 evidence/citation gate。因此模型可能先说出未核实的候选语录，再在后文降级。24 条真实回答中 Q0004 就出现了这个问题。

建议：把运行流程改为：

```text
模型草稿 → 证据ID检查 → quote/date/candidate检查 → 不通过则修正或降级 → 最终流式输出
```

流式体验可以保留，但最终提交前必须有一个本地 gate；未通过时应显示“候选/待核”，而不是保留模型的第一句确认。

### P1：语录和事件目前只是并行搜索，没有关系证明

`trace_quote` 和 `trace_event` 分别返回 LIKE/FTS 命中，不能说明语录和事件存在史料关系。`quote_event_links` 当前为 0 条。

建议增加人工审核关系记录：

```text
directly_discusses
written_in_context_of
later_recap
same_event_record
analogy_only
unrelated
```

每条关系应带 `confidence`、`verification_status`、来源页和支持说明。

### P1：全文索引重建不是原子操作

`agent/index.py` 先删除 FTS 表，再重新创建。中途失败会留下缺失索引。应使用临时 SQLite/临时 FTS 表，完成计数和一致性检查后再替换，保留旧索引可用。

### P1：导入时文章关系会覆盖

`agent/ingest.py` 将文章范围直接写入 `pages.document_id`。文章范围重叠时，后一篇会覆盖前一篇，导致一个页面不能保留多个文章归属。应增加 `page_documents` 或 `article_pages` 多对多表。

### P1：导入没有重新核验 PDF 与 OCR 清单

导入器主要信任 `manifest.json` 和 JSONL，未在每次 `ingest` 中重新验证 PDF SHA256、页数、字符数、连续页锚和 `proofread:false` 标记。应让 `doctor` 或 `ingest --verify` 在导入前失败关闭。

### P1：LLM 协议兼容范围偏窄

`agent/llm.py` 主要解析 Chat Completions 的 `data:` SSE；不完整流、Responses API 事件、无 `[DONE]` 的结束状态和 provider-specific thinking 字段还没有统一处理。`thinking` 当前硬发，网关不支持时可能直接返回 400。应增加 provider capability negotiation 和 Responses/Chat 两套解析器。

### P1：可观测性表没有真正使用

数据库已有 `sessions`、`messages`、`citations`、`llm_runs`，但运行时把会话写成 JSON 文件，实际调用没有写入证据ID、引用、provider、模型、延迟和 token。后续无法回答“这条回答为什么这样说”。应把 JSON 作为导出格式，SQLite 作为主审计记录。

### P2：检索融合仍是简单拼接

FTS 和 hashing vector 结果直接合并后截断，没有统一分数、去重、metadata filter 或 reranker。当前 hashing-charbigram 只是可重复的词法向量基线，不是语义 embedding。建议优先使用 BM25 + 真正 embedding + RRF/加权融合，再加入 rerank。

### P2：错误降级可能隐藏数据库故障

`LocalKnowledge.search` 捕获 SQLite 错误后会回退到旧的 `corpus/search.sqlite`，但返回结果仍可能标成正常。本地数据库损坏时，用户可能得到陈旧答案却不知道。应返回 `degraded:true`、错误来源和 `doctor` 失败状态。

### P2：CLI 和安全细节

- `python -m agent` 把 `search` 路由到数据库 CLI，带 `--config` 时容易和 runtime CLI 产生歧义。
- `--no-llm` 参数目前没有实际禁用逻辑。
- session ID 直接拼接文件路径，应限制为安全字符并拒绝 `..`。
- `LIKE` 查询没有转义 `%` 和 `_`，特殊输入会扩大匹配范围。
- tool loop 只有步数上限，没有总时间、重复工具调用和 token 预算。
- Firecrawl 完整 Markdown 与 raw 元数据都有重复保存，网页多时数据库会膨胀。

## 开源项目对比

| 项目 | 适合借鉴的部分 | 明显短板 | 对本项目的价值 |
|---|---|---|---|
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | Skills、长期记忆、工具循环、多 provider、web_search/web_extract、多渠道 | 面向通用自动化；没有历史时序、原话资格和史料引用约束 | 借鉴工具协议、技能化 persona、工具审批和持久记忆；不要直接照搬其宽松知识边界 |
| [RAGFlow](https://github.com/infiniflow/ragflow) | 深度 PDF/扫描件解析、混合检索、重排、可追溯引用、Agent-native 知识库 | 部署组件重；产品化复杂；不是历史人物专用 | 借鉴解析、chunk、引用 UI、混合召回和知识库管理 |
| [LlamaIndex](https://github.com/run-llama/llama_index) | 文档连接器、节点元数据、查询路由、Workflows、Citation Query Engine | 框架抽象多；需要自行实现时代过滤和人物规则 | 借鉴节点/页锚元数据、子问题分解、引用查询和工作流编排 |
| [Haystack](https://github.com/deepset-ai/haystack) | 显式 pipeline、路由、循环、memory、评测、可观测 hooks；Apache-2.0 | 组装成本较高，UI较少 | 借鉴把日期过滤、引用检查、拒答、评测变成可观测 pipeline 组件 |
| [Dify](https://github.com/langgenius/dify) | 可视化 Workflow、RAG Pipeline、模型配置、MCP、回放和 LLMOps | 自托管组件重；许可证有附加条款和品牌限制 | 借鉴工作流版本、运行回放、数据集管理和 API 化 |
| [AnythingLLM](https://github.com/Mintplex-Labs/anything-llm) | 本地优先、workspace、模型/向量库切换、Agent、MCP、成品 UI | 历史时序和细粒度证据链需自行扩展 | 借鉴按人物/时期隔离 workspace 和本地部署体验 |
| [Open WebUI](https://github.com/open-webui/open-webui) | 本地聊天 UI、知识库、工具、MCP、权限、多模型、引用展示 | 功能面大；品牌/许可证需单独核查；自定义 RAG 边界不如代码框架清晰 | 借鉴 UI、知识库选择、引用展示、工具审批和模型切换 |
| [CharacterEval](https://github.com/morecry/CharacterEval) | 中文角色多轮评测、知识/人格/风格/连贯性维度、人工标注奖励模型 | 文学角色，不是历史证据；仓库规模小 | 借鉴“回答质量不能只看事实命中”的多维评测 |
| [TimeChara](https://github.com/ahnjaewoo/timechara) | point-in-time、past/future knowledge、角色时间一致性测试 | Harry Potter 英文基准，不是 RAG 工具 | 直接借鉴未来知识泄漏测试设计 |
| [RoleLLM](https://github.com/InteractiveNLP-Team/RoleLLM-public) | profile、知识注入、风格与角色条件训练拆分 | 主要是虚构/影视角色；资料和许可证需逐项确认 | 借鉴将人物知识、价值规则和表达风格拆开 |
| [HistFig](https://github.com/mcjkurz/histfig) | 历史人物上传、会话隔离、向量+BM25、FastAPI/Gradio | 小众，许可和历史准确性不足 | 借鉴人物资料管理和会话隔离 |
| [MrMao](https://github.com/seconic037/MrMao) | 最贴近本项目：混合检索、两阶段 think→speak、分层记忆、日期/书名引用 | 个人项目，许可/数据版权不清；思考文本不等于可验证推理 | 借鉴 RRF、分层记忆和引用 UI，但必须保留本项目的 evidence gate |
| [three-kingdom](https://github.com/Gingerate/three-kingdom) | Agentic RAG + 知识图谱、来源分级、检索评分、反思节点、SQLite 人物/事件图 | 规模小、许可和维护需自行确认；题材是三国 | 对来源分级、反思节点、人物/事件图谱最有参考价值 |
| [Dialogues with Chinese Sages](https://github.com/Yinuo-Guo/dialogues-with-chinese-sages) | 角色卡、时代/世界观/语气、中文/英文、说话/写作/文化注解模式 | RAG 细节和历史核验较少，许可需确认 | 借鉴角色配置、输出模式和文化注解，不借鉴其事实保证 |

## 功能优先级

### P0：必须先完成

1. **Citation/quote/date gate 接入 `ask`**：没有证据ID、逐字资格或候选边界时，自动降级为转述/待核。
2. **真正的 quote→source→event 关系库**：人工审核关系、关系类型、置信度和支持页。
3. **导入和索引完整性校验**：PDF SHA256、页数、字符数、页锚、OCR状态；索引临时构建后原子替换。
4. **SQLite 审计记录**：sessions/messages/citations/llm_runs 真实写入，记录 prompt hash、模型、延迟、证据和回答。
5. **混合检索重排**：BM25 + 真 embedding + RRF/metadata filter，保留 source_id、page、evidence status。
6. **历史模式和研究模式分离输出**：历史模式不接受向量结果作为授权史料；研究模式明确“后编版本/候选资料”。

### P1：提高回答质量和可维护性

1. 真正的中文语义 embedding，支持本地模型或用户配置的 embedding API。
2. 按文章/段落建立多对多页面关系，支持章节、上下文窗口和版本链。
3. Responses/Chat SSE 双协议、thinking capability、重试/超时/熔断和重复工具调用检测。
4. 接入 LlamaIndex/Haystack 风格的 Query Router、Sub-question、Citation Query Engine 思路，但不替换本地证据规则。
5. Firecrawl URL allowlist、缓存过期、网页正文哈希和网页候选引用隔离。
6. 把 1,200 题基准接入每次代码变更的回归测试，并增加 LLM-as-judge 之外的人工金标集。

### P2：产品化和扩展能力

1. 本地 Web UI：时间线、证据图、PDF页、候选/已核状态、引用展开。
2. 人物/时期 workspace、多个 persona profile、权限与数据集选择。
3. MCP/OpenAPI 服务、后台索引任务、增量同步和任务队列。
4. GraphRAG 全局查询、实体/事件图、跨卷摘要；先有关系金标后再做，不能用自动图谱替代史料核验。
5. Hermes 式技能版本、记忆压缩、可编辑 Lorebook；不允许“自我学习”自动改变历史事实库。

## 采用建议

当前不建议直接把项目迁移到完整的 Dify/RAGFlow/Open WebUI。它们适合快速产品化，但会增加部署和数据边界复杂度。更合适的路线是保留现有 Python/SQLite 证据核心，吸收 Haystack/LlamaIndex 的 pipeline、citation 和 eval 设计，再选择 Open WebUI 或轻量 FastAPI 作为前端。

最先实现 P0-1、P0-2、P0-3、P0-4；它们直接决定回答是否能被历史研究者复核。向量模型、GraphRAG、UI 和多渠道应排在证据链之后。
