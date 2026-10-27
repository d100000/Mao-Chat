# 本地毛泽东史料问答 Agent

本地 Agent 已建立在 `agent/`，运行数据库为 `data/mao_agent.db`。它把之前的六册 OCR 和核验资料导入 SQLite，提供 FTS5 全文检索、离线向量检索、语录/事件追溯、Hermes 风格工具循环、DeepSeek OpenAI-compatible 流式调用和 Firecrawl 网页搜索。

## 当前数据库

```text
sources:          6
documents:       26
pages:         2239
passages:      2229
evidence_cards:  37
quotes:          25
events:          10
philosophy_rules:13
embeddings:    2277
web_sources:      4
```

六册页面文字已经保存为原项目的 `corpus/*.txt`、逐页 `*.pages.jsonl`，并导入 `data/mao_agent.db`。它们是机器 OCR，直接引语仍应回看 PDF 原页。

## 快速使用

```bash
python3 -m agent doctor
python3 -m agent search '论持久战 速胜论' --kind passages --limit 5
python3 -m agent vector-search '实践如何检验认识' --limit 5
python3 -m agent trace-quote '矫枉必须过正'
python3 -m agent --config agent/config.json ask \
  '请查找1938年反对亡国论和速胜论的原篇、事件背景和哲学方法。' \
  --mode research
```

详细说明见 [Agent 使用说明](docs/agent_usage.md) 和 [运行时 README](agent/README.md)。

## 模型与搜索

模型配置在 `agent/config.json`，示例在 `agent/config.example.json`。当前使用 `deepseek-v4.1-flash` 和流式/思考参数；密钥只从环境变量或 macOS Keychain 读取。Firecrawl 搜索结果保存进 `web_sources`，并始终标记为不可信网页候选，不会覆盖本地历史证据。

## 测试记录

- 根测试套件 31 项通过；Agent 专项测试 12 项通过。
- 已完成真实网关多轮对话、结构化草稿、流式输出和 Firecrawl 搜索测试。
- 多轮会话文件保存在 `data/sessions/`；真实联调摘要保存在 `data/real_runs/`。

## 1,200题评测

`agent/benchmarks/questions.jsonl` 保存了 1,200 条唯一问题，覆盖 historical、research、modern，四种复杂度和语录、事件、哲学、版本冲突、未来知识、缺证处理等类别。

本地评测结果：1,200/1,200 通过；全文/向量/语录/事件检索命中率 100%；历史安全率 100%；未来资料违规 0；向量错误 0；中位检索耗时约 24 ms。报告见 `data/benchmarks/summary.json` 和 `data/benchmarks/report.md`。

另外从 12 个模式/复杂度单元各抽取 2 题，完成 24 次真实模型回答；24/24 返回非空回答，0 次调用错误。该抽样报告见 `data/benchmarks/llm_sample_summary.json`，完整回答见 `data/benchmarks/llm_sample_results.jsonl`。这 24 次是真实 LLM 抽样，不把 1,200 次本地检索冒称为 1,200 次模型回答。抽样内容评审得分 258/260，发现 1 条候选语录归属表达需要收紧，详见 `data/benchmarks/llm_sample_content_review.md`。
