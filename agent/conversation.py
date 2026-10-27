"""Public execution summaries and conservative paragraph publication helpers.

These checks are not a verifier of historical entailment. The private draft
still passes through the existing final review before any prose is released.
"""
import re

SEARCH_POLICY_VERSION = 'automatic-minimized-v1'
SEARCH_RULES = '''联网由你根据本轮需要决定，不由用户操作开关。倾听、澄清一般不检索；
历史和方法先查本地资料。涉及近期变化、具体外部事实，或本地资料缺少关键出处时，
才有针对性地调用 firecrawl_search；不为展示过程强行搜索。搜索词只写公开主题、
时间、地区和需要查证的事实，最多120字；不得复制聊天、用户姓名、联系方式、
账号、详细地址或其他个人信息。工具未提供、被拦截或失败时，说明尚未核实，
用可用本地资料继续，不能声称完成联网核实。网页命中始终是待核候选。'''


def minimized_query(query, private_terms=()):
    """Reject risky queries before billing/dispatch; never echo the raw value.

    Names are not reliably detectable with regex. Reject autobiographical/chat
    phrases as well as common identifiers, and require a model-proposed public
    topic. This is data minimization, not a claim of complete PII detection.
    """
    if not isinstance(query, str):
        return None
    query = query.strip()
    if not 2 <= len(query) <= 120 or '\n' in query:
        return None
    if any(term and term in query for term in private_terms):
        return None
    if re.search(r'https?://\S+[?/#]|[\w.+-]+@[\w.-]+|(?:\+?\d[\d ()-]{6,}\d)|'
                 r'(?:我叫|姓名|手机号|身份证|微信号|住址|账号|联系人|密码|API.?key)|'
                 r'\b(?:sk|fc)-[A-Za-z0-9-]{8,}|(?:token|secret)\s*[:=]|'
                 r'(?:我|我的|我们|用户|客户)(?:现在|最近|今年|目前|是|在|想|叫|说|遇到|担心)', query, re.I):
        return None
    return query


def private_query_terms(user_texts):
    # Extract only explicitly introduced personal names. Do not guess identity
    # from arbitrary Chinese words or classify public historical names as PII.
    names = set()
    for text in user_texts[-100:]:
        names.update(re.findall(r'(?:我叫|姓名[:：]|同事|朋友|客户|联系人[:：])\s*([\u4e00-\u9fff]{2,4})(?=[，。；、\s]|(?:说|在|的|是|想|已|最近|目前|负责))',text))
    return tuple(sorted(names))[:100]


def progress(stage, text, **extra):
    return {'type': 'status', 'stage': stage, 'text': text, **extra}


TOOL_PROGRESS = {
    'understand_user': ('understand', '正在整理你明确说过的情况与待确认事项。'),
    'method_search': ('local', '正在查找相关方法与历史背景。'),
    'local_search': ('local', '正在查找本地资料。'),
    'local_vector_search': ('local', '正在查找相关本地段落。'),
    'trace_quote': ('verify', '正在回查引语的出处与核验资格。'),
    'trace_event': ('local', '正在查找历史事件及资料范围。'),
    'firecrawl_search': ('web', '这部分需要外部资料，正在等待搜索服务返回候选来源。'),
}


def complete_paragraph_end(text):
    """Only closed paragraphs; do not split fenced blocks or quotations."""
    end = 0
    for match in re.finditer(r'\n[ \t]*\n', text):
        prefix = text[:match.end()]
        if prefix.count('```') % 2 or prefix.count('"') % 2 or prefix.count("'") % 2:
            continue
        if any(prefix.count(a) != prefix.count(b) for a, b in [('“', '”'), ('「', '」'), ('『', '』'), ('‘', '’'), ('【', '】')]):
            continue
        if not re.sub(r'^#{1,6}\s+.*$', '', prefix, flags=re.M).strip() or prefix.rstrip().endswith(('：', ':')):
            continue
        end = match.end()
        break
    return end
