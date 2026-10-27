"""Bounded, user-correctable provenance for an issue's understanding.

An exact excerpt establishes where a summary came from, not that the summary
is a verbatim user statement or independently verified fact. Only a user
operation can confirm, pause or directly correct an entry.
"""
from __future__ import annotations

from copy import deepcopy
import re
import uuid


MAX_STATEMENTS = 80
MAX_CHANGES = 12
SCALAR_FIELDS = ('long_term_direction', 'stage_goal', 'turn_request')
LIST_FIELDS = ('known_facts', 'constraints', 'uncertainties')
PATH = re.compile(r'^(?:long_term_direction|stage_goal|turn_request|(?:known_facts|constraints|uncertainties):[0-5])$')


def base_field(field):
    return str(field).split(':', 1)[0]


def public_statements(value):
    result = []
    for item in value[:MAX_STATEMENTS] if isinstance(value, list) else []:
        if not isinstance(item, dict) or not PATH.fullmatch(str(item.get('field', ''))):
            continue
        if item.get('status') not in {'active', 'inactive', 'superseded'}:
            continue
        identity, text = item.get('statement_id'), item.get('value')
        if not isinstance(identity, str) or not re.fullmatch(r'[a-f0-9]{32}', identity) or not isinstance(text, str) or not text.strip():
            continue
        row = {key: str(item.get(key, ''))[:limit] for key, limit in (
            ('statement_id', 32), ('field', 64), ('value', 240),
            ('source_turn_id', 64), ('source_excerpt', 400), ('recorded_at', 80),
            ('supersedes', 32), ('updated_at', 80))}
        row.update(status=item['status'],
                   confirmation='confirmed' if item.get('confirmation') == 'confirmed' else 'tentative',
                   origin=item.get('origin') if item.get('origin') in {'user_excerpt', 'user_correction'} else 'model_summary')
        if row['origin'] == 'model_summary':
            row['source_excerpt'] = ''
        result.append(row)
    return result


def public_changes(value, kind):
    fields = ({'change_id': 32, 'previous': 240, 'current': 240, 'reason': 240,
               'source_turn_id': 64, 'source_excerpt': 400, 'recorded_at': 80,
               'confirmation': 32} if kind == 'goal' else
              {'review_id': 32, 'kind': 32, 'source_turn_id': 64, 'recorded_at': 80,
               'previous_summary': 600, 'current_summary': 600, 'feedback': 2000,
               'action_id': 64, 'action_status': 32, 'notice': 240})
    result = []
    for item in value[-MAX_CHANGES:] if isinstance(value, list) else []:
        if isinstance(item, dict):
            row = {key: item[key][:limit] for key, limit in fields.items() if isinstance(item.get(key), str)}
            if kind == 'review':
                for key in ('previous_unknowns', 'current_unknowns'):
                    row[key] = [x[:240] for x in item.get(key, [])[:6] if isinstance(x, str)] if isinstance(item.get(key), list) else []
            result.append(row)
    return result


def statement_sources(value, question=None):
    result = []
    for item in value[:18] if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        field, quote = item.get('field'), item.get('quote')
        if not isinstance(field, str) or not PATH.fullmatch(field) or not isinstance(quote, str):
            continue
        quote = quote.strip()
        if not quote or len(quote) > 400 or (question is not None and quote not in question):
            continue
        # An uncertainty stays an assistant interpretation even if it happens
        # to borrow some words from the current user message.
        if base_field(field) == 'uncertainties':
            continue
        result.append({'field': field, 'quote': quote})
    return result


def _values(analysis):
    for field in SCALAR_FIELDS:
        value = analysis.get(field, '')
        if isinstance(value, str) and value.strip():
            yield field, value.strip()[:240]
    for field in LIST_FIELDS:
        values = analysis.get(field, [])
        for index, value in enumerate(values[:6] if isinstance(values, list) else []):
            if isinstance(value, str) and value.strip():
                yield '%s:%d' % (field, index), value.strip()[:240]


def effective_analysis(meta):
    """Apply explicit user controls without rewriting the provenance ledger."""
    raw = meta.get('analysis') if isinstance(meta, dict) else None
    if not isinstance(raw, dict):
        return None
    analysis = deepcopy(raw)
    records = public_statements(meta.get('statements'))
    for field in SCALAR_FIELDS:
        entries = [x for x in records if x['field'] == field]
        active = [x for x in entries if x['status'] == 'active']
        controlled = [x for x in active if x['origin'] == 'user_correction' or x['confirmation'] == 'confirmed']
        if controlled:
            analysis[field] = controlled[-1]['value']
        elif entries and not active:
            analysis[field] = ''
        elif any(x['status'] != 'active' and x['value'] == analysis.get(field) for x in entries):
            analysis[field] = active[-1]['value'] if active else ''
    for field in LIST_FIELDS:
        entries = [x for x in records if base_field(x['field']) == field]
        excluded = {x['value'] for x in entries if x['status'] in {'inactive', 'superseded'}}
        controlled = field in {'known_facts', 'constraints'} and bool(excluded)
        active_values = {x['value'] for x in entries if x['status'] == 'active'}
        values = [x for x in analysis.get(field, []) if isinstance(x, str) and x not in excluded
                  and (not controlled or x in active_values)] if isinstance(analysis.get(field), list) else []
        for row in entries:
            if row['status'] == 'active' and row['origin'] == 'user_correction' and row['value'] not in values:
                values.append(row['value'])
        analysis[field] = values[:6]
    analysis['goal'] = analysis.get('stage_goal', analysis.get('goal', ''))
    # A whole-summary sentence may still contain a paused fact. It is safer to
    # rebuild this display-only synopsis from currently usable fields than to
    # let the previous summary silently revive it.
    if any(x['status'] != 'active' or x['origin'] == 'user_correction' for x in records):
        parts = [analysis.get('stage_goal', ''), *analysis.get('known_facts', []), *analysis.get('constraints', [])]
        analysis['summary'] = '；'.join(dict.fromkeys(x for x in parts if isinstance(x, str) and x))[:600]
    return analysis


def prompt_statement_context(meta):
    """A compact override for legacy transcripts and explicitly chosen memory."""
    records = public_statements(meta.get('statements'))
    analysis = effective_analysis(meta) or {}
    usable = {(base_field(field), value) for field, value in _values(analysis)}
    return {
        'notice': '以下记录可纠正。原话摘录只是整理来源，不自动证明摘要准确。用户停用或更正的内容优先于旧对话与助手摘要，不能从历史重建为当前事实；旧确认不证明条件永远不变，当前用户明确修正优先。',
        'active': [{key: row[key] for key in ('statement_id', 'field', 'value', 'origin', 'confirmation', 'source_turn_id', 'source_excerpt')}
                   for row in records if row['status'] == 'active' and (base_field(row['field']), row['value']) in usable][-24:],
        'excluded': [{'field': row['field'], 'value': row['value'], 'status': row['status']}
                     for row in records if row['status'] != 'active'][-MAX_STATEMENTS:],
    }


def _room(records, protected=None):
    """Preserve explicit controls; prune old automatic history before new notes."""
    if len(records) < MAX_STATEMENTS:
        return True
    index = next((i for i, row in enumerate(records) if row['statement_id'] != protected and row['confirmation'] != 'confirmed' and row['origin'] != 'user_correction' and row['status'] != 'inactive'), None)
    if index is None:
        return False
    records.pop(index)
    return True


def add_turn(meta, analysis, question, turn, timestamp):
    """Create tentative records and bounded goal/review change views."""
    records = public_statements(meta.get('statements'))
    previous = effective_analysis(meta) or {}
    analysis = deepcopy(analysis or {})
    if not analysis.get('stage_goal') and analysis.get('goal'):
        analysis['stage_goal'] = analysis['goal']
    verified_sources = statement_sources(analysis.get('statement_sources'), question)
    sources = {row['field']: row['quote'] for row in verified_sources}
    if 'statement_sources' in analysis:
        analysis['statement_sources'] = verified_sources
    for field in SCALAR_FIELDS:
        if field in analysis and not analysis[field] and sources.get(field):
            # A current, attributable withdrawal can leave the goal unknown.
            # Do not invent a replacement goal just to keep this field full.
            for old in records:
                if old['field'] == field and old['status'] == 'active':
                    old['status'] = 'superseded'
    for field, value in list(_values(analysis)):
        group = base_field(field)
        entries = [x for x in records if base_field(x['field']) == group]
        match = next((x for x in reversed(entries) if x['status'] == 'active' and x['value'] == value), None)
        if match:
            match['field'] = field
            if match['origin'] == 'model_summary' and sources.get(field):
                match.update(origin='user_excerpt', source_excerpt=sources[field],
                             source_turn_id=turn, updated_at=timestamp)
            continue
        # Paused values need an explicit resume; a model paraphrase or a
        # reappearance in an old transcript is never such an operation.
        if any(x['status'] == 'inactive' and (x['value'] == value or (group in SCALAR_FIELDS and not sources.get(field))) for x in entries):
            continue
        if (group in {'known_facts', 'constraints'} and not sources.get(field)
                and any(x['status'] in {'inactive', 'superseded'} for x in entries)):
            # Wording differences cannot turn a paused fact into a new fact.
            # Without a new attributable user excerpt the whole list group
            # stays conservative, rather than guessing semantic equivalence.
            continue
        if group in SCALAR_FIELDS and not sources.get(field) and any(x['status'] == 'superseded' and x['value'] == value for x in entries):
            continue
        current = next((x for x in reversed(entries) if x['status'] == 'active'), None)
        if group in SCALAR_FIELDS and current and (current['confirmation'] == 'confirmed' or current['origin'] == 'user_correction') and not sources.get(field):
            continue
        if not _room(records):
            if sources.get(field):
                raise ValueError('理解记录已达上限，不能覆盖已确认内容，请新建议题后继续整理')
            continue
        supersedes = ''
        if group in SCALAR_FIELDS:
            for old in entries:
                if old['status'] == 'active':
                    old['status'] = 'superseded'
                    supersedes = old['statement_id']
        records.append({'statement_id': uuid.uuid4().hex, 'field': field, 'value': value,
                        'source_turn_id': turn, 'source_excerpt': sources.get(field, ''),
                        'recorded_at': timestamp, 'updated_at': timestamp,
                        'confirmation': 'tentative', 'status': 'active',
                        'origin': 'user_excerpt' if sources.get(field) else 'model_summary',
                        'supersedes': supersedes})
    meta['statements'] = records
    meta['analysis'] = analysis
    meta['analysis'] = effective_analysis(meta)
    current = meta['analysis'] or {}
    if current.get('stage_goal', '') != previous.get('stage_goal', ''):
        goals = public_changes(meta.get('goal_history'), 'goal')
        source = next((x for x in reversed(records) if x['field'] == 'stage_goal' and x['status'] == 'active'), {})
        goals.append({'change_id': uuid.uuid4().hex, 'previous': previous.get('stage_goal', ''),
                      'current': current.get('stage_goal', ''), 'source_turn_id': turn,
                      'source_excerpt': sources.get('stage_goal') or source.get('source_excerpt', ''), 'recorded_at': timestamp,
                      'reason': '依据本轮对话更新的暂定理解', 'confirmation': 'tentative'})
        meta['goal_history'] = goals[-MAX_CHANGES:]
    if analysis.get('phase') == 'review':
        reviews = public_changes(meta.get('review_history'), 'review')
        reviews.append({'review_id': uuid.uuid4().hex, 'kind': 'reflection', 'source_turn_id': turn,
                        'recorded_at': timestamp, 'previous_summary': previous.get('summary', '')[:600],
                        'current_summary': current.get('summary', '')[:600], 'feedback': question[:2000],
                        'previous_unknowns': previous.get('uncertainties', [])[:6],
                        'current_unknowns': current.get('uncertainties', [])[:6],
                        'notice': '这是本轮理解变化与用户表达，不证明建议已被接受或执行。'})
        meta['review_history'] = reviews[-MAX_CHANGES:]
    return meta['analysis']


def update_statement(meta, identity, status=None, corrected_value=None, *, timestamp):
    """Apply an authenticated, explicit UI operation; never called by a model."""
    records = public_statements(meta.get('statements'))
    row = next((x for x in records if x['statement_id'] == identity), None)
    if row is None:
        raise ValueError('理解记录不存在')
    if row['status'] == 'superseded':
        raise ValueError('这条记录已被取代，请操作最新版本')
    previous_goal = (effective_analysis(meta) or {}).get('stage_goal', '')
    if corrected_value is not None:
        if status is not None or not isinstance(corrected_value, str) or not corrected_value.strip() or len(corrected_value) > 240:
            raise ValueError('更正内容需为1至240字，不能同时设置状态')
        if not _room(records, identity):
            raise ValueError('理解记录已达上限，请新建议题后继续整理')
        row['status'] = 'superseded'
        turn = uuid.uuid4().hex
        new = {**row, 'statement_id': uuid.uuid4().hex, 'value': corrected_value.strip(),
               'status': 'active', 'confirmation': 'confirmed', 'origin': 'user_correction',
               'source_turn_id': turn, 'source_excerpt': corrected_value.strip(),
               'recorded_at': timestamp, 'updated_at': timestamp, 'supersedes': row['statement_id']}
        records.append(new)
    else:
        if status not in {'confirmed', 'inactive', 'active'}:
            raise ValueError('理解状态无效')
        if status == 'confirmed':
            if row['status'] != 'active':
                raise ValueError('请先恢复这条记录，再确认')
            row['confirmation'] = 'confirmed'
        else:
            if status == 'active':
                for old in records:
                    if old is not row and old['field'] == row['field'] and old['status'] == 'active' and base_field(row['field']) in SCALAR_FIELDS:
                        old['status'] = 'superseded'
                restored = deepcopy(meta.get('analysis') or {})
                group = base_field(row['field'])
                if group in SCALAR_FIELDS:
                    restored[group] = row['value']
                else:
                    values = restored.get(group, [])
                    restored[group] = list(dict.fromkeys([*values, row['value']]))[:6] if isinstance(values, list) else [row['value']]
                meta['analysis'] = restored
            row['status'] = status
        row['updated_at'] = timestamp
        turn = row['source_turn_id']
    meta['statements'] = records
    meta['analysis'] = effective_analysis(meta)
    current_goal = (meta['analysis'] or {}).get('stage_goal', '')
    if previous_goal != current_goal:
        history = public_changes(meta.get('goal_history'), 'goal')
        history.append({'change_id': uuid.uuid4().hex, 'previous': previous_goal,
                        'current': current_goal, 'source_turn_id': turn,
                        'source_excerpt': corrected_value.strip() if corrected_value is not None else '',
                        'recorded_at': timestamp, 'reason': '用户在理解笔记中更正' if corrected_value is not None else '用户%s了理解记录' % ('停用' if status == 'inactive' else '恢复'),
                        'confirmation': 'confirmed'})
        meta['goal_history'] = history[-MAX_CHANGES:]
    return row
