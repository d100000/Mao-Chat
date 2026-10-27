"""Request-scoped billing gates shared by every normal model/tool stage.

The context contains server-owned IDs only. It never accepts client usage or
stores prompts. CLI use outside an HTTP billing scope remains unchanged.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
import json
import uuid

_SCOPE = ContextVar('jiaoyuan_billing', default=None)


@contextmanager
def billing_scope(store, request_id, *, assert_active=None, search_bound=None):
    state = {'store': store, 'request_id': request_id, 'active': assert_active,
             'call_id': None, 'search_bound': search_bound}
    token = _SCOPE.set(state)
    try:
        yield state
    finally:
        _SCOPE.reset(token)


def input_bound(payload):
    # UTF-8 bytes are a conservative tokenizer-independent content bound for
    # the supported byte-token adapters. Include JSON/tool framing and a fixed
    # envelope allowance. A larger actual payload is rejected before dispatch.
    fields = {k: payload[k] for k in ('messages', 'tools', 'response_format') if k in payload}
    return len(json.dumps(fields, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) + 2048


def before_model_request(payload, config):
    state = _SCOPE.get()
    if state is None:
        return
    if state['active']:
        state['active']()
    identity = uuid.uuid4().hex
    state['store'].before_call(state['request_id'], identity, input_bound(payload),
                               payload['max_tokens'], model=config.model, gateway=config.base_url)
    state['call_id'] = identity


def record_billed_call(usage, status='ok', provider_request_id=None):
    state = _SCOPE.get()
    if state is None or state['call_id'] is None:
        return
    identity, state['call_id'] = state['call_id'], None
    state['store'].record_call(state['request_id'], identity, usage,
                               provider_request_id=provider_request_id, status=status)


@contextmanager
def billed_search():
    state = _SCOPE.get()
    if state is None:
        yield
        return
    if state['active']:
        state['active']()
    if state['search_bound'] is None or Decimal(state['search_bound']) <= 0:
        raise ValueError('搜索费用上界尚未配置，本轮未发起外部搜索')
    identity = uuid.uuid4().hex
    state['store'].before_service_call(state['request_id'], identity, state['search_bound'])
    try:
        yield
    finally:
        # Firecrawl credits do not establish a CNY price. Keep the conservative
        # cash commitment until an operator reconciles an actual invoice.
        state['store'].record_service_call(state['request_id'], identity, None)
