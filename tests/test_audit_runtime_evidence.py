import hashlib
import json
import sys
import unittest
from pathlib import Path

import test_token_audit as fixtures

event, root_rows, write_jsonl = fixtures.event, fixtures.root_rows, fixtures.write_jsonl

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/codex-token-audit/scripts'
sys.path.insert(0, str(SCRIPTS))
import bundle


def rehash(directory, name):
    manifest_path = directory / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    data = (directory / name).read_bytes()
    for entry in manifest['files']:
        if entry['name'] == name:
            entry['size_bytes'] = len(data)
            entry['sha256'] = hashlib.sha256(data).hexdigest()
    manifest_path.write_text(json.dumps(manifest))


class RuntimeEvidenceTests(unittest.TestCase):
    setUp = fixtures.TokenAuditTests.setUp
    tearDown = fixtures.TokenAuditTests.tearDown
    run_audit = fixtures.TokenAuditTests.run_audit
    report = fixtures.TokenAuditTests.report
    def test_quota_tier_tools_and_context_are_sanitized_and_linked(self):
        rows = root_rows()
        rows[2]['payload']['service_tier'] = 'priority'
        rows[3]['payload']['usage']['service_tier'] = 'standard'
        quota = lambda n, timestamp: {'type': 'event_msg', 'timestamp': timestamp, 'payload': {'type': 'token_count', 'rate_limits': {'limit_id': 'main', 'limit_name': 'SECRET_NAME', 'plan_type': 'pro', 'primary': {'used_percent': n, 'window_minutes': 300, 'resets_at': 123456}, 'credits': {'has_credits': True, 'unlimited': False, 'balance': 20}, 'email': 'SECRET_EMAIL'}}}
        call = {'type': 'response_item', 'timestamp': '2026-10-28T10:00:10Z', 'payload': {'type': 'function_call', 'name': 'exec', 'call_id': 'call1', 'arguments': 'SECRET_COMMAND'}}
        result = {'type': 'response_item', 'timestamp': '2026-10-28T10:00:12Z', 'payload': {'type': 'function_call_output', 'call_id': 'call1', 'output': 'SECRET_OUTPUT', 'truncated': True}}
        rows[3:3] = [quota(10, '2026-10-28T10:00:01Z'), call, result, quota(17, '2026-10-28T10:00:15Z')]
        write_jsonl(self.sessions / 'root.jsonl', rows)
        proc = self.run_audit('--task-label', 'Example task')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.report()
        rt = report['runtime_evidence']
        self.assertEqual(rt['task_label'], 'Example task')
        self.assertEqual(rt['quota_comparisons'][0]['delta_percentage_points'], 7)
        self.assertEqual(rt['tools']['pairs'][0]['interval_seconds'], 2)
        self.assertEqual(rt['tools']['pairs'][0]['name'], 'exec')
        self.assertEqual(rt['root_input_by_turn'][0]['max']['input_tokens'], 110)
        self.assertEqual(report['requests'][0]['configured_service_tier']['value'], 'priority')
        self.assertEqual(report['requests'][0]['observed_service_tier']['value'], 'standard')
        output = json.dumps(report)
        for secret in ('SECRET_NAME', 'SECRET_EMAIL', 'SECRET_COMMAND', 'SECRET_OUTPUT'):
            self.assertNotIn(secret, output)
        result_event = next(e for e in report['events'] if e['event_type'] == 'function_call_output')
        self.assertTrue(result_event['result_truncated'])
        self.assertFalse(result_event['source_line_incomplete'])
        bundle.validate_bundle(self.out)

    def test_quota_reset_reverse_and_ambiguous_tools_stay_unknown(self):
        rows = root_rows()
        for index, (n, reset, ts) in enumerate([(30, 1, '2026-10-28T10:00:01Z'), (20, 2, '2026-10-28T10:00:02Z')]):
            rows.insert(3 + index, {'type':'event_msg', 'timestamp':ts, 'payload':{'type':'token_count', 'rate_limits':{'limit_id':'main', 'plan_type':'pro', 'primary':{'used_percent':n, 'window_minutes':300, 'resets_at':reset}}}})
        for typ in ('function_call', 'function_call', 'function_call_output'):
            rows.insert(3, {'type':'response_item', 'timestamp':'2026-10-28T10:00:03Z', 'payload':{'type':typ, 'name':'exec', 'call_id':'duplicate'}})
        write_jsonl(self.sessions / 'root.jsonl', rows)
        proc = self.run_audit()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rt = self.report()['runtime_evidence']
        self.assertIsNone(rt['quota_comparisons'][0]['delta_percentage_points'])
        self.assertEqual(rt['tools']['pairs'], [])
        self.assertGreater(rt['tools']['unknown_pair_count'], 0)

    def test_runtime_summary_tampering_is_rejected_even_with_rehashed_inventory(self):
        self.assertEqual(self.run_audit().returncode, 0)
        path = self.out / 'audit.json'
        audit = json.loads(path.read_text())
        audit['runtime_evidence']['tools']['call_count'] = 999
        path.write_text(json.dumps(audit))
        rehash(self.out, 'audit.json')
        with self.assertRaises(bundle.BundleError):
            bundle.validate_bundle(self.out)

    def test_old_v2_without_extension_remains_valid(self):
        self.assertEqual(self.run_audit().returncode, 0)
        path = self.out / 'audit.json'
        audit = json.loads(path.read_text())
        audit.pop('runtime_evidence', None)
        manifest_path = self.out / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['collector']['version'] = '2.0.0'
        manifest_path.write_text(json.dumps(manifest))
        path.write_text(json.dumps(audit))
        rehash(self.out, 'audit.json')
        for filename, fields in (('requests.jsonl', ('configured_service_tier', 'observed_service_tier')),
                                 ('events.jsonl', ('source_line_incomplete', 'result_truncated', 'source_copies_ambiguous'))):
            record_path = self.out / filename
            records = [json.loads(line) for line in record_path.read_text().splitlines()]
            for record in records:
                for field in fields:
                    record.pop(field, None)
            record_path.write_text(''.join(json.dumps(record) + '\n' for record in records))
            rehash(self.out, filename)
        bundle.validate_bundle(self.out)

    def test_task_label_rejects_multiline(self):
        for label in ('private\nline', 'private\u2028line'):
            with self.subTest(label=repr(label)):
                result = self.run_audit('--task-label', label)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.out.exists())

    def test_camel_quota_decrease_and_explicit_settings_provenance(self):
        rows = root_rows()
        rows[3]['payload']['usage']['serviceTier'] = 'flex'
        rows[3:3] = [
            event('thread_settings_applied', threadSettings={'serviceTier':'priority', 'email':'SECRET_EMAIL'}),
            {'type':'event_msg','timestamp':'2026-10-28T10:00:01Z','payload':{'type':'token_count','rateLimits':{'limitId':'main','planType':'pro','primary':{'usedPercent':50,'windowMinutes':300,'resetsAt':42}, 'credits':{'balance':'12.50'}}}},
            {'type':'event_msg','timestamp':'2026-10-28T10:00:02Z','payload':{'type':'token_count','rateLimits':{'limitId':'main','planType':'pro','primary':{'usedPercent':40,'windowMinutes':300,'resetsAt':42}}}},
        ]
        write_jsonl(self.sessions / 'root.jsonl', rows)
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report()
        comp = report['runtime_evidence']['quota_comparisons'][0]
        self.assertEqual(comp['delta_percentage_points'], -10)
        self.assertTrue(comp['decrease_or_reset'])
        self.assertEqual(report['requests'][0]['configured_service_tier']['provenance'], 'thread_settings_applied')
        self.assertEqual(report['requests'][0]['observed_service_tier']['value'], 'flex')
        quotas = [e['quota'] for e in report['events'] if 'quota' in e]
        self.assertEqual(quotas[0]['credits']['balance'], '12.50')
        self.assertNotIn('SECRET_EMAIL', json.dumps(report))

    def test_unproven_timestamps_and_malformed_metadata_remain_unknown(self):
        rows = root_rows()
        rows[3:3] = [
            {'type':'event_msg','payload':{'type':'token_count','rate_limits':{'limit_id':'main','plan_type':'pro','primary':{'used_percent':10,'window_minutes':300,'resets_at':50},'credits':{'balance':'SECRET_VALUE'}}}},
            {'type':'event_msg','payload':{'type':'token_count','rate_limits':{'limit_id':'main','plan_type':'pro','primary':{'used_percent':20,'window_minutes':300,'resets_at':50}}}},
        ]
        write_jsonl(self.sessions / 'root.jsonl', rows)
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report()
        self.assertIsNone(report['runtime_evidence']['quota_comparisons'][0]['delta_percentage_points'])
        self.assertIsNone(report['requests'][0]['configured_service_tier'])
        self.assertIsNone(report['requests'][0]['observed_service_tier'])
        self.assertNotIn('SECRET_VALUE', json.dumps(report))

    def test_runtime_extension_cannot_be_stripped_from_new_collector_bundle(self):
        self.assertEqual(self.run_audit().returncode, 0)
        path = self.out / 'audit.json'
        audit = json.loads(path.read_text())
        audit.pop('runtime_evidence')
        path.write_text(json.dumps(audit))
        rehash(self.out, 'audit.json')
        with self.assertRaises(bundle.BundleError):
            bundle.validate_bundle(self.out)

    def test_tampered_runtime_metadata_fails_even_after_rehash(self):
        rows = root_rows()
        rows.insert(3, {'type':'response_item', 'payload':{'type':'function_call_output', 'call_id':'x','output':'text','truncated':False}})
        write_jsonl(self.sessions / 'root.jsonl', rows)
        self.assertEqual(self.run_audit().returncode, 0)
        path = self.out / 'events.jsonl'
        events = [json.loads(line) for line in path.read_text().splitlines()]
        target = next(e for e in events if e['event_type'] == 'function_call_output')
        target['result_truncated'] = True
        path.write_text(''.join(json.dumps(e) + '\n' for e in events))
        rehash(self.out, 'events.jsonl')
        with self.assertRaises(bundle.BundleError):
            bundle.validate_bundle(self.out)

    def test_explicit_result_truncation_is_distinct_from_incomplete_line(self):
        import audit_usage
        path = self.base / 'partial.jsonl'
        row = {'type':'response_item', 'payload':{'type':'function_call_output', 'call_id':'x', 'output':'result', 'truncated':False}}
        path.write_text(json.dumps(row))
        sanitized = list(audit_usage._json_lines(path))[0][1]['payload']
        self.assertTrue(sanitized['truncated'])
        self.assertFalse(sanitized['result_truncated'])
        self.assertTrue(sanitized['source_line_incomplete'])

    def test_changed_configured_tier_pointer_cannot_be_rehashed_into_a_valid_bundle(self):
        rows = root_rows()
        rows[2]['payload']['service_tier'] = 'priority'
        rows.insert(3, event('thread_settings_applied', thread_settings={'service_tier':'standard'}))
        write_jsonl(self.sessions / 'root.jsonl', rows)
        proc = self.run_audit()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        path = self.out / 'requests.jsonl'
        requests = [json.loads(line) for line in path.read_text().splitlines()]
        tier_event = next(e for e in self.report()['events'] if e['event_type'] == 'turn_context')
        requests[0]['configured_service_tier'] = {'value':'priority', 'source':tier_event['source'], 'provenance':'turn_context'}
        path.write_text(''.join(json.dumps(r) + '\n' for r in requests))
        rehash(self.out, 'requests.jsonl')
        with self.assertRaises(bundle.BundleError):
            bundle.validate_bundle(self.out)

    def test_disjoint_root_turns_keep_quota_comparisons_and_inputs_separate(self):
        rows = root_rows()[:8]
        def quota(n, ts):
            return {'type':'event_msg','timestamp':ts,'payload':{'type':'token_count','rate_limits':{'limit_id':'main','plan_type':'pro','primary':{'used_percent':n,'window_minutes':300,'resets_at':42}}}}
        rows[3:3] = [quota(10,'2026-10-28T10:00:01Z'), quota(12,'2026-10-28T10:00:02Z')]
        rows += [event('task_started', turn_id='turn-b', timestamp='2026-10-28T11:00:00Z'),
                 {'type':'turn_context','payload':{'turn_id':'turn-b','model':'m'}},
                 quota(50,'2026-10-28T11:00:01Z'), quota(53,'2026-10-28T11:00:02Z')]
        req = fixtures.usage('other', 200, 5, 2)
        req['payload']['turn_id'] = 'turn-b'
        rows += [req, event('task_complete', turn_id='turn-b', timestamp='2026-10-28T11:01:00Z')]
        write_jsonl(self.sessions / 'root.jsonl', rows)
        proc = self.run_audit('--turn-id', 'turn-b')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.report()
        comparisons = report['runtime_evidence']['quota_comparisons']
        self.assertEqual({c['root_turn_id']:c['delta_percentage_points'] for c in comparisons}, {'turn-a':2, 'turn-b':3})
        self.assertEqual(len([e for e in report['events'] if 'quota' in e]), 4)
        self.assertEqual({v['root_turn_id']:v['max']['input_tokens'] for v in report['runtime_evidence']['root_input_by_turn']}, {'turn-a':110, 'turn-b':200})

    def test_duplicate_sources_make_quota_comparison_unavailable(self):
        rows = root_rows()
        for index, (n, ts) in enumerate([(10,'2026-10-28T10:00:01Z'), (20,'2026-10-28T10:00:02Z')]):
            rows.insert(3 + index, {'type':'event_msg','timestamp':ts,'payload':{'type':'token_count','rate_limits':{'limit_id':'main','plan_type':'pro','primary':{'used_percent':n,'window_minutes':300,'resets_at':42}}}})
        write_jsonl(self.sessions / 'root.jsonl', rows)
        write_jsonl(self.sessions / 'archive' / 'copy.jsonl', rows)
        proc = self.run_audit()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(self.report()['runtime_evidence']['quota_comparisons'][0]['delta_percentage_points'])

    def test_unreasonably_large_numeric_quota_is_discarded_without_crashing(self):
        rows = root_rows()
        rows.insert(3, {'type':'event_msg','payload':{'type':'token_count','rate_limits':{'primary':{'used_percent':10**400, 'resets_at':10**400}}}})
        write_jsonl(self.sessions / 'root.jsonl', rows)
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        quota = next(e['quota'] for e in self.report()['events'] if 'quota' in e)
        self.assertEqual(quota['primary'], {})

    def test_percentages_above_one_hundred_are_retained_as_observations(self):
        rows = root_rows()
        rows.insert(3, {'type':'event_msg','payload':{'type':'token_count','rate_limits':{'primary':{'used_percent':125}}}})
        write_jsonl(self.sessions / 'root.jsonl', rows)
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        quota = next(e['quota'] for e in self.report()['events'] if 'quota' in e)
        self.assertEqual(quota['primary']['used_percent'], 125)

    def test_naive_timestamp_does_not_prove_quota_order(self):
        rows = root_rows()
        for index, (n, ts) in enumerate([(10, '2026-10-28T10:00:01'), (20, '2026-10-28T10:00:02Z')]):
            rows.insert(3 + index, {'type':'event_msg','timestamp':ts,'payload':{'type':'token_count','rate_limits':{'limit_id':'main','plan_type':'pro','primary':{'used_percent':n,'window_minutes':300,'resets_at':42}}}})
        write_jsonl(self.sessions / 'root.jsonl', rows)
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.report()['runtime_evidence']['quota_comparisons'][0]['delta_percentage_points'])

    def test_copied_sessions_with_explicit_tier_remain_collectible(self):
        rows = root_rows()
        rows[2]['payload']['service_tier'] = 'priority'
        write_jsonl(self.sessions / 'root.jsonl', rows)
        write_jsonl(self.sessions / 'archive' / 'copy.jsonl', rows)
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.report()['requests'][0]['configured_service_tier']['value'], 'priority')

    def test_child_quota_is_kept_separate_from_root_account_observations(self):
        def quota(n, ts):
            return {'type':'event_msg','timestamp':ts,'payload':{'type':'token_count','rate_limits':{'limit_id':'main','plan_type':'pro','primary':{'used_percent':n,'window_minutes':300,'resets_at':42}}}}
        rows = root_rows()
        rows[3:3] = [quota(10,'2026-10-28T10:00:10Z'), quota(12,'2026-10-28T10:00:20Z')]
        write_jsonl(self.sessions / 'root.jsonl', rows)
        child = [{'type':'session_meta','payload':{'id':'child','source':{'subagent':{'thread_spawn':{'parent_thread_id':'root'}}}}},
                 event('task_started', turn_id='child-turn', timestamp='2026-10-28T10:00:30Z'),
                 quota(70,'2026-10-28T10:00:40Z'), quota(75,'2026-10-28T10:00:50Z'),
                 event('task_complete', turn_id='child-turn', timestamp='2026-10-28T10:01:00Z')]
        write_jsonl(self.sessions / 'child.jsonl', child)
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        comparisons = self.report()['runtime_evidence']['quota_comparisons']
        self.assertEqual({c['session_id']:c['delta_percentage_points'] for c in comparisons}, {'root':2, 'child':5})

    def test_non_result_truncation_does_not_claim_a_result_was_truncated(self):
        rows = root_rows()
        rows.insert(3, event('token_count', truncated=True, rate_limits={'limit_id':'main'}))
        write_jsonl(self.sessions / 'root.jsonl', rows)
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        record = next(e for e in self.report()['events'] if e['event_type'] == 'token_count')
        self.assertNotIn('result_truncated', record)

    def test_mixed_result_text_size_does_not_claim_complete_result_size(self):
        rows = root_rows()
        rows.insert(3, {'type':'response_item','payload':{'type':'function_call_output','call_id':'x','output':[{'type':'text','text':'text'},{'type':'image','data':'PRIVATE'}]}})
        write_jsonl(self.sessions / 'root.jsonl', rows)
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report()
        self.assertEqual(report['runtime_evidence']['tools']['known_result_bytes'], 0)
        self.assertEqual(report['runtime_evidence']['tools']['unknown_result_bytes_count'], 1)
        self.assertNotIn('PRIVATE', json.dumps(report))

    def test_documented_app_server_window_duration_is_retained_and_compared(self):
        rows = root_rows()
        for index, (primary, secondary) in enumerate(((10, 20), (17, 24))):
            rows.insert(3 + index, {'type':'event_msg', 'timestamp':f'2026-10-28T10:00:0{index + 1}Z',
                'payload':{'type':'token_count', 'rateLimits':{'limitId':'main', 'planType':'pro',
                    'primary':{'usedPercent':primary, 'windowDurationMins':300, 'resetsAt':42},
                    'secondary':{'usedPercent':secondary, 'windowDurationMins':10080, 'resetsAt':84}}}})
        rows.insert(5, {'type':'event_msg', 'payload':{'type':'token_count', 'rateLimits':{
            'limitId':'invalid', 'primary':{'windowDurationMins':'PRIVATE_BAD_VALUE'},
            'secondary':{'windowDurationMins':True}}}})
        write_jsonl(self.sessions / 'root.jsonl', rows)
        proc = self.run_audit()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.report()
        quotas = [e['quota'] for e in report['events'] if 'quota' in e]
        self.assertEqual(quotas[0]['primary'].get('window_minutes'), 300)
        self.assertEqual(quotas[0]['secondary'].get('window_minutes'), 10080)
        self.assertNotIn('window_minutes', quotas[2]['primary'])
        self.assertNotIn('window_minutes', quotas[2]['secondary'])
        self.assertNotIn('PRIVATE_BAD_VALUE', json.dumps(report))
        comparisons = {c['window']:c['delta_percentage_points'] for c in report['runtime_evidence']['quota_comparisons'] if c['bucket_id'] == 'main'}
        self.assertEqual(comparisons, {'primary':7, 'secondary':4})
