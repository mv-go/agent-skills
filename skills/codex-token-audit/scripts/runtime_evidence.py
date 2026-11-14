"""Privacy-bounded runtime evidence and deterministic summaries."""
import math
import re
from collections import defaultdict
from decimal import Decimal


def identifier(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', value))


def finite(value, *, minimum=None, maximum=None):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and (minimum is None or value >= minimum) and (maximum is None or value <= maximum)
    except OverflowError:
        return False


def pick(obj, snake, camel):
    return obj.get(snake, obj.get(camel)) if isinstance(obj, dict) else None


def quota(raw):
    if not isinstance(raw, dict):
        return None
    out = {}
    for snake, camel in (('limit_id', 'limitId'), ('plan_type', 'planType')):
        value = pick(raw, snake, camel)
        if identifier(value):
            out[snake] = value
    for name in ('primary', 'secondary'):
        window = raw.get(name)
        if not isinstance(window, dict):
            continue
        safe = {}
        for snake, camel, lo, hi in (('used_percent','usedPercent',0,None), ('window_minutes','windowMinutes',0,None), ('resets_at','resetsAt',0,None)):
            value = (window.get(snake, window.get('windowDurationMins', window.get(camel)))
                     if snake == 'window_minutes' else pick(window, snake, camel))
            if finite(value, minimum=lo, maximum=hi) and (snake != 'window_minutes' or value > 0):
                safe[snake] = value
        out[name] = safe
    credits = raw.get('credits')
    if isinstance(credits, dict):
        safe = {}
        for snake, camel in (('has_credits','hasCredits'), ('unlimited','unlimited')):
            value = pick(credits, snake, camel)
            if isinstance(value, bool):
                safe[snake] = value
        value = credits.get('balance')
        if finite(value, minimum=0) or (isinstance(value, str) and re.fullmatch(r'[0-9]{1,30}(?:\.[0-9]{1,30})?', value)):
            safe['balance'] = value
        out['credits'] = safe
    return out


def tier(raw):
    value = pick(raw, 'service_tier', 'serviceTier')
    return value if identifier(value) else None


def pointer(record):
    return {'source': record['source'], 'event_id': record.get('event_id')}


def _request_pointer(record):
    return {'input_tokens': record['input_tokens'], 'response_id': record.get('response_id'), 'source': record['source']}


def summarize(requests, events, scope, task_label):
    comparisons = []
    groups = defaultdict(list)
    for e in events:
        q = e.get('quota')
        if not isinstance(q, dict):
            continue
        for kind in ('primary', 'secondary'):
            if kind in q:
                for turn in e.get('root_turn_ids', [e.get('root_turn_id')]):
                    groups[(turn, e['session_id'], e['source']['file'], q.get('limit_id'), kind)].append(e)
    for (turn, session, source, bucket, kind), group in sorted(groups.items(), key=lambda x: str(x[0])):
        group.sort(key=lambda e: e['source']['line'])
        first, last = group[0], group[-1]
        a, b = first['quota'], last['quota']
        aw, bw = a[kind], b[kind]
        comparable = (len(group) >= 2 and bucket is not None and a.get('plan_type') is not None
                      and a.get('plan_type') == b.get('plan_type')
                      and all(aw.get(k) is not None and aw.get(k) == bw.get(k) for k in ('window_minutes', 'resets_at'))
                      and all(finite(x.get('timestamp')) for x in group)
                      and all(x['timestamp'] < y['timestamp'] for x, y in zip(group, group[1:]))
                      and all(not e.get('source_copies_ambiguous') for e in group)
                      and all(e['quota'].get('plan_type') == a.get('plan_type') and all(e['quota'][kind].get(k) == aw.get(k) for k in ('window_minutes','resets_at')) for e in group)
                      and finite(aw.get('used_percent')) and finite(bw.get('used_percent')))
        difference = Decimal(str(bw['used_percent'])) - Decimal(str(aw['used_percent'])) if comparable else None
        delta = (int(difference) if difference == difference.to_integral_value() else float(difference)) if difference is not None else None
        comparisons.append({'root_turn_id':turn, 'session_id':session, 'source_file':source, 'bucket_id':bucket, 'window':kind,
                            'first':pointer(first), 'last':pointer(last), 'observation_count':len(group),
                            'delta_percentage_points':delta, 'decrease_or_reset':delta < 0 if delta is not None else None,
                            'interpretation':'account-wide observation; account identity unverified; task attribution unavailable; span may be partial'})
    calls = ('tool_call','tool_use','function_call','custom_tool_call')
    results = ('function_call_output','tool_result','custom_tool_call_output')
    tool_events = [e for e in events if e.get('event_type') in calls + results]
    grouped = defaultdict(list)
    for e in tool_events:
        key = (e['session_id'], e.get('turn_id'), e.get('root_turn_id'), e['source']['file'], e.get('call_id'))
        grouped[key].append(e)
    pairs = []
    paired = 0
    for key, group in grouped.items():
        c = [e for e in group if e['event_type'] in calls]
        r = [e for e in group if e['event_type'] in results]
        if key[1] is None or key[4] is None or len(c) != 1 or len(r) != 1 or any(e.get('source_copies_ambiguous') for e in group):
            continue
        call, result = c[0], r[0]
        if (call['source']['line'] >= result['source']['line'] or not finite(call.get('timestamp'))
            or not finite(result.get('timestamp')) or call['timestamp'] >= result['timestamp']):
            continue
        pairs.append({'call':pointer(call), 'result':pointer(result), 'name':call.get('name'), 'interval_seconds':result['timestamp'] - call['timestamp']})
        paired += 2
    tool_results = [e for e in tool_events if e['event_type'] in results]
    tools = {'call_count':sum(e['event_type'] in calls for e in tool_events), 'result_count':len(tool_results),
             'known_result_chars':sum(e.get('result_chars',0) for e in tool_results if not e.get('opaque')),
             'known_result_bytes':sum(e.get('result_bytes',0) for e in tool_results if not e.get('opaque')),
             'unknown_result_chars_count':sum('result_chars' not in e or bool(e.get('opaque')) for e in tool_results),
             'unknown_result_bytes_count':sum('result_bytes' not in e or bool(e.get('opaque')) for e in tool_results),
             'unknown_pair_count':len(tool_events) - paired, 'pairs':pairs,
             'interval_note':'observed call-to-result interval; not model-billed time; wrappers have unknown internal operations'}
    inputs = []
    for window in scope.get('windows', [scope]):
        turn = window['turn_id']
        rows = [r for r in requests if r['session_id'] == scope['session_id'] and turn in r.get('root_turn_ids',[r.get('root_turn_id')])]
        sources = {r['source']['file'] for r in rows}
        ordered = len(sources) == 1 and not any(e.get('source_copies_ambiguous') for e in events if e['session_id'] == scope['session_id'])
        rows.sort(key=lambda r:r['source']['line'])
        inputs.append({'root_turn_id':turn, 'first':_request_pointer(rows[0]) if rows and ordered else None,
                       'last':_request_pointer(rows[-1]) if rows and ordered else None,
                       'max':_request_pointer(max(rows,key=lambda r:r['input_tokens'])) if rows else None,
                       'note':'observed request input; not a separable instruction or plugin baseline'})
    return {'version':1, 'task_label':task_label, 'quota_comparisons':comparisons, 'tools':tools, 'root_input_by_turn':inputs}


def validate_new_fields(requests, events, task_label):
    if task_label is not None and (not isinstance(task_label,str) or not 1 <= len(task_label) <= 120 or not task_label.isprintable() or not task_label.strip()):
        raise ValueError('invalid task label')
    for e in events:
        if 'quota' in e and (not isinstance(e['quota'],dict) or quota(e['quota']) != e['quota'] or e.get('event_type') != 'token_count'):
            raise ValueError('invalid quota evidence')
        for k in ('result_truncated','source_line_incomplete','source_copies_ambiguous'):
            if k in e and not isinstance(e[k],bool):
                raise ValueError('invalid runtime flag')
        if 'result_truncated' in e and e.get('event_type') not in ('function_call_output', 'tool_result', 'custom_tool_call_output'):
            raise ValueError('result truncation on non-result event')
        if 'result_truncated' in e and not e.get('source_line_incomplete', False) and e.get('truncated') != e['result_truncated']:
            raise ValueError('inconsistent truncation flags')
        for key in ('result_chars', 'result_bytes'):
            if key in e and (not isinstance(e[key], int) or isinstance(e[key], bool) or e[key] < 0):
                raise ValueError('invalid result size')
        if 'configured_service_tier' in e and (not identifier(e['configured_service_tier']) or e['event_type'] not in ('turn_context','thread_settings_applied')):
            raise ValueError('invalid configured tier evidence')
    for r in requests:
        candidates = [e for e in events if e.get('configured_service_tier') is not None
                      and e['session_id'] == r['session_id'] and e['source']['file'] == r['source']['file']
                      and e['source']['line'] < r['source']['line']
                      and r.get('root_turn_id') in e.get('root_turn_ids', [e.get('root_turn_id')])]
        latest = max(candidates, key=lambda e: e['source']['line']) if candidates else None
        expected = ({'value': latest['configured_service_tier'], 'source': latest['source'],
                     'provenance': latest['event_type']} if latest else None)
        if r.get('configured_service_tier') != expected:
            raise ValueError('configured tier does not match preceding setting')
        for key in ('configured_service_tier','observed_service_tier'):
            value = r.get(key)
            if value is None:
                continue
            if not isinstance(value,dict) or set(value) != {'value','source','provenance'} or not identifier(value['value']):
                raise ValueError('invalid service tier evidence')
            if key == 'observed_service_tier':
                if value['source'] != r['source'] or value['provenance'] != 'response usage metadata':
                    raise ValueError('invalid response tier provenance')
            elif not any(e.get('configured_service_tier') == value['value'] and e['source'] == value['source'] and e['session_id'] == r['session_id'] and e['source']['file'] == r['source']['file'] and e['source']['line'] < r['source']['line'] and value['provenance'] == e['event_type'] for e in events):
                raise ValueError('invalid configured tier provenance')
