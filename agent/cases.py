"""Structured, limited historical references from the reviewed timeline.

The source library verifies ten narrowly scoped records, not ten complete
causal case studies. Missing conditions and actual effects remain unknown.
"""
from __future__ import annotations

import hashlib
import json


CASE_DEFINITIONS = {
    'TL01': ('调查实际情况', '1927年湖南农民运动的调查与革命政治语境。',
             '了解相关人的具体处境和实际经验后再作判断。',
             ['不把农运对象直接等同现代客户、家庭或企业', '不凭调查发生证明报告所有判断或后续效果']),
    'TL02': ('分清行动事实与成功归因', '1935年长征途中到达陕北的军事政治语境。',
             '核对行动发生的时间和位置；当前资料不足以据此归因现代选择的成败。',
             ['不把行程事实写成意志决定成功的因果证据', '不拼入1936年会师或未核战事']),
    'TL03': ('判断受时间与条件约束', '1938年抗日战争期间的公开讲演活动。',
             '区分当时的条件性分析和后来才知道的结果。',
             ['不保证未来胜利或精确日期', '不把后编措辞全部视为讲演当时的逐字记录']),
    'TL04': ('区分起草与集体会议事实', '1943年中共中央政治局会议与党的领导方法。',
             '共同决定时区分谁起草、谁讨论、哪些内容已获事实支持。',
             ['不凭起草与通过推测现场对白或一致意见', '不把党的组织制度直接搬到现代团队']),
    'TL05': ('文本类型与阶段条件', '1945年中共七大书面政治报告的提交。',
             '判断依据先区分书面文本、口头表达和后续实践。',
             ['不把提交报告等同于全文现场逐字讲授', '不以纲领文本证明政策已实施或有效']),
    'TL06': ('区分阶段事件与后续结果', '1949年西柏坡七届二中全会报告。',
             '核对当前已发生的步骤，不把后续决议或结果提前填入。',
             ['不把完整会期的结束信息倒填到报告当天', '不补会议现场反应、私人心理或政策效果']),
    'TL07': ('区分开幕表达与最终决定', '1949年政协第一届全体会议公开开幕活动。',
             '区分表达、讨论与最终决定，保留不同参与者的责任。',
             ['不将后来选举与最终决定写成开幕当日既成事实', '不把同页其他人的讲话归为毛泽东原话']),
    'TL08': ('讲话经历与版本措辞分开', '1956年社会主义建设背景下的政治局扩大会议讲话。',
             '核对经验发生时间与现存文本资格，借鉴前比较成立条件。',
             ['不把后期出版措辞全部认定当日逐字出现', '不把国家治理关系直接视为家庭或企业管理规则']),
    'TL09': ('原讲话与后改文本的张力', '1957年最高国务会议扩大会议的原讲话。',
             '遇到修订或意见变化，分别核对当时表达与后来整理。',
             ['不把6月整理发表版全部倒填为2月现场原话', '不以文本宗旨证明实际政治后果']),
    'TL10': ('修订发表的版本线索', '1957年讲话整理、修改与正式发表的研究者版本记录。',
             '版本变化可作为核对线索，不建立人物的当日亲历或获知记忆。',
             ['不作为人物在2月或3月已知的事实', '不因正式发表就认定本人当日知悉或在场']),
}


def _json(value):
    try:
        result = json.loads(value or '{}')
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def build_case(event, source):
    """Return a limited reference only while event and source remain reviewed.

    TL10 intentionally remains unavailable through this route: its source is
    a version record without a verified public participation/knowledge date.
    """
    if not isinstance(event, dict) or not isinstance(source, dict):
        return None
    identity = source.get('evidence_id')
    definition = CASE_DEFINITIONS.get(identity)
    raw = _json(event.get('metadata_json'))
    source_raw = _json(source.get('raw_json'))
    if (definition is None or source.get('reviewed') not in (True, 1)
            or source.get('source_role') != 'biographical_record'
            or event.get('evidence_id') != identity
            or event.get('verification_status') != 'reviewed'
            or raw.get('public_event_verified') is not True
            or source_raw.get('public_event_verified') is not True):
        return None
    # A changed imported event cannot silently retain the old reviewed fact.
    fact = str(source.get('summary') or '').strip()
    if not fact or str(event.get('description') or '').strip() != fact:
        return None
    label, conditions, analogy, non_analogies = definition
    source_material = {k: source.get(k) for k in ('evidence_id', 'summary', 'reviewed', 'raw_json')}
    fingerprint = hashlib.sha256(json.dumps(source_material, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {
        'case_id': 'case:' + identity, 'event_id': event.get('event_id'),
        'record_type': 'historical_case', 'title': event.get('title'), 'label': label,
        'facts': [fact], 'supporting_evidence_ids': [identity],
        'start_date': event.get('start_date'), 'end_date': event.get('end_date'),
        'historical_conditions': conditions,
        'problem_context': '当前核验范围仅支持所列事件事实，未独立核验完整问题成因。',
        'resource_conditions': [], 'outcome_evidence': [],
        'unknowns': ['具体资源、讨论过程和未列细节没有独立核验', '该事件的实际效果与成功、失败归因没有独立结果证据'],
        'modern_analogy': analogy, 'non_analogies': list(non_analogies),
        'source_urls': source_raw.get('source_urls', []),
        'source_id': source.get('source_id'),
        'source_fingerprint': fingerprint,
        'verification_status': 'limited_facts', 'completion': 'limited_facts',
        'reviewed': True, 'quote_eligible': False, 'quote_verified': False,
        'notice': '有限事实参照，不是完整成败案例；现代类比为研究者解释，不授予原话或因果资格。',
    }
