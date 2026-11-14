import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/codex-token-audit/scripts'
RATES = SCRIPTS.parent / 'references/standard-short-2026-10-28.json'
sys.path.insert(0, str(SCRIPTS))


def fixture_report(counts=(50000, 100000, 300000, 500000)):
    requests = []
    for i, inp in enumerate(counts):
        cached = inp * 9 // 10
        requests.append(dict(response_id='r' + str(i), session_id='root', agent='root',
                             turn_id='turn-a', root_turn_id='turn-a', parent_id=None,
                             model='gpt-6.1-sol', effort='high', timestamp=None,
                             source={'file': 'source-0001', 'line': 4 + 2*i},
                             input_tokens=inp, cached_input_tokens=cached, new_tokens=inp-cached,
                             output_tokens=1000, total_tokens=inp+1000,
                             reasoning_output_tokens=100, cache_write_input_tokens=0))
    return dict(scope={'session_id':'root', 'turn_id':'turn-a', 'windows':[dict(turn_id='turn-a',task_start_line=2,task_terminal_line=12)]},
                coverage={'status':'complete','warnings':[]}, provenance={'models':'client turn_context telemetry; not server attestation'},
                requests=requests, events=[], compaction_markers=[
                    dict(event_id='c1',session_id='root',root_turn_id='turn-a',source_file='source-0001',line=5,timestamp=None),
                    dict(event_id='c2',session_id='root',root_turn_id='turn-a',source_file='source-0001',line=9,timestamp=None)],
                reconciliation={'status':'not_comparable'})


class BundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = __import__('bundle')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name) / 'run'

    def build(self, report=None, **kwargs):
        self.bundle.write_bundle(report or fixture_report(), self.out, rates_path=RATES, run_id='fixture-run', **kwargs)
        return json.loads((self.out/'audit.json').read_text())

    def test_complete_bundle_exact_aggregates_and_disjoint_segments(self):
        audit = self.build()
        manifest = self.bundle.validate_bundle(self.out)
        self.assertEqual(manifest['schema_version'], 2)
        self.assertEqual(audit['raw_totals']['input_tokens'], 950000)
        self.assertEqual(audit['normalized_total']['values']['total_tokens'], '157750')
        metrics = audit['root_context_metrics']
        self.assertEqual(metrics['mean_input_tokens'], '237500')
        self.assertEqual(metrics['median_input_tokens'], '200000')
        self.assertEqual(metrics['p95_input_tokens'], 500000)
        self.assertEqual(metrics['cache_hit_fraction']['decimal'], '0.9')
        self.assertEqual([s['raw_totals']['input_tokens'] for s in audit['compaction_segments']], [50000,400000,500000])
        self.assertEqual(sum(s['request_count'] for s in audit['compaction_segments']),4)
        self.assertEqual(sum(b['request_count'] for b in metrics['input_size_buckets']),4)
        self.assertNotIn('requests',audit)
        self.assertFalse((self.out/'audit.md').exists())
        self.assertEqual(len((self.out/'requests.jsonl').read_text().splitlines()),4)
        self.assertEqual(os.stat(self.out).st_mode & 0o777,0o700)
        self.assertTrue(all(os.stat(p).st_mode & 0o777 == 0o600 for p in self.out.iterdir()))

    def test_300_high_cache_requests_are_complete_and_deterministic(self):
        report = fixture_report((500000,)*300)
        self.build(report)
        second=Path(self.temp.name)/'second'
        self.bundle.write_bundle(report, second, rates_path=RATES, run_id='fixture-run')
        for name in ('audit.json','requests.jsonl','events.jsonl','pricing.json','schema.json'):
            self.assertEqual((self.out/name).read_bytes(),(second/name).read_bytes(),name)
        audit=json.loads((self.out/'audit.json').read_text())
        self.assertEqual(audit['raw_totals']['input_tokens'],150000000)
        self.assertEqual(audit['root_context_metrics']['calls'],300)
        self.assertEqual(len((self.out/'requests.jsonl').read_text().splitlines()),300)

    def test_validation_detects_tampered_bytes_and_rehashed_wrong_totals(self):
        self.build()
        p=self.out/'audit.json'
        audit=json.loads(p.read_text());audit['raw_totals']['input_tokens']+=1
        p.write_text(json.dumps(audit)+'\n')
        with self.assertRaises(ValueError):self.bundle.validate_bundle(self.out)
        m=self.out/'manifest.json';manifest=json.loads(m.read_text())
        entry=next(f for f in manifest['files'] if f['name']=='audit.json')
        entry.update(size_bytes=p.stat().st_size,sha256=hashlib.sha256(p.read_bytes()).hexdigest())
        m.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):self.bundle.validate_bundle(self.out)

    def test_no_overwrite_symlink_escape_and_private_source_rejected(self):
        self.build()
        with self.assertRaises((ValueError,OSError)):
            self.bundle.write_bundle(fixture_report(),self.out,rates_path=RATES)
        link=Path(self.temp.name)/'link';link.symlink_to(self.out,target_is_directory=True)
        with self.assertRaises(ValueError):self.bundle.validate_bundle(link)
        report=fixture_report();report['requests'][0]['source']['file']='/private/SECRET_SENTINEL/log.jsonl'
        with self.assertRaises(ValueError):self.bundle.write_bundle(report,Path(self.temp.name)/'bad',rates_path=RATES)

    def test_missing_prices_remain_partial_root_and_children_partition_total(self):
        report=fixture_report();report['requests'][1].update(session_id='child',agent='worker',parent_id='root',model='unknown-model',cache_write_input_tokens=None)
        audit=self.build(report)
        self.assertFalse(audit['normalized_total']['complete'])
        self.assertEqual(audit['summaries']['children']['request_count'],1)
        self.assertEqual(audit['summaries']['root']['raw_totals']['input_tokens'],850000)
        self.assertEqual(audit['summaries']['children']['raw_totals']['input_tokens'],100000)
        self.assertEqual(audit['summaries']['all']['unpriced_request_count'],1)

    def test_timestamp_gaps_do_not_reverse_proven_root_source_order(self):
        report=fixture_report()
        report['requests'][0]['timestamp']=10.0
        report['requests'][1]['timestamp']=None
        report['requests']=[report['requests'][1],report['requests'][0],*report['requests'][2:]]
        audit=self.build(report)
        self.assertEqual(audit['root_context_metrics']['first_input_tokens'],50000)
        self.assertEqual(audit['root_context_metrics']['last_input_tokens'],500000)

    def test_validation_rejects_rehashed_per_agent_aggregate_corruption(self):
        self.build()
        path=self.out/'audit.json';value=json.loads(path.read_text())
        value['rows']=[];path.write_text(json.dumps(value)+'\n')
        manifest_path=self.out/'manifest.json';manifest=json.loads(manifest_path.read_text())
        for entry in manifest['files']:
            if entry['name']=='audit.json':entry.update(size_bytes=path.stat().st_size,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):self.bundle.validate_bundle(self.out)

    def test_segments_never_compare_lines_of_different_archive_streams(self):
        report=fixture_report()
        report['requests'][1]['source']={'file':'source-revision','line':5}
        report['requests'][1]['order_uncertain']=True
        audit=self.build(report)
        self.assertEqual(sum(s['request_count'] for s in audit['compaction_segments']),4)
        self.assertIsNone(audit['root_context_metrics']['first_input_tokens'])
        self.assertEqual(audit['raw_totals']['input_tokens'],950000)

    def test_real_bundle_to_fake_drive_retries_without_recollection(self):
        from test_drive_export import FakeDrive, load_module
        self.build()
        transport=FakeDrive();transport.ambiguous_name='events.jsonl'
        exporter=load_module()
        self.assertEqual(exporter.upload_bundle(self.out,'destination',transport)['status'],'blocked')
        receipt=exporter.upload_bundle(self.out,'destination',transport)
        self.assertEqual(receipt['status'],'complete')
        self.assertEqual(len(receipt['files']),6)
        self.assertEqual(sum(p.get('name')=='events.jsonl' for p in transport.objects.values()),1)
        self.bundle.validate_bundle(self.out)

    def test_copied_skill_collects_offline_without_swarm_or_drive(self):
        from test_token_audit import root_rows, write_jsonl
        isolated=Path(self.temp.name)/'isolated'
        shutil.copytree(SCRIPTS.parent,isolated)
        sessions=Path(self.temp.name)/'sessions'
        write_jsonl(sessions/'root.jsonl',root_rows())
        script=isolated/'scripts/audit_usage.py'
        harness="import socket,runpy,sys; socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('offline')); sys.path.insert(0,sys.argv[1]); sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name='__main__')"
        result=subprocess.run([sys.executable,'-I','-c',harness,str(script.parent),str(script),
                               '--session-id','root','--turn-id','turn-a','--sessions-dir',str(sessions),'--output-dir',str(self.out)],
                              cwd=self.temp.name,env={**os.environ,'CODEX_HOME':str(Path(self.temp.name)/'codex')},capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('NOT CONFIGURED',result.stdout)
        self.assertNotIn('audit.md',result.stdout)
        self.assertLess(len(result.stdout.splitlines()),5)
        self.assertEqual(self.bundle.validate_bundle(self.out)['schema_version'],2)


if __name__=='__main__':unittest.main()
