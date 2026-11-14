import argparse
import importlib.util
import json
import tempfile
import sys
import unittest
from pathlib import Path

from test_token_audit import SCRIPT, event, root_rows, usage, write_jsonl

spec = importlib.util.spec_from_file_location('audit_collection', SCRIPT)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class CollectionTests(unittest.TestCase):
    def test_separate_turn_windows_exclude_intervening_turn_and_remove_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / 'private-sessions'
            rows = root_rows()[:8]
            rows += [event('task_started', turn_id='other', timestamp='2026-10-28T10:03:00Z'),
                     usage('excluded', 999, 0, 1), event('task_complete', turn_id='other', timestamp='2026-10-28T10:04:00Z'),
                     event('task_started', turn_id='turn-b', timestamp='2026-10-28T10:05:00Z'),
                     {'type': 'turn_context', 'payload': {'turn_id': 'turn-b', 'model': 'gpt-6.1-sol', 'effort': 'high'}}]
            last = usage('b', 10, 2, 1); last['payload']['turn_id'] = 'turn-b'
            rows += [last, event('task_complete', turn_id='turn-b', timestamp='2026-10-28T10:06:00Z')]
            write_jsonl(sessions / 'root.jsonl', rows)
            args = argparse.Namespace(sessions_dir=str(sessions), sessions_dir_explicit=True, session_id='root', turn_id=['turn-a', 'turn-b'], root_only=False, rates=None, markdown=False)
            report = audit.collect_task(args)
            self.assertEqual({r['response_id'] for r in report['requests']}, {'r1', 'r2', 'b'})
            self.assertEqual(report['raw_totals']['input_tokens'], 200)
            self.assertEqual([w['turn_id'] for w in report['scope']['windows']], ['turn-a', 'turn-b'])
            self.assertEqual(report['scope']['windows'][0]['source_file'], 'source-0001')
            self.assertNotIn(tmp, json.dumps(report))

    def test_result_metadata_is_retained_without_body_and_scoped_to_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / 'sessions'
            rows = root_rows()
            rows.insert(3, {'type': 'response_item', 'timestamp': '2026-10-28T10:00:01Z', 'payload': {'type': 'function_call', 'name': 'exec_command', 'call_id': 'call1', 'arguments': 'SECRET_COMMAND'}})
            rows.insert(4, {'type': 'response_item', 'timestamp': '2026-10-28T10:00:02Z', 'payload': {'type': 'function_call_output', 'call_id': 'call1', 'output': 'SECRET_RESULT é'}})
            write_jsonl(sessions / 'root.jsonl', rows)
            args = argparse.Namespace(sessions_dir=str(sessions), sessions_dir_explicit=True, session_id='root', turn_id=['turn-a'], root_only=False, rates=None, markdown=False)
            report = audit.collect_task(args)
            result = next(e for e in report['events'] if e['event_type'] == 'function_call_output')
            self.assertEqual((result['result_chars'], result['result_bytes']), (15, 16))
            self.assertEqual(result['call_id'], 'call1')
            self.assertNotIn('SECRET_', json.dumps(report))

    def test_tool_history_is_bounded_by_previous_request_and_fork_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / 'sessions'
            rows = root_rows()[:8]
            rows.insert(3, {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'spawn_agent', 'call_id': 'sp', 'arguments': json.dumps({'fork_turns': 'none', 'message': 'SECRET_PROMPT'})}})
            rows.insert(5, {'type': 'event_msg', 'payload': {'type': 'tool_call', 'name': 'search'}})
            write_jsonl(sessions / 'root.jsonl', rows)
            args = argparse.Namespace(sessions_dir=str(sessions), sessions_dir_explicit=True, session_id='root', turn_id=['turn-a'], root_only=False, rates=None, markdown=False)
            report = audit.collect_task(args)
            reqs = {r['response_id']: r for r in report['requests']}
            self.assertEqual(reqs['r1']['preceding_tool_names'], ['spawn_agent'])
            self.assertEqual(reqs['r2']['preceding_tool_names'], ['search'])
            spawn = next(e for e in report['events'] if e.get('name') == 'spawn_agent')
            self.assertEqual(spawn['fork_turns'], 'none')
            self.assertNotIn('SECRET_PROMPT', json.dumps(report))

    def test_equal_timestamp_compactions_are_distinct_and_archive_events_deduplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / 'sessions'
            rows = root_rows()[:8]
            rows.insert(5, {'type': 'compacted', 'payload': {'timestamp': '2026-10-28T10:01:00Z'}})
            write_jsonl(sessions / 'root.jsonl', rows)
            write_jsonl(sessions / 'copy.jsonl', rows)
            args = argparse.Namespace(sessions_dir=str(sessions), sessions_dir_explicit=True, session_id='root', turn_id=['turn-a'], root_only=False, rates=None, markdown=False)
            report = audit.collect_task(args)
            self.assertEqual(len(report['compaction_markers']), 2)
            self.assertEqual(len([e for e in report['events'] if e['event_type'] == 'compacted']), 2)

    def test_mixed_result_counts_text_and_flags_images_without_retaining_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / 'sessions'
            rows = root_rows()[:8]
            rows.insert(3, {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'x', 'output': [{'type': 'text', 'text': 'hé'}, {'type': 'image', 'data': 'SECRET_IMAGE'}], 'truncated': True}})
            write_jsonl(sessions / 'root.jsonl', rows)
            args = argparse.Namespace(sessions_dir=str(sessions), sessions_dir_explicit=True, session_id='root', turn_id=['turn-a'], root_only=False, rates=None, markdown=False)
            result = next(e for e in audit.collect_task(args)['events'] if e['event_type'] == 'function_call_output')
            self.assertEqual((result['result_chars'], result['result_bytes']), (2, 3))
            self.assertTrue(result['mixed'] and result['images'] and result['truncated'])
            self.assertNotIn('SECRET_IMAGE', json.dumps(result))

    def test_private_agent_path_does_not_escape_to_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / 'sessions'
            write_jsonl(sessions / 'root.jsonl', root_rows()[:8])
            child = [{'type': 'session_meta', 'payload': {'id': 'child', 'agent_path': '/private/SECRET_PATH', 'source': {'subagent': {'thread_spawn': {'parent_thread_id': 'root'}}}}}, event('task_started', turn_id='c', timestamp='2026-10-28T10:00:10Z'), {'type': 'turn_context', 'payload': {'turn_id': 'c', 'model': 'm', 'effort': 'high'}}]
            record = usage('child-r', 2, 0, 1); record['payload']['turn_id'] = 'c'; record['timestamp'] = '2026-10-28T10:00:20Z'
            child.extend([record, event('task_complete', turn_id='c', timestamp='2026-10-28T10:00:30Z')])
            write_jsonl(sessions / 'child.jsonl', child)
            args = argparse.Namespace(sessions_dir=str(sessions), sessions_dir_explicit=True, session_id='root', turn_id=['turn-a'], root_only=False, rates=None, markdown=False)
            self.assertNotIn('SECRET_PATH', json.dumps(audit.collect_task(args)))

    def test_events_reference_adjacent_requests_within_same_source_and_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / 'sessions'
            rows = root_rows()[:8]
            rows.insert(5, {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'search', 'call_id': 'c'}})
            write_jsonl(sessions / 'root.jsonl', rows)
            args = argparse.Namespace(sessions_dir=str(sessions), sessions_dir_explicit=True, session_id='root', turn_id=['turn-a'], root_only=False, rates=None, markdown=False)
            report = audit.collect_task(args)
            item = next(e for e in report['events'] if e.get('call_id') == 'c')
            self.assertEqual((item['previous_response_id'], item['next_response_id']), ('r1', 'r2'))
            self.assertIn(item['event_id'], next(r for r in report['requests'] if r['response_id'] == 'r2')['event_ids'])

    def test_unterminated_valid_result_line_is_flagged_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'result.jsonl'
            path.write_text(json.dumps({'type': 'response_item', 'payload': {'type': 'function_call_output', 'output': 'x'}}))
            _, row, _, truncated = next(audit._json_lines(path))
            self.assertTrue(truncated)
            self.assertTrue(row['payload']['truncated'])

    def test_shifted_archived_copies_deduplicate_events_and_compactions(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / 'sessions'
            rows = root_rows()[:8]
            rows.insert(5, {'type': 'compacted', 'payload': {'timestamp': '2026-10-28T10:01:00Z'}})
            write_jsonl(sessions / 'a.jsonl', rows)
            write_jsonl(sessions / 'z.jsonl', rows[:1] + [{'type': 'unrelated'}] + rows[1:])
            args = argparse.Namespace(sessions_dir=str(sessions), sessions_dir_explicit=True, session_id='root', turn_id=['turn-a'], root_only=False, rates=None, markdown=False)
            report = audit.collect_task(args)
            self.assertEqual(len(report['compaction_markers']), 2)
            self.assertEqual(len([e for e in report['events'] if e['event_type'] == 'compacted']), 2)
            self.assertEqual(len([e for e in report['events'] if e['event_type'] == 'task_started']), 1)
            self.assertEqual({r['source']['file'] for r in report['requests']}, {report['scope']['source_file']})

    def test_spawn_result_links_observed_fork_only_to_confirmed_child_and_warns_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / 'sessions'
            rows = root_rows()[:8]
            rows.insert(3, {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'spawn_agent', 'call_id': 'sp', 'arguments': json.dumps({'fork_turns': 'none', 'model': 'gpt-6-luna', 'reasoning_effort': 'low', 'message': 'SECRET_PROMPT'})}})
            rows.insert(4, {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'sp', 'output': json.dumps({'agent_id': 'child'})}})
            rows.insert(5, {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'spawn_agent', 'call_id': 'sp2', 'arguments': '{}'}})
            rows.insert(6, {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'sp2', 'output': json.dumps({'agent_id': 'missing'})}})
            write_jsonl(sessions / 'root.jsonl', rows)
            child = [{'type': 'session_meta', 'payload': {'id': 'child', 'source': {'subagent': {'thread_spawn': {'parent_thread_id': 'root'}}}}}, event('task_started', turn_id='c', timestamp='2026-10-28T10:00:10Z'), event('task_complete', turn_id='c', timestamp='2026-10-28T10:00:30Z')]
            write_jsonl(sessions / 'child.jsonl', child)
            args = argparse.Namespace(sessions_dir=str(sessions), sessions_dir_explicit=True, session_id='root', turn_id=['turn-a'], root_only=False, rates=None, markdown=False)
            report = audit.collect_task(args)
            metadata = next(a for a in report['agent_metadata'] if a['session_id'] == 'child')
            self.assertEqual((metadata['fork_turns'], metadata['spawn_model'], metadata['spawn_effort']), ('none', 'gpt-6-luna', 'low'))
            self.assertTrue(any('missing' in w.lower() for w in report['coverage']['warnings']))
            self.assertNotIn('SECRET_PROMPT', json.dumps(report))

    def test_requests_keep_source_order_when_timestamps_are_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / 'sessions'
            rows = root_rows()[:8]
            rows[3]['timestamp'] = '2026-10-28T10:00:10Z'
            write_jsonl(sessions / 'root.jsonl', rows)
            args = argparse.Namespace(sessions_dir=str(sessions), sessions_dir_explicit=True, session_id='root', turn_id=['turn-a'], root_only=False, rates=None, markdown=False)
            report = audit.collect_task(args)
            self.assertEqual([r['response_id'] for r in report['requests']], ['r1', 'r2'])

    def test_revision_from_another_copy_marks_cross_source_order_uncertain(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / 'sessions'
            rows = root_rows()[:8]
            rows[3]['timestamp'] = '2026-10-28T10:00:10Z'
            copy_rows = json.loads(json.dumps(rows))
            copy_rows[3]['timestamp'] = '2026-10-28T10:00:11Z'
            copy_rows[3]['payload']['usage']['input_tokens'] = 111
            copy_rows[3]['payload']['usage']['total_tokens'] = 125
            write_jsonl(sessions / 'a.jsonl', rows)
            write_jsonl(sessions / 'z.jsonl', copy_rows)
            args = argparse.Namespace(sessions_dir=str(sessions), sessions_dir_explicit=True, session_id='root', turn_id=['turn-a'], root_only=False, rates=None, markdown=False)
            report = audit.collect_task(args)
            self.assertTrue(next(r for r in report['requests'] if r['response_id'] == 'r1')['order_uncertain'])
            self.assertTrue(report['compaction_markers'][0]['order_uncertain'])

    def test_path_values_in_event_identifiers_and_model_are_not_exported(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / 'sessions'
            rows = root_rows()[:8]
            rows[2]['payload']['model'] = '/private/SECRET_MODEL'
            rows.insert(3, {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'test', 'call_id': '/private/SECRET_CALL', 'result_id': 'https://SECRET_URL/path'}})
            write_jsonl(sessions / 'root.jsonl', rows)
            args = argparse.Namespace(sessions_dir=str(sessions), sessions_dir_explicit=True, session_id='root', turn_id=['turn-a'], root_only=False, rates=None, markdown=False)
            result = json.dumps(audit.collect_task(args))
            self.assertNotIn('SECRET_', result)

    def test_path_session_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / 'sessions'
            rows = root_rows()[:8]
            rows[0]['payload']['session_id'] = '/private/SECRET_SESSION'
            write_jsonl(sessions / 'root.jsonl', rows)
            args = argparse.Namespace(sessions_dir=str(sessions), sessions_dir_explicit=True, session_id='/private/SECRET_SESSION', turn_id=['turn-a'], root_only=False, rates=None, markdown=False)
            with self.assertRaises(audit.AuditError):
                audit.collect_task(args)

    def test_multiwindow_coverage_exposes_each_window_without_false_aggregate(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / 'sessions'
            rows = root_rows()[:8]
            rows += [event('task_started', turn_id='turn-b', timestamp='2026-10-28T10:05:00Z'), {'type': 'turn_context', 'payload': {'turn_id': 'turn-b', 'model': 'm', 'effort': 'high'}}]
            valid = usage('b', 10, 2, 1); valid['payload']['turn_id'] = 'turn-b'
            invalid = usage('bad', 1, 2, 1); invalid['payload']['turn_id'] = 'turn-b'
            rows += [valid, invalid, event('task_complete', turn_id='turn-b', timestamp='2026-10-28T10:06:00Z')]
            write_jsonl(sessions / 'root.jsonl', rows)
            args = argparse.Namespace(sessions_dir=str(sessions), sessions_dir_explicit=True, session_id='root', turn_id=['turn-a', 'turn-b'], root_only=False, rates=None, markdown=False)
            coverage = audit.collect_task(args)['coverage']
            self.assertEqual([w['invalid_records'] for w in coverage['per_window']], [0, 1])
            self.assertIsNone(coverage['invalid_records'])
            self.assertEqual(coverage['status'], 'incomplete')

    def test_cli_delivery_output_is_compact_when_receipt_has_many_files(self):
        import contextlib
        import io
        import types
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / 'sessions'
            write_jsonl(sessions / 'root.jsonl', root_rows()[:8])
            fake = types.SimpleNamespace(upload_bundle=lambda *_: {'status': 'complete', 'folder_link': 'https://drive.example/folder', 'files': {str(i): 'PRIVATE_DETAIL' for i in range(100)}})
            output = io.StringIO()
            with patch.dict('os.environ', {'CODEX_HOME': tmp}), patch.dict('sys.modules', {'drive_export': fake}), patch.object(sys, 'path', [str(SCRIPT.parent), *sys.path]), contextlib.redirect_stdout(output):
                code = audit.main(['--session-id', 'root', '--turn-id', 'turn-a', '--sessions-dir', str(sessions), '--output-dir', str(Path(tmp) / 'out'), '--drive-folder-id', 'folder', '--drive-transport', 'gws'])
            self.assertEqual(code, 0)
            self.assertIn('https://drive.example/folder', output.getvalue())
            self.assertNotIn('PRIVATE_DETAIL', output.getvalue())
            self.assertLessEqual(len(output.getvalue().splitlines()), 4)
