"""Versioned private telemetry bundles; no network or semantic classification."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

import runtime_evidence

SCHEMA_VERSION = 2
COLLECTOR_VERSION = '2.1.0'
RAW_FIELDS = ('new_tokens', 'cached_input_tokens', 'input_tokens', 'output_tokens', 'total_tokens')
EQUIV_FIELDS = ('new_tokens', 'cached_tokens', 'input_tokens', 'output_tokens', 'total_tokens')
REQUIRED_FILES = {'audit.json', 'requests.jsonl', 'events.jsonl', 'schema.json', 'pricing.json'}


class BundleError(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n'


def _decimal(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise BundleError('invalid nonnegative decimal')
    return result


def _number(value):
    """Canonical non-exponent decimal; arithmetic never uses binary floats."""
    return format(value, 'f').rstrip('0').rstrip('.') if '.' in format(value, 'f') else format(value, 'f')


def _ratio(numerator, denominator):
    n, d = Decimal(numerator), Decimal(denominator)
    if not d:
        return None
    return {'numerator': _number(n), 'denominator': _number(d), 'decimal': _number(n / d)}


def _path(path):
    p = Path(path).expanduser().absolute()
    for part in (p, *p.parents):
        if part.is_symlink() and str(part) not in ('/tmp', '/var'):
            raise BundleError('bundle path may not contain symlinks')
    return p


def _private_write(path, chunks):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        if isinstance(chunks, str):
            stream.write(chunks)
        else:
            stream.writelines(chunks)


def _hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path):
    def bad_constant(_):
        raise BundleError('nonfinite JSON value')
    with path.open(encoding='utf-8') as stream:
        return json.load(stream, parse_constant=bad_constant)


def _lines(path):
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            if not line.endswith('\n') or not line.strip():
                raise BundleError('invalid JSONL line')
            value = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(BundleError('nonfinite JSON value')))
            if not isinstance(value, dict):
                raise BundleError('JSONL record must be an object')
            yield value


def _safe_source(source):
    if not isinstance(source, dict):
        raise BundleError('source pointer missing')
    name = source.get('file')
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', name):
        raise BundleError('source must use a private-path-free alias')
    if not isinstance(source.get('line'), int) or isinstance(source['line'], bool) or source['line'] < 1:
        raise BundleError('source line must be positive')


def _normalize(request, pricing):
    models = pricing.get('models', {})
    rates = models.get(request.get('model'))
    reference = models.get(pricing.get('reference_model'))
    if not rates or not reference or request.get('cache_write_input_tokens') != 0:
        return None
    denominator = _decimal(reference['input'])
    if not denominator:
        raise BundleError('reference input price must be positive')
    new = Decimal(request['new_tokens']) * _decimal(rates['input']) / denominator
    cache = Decimal(request['cached_input_tokens']) * _decimal(rates['cached']) / denominator
    output = Decimal(request['output_tokens']) * _decimal(rates['output']) / denominator
    return {k: _number(v) for k, v in zip(EQUIV_FIELDS, (new, cache, new+cache, output, new+cache+output))}


def _request_valid(request):
    _safe_source(request.get('source'))
    for field in RAW_FIELDS:
        value = request.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise BundleError('invalid request counters')
    if request['input_tokens'] != request['new_tokens'] + request['cached_input_tokens'] or request['total_tokens'] != request['input_tokens'] + request['output_tokens']:
        raise BundleError('request arithmetic invariant failed')
    for field, upper in (('reasoning_output_tokens','output_tokens'), ('cache_write_input_tokens','new_tokens')):
        value=request.get(field)
        if value is not None and (not isinstance(value,int) or isinstance(value,bool) or not 0 <= value <= request[upper]):
            raise BundleError('invalid optional request counter')


def _summary(requests):
    raw = {k: sum(r[k] for r in requests) for k in RAW_FIELDS}
    priced = [r for r in requests if r.get('normalized') is not None]
    values = {k: _number(sum((Decimal(r['normalized'][k]) for r in priced), Decimal(0))) for k in EQUIV_FIELDS}
    return {'request_count':len(requests), 'raw_totals':raw,
            'normalized_total':{'values':values, 'complete':len(priced)==len(requests)},
            'priced_request_count':len(priced), 'unpriced_request_count':len(requests)-len(priced),
            'unknown_components':{k:sum(r.get(k) is None for r in requests) for k in ('model','effort','timestamp','reasoning_output_tokens','cache_write_input_tokens')},
            'weighted_cached_tokens':values['cached_tokens'],
            'weighted_cache_share_of_known_subtotal':_ratio(values['cached_tokens'], values['total_tokens'])}


def _metrics(requests):
    sources={(r['session_id'],r['source']['file']) for r in requests}
    proven_order=len(sources)<=1
    if proven_order:
        requests=sorted(requests,key=lambda r:r['source']['line'])
    inputs = [r['input_tokens'] for r in requests]
    ordered=sorted(inputs)
    n=len(inputs)
    buckets=[]
    edges=(0,100000,200000,300000,400000,None)
    for lo,hi in zip(edges,edges[1:]):
        subset=[r for r in requests if r['input_tokens']>=lo and (hi is None or r['input_tokens']<hi)]
        buckets.append({'lower_inclusive':lo,'upper_exclusive':hi,**_summary(subset)})
    return {'calls':n,'first_input_tokens':inputs[0] if n and proven_order else None,'last_input_tokens':inputs[-1] if n and proven_order else None,
            'mean_input_tokens':_number(Decimal(sum(inputs))/n) if n else None,
            'mean_input_ratio':_ratio(sum(inputs),n),
            'median_input_tokens':_number(Decimal(ordered[(n-1)//2]+ordered[n//2])/2) if n else None,
            'p95_input_tokens':ordered[(95*n+99)//100-1] if n else None,
            'p95_method':'nearest-rank ceil(0.95*n)',
            'max_input_tokens':max(inputs) if n else None,
            'input_sum_tokens':sum(inputs),'cached_input_sum_tokens':sum(r['cached_input_tokens'] for r in requests),
            'cache_hit_fraction':_ratio(sum(r['cached_input_tokens'] for r in requests),sum(inputs)),
            'input_size_buckets':buckets,
            'order_basis':'source line within a session; cross-session order is unproven'}


def _agent_rows(requests):
    grouped=defaultdict(list)
    for r in requests:
        grouped[(r['session_id'],r.get('model'),r.get('effort'))].append(r)
    rows=[]
    for key,subset in sorted(grouped.items(),key=lambda x:tuple(str(y) for y in x[0])):
        rows.append(dict(session_id=key[0],agent=subset[0]['agent'],model=key[1],effort=key[2],**_summary(subset)))
    normalized=[{'session_id':row['session_id'],'agent':row['agent'],'model':row['model'],'effort':row['effort'],
                 'normalized':row['normalized_total']['values'] if row['priced_request_count'] else None,
                 'complete':row['normalized_total']['complete']} for row in rows]
    return rows,normalized


def _window(record, default):
    return record.get('root_turn_id') or (record.get('root_turn_ids') or [None])[0] or default


def _segments(requests, markers, default):
    groups=defaultdict(list)
    marker_groups=defaultdict(list)
    for r in requests:
        groups[(r['session_id'],_window(r,default),r['source']['file'])].append(r)
    for m in markers:
        marker_groups[(m['session_id'],_window(m,default),m['source_file'])].append(m)
    segments=[]
    clean_markers=[]
    for key in sorted(set(groups)|set(marker_groups),key=lambda key:tuple(str(x) for x in key)):
        reqs=groups[key]
        reqs.sort(key=lambda r:(r['source']['line'],r['source']['file']))
        unique={}
        for m in marker_groups[key]:
            # Equal timestamps do not merge distinct source positions. Archive copies
            # of an observed marker are keyed by its stable event identity or line.
            identity=m.get('event_id') or (m.get('source_file'),m['line'])
            unique.setdefault(str(identity),m)
        ordered=sorted(unique.values(),key=lambda m:(m['line'],m.get('source_file','')))
        boundaries=[m['line'] for m in ordered]
        for index in range(len(boundaries)+1):
            lo=boundaries[index-1] if index else None
            hi=boundaries[index] if index<len(boundaries) else None
            subset=[r for r in reqs if (lo is None or r['source']['line']>lo) and (hi is None or r['source']['line']<hi)]
            segment_id='segment-'+hashlib.sha256(f'{key!r}:{index}'.encode()).hexdigest()[:20]
            previous=None
            for r in subset:
                r['segment_id']=segment_id
            for r in reqs:
                if lo is not None and r['source']['line']<lo:
                    previous=r
            following=next((r for r in reqs if hi is not None and r['source']['line']>hi),None)
            pointer=lambda r: {'response_id':r['response_id'],'source':r['source'],'input_tokens':r['input_tokens']} if r else None
            segments.append({'segment_id':segment_id,'session_id':key[0],'root_turn_id':key[1],
                             'source_alias':key[2],'order_basis':'same source stream only; cross-source ordering unproven',
                             'start_marker_id':ordered[index-1].get('event_id') if index else None,
                             'end_marker_id':ordered[index].get('event_id') if index<len(ordered) else None,
                             'nearest_before':pointer(previous),'nearest_after':pointer(following),**_summary(subset)})
        for m in ordered:
            before=[r for r in reqs if r['source']['line']<m['line']]
            after=[r for r in reqs if r['source']['line']>m['line']]
            clean={k:m.get(k) for k in ('event_id','session_id','root_turn_id','turn_id','timestamp','source_file','line')}
            clean['nearest_before']=pointer(before[-1] if before else None)
            clean['nearest_after']=pointer(after[0] if after else None)
            clean_markers.append(clean)
        previous=None
        for r in reqs:
            r['previous_request_id']=previous['response_id'] if previous else None
            r['input_delta_tokens']=r['input_tokens']-previous['input_tokens'] if previous else None
            r['adjacency_basis']='source line in selected session/window'
            previous=r
    return segments,clean_markers


def _schema():
    counter={'type':'integer','minimum':0}
    optional={'type':['integer','null'],'minimum':0,'description':'null is unknown, never zero'}
    return {'$schema':'https://json-schema.org/draft/2020-12/schema', 'schema_version':2,
            'title':'Codex Token Audit bundle',
            'description':'Prepared metadata only. No message bodies, commands, reasoning text, images or absolute local paths.',
            'units':{'raw':'integer tokens; cached is a subset of input, reasoning a subset of output',
                     'normalized':'decimal strings in ordinary Sol input-token API-price equivalents; no display rounding',
                     'ratios':'numerator/denominator is authoritative; decimal may repeat and uses Decimal precision 28',
                     'text_sizes':'observed Unicode characters and UTF-8 bytes, never token or cost attribution'},
            'relations':{'manifest.json':'immutable inventory hashes all payload files, not itself or delivery receipt',
                         'audit.json':'aggregates and disjoint compaction segments; root included once in total',
                         'requests.jsonl':'one selected deduplicated response per row; segment_id joins audit segments; event_ids join events',
                         'events.jsonl':'all observed scoped metadata; source aliases plus line; cross-session order unproven',
                         'pricing.json':'exact selected dated snapshot; missing prices/cache-write retention stay unknown',
                         'delivery-receipt.json':'mutable, local delivery state; not part of immutable payload inventory'},
            'ordering':'source_line is local evidence, not a fabricated global order; timestamps can be absent or equal',
            'privacy':'source.file/source_file are aliases, never raw paths; only allowlisted metadata is retained',
            'runtime_evidence':{'version':1, 'optional':True,
                'description':'Recomputed from requests/events. Observed span may cover only part of a task. Quota is account-wide, identity unverified and attribution unavailable. Tool intervals are not model-billed time.',
                'fields':{'task_label':'optional caller-supplied single-line text, 1–120 characters',
                          'quota_comparisons':'per root turn/session/source/bucket/window first and last event pointers, count, signed percentage-point delta or null, decrease/reset flag; changes or ambiguity prevent comparison',
                          'tools':'call/result counts, known character and byte totals, unknown counts and uniquely paired source-linked observed intervals',
                          'root_input_by_turn':'first/last/max root request input and source/response pointers; no instruction/plugin baseline inferred'}},
            '$defs':{
                'source':{'type':'object','required':['file','line'],'properties':{'file':{'type':'string','pattern':'^[A-Za-z0-9_-]+$'},'line':{'type':'integer','minimum':1}}},
                'request':{'type':'object','required':['session_id','turn_id','response_id','source',*RAW_FIELDS,'segment_id'],
                           'properties':{**{k:counter for k in RAW_FIELDS},**{k:optional for k in ('reasoning_output_tokens','cache_write_input_tokens')},
                                         'source':{'$ref':'#/$defs/source'},'normalized':{'type':['object','null'],'description':'null means unpriced; known subtotal sums only priced responses'},
                                         'timestamp':{'type':['number','null']},'event_ids':{'type':'array','items':{'type':'string'}},
                                         'configured_service_tier':{'type':['object','null'],'description':'explicit client setting and source pointer, not response speed'},
                                         'observed_service_tier':{'type':['object','null'],'description':'explicit response usage metadata and source pointer; null if absent'}}},
                'event':{'type':'object','required':['event_id','event_type','session_id','source'],
                         'properties':{'source':{'$ref':'#/$defs/source'},'result_chars':optional,'result_bytes':optional,
                                       **{k:{'type':['boolean','null']} for k in ('mixed','truncated','images','opaque')},
                                       'quota':{'type':'object','description':'allowlisted account-wide bucket ID and plan, primary/secondary used_percent, window_minutes and resets_at, credits has_credits/unlimited/balance; absent fields unknown'},
                                       'configured_service_tier':{'type':'string','description':'explicit structured client setting'},
                                       'result_truncated':{'type':'boolean','description':'explicit result flag'},'source_line_incomplete':{'type':'boolean','description':'source line lacked final newline'},
                                       'fork_turns':{'enum':['none','all',None],'description':'recorded setting only; no inference from policy or input size'}}},
                'manifest':{'type':'object','required':['schema_version','run_id','files','scope','coverage','collector']},
                'audit':{'type':'object','required':['schema_version','raw_totals','normalized_total','summaries','compaction_segments']}}}


def _commit():
    try:
        result=subprocess.run(['git','-C',str(Path(__file__).resolve().parent),'rev-parse','HEAD'],capture_output=True,text=True,timeout=3)
        value=result.stdout.strip()
        return value if result.returncode==0 and re.fullmatch('[0-9a-f]{40,64}',value) else None
    except (OSError,subprocess.TimeoutExpired):
        return None


def write_bundle(report, output_dir, *, rates_path, run_id=None, markdown=False):
    """Write one new private run directory. Existing files are never overwritten."""
    run_id=run_id or uuid.uuid4().hex
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',run_id):
        raise BundleError('unsafe run_id')
    requests=copy.deepcopy(report['requests'])
    events=copy.deepcopy(report.get('events',[]))
    pricing=_read_json(Path(rates_path))
    for r in requests:
        _request_valid(r)
        r['normalized']=_normalize(r,pricing)
        r['unknown_fields']=[k for k in ('model','effort','timestamp','reasoning_output_tokens','cache_write_input_tokens') if r.get(k) is None]
        r['order']={'basis':'source_line','cross_session':'unproven'}
    for e in events:
        _safe_source(e.get('source'))
    default=report['scope'].get('turn_id')
    markers=report.get('compaction_markers',report.get('compaction_segments',[]))
    segments,clean_markers=_segments(requests,markers,default)
    root_id=report['scope']['session_id']
    root=[r for r in requests if r['session_id']==root_id]
    children=[r for r in requests if r['session_id']!=root_id]
    rows,normalized_rows=_agent_rows(requests)
    total=_summary(requests)
    metadata=copy.deepcopy(report.get('agent_metadata',[]))
    if isinstance(metadata,dict):metadata=list(metadata.values())
    for meta in metadata:
        own=[r for r in requests if r['session_id']==meta.get('session_id')]
        meta['first_observed_input_tokens']=_metrics(own)['first_input_tokens']
        meta['first_input_order_basis']='same source stream only; unknown across unaligned copies'
        meta.setdefault('fork_turns',None)
        meta.setdefault('fork_provenance','unknown; not inferred from current policy')
    audit={'schema_version':2,'scope':report['scope'],'provenance':report.get('provenance',{}),
           'coverage':report['coverage'],'raw_totals':total['raw_totals'],'normalized_total':total['normalized_total'],
           'request_count':len(requests),'event_count':len(events),'rows':rows,
           'normalized_rows':normalized_rows,
           'summaries':{'all':total,'root':_summary(root),'children':_summary(children)},
           'root_context_metrics':_metrics(root),'all_context_metrics':_metrics(requests),
           'compaction_segments':segments,'compaction_markers':clean_markers,'agent_metadata':metadata,
           'reconciliation':report.get('reconciliation',{'status':'not_comparable'}),
           'pricing':{'file':'pricing.json','reference_model':pricing.get('reference_model'),'scenario':pricing.get('scenario'),
                      'verified_at':pricing.get('verified_at'),'unit':'ordinary Sol input-token API-price equivalents; not subscription quota'}}
    try:
        runtime_evidence.validate_new_fields(requests, events, report.get('task_label'))
    except ValueError as exc:
        raise BundleError(str(exc)) from exc
    audit['runtime_evidence']=runtime_evidence.summarize(requests, events, report['scope'], report.get('task_label'))
    out=_path(output_dir)
    if out.exists():
        raise BundleError('output directory already exists; refusing to overwrite')
    out.mkdir(parents=True,mode=0o700)
    # JSONL is streamed to disk; no all-record serialized string or top-N truncation.
    _private_write(out/'requests.jsonl',(json.dumps(r,ensure_ascii=False,sort_keys=True,allow_nan=False)+'\n' for r in requests))
    _private_write(out/'events.jsonl',(json.dumps(e,ensure_ascii=False,sort_keys=True,allow_nan=False)+'\n' for e in events))
    for name,value in (('audit.json',audit),('schema.json',_schema()),('pricing.json',pricing)):
        _private_write(out/name,_json(value))
    if markdown:
        text=report.get('_markdown')
        if not isinstance(text,str):
            raise BundleError('compatibility Markdown was not supplied by collector')
        _private_write(out/'audit.md',text)
    files=[]
    for p in sorted(out.iterdir()):
        count=len(requests) if p.name=='requests.jsonl' else len(events) if p.name=='events.jsonl' else 1
        files.append({'name':p.name,'size_bytes':p.stat().st_size,'sha256':_hash(p),'record_count':count})
    manifest={'schema_version':2,'run_id':run_id,'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),
              'collector':{'version':COLLECTOR_VERSION,'commit':_commit(),'commit_provenance':'local git HEAD when available; installed package may be unversioned'},
              'scope':report['scope'],'coverage':report['coverage'],'provenance':report.get('provenance',{}),
              'collection_status':report['coverage']['status'],'delivery':{'status':'NOT CONFIGURED','receipt':'delivery-receipt.json; separate from immutable bundle'},
              'files':files,'manifest_checksum':'not self-referential; delivery hashes manifest separately'}
    _private_write(out/'manifest.json',_json(manifest))
    validate_bundle(out)
    return out


def validate_bundle(path):
    """Check schema, complete inventory, bytes, rows, links and arithmetic offline."""
    out=_path(path)
    if not out.is_dir():raise BundleError('bundle directory unavailable')
    manifest=_read_json(out/'manifest.json')
    if manifest.get('schema_version')!=2 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',manifest.get('run_id','')):
        raise BundleError('unsupported manifest schema or identity')
    names=set()
    entries=manifest.get('files')
    if not isinstance(entries,list):raise BundleError('manifest inventory missing')
    for entry in entries:
        name=entry.get('name')
        if name not in REQUIRED_FILES|{'audit.md'} or name in names:
            raise BundleError('invalid or repeated inventory filename')
        names.add(name)
        p=_path(out/name)
        if not p.is_file() or p.stat().st_size!=entry.get('size_bytes') or _hash(p)!=entry.get('sha256'):
            raise BundleError('bundle size or SHA-256 mismatch')
        if name.endswith('.jsonl'):
            count=sum(1 for _ in _lines(p))
        else:
            if name.endswith('.json') and not isinstance(_read_json(p),dict):raise BundleError('JSON object required')
            count=1
        if count!=entry.get('record_count'):raise BundleError('bundle row count mismatch')
    if not REQUIRED_FILES<=names:raise BundleError('bundle payload missing')
    if {p.name for p in out.iterdir()}-names-{'manifest.json','delivery-receipt.json','.delivery.lock'}:
        raise BundleError('unexpected bundle file')
    for p in out.iterdir():
        if p.is_symlink():raise BundleError('bundle may not contain symlinks')
    requests=list(_lines(out/'requests.jsonl'))
    events=list(_lines(out/'events.jsonl'))
    pricing=_read_json(out/'pricing.json')
    seen=set()
    for r in requests:
        _request_valid(r)
        rid=r.get('response_id')
        if rid is not None and rid in seen:raise BundleError('duplicate response identity')
        if rid is not None:seen.add(rid)
        if r.get('normalized')!=_normalize(r,pricing):raise BundleError('request price equivalence mismatch')
    ids=set()
    for event in events:
        _safe_source(event.get('source'))
        if not event.get('event_id') or event['event_id'] in ids or not event.get('event_type'):
            raise BundleError('event schema/identity invalid')
        ids.add(event['event_id'])
    for r in requests:
        if any(e not in ids for e in r.get('event_ids',[])):raise BundleError('request references missing event')
    audit=_read_json(out/'audit.json')
    schema=_read_json(out/'schema.json')
    if audit.get('schema_version')!=2 or schema.get('schema_version')!=2:raise BundleError('schema version mismatch')
    if 'runtime_evidence' not in audit and manifest.get('collector', {}).get('version') == COLLECTOR_VERSION:
        raise BundleError('runtime evidence missing from current collector bundle')
    if 'runtime_evidence' in audit:
        extension = audit['runtime_evidence']
        if not isinstance(extension, dict): raise BundleError('invalid runtime evidence')
        try:
            runtime_evidence.validate_new_fields(requests, events, extension.get('task_label'))
            if extension != runtime_evidence.summarize(requests, events, audit['scope'], extension.get('task_label')):
                raise BundleError('runtime evidence summary mismatch')
        except (ValueError, KeyError, TypeError) as exc:
            raise BundleError('invalid runtime evidence') from exc
    expected=_summary(requests)
    if audit.get('raw_totals')!=expected['raw_totals'] or audit.get('normalized_total')!=expected['normalized_total'] or audit.get('request_count')!=len(requests) or audit.get('event_count')!=len(events):
        raise BundleError('aggregate arithmetic or record count mismatch')
    if audit.get('scope')!=manifest.get('scope') or audit.get('coverage')!=manifest.get('coverage'):
        raise BundleError('manifest scope or coverage differs from audit')
    if audit['summaries']['all']!=expected:raise BundleError('total summary mismatch')
    rows,normalized_rows=_agent_rows(requests)
    if audit.get('rows')!=rows or audit.get('normalized_rows')!=normalized_rows:
        raise BundleError('per-agent aggregate mismatch')
    root=manifest['scope']['session_id']
    for name,subset in (('root',[r for r in requests if r['session_id']==root]),('children',[r for r in requests if r['session_id']!=root])):
        if audit['summaries'][name]!=_summary(subset):raise BundleError('root/child partition mismatch')
    segment_ids=set()
    for segment in audit.get('compaction_segments',[]):
        sid=segment['segment_id']
        if sid in segment_ids:raise BundleError('duplicate segment identity')
        segment_ids.add(sid)
        subset=[r for r in requests if r.get('segment_id')==sid]
        for key,value in _summary(subset).items():
            if segment.get(key)!=value:raise BundleError('segment arithmetic mismatch')
    if any(r.get('segment_id') not in segment_ids for r in requests):raise BundleError('request lacks disjoint segment membership')
    if sum(s['request_count'] for s in audit['compaction_segments'])!=len(requests):raise BundleError('segments do not partition requests')
    for key,subset in (('root_context_metrics',[r for r in requests if r['session_id']==root]),('all_context_metrics',requests)):
        if audit[key]!=_metrics(subset):raise BundleError('context metrics or buckets mismatch')
    return manifest
