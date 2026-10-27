"""Bounded, inspectable checks for cited prose and attributed direct quotations.

This ledger does not prove that a paraphrase follows from a source. It checks
only explicit citations and likely historical quotations, leaving ordinary
user quotes and proposed modern wording outside the quotation gate.
"""
from __future__ import annotations

from datetime import date
import hashlib
import json
import re
import unicodedata


MARKER = r'【((?:[A-Za-z][A-Za-z0-9_-]*(?::[A-Za-z0-9_-]+)*)|(?:\d{2}_[A-Za-z0-9_-]+:p\d{4,}))[^】]*】'
QUOTED = re.compile(r'''“([^”]+)”|「([^」]+)」|『([^』]+)』|"([^"]+)"|‘([^’]+)’|'([^']+)' ''', re.VERBOSE)
AUTHOR = re.compile(r'(?:毛泽东|毛主席|主席|教员|作者).{0,16}(?:说|写|指出|强调|原话|讲)|《[^》\n]+》.{0,12}(?:说|写道|指出|强调)|(?:原文|原著).{0,10}(?:说|写|指出|强调)|(?:毛泽东|毛主席|主席|教员|作者|原文|原著)\s*[:：]|(?:原话|逐字引语|直接引语|短引|引文)\s*(?:是|为|[:：])')
HISTORICAL_AUTHOR = re.compile(r'(?:毛泽东|毛主席|主席|教员|作者).{0,16}(?:说|写|指出|强调|原话|讲)|《[^》\n]+》.{0,12}(?:说|写道|指出|强调)|(?:原文|原著).{0,10}(?:说|写|指出|强调)|(?:毛泽东|毛主席|主席|教员|作者|原文|原著)\s*[:：]')
MODERN = re.compile(r'(?:你(?:刚才)?说|你提到|用户(?:说|原话|自述)|客户|同事|负责人|朋友|家人|我说|可以问|试着问|建议问|例如问|比如问|可以说|现代建议|今天的建议)')
DISAVOW = re.compile(r'(?:(?:网上|有人|流传|所谓).{0,8}(?:误传|伪引语)|(?:伪造|杜撰)的引语|不是(?:毛泽东的?)?原话|并非原话|不能当作原话|不可当作原话|不能引用为)')


def _normalize(value):
    # Exact punctuation and characters are retained. Only layout whitespace
    # and Unicode canonical composition are ignored, never semantic variants.
    text = unicodedata.normalize('NFC', str(value or ''))
    text = re.sub(r'(?<=[\u3400-\u9fff。，；：！？])\s+(?=[\u3400-\u9fff。，；：！？])', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def _rows(tools):
    for tool in tools if isinstance(tools, list) else []:
        result = tool.get('result', {}) if isinstance(tool, dict) else {}
        if not isinstance(result, dict):
            continue
        for key in ('results', 'method_notes', 'principles', 'cases'):
            for row in result.get(key, []) if isinstance(result.get(key), list) else []:
                if isinstance(row, dict):
                    yield row, result.get('untrusted') is True or row.get('untrusted') is True


def _identity(row):
    if row.get('record_type') == 'historical_case':
        return str(row.get('case_id') or '')
    if row.get('record_type') == 'historical_event':
        return str(row.get('event_id') or '')
    return next((str(row[k]) for k in ('evidence_id', 'id', 'quote_id', 'event_id', 'rule_id', 'principle_id', 'anchor') if row.get(k)), '')


def _aliases(row):
    identity = _identity(row)
    output = {identity} if identity else set()
    pairs = {'evidence_id': 'evidence', 'quote_id': 'quote', 'event_id': 'event',
             'rule_id': 'rule', 'principle_id': 'principle', 'case_id': 'case', 'anchor': 'page'}
    for key, prefix in pairs.items():
        if row.get('record_type') == 'historical_case' and key != 'case_id':
            continue
        if row.get('record_type') == 'historical_event' and key != 'event_id':
            continue
        if row.get(key):
            output.update((str(row[key]), prefix + ':' + str(row[key])))
    if row.get('id'):
        output.add(str(row['id']))
    return output


def _eligible(row, untrusted, mode, as_of):
    if untrusted or row.get('record_type') in {'historical_case', 'historical_event', 'principle_note', 'method_note'}:
        return False
    if row.get('verification_status') in {'candidate', 'method_note', 'principle_note', 'limited_facts'}:
        return False
    if row.get('reviewed') not in (True, 1) and row.get('verification_status') != 'reviewed':
        return False
    if row.get('source_role') == 'biographical_record':
        return False
    if not (row.get('quote_eligible') is True or row.get('quote_verified') in (True, 1)
            or (row.get('quote_id') and row.get('verification_status') == 'reviewed')):
        return False
    if mode == 'historical':
        try:
            cutoff = date.fromisoformat(as_of)
            wording = date.fromisoformat(row.get('earliest_wording_date', ''))
            known = row.get('known_to_character_from')
            return wording <= cutoff and (not known or date.fromisoformat(known) <= cutoff)
        except (TypeError, ValueError):
            return False
    return True


def _fingerprint(row):
    qualification = {k: row.get(k) for k in ('record_type', 'evidence_id', 'quote_id', 'event_id', 'rule_id', 'principle_id', 'case_id',
                    'summary', 'excerpt', 'text', 'quote_verified', 'quote_eligible', 'reviewed', 'verification_status',
                    'source_role', 'earliest_wording_date', 'known_to_character_from') if k in row}
    return hashlib.sha256(json.dumps(qualification, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def check_direct_quote(text, markers, tools, mode='consult', as_of=None):
    """Validate an explicitly declared quote, including structured drafts."""
    wanted = set(markers)
    candidates = [(row, untrusted) for row, untrusted in _rows(tools) if wanted.intersection(_aliases(row))]
    eligible = [(row, untrusted) for row, untrusted in candidates if _eligible(row, untrusted, mode, as_of)]
    matched = [row for row, _ in eligible if _normalize(text) and _normalize(text) in _normalize(row.get('excerpt') or (row.get('text') if row.get('quote_id') else ''))]
    return {'text': text, 'kind': 'direct_quote',
            'evidence_ids': sorted({_identity(row) for row, _ in candidates if _identity(row)}) or list(markers),
            'status': 'matched_excerpt' if matched else 'text_mismatch' if eligible else 'quotation_not_eligible',
            'notice': '逐字核对仅覆盖已核连续片段，不证明转述、整篇或思想结论。'}


def retains_rejected_quote(content, rejected):
    """Catch a known rejected fragment simply republished without quote marks.

    This is not an entailment checker. A local, explicit source-gap statement
    may mention the rejected text; silently promoting it to prose may not.
    """
    text = _normalize(content)
    gap = re.compile(r'未见.{0,12}(?:出处|来源|对应|依据)|没有.{0,12}(?:可靠出处|可靠来源|逐字依据)|查不到|来源不明|无法核实|不能认定|不能证明|不应认定|不是原话|并非原话|不可作为|未核|待核|不支持')
    for fragment in rejected[:40]:
        needle = _normalize(fragment).strip('。！？!?;；，,')
        if not needle:
            continue
        start = text.find(needle)
        while start != -1:
            before = re.split(r'[。！？]', text[max(0, start - 100):start])[-1]
            after = re.split(r'[。！？]', text[start + len(needle):start + len(needle) + 100])[0]
            if not gap.search(before + after):
                return True
            start = text.find(needle, start + len(needle))
    return False


def check_answer_claims(content, tools, mode='consult', as_of=None, user_texts=()):
    """Check explicit source use; reject an unsupported attributed quotation.

    Uncited ordinary prose is not silently classified as verified history.
    The coverage string is part of the public contract and must be preserved.
    """
    content = str(content or '')
    if isinstance(user_texts, str):
        user_texts = (user_texts,)
    user_texts = [value for value in user_texts if isinstance(value, str)][-100:] if isinstance(user_texts, (list, tuple)) else []
    rows = list(_rows(tools))
    lookup = {}
    for row, untrusted in rows:
        for alias in _aliases(row):
            lookup.setdefault(alias, []).append((row, untrusted))
    claims, quote_checks = [], []
    for index, match in enumerate(re.finditer(MARKER, content)):
        if index >= 40:
            break
        marker = match.group(1)
        before = content[:match.start()]
        start = before.rfind('\n') + 1
        text = content[start:match.start()].strip()[-600:]
        sources = lookup.get(marker, [])
        identities = sorted({_identity(row) for row, _ in sources if _identity(row)})
        claims.append({'text': text or '见正文引用处', 'kind': 'modern_inference' if re.search(r'现代|类比|建议|可以尝试|本周', text) else 'cited_statement',
                       'evidence_ids': identities or [marker], 'status': 'source_found_needs_review' if sources else 'source_missing',
                       'source_fingerprints': {_identity(row): _fingerprint(row) for row, _ in sources if _identity(row)},
                       'notice': '只核对来源存在；转述支持范围、现实前提与类比仍需审阅。'})
    for match in QUOTED.finditer(content):
        quote = next(value for value in match.groups() if value is not None)
        prefix = content[max(0, match.start() - 180):match.start()]
        prefix = re.split(r'[。！？\n]', prefix.rstrip())[-1]
        suffix = content[match.end():match.end() + 180]
        suffix_clause = re.split(r'[。！？\n]', suffix)[0]
        surrounding = prefix + suffix_clause
        if re.search(r'(?:而不是|不能写成|不应写成|不要写成|不能称作|不宜写成)\s*$', prefix):
            # A local negated wording example does not assert that the quoted
            # words were spoken. It does not exempt other quotations nearby.
            continue
        author_suffix = re.match(r'\s*(?:这(?:句|段|是).{0,8})?(?:是|为)?毛泽东.{0,6}原话', suffix)
        attributed = bool(AUTHOR.search(prefix) or author_suffix)
        historical_author = bool(HISTORICAL_AUTHOR.search(prefix) or author_suffix)
        markers = re.findall(MARKER, suffix_clause)[:4]
        if not markers:
            markers = re.findall(MARKER, prefix)[-2:]
        adjacent = bool(re.match(r'\s*[，,。；;：:]*\s*' + MARKER, suffix))
        if not historical_author and not adjacent and any(quote in user_text for user_text in user_texts):
            # "原话：" can introduce the user's own goals or constraints.
            # Only actual user-role text is eligible for this exemption;
            # repeating a forged Mao quote in a question never grants it
            # historical authorship through explicit attribution or a source
            # marker immediately attached to the quoted wording.
            continue
        if DISAVOW.search(surrounding) or (MODERN.search(prefix) and not attributed):
            continue
        cited_rows = [row for marker in markers for row, _ in lookup.get(marker, [])]
        interpretive_only = bool(cited_rows) and all(
            row.get('record_type') in {'principle_note', 'method_note', 'historical_case'}
            or row.get('verification_status') in {'principle_note', 'method_note', 'limited_facts'}
            for row in cited_rows)
        if not attributed and interpretive_only:
            # Quoting a modern question, label, or researcher interpretation
            # does not assert the historical author's exact wording. Keep its
            # explicit citation in the review ledger, without granting it
            # author quotation rights. Explicit attribution still fails below.
            continue
        term_suffix = re.sub(r'^(?:\s*' + MARKER + r')+', '', suffix_clause).strip()
        quoted_term = len(_normalize(quote)) <= 6 and bool(re.match(r'(?:这个)?(?:概念|一词|这一词|这个词|这一说法|这个说法)|(?:是|作为).{0,8}(?:概念|关键词|方法概括)', term_suffix))
        if not attributed and (quoted_term or not adjacent):
            continue
        if len(quote_checks) >= 40:
            # Bound the published ledger, never the truth condition. User
            # wording and modern labels above do not consume this budget.
            # A further historical quotation cannot silently escape checking.
            quote_checks[-1] = {'text': '', 'kind': 'direct_quote', 'evidence_ids': [],
                                'status': 'check_limit_reached',
                                'notice': '历史直接引语超过40条核对上限，未放行此答复，请减少引语后重试。'}
            break
        if len(quote) > 800:
            quote_checks.append({'text': quote[:800], 'kind': 'direct_quote', 'evidence_ids': markers,
                                 'status': 'quote_too_long', 'notice': '直接引语超过逐段核对上限，请缩短并提供连续片段。'})
        else:
            quote_checks.append(check_direct_quote(quote, markers, tools, mode, as_of))
    return {'passed': all(item['status'] == 'matched_excerpt' for item in quote_checks),
            'claims': claims, 'quote_checks': quote_checks,
            'coverage': '最多列出40处显式引用；可识别历史直接引语超过40条时拒绝放行。未引注主张及转述蕴含未自动验证。',
            'scope': '来源存在、逐字片段及历史日期资格是确定性检查；现代前提与类比仍待审阅。'}
