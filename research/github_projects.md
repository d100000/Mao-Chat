# 从著作语录和回忆录构建人物对话的开源项目调研

核验日期：2026 年 10 月 25 日。本文针对有历史人物原始文献、要求回答符合其时代、经历和思想的人物对话项目。建议先采用“角色规则 + 可追溯史料检索 + 年代过滤 + 回答核验”，在资料和评测稳定以后再考虑微调。单独一张人物卡或一份 SKILL.md 能规定行为，但不会自动建立全文知识库，也不能保证历史准确。

## 项目比较

| 项目 | 主要方式 | 数据与接口 | 可借鉴之处 | 对本项目的限制 |
|---|---|---|---|---|
| [ChatHaruhi](https://github.com/LC1332/Chat-Haruhi-Suzumiya) | 人物提示 + 原作场景检索，也有训练数据路线 | TXT 场景、人物 system prompt、含文本和向量的 JSONL | 从长文本提取场景和人物材料，按问题检索后放入上下文 | 面向文学角色，需增加史料类型、日期、版本及出处；年代隔离不是原生保证 |
| [Character-LLM](https://github.com/choosewhatulike/trainable-agents) | 人物资料 → 场景 → 补全经历 → 监督微调 | 段落资料、生成的 JSONL、用于训练的对话 JSON | 把经历组织为具体情境，区分单轮和多轮采访评测 | 场景补全含生成细节，不能直接作为真实历史经历 |
| [RoleLLM / RoleBench](https://github.com/InteractiveNLP-Team/RoleLLM-public) | 角色知识抽取、语言风格模仿、角色条件微调 | 角色简介和对话 JSONL；通用问题与角色专属问题分组 | 将“知道什么”与“怎么说”拆开；分别评测知识与风格 | GitHub 当前主要是说明与图片，实际数据在 Hugging Face；不能当成现成完整机器人 |
| [SillyTavern](https://github.com/SillyTavern/SillyTavern) | 聊天前端 + 人物卡 + World Info + Data Bank RAG | JSON/PNG 人物卡、世界书条目、文件附件和向量索引 | 能较快接入现成模型，验证角色定义与语料库的效果 | 日期过滤、逐条引文校验仍需扩展；前端不等于模型 |
| [OpenCharacters](https://github.com/josephrocca/OpenCharacters) | 浏览器人物聊天 + 摘要记忆 + 相关记忆检索 | 人物定义、消息数组、lorebook、customData、自定义 JS | 可试验长期对话记忆和自定义检索 | README 明示维护模式；聊天摘要会丢失限定词，不能回写为史实 |
| [Character Card V2](https://github.com/malfoyslastname/character-card-spec-v2) | 角色交换格式，无训练和检索引擎 | `chara_card_v2` JSON，内含人物定义、系统提示、示例及人物书 | 同一人物定义可在不同兼容前端间迁移 | 规范没有执行历史约束的能力，扩展字段须由运行时读取 |

## 核验到的实现细节

### ChatHaruhi 适合借鉴检索流程

项目提供从小说中提取场景摘要与人物对话、整理人物文本、设计 system prompt、计算 embedding、载入角色的流程。历史文献可以复用“提取和检索”的思路，但应直接保留原文，不能沿用小说改写来生成史实。参见[人物资料提取说明](https://github.com/LC1332/Chat-Haruhi-Suzumiya/blob/main/characters/novel_collecting/Readme.md)。

实际读取了 [ChatHaruhi.py](https://github.com/LC1332/Chat-Haruhi-Suzumiya/blob/main/ChatHaruhi2.0/ChatHaruhi/ChatHaruhi.py)：`get_text_from_data` 读取 `text` 或 `enc_text`；JSONL 的向量字段中用 `system_prompt`、`config` 表示特殊行；`add_story` 做相似检索并在 token 预算内插入场景；对话历史另行插入。该路径没有史料日期、书籍版本、来源可信度等硬过滤。应用到本项目时，应在检索结果进入模型前完成这些过滤，并把出处随文本一并注入。

### Character-LLM 适合研究情境化训练

仓库的流程从人物 profile 提取场景，再生成场景内的交互经历，最终训练特定人物模型。其资料里包含历史人物，因而与本需求有直接关联；但这种 experience reconstruction 会补写细节，适合作为模拟语料研究，不能视为历史记录。参见[官方 README](https://github.com/choosewhatulike/trainable-agents/blob/main/README.md)和[训练数据卡](https://huggingface.co/datasets/OpenMOSS-Team/character-llm-data/blob/main/README.md)。

若后期使用这一思路，生成样本必须另存为 `synthetic_dialogue`，附上支持其观点的真实文献片段。没有证据的地点、动作、心理、私下对话不得混入事实库。

### RoleLLM 适合拆分知识与语言风格

官方框架分别处理角色资料、从长文本提取角色知识、模仿说话风格、角色条件微调。RoleBench 把通用指令、角色专属指令及角色泛化测试分开，并提供中英文资料。参见[框架说明](https://github.com/InteractiveNLP-Team/RoleLLM-public/blob/main/README.md)和[数据结构说明](https://huggingface.co/datasets/ZenMoore/RoleBench/blob/main/README.md)。

本次读取的 [GitHub 根目录](https://api.github.com/repos/InteractiveNLP-Team/RoleLLM-public/contents/)只有 README 和 assets。它可作为数据设计和评测参考，不能据此宣称已经拿到完整训练部署代码。

### SillyTavern 适合先做可聊天的实验界面

[World Info](https://docs.sillytavern.app/usage/core-concepts/worldinfo/)可以按关键词等条件插入背景资料；[Data Bank](https://github.com/SillyTavern/SillyTavern-Docs/blob/main/Usage/Characters/data-bank.md)可以按角色管理文献附件，Vector Storage 将文件切块并检索相关内容。其文档也明确说明，背景插入并不保证模型一定采用。

应将“经核验的人物定义”和“按年代划分的史料”分别配置。Data Bank 的角色附件不会随人物卡一起导出，交付时必须同时交付资料和索引配置。默认向量提供者未必适合中文，应根据中文检索评测选择，不能只看英文示例。

### OpenCharacters 适合实验对话记忆

其 [README](https://github.com/josephrocca/OpenCharacters/blob/main/README.md)说明浏览器保存、自动摘要、相关记忆检索和 lorebook。[自定义代码接口](https://github.com/josephrocca/OpenCharacters/blob/main/docs/custom-code.md)允许读取消息并扩展检索流程。

适合把用户偏好、当前讨论主题保存为会话记忆。文献事实应保持只读；机器人以前自己生成的说法，不能因为进入摘要而升级成历史证据。

### 人物卡可以输出 但必须附运行规则

[V2 规范](https://github.com/malfoyslastname/character-card-spec-v2/blob/main/spec_v2.md)包含 `description`、`personality`、`scenario`、`mes_example`、`system_prompt`、`character_book` 与 `extensions`。其中 `creator_notes` 不进入提示，不能把关键年代约束只写在该字段里。

建议把日期与史料字段放在独立知识库；人物卡只保存核心定义、模式说明及知识库关联信息。需要跨前端保存的字段可使用命名空间扩展，例如 `extensions.historical_persona`，但任何“按年份过滤”的功能都要有实际执行代码。

## 许可和维护状态

以下“最近推送”来自核验时的 GitHub 仓库 API `pushed_at`，采用 UTC 日期；它不等于最近正式发布、默认分支最近功能提交或维护承诺。`updated_at` 会受非代码活动影响，本文不据此判断维护活跃度。

| 项目 | 许可证据 | 最近推送 | 状态与采用影响 |
|---|---|---|---|
| Character-LLM | [代码 LICENSE](https://github.com/choosewhatulike/trainable-agents/blob/main/LICENSE) 为 Apache-2.0；[数据卡](https://huggingface.co/datasets/OpenMOSS-Team/character-llm-data/blob/main/README.md) 为 CC BY-NC 4.0；README 另有学术用途限制说明，模型还关联 LLaMA 1 条款 | 2024-10-29 | [API](https://api.github.com/repos/choosewhatulike/trainable-agents) 显示未归档。许可应按代码、数据、权重分别核对，不能笼统称可商用 |
| ChatHaruhi | [代码 LICENSE](https://github.com/LC1332/Chat-Haruhi-Suzumiya/blob/main/LICENSE) 为 Apache-2.0；[54K 数据卡](https://huggingface.co/datasets/silk-road/ChatHaruhi-54K-Role-Playing-Dialogue/blob/main/README.md) 标注 CC BY 4.0 | 2024-08-13 | [API](https://api.github.com/repos/LC1332/Chat-Haruhi-Suzumiya) 显示未归档。原作材料及各模型许可须另核 |
| RoleLLM-public | 仓库 API `license=null`，根目录未见 LICENSE；[RoleBench 数据卡](https://huggingface.co/datasets/ZenMoore/RoleBench/blob/main/README.md) 标 Apache-2.0 | 2024-10-11 | [API](https://api.github.com/repos/InteractiveNLP-Team/RoleLLM-public) 显示未归档。数据许可不能直接代替仓库代码许可 |
| SillyTavern | [LICENSE](https://github.com/SillyTavern/SillyTavern/blob/release/LICENSE) 为 AGPL-3.0 | 2026-10-02 | [API](https://api.github.com/repos/SillyTavern/SillyTavern) 显示未归档；本次日期内有推送 |
| OpenCharacters | [LICENSE](https://github.com/josephrocca/OpenCharacters/blob/main/LICENSE) 为 MIT | 2025-03-29 | [API](https://api.github.com/repos/josephrocca/OpenCharacters) 显示未归档，但 README 明示只做修复和小更新的维护模式 |
| Character Card V2 | [API](https://api.github.com/repos/malfoyslastname/character-card-spec-v2) 的 `license=null` | 2023-06-22 | 未归档；用于理解兼容结构，不把格式规范误作完整开源运行框架 |

这些许可信息是上游项目自述及文件核验结果，不涉及用户六份 PDF 的再分发授权判断。

## 针对毛泽东文献的组合方案

以下是基于上述项目的工程设计判断，不是某一项目已经实现的历史准确性保证。

1. **先保存原文。** 每页保留文件、PDF 页码、原书页码及 OCR 状态；修订文本与原始转写并存。语录再链接到原篇章，避免脱离上下文。
2. **再建证据单元。** 将著作日期、谈话日期、事件日期、出版日期分开；人物原文、编辑说明、他人回忆、研究者推断分开。回忆录需要作者身份、成文时间、所述事件时间与相互印证关系。
3. **从文献提炼思考规则。** 每条“实践检验”“调查研究”“主要矛盾”等规则都附适用语境、原文证据和年代，不能把某一时期的政治判断概括成终生不变的所有场景规则。
4. **检索先过滤后相似排序。** 历史情境模式只能调用指定时点前人物可能掌握的事实；后来的编者注、回忆和研究可供研究模式使用，不能混入第一人称记忆。
5. **生成时标出证据层级。** 原文引语须可逐字核对；概括、情境模拟、对现代问题的类比推演应有不同标记。没有证据时回答“现有资料不足”，保留空白。
6. **把 skill、知识库和前端分开。** SKILL.md 规定检索及回答流程；JSONL/数据库保存证据；SillyTavern 或自建前端承载聊天。后续可导出人物卡，不能把三者混称一个提示词。
7. **先做反例评测再训练。** 检查未来事件泄漏、伪引语、错年代、把他人回忆当本人经历、同一问题跨轮矛盾，以及只模仿口号却没有思想依据等错误。微调只使用经过核验的数据和明确标记的合成模拟。

建议第一阶段采用自己的史料结构与检索脚本，借鉴 ChatHaruhi 的上下文组织；用 SillyTavern 验证聊天体验，借鉴 RoleLLM 的知识与风格分开评测。Character-LLM 留作后续训练方法研究，OpenCharacters 用于试验长期会话记忆。
