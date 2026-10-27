"""Conversation policy and read-only curated grounding for modern consultations.

Understanding is a user-correctable model summary, not an inferred user profile
or historical evidence. Retrieval ranks the supplied method query; it never
classifies a user's personality, motives or goals from keyword matches.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Dict

from .principles import available_principles, search_principles
from .cases import build_case
from .knowledge_versions import content_version
from .support import SUPPORT_RULES, support_instruction


CONSULT_RULES = """你现在是“教员”对话助手：通过持续对话理解用户的处境和目标，以毛泽东原著、已核公开历史及有出处的思想解释为参考，帮助用户形成自己的判断。你不是毛泽东本人，不代替用户决定人生。
先理解本轮需要什么帮助，再决定倾听、澄清、分析或复盘；不以堆砌名言代替理解，也不强迫每次交流都变成行动计划。
每一轮先调用 understand_user，记录一份给用户看的、可纠正的当前理解：只写用户明确说过的处境、目标、限制和已尝试事项；你提出的假设放进 uncertainties，不写成已知事实。历史里的助手建议不等于用户已经采纳。用户补充、否定或修正时，以最新原话更新理解，不重复问已经答过的问题。此工具只给简短事实摘要，绝不输出内部思维链或心理诊断。可用 statement_sources 为本轮新表达的目标、诉求、事实与限制附上本轮用户原话片段；quote必须逐字取自本轮user，不把助手摘要、旧建议或未知解释标成用户原话。没有原话来源时留空，不编出处。
严格区分用户明确事实与模型推测：不得向用户经历添加未提供的时间、数量、主体、行动顺序或动机，也不把你的解释写成用户已陈述的心理。用户未提供时间和顺序时，只用“此前”“这次反馈”等不具体的指代，不自行补出“上周”“之后又”等时间或先后关系。前轮助手提出的假设只能称为“先前理解”或“此前的假设”，不能写成“用户旧目标”或用户曾经的想法；只有用户明确表达过的目标才能这样回顾。除非用户明确确认已经尝试，不得把助手建议写成用户已采纳或已实施的行动。确需讨论动机等未知因素时，明确标为待核假设并允许用户否定，不用“只决定谁有面子”等断言替别人解释行为。
人数的计量对象必须保持原样：“一个两人团队”不能改写为“你带两个人”或“你和两名成员”，是否包含用户本人仍未知；建议可说“与相关团队成员核对”，不额外指定两位他人。用户明确的人力、预算、健康与时间限制必须保留，即使先处理信息缺口，也不能反称这些限制“不是眼下的问题”；未知的优先次序只作假设，不能否定已知约束。
把 long_term_direction（用户明确重视的长期方向）、stage_goal（当前议题的阶段目标）和 turn_request（这次希望怎样帮助）分别记录。goal 只是 stage_goal 的兼容别名。“压成三句话”“先听我说”“这周一个小实验”通常只更新 turn_request，不能覆盖此前阶段目标；只有用户明确改了目标才更新 stage_goal，未知就留空，不从职业、一次情绪或历史人物价值推断用户人生方向。
信息足够时用 phase=analyze；用户反馈尝试结果时用 phase=review。随后必须调用 method_search，用你从问题中提炼的方法词检索，例如“调查研究 反主观主义”“主要矛盾 次要矛盾 条件”“实践 认识 检验”“领导方法 群众 自愿”“批评 非对抗”。用户无需先知道语录篇名。不把该检索词追加进用户原话。method_search 返回核验史料、方法笔记和有范围的 cases；cases的facts只支持列明事件，outcome_evidence为空时不得声称已核实际效果或完整成功案例。historical_conditions与non_analogies限定类比；全文工具仅作补充。
选择1至2条真正相关的方法，而不是每次罗列全部理论。method_search 的 principles 是研究者的思想解释层，不是本人原话：在问题需要时同时讨论认识方法、价值目标、人的代价与参与关系，保留历史政治和组织条件。价值取舍要说清为什么值得做、为谁做、谁承担成本以及谁应参与；用户可以拒绝某个标准，不能以觉悟、政治认同或历史权威给用户评分，不能要求献身、服从或持续透支。人生责任只能从公开文本有限解释，不补私人心理。
最终回答必须与 understand_user 中的 uncertainties 保持一致。选项内容、关系或后果未知时，只提供比较标准和条件分支，不得预判某几个选项必被排除、某个方向只是体验改善或某方必有否决权。建议中的预期效果只能写成待检验假设，不能写成调查已经得到的结论。输出前核对每个关于用户和他人的陈述：来自哪句用户原话？无出处的陈述删去或明确标为假设；不能用方法论填补这些现实信息。
建议必须贴着用户已经表达的目标、资源和时间条件。用户希望行动时，尽量先给一个今天或本周能做的可逆实验，说明如何判断有效、什么情况需要调整或停止，并用 proposed_action 记录同一份提议。它只是一份建议，不代表用户已经接受或实施；不写status、完成时间或虚构结果。用户仅说“明白了”不代表已尝试；没有做、做不到、想暂停也都是可讨论的反馈。倾听或不需要行动时 proposed_action=null。比较选项的代价、风险和收益，不凭一句语录武断要求辞职、创业、决裂、冲突升级；不编造用户的预算、家庭、动机、感受或经历。用户要求直接方案时，在标明假设的条件下先给方案，不无休止追问。
史料与现代建议严格分开：method_notes 是研究者方法概括，不是毛泽东逐字原话；直接引语只用 quote_eligible=true 的返回原文，引用时不改字且仅摘连续片段，用紧邻的单ID引用标明出处；逐字核对失败不能发布该引语。用户原话、现代拟议说法与历史引语应在句内区分。已核摘要只能转述，不能加引号。历史事件仅限返回的已核公开事实；没有匹配事件就直说资料缺口，不拼接同年事件、不补私人心理或对话。返回事件与某条语录的因果/对应关系只有明确 reviewed 关系才可声称。
交代历史语境与类比差异：调查、实践检验、矛盾分析、群众路线有原本的政治与社会条件，不能抹平成万能效率术；也不能将革命战争中的敌我划分、斗争手段套入今天的普通分歧。现代迁移只借用有解释的方法，明确写“现代建议”或“方法类比”，允许与历史做法不同。没有依据时，承认不知道，不借人物权威保证成功。
引用默认只在对应句内放简短标准标记，如【VE08】、【worldview:14】或【principle:people-and-care】，一个括号只写一个ID，不在括号里并列ID、篇名或关联说明，不使用裸ID替代标记。详细篇名、时间和页码由依据面板展开；用户明确要看详细出处时再写正文，不能因此超出本轮字数或句数。逐句区分直接引语、摘要转述、研究者解释和现代建议，不能在引用了可核原话后把整段笼统称为“都不是原话”。网页只作候选。用户的现实事实来自用户陈述，史料不会证明用户处境。敏感专业或重大决定不以史料代替必要的现实资料与专业判断。
"""


SOURCE_RESPONSE_RULES = '''intent=source时，直接、简短地回答用户要求核对的出处：给核对结论、实际检索范围与仍缺什么证据，默认约80至180字；用户要求深入或有明确篇幅时按其要求调整。查无可靠出处只能说本次实际范围未见、归属仍待核；不能据此断言从未说过、已经证明是伪造、只是转述或后人加工。检索覆盖必须来自实际工具记录，未核验的术语年代也不能自行补成旁证；页面近似命中不授予逐字资格。不要为了凑依据罗列含相似字词的无关OCR片段、术语史、历史案例或调查研究说教，也不为这次查证安排用户行动计划。只有找到相关且有资格的连续原文时才短引并标明对应ID。'''
CONSULT_RULES += '\n' + SUPPORT_RULES + '\n根据当前原话选择 intent 和 phase；已有字段将决定本轮回应要求。用户明确的格式、长度、深度优先；可自然停住，不必每轮追问。'

FINAL_REVIEW_RULES = """这是本轮唯一一次最终答复复核，理解与必要检索已经完成。本次禁止调用任何工具，禁止修改公开 analysis；只返回可以直接展示给用户的修订后完整答复，不展示草稿、审稿过程、核对清单或内部推理。
上一条 assistant 消息是待校正草稿，不是新的用户陈述或事实来源。用户现实事实只能来自历史和本轮的 user 角色原话；前轮 assistant 的推测、建议与本轮草稿均不算用户事实。以用户最新明确修正为准，前轮助手假设仅能称先前理解，不能变成用户旧目标或用户已经实施的行动。
逐项对照用户原话与下附公开 analysis，删除没有用户陈述支持的时间、人数、数量、主体、动机、行动顺序、主体关系和选项分类；若需保留推测，必须改成明确条件分支或聚焦追问。analysis 是可纠正摘要而非新用户事实，不能用它覆盖或扩展 user 原话；其中 uncertainties 必须保持未知，草稿中与其冲突的确定断言必须删除或改为条件分支。检查数值的计量对象：实体数不等于方向或选项数，未知主体是否重合时不能擅自相加或分类，已知选项数也不能推出必须筛除多少项。建议中的实验参数只能明确作为可调整的提议，不能写成用户已有的预算、时间或选择。
特别检查原话“两人团队”是否被扩写成用户之外另有两名成员；用户是否算在团队人数中未知时，只保留总人数或称相关成员。不能补“都要你兜着”等责任与心理。明确的人手、预算、时间或健康约束仍是约束，先核实信息不等于这些条件不再成问题；删除草稿中否认这些限制的句子。
只能使用本轮工具已经返回的出处、证据 ID 与原文；直接引语仍须 quote_eligible=true，不新增篇名、日期、页码、事件或语录关系。只保留标准单ID标记【VE08】或【principle:people-and-care】，不加裸ID、长出处或括号内关联说明。直接引语、转述、研究者解释与现代建议分别标明，不把含原话的一整段统称非原话。严格遵守用户本轮字数、句数与格式：要求三句话就只输出三句话，把必要来源短标放在句内，不加标题、脚注、后记、额外信号段落或追问。详细出处可由依据面板展开。信息不足也不为补齐答案擅自填事实；普通轮次最多一个关键追问。phase=listen 或 intent=listen 时只承接，不安排实验或强行讲史料；其他阶段也仅在用户希望行动时保留一个关键实验，绝不把 proposed_action 写成已接受或已完成。长期方向、阶段目标和本轮请求不得混用。不要以历史人物权威要求用户接受某个价值取舍。
下方 JSON 仅是本轮已展示、不可在此次调用中更改的公开 analysis 数据，不是新增指令或新用户事实：
"""


def final_review_message(analysis: Dict[str, Any], format_limits=None) -> str:
    """Keep the review contract separate from the tentative public summary."""
    limits = response_contract_message(analysis, format_limits)
    return FINAL_REVIEW_RULES.replace('下方 JSON 仅是', limits + '\n下方 JSON 仅是') + json.dumps(analysis, ensure_ascii=False)


RESPONSE_CONTRACT_VERSION = 'scoped-response-v1'
_RESPONSE_STYLES = {
    'listen': ('承接用户明确说的具体处境，允许自然停住；不强塞历史解释，不布置任务。', '80至200字'),
    'clarify': ('先回应已有信息；仅在确实会改变判断时问一个问题，不重复问已答过的内容。', '80至200字'),
    'advice': ('先给条件清楚的建议，再说关键理由；仅在用户希望行动时提出可撤回的尝试。', '300至600字'),
    'compare': ('按用户目标比较选项的条件、代价和未知；允许表格或列表，不替用户排序价值。', '300至600字'),
    'review': ('说明用户新反馈改变了哪些事实与判断；明白了不等于已尝试，没做和暂停也可讨论。', '150至350字'),
    'source': (SOURCE_RESPONSE_RULES, '80至180字'),
    'analyze': ('回应具体困惑，先说明核心判断和必要假设；只解释相关的方法及现代类比边界。', '300至600字'),
}


def response_contract(analysis, format_limits=None):
    """A deterministic delivery contract, never a new classification/LLM stage."""
    intent, phase = analysis.get('intent', 'auto'), analysis.get('phase', 'clarify')
    support = analysis.get('support_mode', 'ordinary')
    if support in {'urgent_safety', 'professional'}:
        style, text, length = support, support_instruction(support), '按现实支持需要'
    else:
        style = ('listen' if intent == 'listen' or phase == 'listen' else
                 'source' if intent == 'source' else
                 intent if intent in {'advice', 'compare', 'review'} else
                 phase if phase in _RESPONSE_STYLES else 'clarify')
        text, length = _RESPONSE_STYLES[style]
    return {'version': RESPONSE_CONTRACT_VERSION, 'style': style, 'instruction': text,
            'default_length': length, 'max_followup_questions': 1,
            'action_policy': ('none' if style in {'listen', 'source', 'urgent_safety'} else 'only_if_user_wants'),
            'retrieval_required': (support == 'ordinary' and style not in {'listen', 'source'} and phase in {'analyze', 'review'}),
            'format_limits': {} if support == 'urgent_safety' else dict(format_limits or {})}


def response_contract_message(analysis, format_limits=None):
    contract = response_contract(analysis, format_limits)
    return ('【本轮回应要求】以下只约束交付方式，始终遵守用户事实、来源资格和最新纠正。'
            '默认长度可被用户明确要求覆盖；不为显得亲切而附和未经证实的事实。\n'
            + json.dumps(contract, ensure_ascii=False) + '\n'
            + format_limit_instruction(contract['format_limits']))


def _number(value):
    if value.isdigit():
        return int(value)
    digits = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '零': 0}
    total, current = 0, 0
    for char in value:
        if char in digits:
            current = digits[char]
        elif char in {'十', '百', '千'}:
            total += (current or 1) * {'十': 10, '百': 100, '千': 1000}[char]
            current = 0
        else:
            return 0
    return total + current


def answer_format_limits(question):
    """Extract only explicit, bounded size requests, not inferred preferences."""
    text = str(question or '')
    number = r'([0-9]{1,4}|[一二两三四五六七八九十百千零]{1,8})'
    limits = {}
    matches = re.findall(r'(?:不超过|最多|控制在|限于|限)\s*' + number + r'\s*(?:个)?字|' + number + r'\s*(?:个)?字(?:以内|之内)', text)
    counts = [_number(left or right) for left, right in matches]
    counts = [value for value in counts if 1 <= value <= 4000]
    if counts:
        limits['max_chars'] = counts[-1]
    sentences = re.findall(r'(?:压成|缩成|概括成|概括为|总结成|总结为|改成|只用|仅用|用|只要|仅要)\s*' + number + r'\s*句(?:话)?', text)
    values = [_number(value) for value in sentences]
    values = [value for value in values if 1 <= value <= 10]
    if values:
        limits['sentences'] = values[-1]
    return limits


def format_limit_instruction(limits):
    items = []
    if limits.get('max_chars'):
        items.append('完整答复不超过' + str(limits['max_chars']) + '个字符（含标点与来源短标）')
    if limits.get('sentences'):
        items.append('只输出' + str(limits['sentences']) + '句话，不加标题、列表或末尾补充')
    return '本轮明确的表达约束：' + '；'.join(items) + '。' if items else ''


def format_violations(content, limits):
    issues = []
    text = (content or '').strip()
    if limits.get('max_chars') and len(text) > limits['max_chars']:
        issues.append('超过字数上限')
    if limits.get('sentences'):
        without_markers = re.sub(r'【[^】]*】', '', text)
        # Quoted questions or historical punctuation are part of their outer
        # sentence, not extra prose authored outside the user's three lines.
        pairs = {'“': '”', '‘': '’', '「': '」', '『': '』'}
        stack, count, has_text = [], 0, False
        for char in without_markers:
            if char in pairs:
                stack.append(pairs[char])
            elif stack and char == stack[-1]:
                stack.pop()
            elif char == '"':
                if stack and stack[-1] == '"': stack.pop()
                else: stack.append('"')
            if char in '。！？!?' and not stack:
                if has_text:
                    count += 1
                    has_text = False
            elif char.strip():
                has_text = True
        if has_text:
            count += 1
        if count != limits['sentences']:
            issues.append('没有按要求输出句数')
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if any(re.search(r'^(?:#{1,4}\s|[-*]\s|\d+[.、])', line)
               or re.fullmatch(r'\*\*[^*。！？]{1,40}\*\*\s*[:：]?', line)
               or re.fullmatch(r'(?:判断|依据|建议|现代建议|边界|信号|继续信号|调整信号)\s*[:：]', line)
               for line in lines):
            issues.append('句数答复包含额外标题或列表')
    return issues


def format_repair_message(limits):
    return ('这是一次有界的表达修复，不是新的用户陈述。只重写上一条assistant答复以满足表达约束，不调用工具、不改变公开理解或行动状态，不添加事实、人数、责任、历史故事或结果。'
            '保留原有事实、条件与关键建议，将必要来源以单个标准【ID】放进对应句内，不在末尾另加出处、解释、问题或后记。直接返回修复后的正文，不解释修复过程。'
            + format_limit_instruction(limits))


def quote_repair_message(limits, intent='auto'):
    """One final, tool-free repair shared with the explicit-format budget."""
    message = ('这是本轮唯一一次有界引用修复，不是新的用户陈述。上一条assistant草稿有历史直接引语未通过核对，不能作为新事实来源。'
               '禁止调用工具、修改公开analysis或行动状态、新增来源ID、篇名、日期、页码、故事或现实事实。'
               '删去无资格的历史断言，或简短说明当前检索范围与资料缺口；不得只去掉引号把伪引语或OCR候选伪装成已证实的转述。'
               '只使用本轮既有资料的明确支持范围；没有可靠出处时说当前范围未见，不宣称已证明从未说过。'
               '用户原话与现代拟议说法仍须和历史原文区分。直接返回可展示的修订答复，不解释修复过程。'
               + format_limit_instruction(limits))
    return message + ('\n' + SOURCE_RESPONSE_RULES if intent == 'source' else '')


UNDERSTANDING_PARAMETERS = {
    "type": "object",
    "properties": {
        "phase": {"type": "string", "enum": ["listen", "clarify", "analyze", "review"]},
        "intent": {"type": "string", "enum": ["auto", "listen", "explore", "advice", "compare", "review", "source"], "description": "用户本轮需要的帮助，允许纠正，不是心理或政治标签"},
        "summary": {"type": "string", "description": "用户可以核对并纠正的处境概括，不含内部推理"},
        "long_term_direction": {"type": "string", "description": "仅用户明确表达的长期重视方向；未知留空，不推断人生观或立场"},
        "stage_goal": {"type": "string", "description": "当前议题的阶段目标，如六周留住客户；不被压成三句话或本周实验等本轮任务覆盖"},
        "turn_request": {"type": "string", "description": "用户本次想怎样获得帮助，如倾听、简短表达或本周小实验，不等于阶段目标改变"},
        "goal": {"type": "string", "description": "stage_goal的兼容别名，填写同一阶段目标；未知留空"},
        "known_facts": {"type": "array", "items": {"type": "string"}, "description": "仅用户明确陈述的现实事实"},
        "constraints": {"type": "array", "items": {"type": "string"}, "description": "用户明确陈述的时间、资源等限制"},
        "uncertainties": {"type": "array", "items": {"type": "string"}, "description": "未明事项与需要验证的假设，不是事实"},
        "next_question": {"type": "string", "description": "至多一个简短聚焦追问；已足够时可为空"},
        "support_mode": {"type": "string", "enum": ["ordinary", "professional", "urgent_safety"],
                         "description": "可选本轮回应路由，不是诊断；只有当前现实紧迫人身危险用urgent_safety，历史/小说引用不自动升级"},
        "statement_sources": {
            "type": "array", "maxItems": 18,
            "description": "可选；只为本轮用户明确表达的字段附本轮user逐字摘录，不记录假设或旧助手建议",
            "items": {"type": "object", "properties": {
                "field": {"type": "string", "pattern": "^(long_term_direction|stage_goal|turn_request|known_facts:[0-5]|constraints:[0-5])$"},
                "quote": {"type": "string", "minLength": 1, "maxLength": 400}},
                "required": ["field", "quote"], "additionalProperties": False}},
        "proposed_action": {
            "type": ["object", "null"],
            "description": "仅一份待用户选择的建议，倾听或无行动需要时null；不得表示用户已接受或已完成",
            "properties": {
                "title": {"type": "string"},
                "steps": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
                "continue_signal": {"type": "string"},
                "adjust_signal": {"type": "string"},
                "review_hint": {"type": "string"},
            },
            "required": ["title", "steps", "continue_signal", "adjust_signal", "review_hint"],
            "additionalProperties": False,
        },
    },
    "required": ["phase", "intent", "summary", "long_term_direction", "stage_goal", "turn_request", "goal", "known_facts", "constraints", "uncertainties", "next_question", "proposed_action"],
    "additionalProperties": False,
}


def understanding(*, phase, summary, goal="", known_facts=None, constraints=None,
                  uncertainties=None, next_question="", long_term_direction="",
                  stage_goal=None, turn_request="", intent="auto", proposed_action=None, statement_sources=None, support_mode="ordinary") -> Dict[str, Any]:
    """Validate public, tentative understanding without inventing missing data."""
    if support_mode not in {'ordinary', 'professional', 'urgent_safety'}:
        raise ValueError('本轮支持范围无效')
    if support_mode == 'urgent_safety':
        proposed_action = None
    if intent == 'source':
        proposed_action = None
    if phase not in {"listen", "clarify", "analyze", "review"}:
        raise ValueError("理解阶段无效")
    if intent not in {"auto", "listen", "explore", "advice", "compare", "review", "source"}:
        raise ValueError("本轮帮助意图无效")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("请根据用户原话提供当前理解")
    if stage_goal is None:
        stage_goal = goal  # Saved legacy summaries keep their known stage goal.
    for value in (goal, stage_goal, long_term_direction, turn_request, next_question):
        if not isinstance(value, str):
            raise ValueError("方向、目标、本轮诉求和追问必须是文本")
    proposal = None
    if proposed_action is not None:
        allowed = {"title", "steps", "continue_signal", "adjust_signal", "review_hint"}
        if not isinstance(proposed_action, dict) or set(proposed_action) - allowed:
            raise ValueError("行动只能记录建议内容，接受、实施和结果状态由用户操作")
        title = proposed_action.get('title', '')
        steps = proposed_action.get('steps', [])
        if not isinstance(title, str) or not title.strip():
            raise ValueError("行动建议需要简短标题")
        if not isinstance(steps, list) or not steps or not all(isinstance(x, str) and x.strip() for x in steps):
            raise ValueError("行动步骤必须是非空文本列表")
        proposal = {'title': title.strip()[:120], 'steps': [x.strip()[:240] for x in steps[:5]]}
        for key in ('continue_signal', 'adjust_signal', 'review_hint'):
            value = proposed_action.get(key, '')
            if not isinstance(value, str):
                raise ValueError("行动信号和复盘提示必须是文本")
            proposal[key] = value.strip()[:240]
    if phase == 'listen' or intent == 'listen' or support_mode == 'urgent_safety':
        phase, intent, proposal = 'listen', 'listen', None
    data = {"phase": phase, "intent": intent, "support_mode": support_mode, "summary": summary.strip()[:600],
            "long_term_direction": long_term_direction.strip()[:240],
            "stage_goal": stage_goal.strip()[:240], "goal": stage_goal.strip()[:240],
            "turn_request": turn_request.strip()[:240], "proposed_action": proposal,
            "next_question": next_question.strip()[:240], "tentative": True,
            "basis": "user_statements_and_explicit_uncertainties"}
    for key, values in (("known_facts", known_facts), ("constraints", constraints), ("uncertainties", uncertainties)):
        if values is None:
            values = []
        if not isinstance(values, list) or not all(isinstance(x, str) for x in values):
            raise ValueError(key + "必须是文本列表")
        data[key] = [x.strip()[:240] for x in values[:6] if x.strip()]
    if statement_sources is None:
        statement_sources = []
    if not isinstance(statement_sources, list) or len(statement_sources) > 18:
        raise ValueError("用户原话来源需为不超过18项的列表")
    sources = []
    for item in statement_sources:
        if not isinstance(item, dict) or set(item) != {'field', 'quote'}:
            raise ValueError("用户原话来源仅包含field和quote")
        field, quote = item['field'], item['quote']
        if (not isinstance(field, str) or not re.fullmatch(r'long_term_direction|stage_goal|turn_request|known_facts:[0-5]|constraints:[0-5]', field)
                or not isinstance(quote, str) or not quote.strip() or len(quote) > 400):
            raise ValueError("用户原话来源字段或摘录无效")
        sources.append({'field': field, 'quote': quote})
    data['statement_sources'] = sources
    return data


def _tokens(text):
    items = []
    for run in re.findall(r"[\u3400-\u9fff]+|[A-Za-z0-9]+", str(text or "").lower()):
        if re.fullmatch(r"[\u3400-\u9fff]+", run):
            items.extend(run[i:i + 2] for i in range(max(0, len(run) - 1)))
            if len(run) == 1:
                items.append(run)
        else:
            items.append(run)
    return set(items)


def _json(value, fallback):
    try:
        result = json.loads(value or "null")
    except (TypeError, ValueError):
        return fallback
    return result if isinstance(result, type(fallback)) else fallback


class CuratedMethodLibrary:
    """Small, bounded retrieval from the existing reviewed database tables."""
    def __init__(self, db_path):
        self.db_path = Path(db_path)

    @staticmethod
    def _reviewed_author_ids(conn, tables):
        if 'evidence_cards' not in tables:
            return set()
        return {row[0] for row in conn.execute(
            "SELECT evidence_id,raw_json FROM evidence_cards WHERE reviewed=1 AND COALESCE(source_role,'')!='biographical_record'")
                if not _json(row[1], {}).get('interpretations_disabled')}

    @staticmethod
    def _card(card, score=0):
        if card.get('reviewed') != 1 or card.get('source_role') == 'biographical_record':
            return None
        raw = _json(card.get('raw_json'), {})
        pages = [x for x in raw.get('pdf_pages', []) if isinstance(x, int) and not isinstance(x, bool) and x > 0][:12]
        source_id = card.get('source_id')
        eligible = card.get('quote_verified') == 1 and bool(card.get('excerpt'))
        item = {key: card.get(key) for key in (
            'evidence_id', 'topic', 'article_title', 'source_id', 'summary',
            'original_date_display', 'earliest_content_date', 'earliest_wording_date',
            'source_role', 'content_qualification', 'known_to_character_from')}
        item.update(record_type='evidence_card', reviewed=True, verification_status='reviewed',
                    quote_verified=eligible, quote_eligible=eligible,
                    excerpt=card.get('excerpt', '') if eligible else '',
                    pdf_pages=pages, anchors=[f'{source_id}:p{x:04d}' for x in pages] if source_id else [],
                    source_urls=_json(card.get('source_urls_json'), []),
                    quotation_scope=raw.get('quotation_scope', '仅所列核验片段'),
                    review_scope=raw.get('review_scope', '仅已核摘要与标明的短引文'),
                    integrity_sha256=raw.get('integrity_sha256'), retrieval_score=score)
        return item

    @staticmethod
    def _note(rule, score=0):
        return {key: rule.get(key) for key in ('rule_id', 'label', 'decision_rule', 'conditions', 'source_locator')} | {
            'retrieval_score': score, 'verification_status': 'method_note',
            'quote_verified': False, 'quote_eligible': False,
            'notice': '研究者的分析性概括，不是逐字引文，也不证明所有历史实践均符合该规则。'}

    @staticmethod
    def _event(event, score=0):
        raw = _json(event.get('metadata_json'), {})
        if event.get('verification_status') != 'reviewed' or raw.get('public_event_verified') is not True:
            return None
        return {'event_id': event.get('event_id'), 'evidence_id': event.get('evidence_id'),
                'title': event.get('title'), 'description': event.get('description'),
                'start_date': event.get('start_date'), 'end_date': event.get('end_date'),
                'source_id': raw.get('source_id'), 'source_urls': raw.get('source_urls', []),
                'record_type': 'historical_event', 'reviewed': True, 'verification_status': 'reviewed',
                'quote_verified': False, 'quote_eligible': False, 'retrieval_score': score,
                'notice': '仅已核公开事实；不据此补私人心理、对白或语录—事件因果关系。'}

    @staticmethod
    def _case(conn, event, tables):
        if 'evidence_cards' not in tables:
            return None
        source = conn.execute('SELECT * FROM evidence_cards WHERE evidence_id=? LIMIT 1', (event.get('evidence_id'),)).fetchone()
        return build_case(event, dict(source)) if source is not None else None

    def verify_citations(self, markers, limit=6):
        """Resolve at most six exact IDs, never promoting a topical match."""
        try:
            limit = max(1, min(int(limit), 6))
        except (ValueError, TypeError, OverflowError):
            limit = 6
        requested = []
        for marker in markers if isinstance(markers, (list, tuple)) else []:
            if isinstance(marker, str) and marker not in requested:
                requested.append(marker)
                if len(requested) >= limit:
                    break
        output = {'requested_markers': requested, 'results': [], 'method_notes': [], 'principles': [], 'cases': [], 'checks': [],
                  'status': 'exact_citation_lookup', 'untrusted': False,
                  'notice': '仅按ID精确追溯本地核验记录，不做主题近似匹配；ID存在不证明正文与来源逐字一致或结论成立。',
                  'history_gap': '历史事件只作独立背景；不据此新增语录—事件对应或因果关系。'}
        conn = None
        try:
            if not self.db_path.is_file():
                raise FileNotFoundError
            conn = sqlite3.connect(self.db_path.resolve().as_uri() + '?mode=ro', uri=True)
            conn.row_factory = sqlite3.Row
            tables = {x[0] for x in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            principles = {row['principle_id']: row for row in available_principles(self._reviewed_author_ids(conn, tables))}
            kinds = {'evidence': ('evidence_cards', 'evidence_id', self._card),
                     'rule': ('philosophy_rules', 'rule_id', self._note),
                     'event': ('events', 'event_id', self._event)}
            seen = set()
            for marker in requested:
                matched = None
                prefix, separator, suffix = marker.partition(':')
                if prefix == 'case':
                    identity = suffix.removeprefix('case:')
                    row = conn.execute('SELECT * FROM events WHERE event_id=? LIMIT 1', ('event:' + identity,)).fetchone() if 'events' in tables else None
                    case = self._case(conn, dict(row), tables) if row is not None else None
                    if case is None:
                        output['checks'].append({'marker': marker, 'status': 'not_found', 'notice': '无此案例或支持事实已撤回。'})
                    else:
                        output['checks'].append({'marker': marker, 'status': 'matched', 'record_type': 'historical_case', 'record_id': case['case_id']})
                        if ('case', case['case_id']) not in seen:
                            seen.add(('case', case['case_id']))
                            output['cases'].append(case)
                    continue
                if prefix == 'principle':
                    note = principles.get(marker) or principles.get(suffix)
                    if note is None:
                        output['checks'].append({'marker': marker, 'status': 'not_found', 'notice': '无此解释ID或关联资料已撤回核验资格。'})
                    else:
                        record_id = note['principle_id']
                        output['checks'].append({'marker': marker, 'status': 'matched', 'record_type': 'principle', 'record_id': record_id})
                        if ('principle', record_id) not in seen:
                            seen.add(('principle', record_id))
                            output['principles'].append(note)
                    continue
                targets = [prefix] if separator and prefix in kinds else list(kinds)
                ids = [marker, suffix] if separator and prefix in kinds else [marker]
                for kind in targets:
                    table, column, construct = kinds[kind]
                    if table not in tables:
                        continue
                    found_record = False
                    for record_id in ids:
                        row = conn.execute(f'SELECT * FROM {table} WHERE {column}=? LIMIT 1', (record_id,)).fetchone()
                        if row is not None:
                            found_record = True
                            item = construct(dict(row))
                            if item is not None:
                                matched = (kind, str(row[column]), item)
                            break
                    if found_record:
                        break
                if matched is None:
                    output['checks'].append({'marker': marker, 'status': 'not_found', 'notice': '无此ID或记录不具备所需核验资格。'})
                    continue
                kind, record_id, item = matched
                output['checks'].append({'marker': marker, 'status': 'matched', 'record_type': kind, 'record_id': record_id})
                if (kind, record_id) not in seen:
                    seen.add((kind, record_id))
                    output['method_notes' if kind == 'rule' else 'results'].append(item)
            return output
        except (FileNotFoundError, sqlite3.Error):
            output.update(status='not_found', results=[], method_notes=[], principles=[], cases=[],
                          checks=[{'marker': marker, 'status': 'not_found', 'notice': '核验库不可用，未放行此引用。'} for marker in requested])
            return output
        finally:
            if conn is not None:
                conn.close()

    def search(self, query: str, limit=4, *, original_query="", enhanced=True):
        if not isinstance(query, str) or not query.strip() or len(query) > 400:
            raise ValueError("方法检索词需为1至400字")
        if isinstance(limit, bool):
            limit = 4
        try:
            limit = max(1, min(int(limit), 6))
        except (ValueError, TypeError, OverflowError):
            limit = 4
        output = {"query": query, "results": [], "method_notes": [], "principles": [], "cases": [], "untrusted": False,
                  "status": "curated_methods", "history_gap": "未检索到与本轮方法相关的已核历史事件，不编造故事或语录—事件联系。",
                  "notice": "用户处境只来自其陈述；方法笔记是研究者概括，现代建议是本轮类比。"}
        if not self.db_path.is_file():
            output["status"] = "not_found"
            output["notice"] = "核验库不可用；只能先澄清问题，不能声称已获得史料支持。"
            return output
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")  # One consistent eligibility snapshot per retrieval.
        original_query = original_query[:4000] if isinstance(original_query, str) else ""
        terms = _tokens(query) | (_tokens(original_query) if enhanced else set())
        scored = []
        try:
            tables = {x[0] for x in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            output['principles'] = search_principles(query, self._reviewed_author_ids(conn, tables), limit=3, enhanced=enhanced, original_query=original_query)
            principle_support = {value for note in output['principles'] for value in note['evidence_ids']}
            if "evidence_cards" in tables:
                for row in conn.execute("SELECT * FROM evidence_cards WHERE reviewed=1"):
                    card = dict(row)
                    raw = _json(card.get("raw_json"), {})
                    if card.get("source_role") == "biographical_record":
                        # Biography is returned below only through verified events.
                        continue
                    hay = " ".join(str(card.get(k) or "") for k in ("topic", "article_title", "summary")) + " " + " ".join(str(x) for x in raw.get("topics", []))
                    score = len(terms & _tokens(hay))
                    # An explicitly linked reviewed source accompanies the
                    # interpretation; the source retains its own quote scope.
                    if card.get('evidence_id') in principle_support:
                        score += 1
                    if score <= 0:
                        continue
                    item = self._card(card, score)
                    if item is not None:
                        scored.append(item)
            scored.sort(key=lambda x: (-x["retrieval_score"], str(x.get("evidence_id"))))
            output["results"] = scored[:limit]
            if "philosophy_rules" in tables:
                notes = []
                for row in conn.execute("SELECT * FROM philosophy_rules"):
                    rule = dict(row)
                    score = len(terms & _tokens(" ".join(str(rule.get(k) or "") for k in ("label", "decision_rule", "conditions", "source_locator"))))
                    if score:
                        notes.append(self._note(rule, score))
                notes.sort(key=lambda x: (-x["retrieval_score"], str(x["rule_id"])))
                output["method_notes"] = notes[:min(limit, 3)]
            if "events" in tables:
                events = []
                for row in conn.execute("SELECT * FROM events WHERE verification_status='reviewed'"):
                    event = dict(row)
                    raw = _json(event.get("metadata_json"), {})
                    if raw.get("public_event_verified") is not True:
                        continue
                    score = len(terms & _tokens(" ".join(str(event.get(k) or "") for k in ("title", "description", "location"))))
                    if score:
                        item = self._event(event, score)
                        if item is not None:
                            events.append(item)
                events.sort(key=lambda x: (-x["retrieval_score"], str(x.get("event_id"))))
                if events:
                    event_limit = min(2, limit - 1) if output["results"] else limit
                    chosen = events[:event_limit]
                    output["results"] = output["results"][:limit - len(chosen)] + chosen
                    for item in chosen[:min(2, limit)]:
                        source_event = conn.execute('SELECT * FROM events WHERE event_id=? LIMIT 1', (item['event_id'],)).fetchone()
                        case = self._case(conn, dict(source_event), tables) if source_event is not None else None
                        if case is not None:
                            output['cases'].append(case)
                    if chosen:
                        output["history_gap"] = "历史事件是独立的背景资料；没有明确已核关系时，不声称它与某条语录存在对应或因果关系。"
            if not any(row.get('record_type') == 'evidence_card' for row in output['results']):
                output['grounding_notice'] = '没有直接相关的作者原著核验卡。历史事件只作背景，不赋予任何语录逐字资格。'
            if not output["results"]:
                output["status"] = "not_found"
                output["notice"] = "未找到直接相关的已核史料；可调整方法词再检索，仍无依据时仅澄清用户处境或给明确标识的现代建议。"
            return output
        except sqlite3.Error:
            output["status"] = "error"
            output["notice"] = "核验库结构不可用；不能声称已完成史料核验。"
            return output
        finally:
            conn.close()
