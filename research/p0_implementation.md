# P0 功能实现记录

实现日期：2026-10-26

本轮按代码 Review 的 P0 优先级完成了证据链、数据完整性、审计和混合检索基础功能。

## 已完成

1. `Agent.ask()` 增加 evidence gate：候选页命中必须标为候选/待核，未知证据标记会被提示，历史模式继续强制截止日期。
2. 安全默认的 CLI 输出先经过 gate，再分块输出；原始增量输出改为显式 `--unsafe-stream` 调试选项。
3. `sessions`、`messages`、`citations`、`llm_runs` 开始真实写入 SQLite；会话 ID限制为安全字符，密钥不入库。
4. 导入前校验六份 PDF SHA256、2,239页连续性、来源ID、字符数和 `proofread:false`。
5. FTS5 索引使用 staging 表和事务交换；重建失败会回滚并保留旧索引。
6. 增加 `quote_event_links` 导入、审核和追溯功能。当前 `data/quote_event_links.jsonl` 为空，表示没有人工确认关系，系统不会自动编造关联。
7. research/modern 默认使用 FTS + hashing-vector 的 RRF 混合检索，并返回检索来源和分数；historical 不把向量结果当作授权史料。
8. `search --config` 现在进入 runtime 混合检索，`doctor --no-llm` 能真正跳过模型密钥检查。

## 当前验证

- Agent 专项测试：18项通过。
- 项目原有测试：31项通过。
- 数据库重新导入：2,239页、2,229段落、25条语录、10条事件、13条哲学规则。
- 原子索引重建、清单校验失败回滚、审核关系追溯和候选语录 gate 均有回归测试。

## 仍列为 P1

- `pages` 与文章范围的多对多关系，避免重叠文章覆盖 `document_id`。
- 真正语义 embedding 和更强 reranker；当前向量仍是离线词法基线。
- Responses API SSE、provider capability negotiation、工具重复/预算/超时控制。
- 已审核的语录—事件关系数据本身，需要逐条史料审查后填入模板；系统不会自动生成历史关系。
