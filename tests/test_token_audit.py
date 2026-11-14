import json
import os
import subprocess
import sys
import tempfile
import unittest
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/codex-token-audit/scripts/audit_usage.py"


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def event(kind, **fields):
    return {"type": "event_msg", "payload": {"type": kind, **fields}}


def usage(response_id, input_tokens, cached, output, **extra):
    return {
        "type": "token_usage_record",
        "payload": {
            "response_id": response_id,
            "session_id": "root",
            "turn_id": "turn-a",
            "usage": {
                "input_tokens": input_tokens,
                "cached_input_tokens": cached,
                "output_tokens": output,
                **extra,
            },
        },
    }


def root_rows():
    return [
        {"type": "session_meta", "payload": {"session_id": "root"}},
        event("task_started", turn_id="turn-a", timestamp="2026-10-28T10:00:00Z"),
        {"type": "turn_context", "payload": {"turn_id": "turn-a", "model": "gpt-6.1-sol", "reasoning_effort": "high"}},
        usage("r1", 110, 60, 14, reasoning_output_tokens=4, total_tokens=124),
        {"type": "compacted", "payload": {"timestamp": "2026-10-28T10:01:00Z"}},
        {"type": "turn_context", "payload": {"turn_id": "turn-a", "model": "gpt-6-luna", "reasoning_effort": "low"}},
        usage("r2", 80, 30, 20, reasoning_output_tokens=6, total_tokens=100),
        event("task_complete", turn_id="turn-a", timestamp="2026-10-28T10:02:00Z"),
        event("task_started", turn_id="turn-current", timestamp="2026-10-28T10:03:00Z"),
        {"type": "turn_context", "payload": {"turn_id": "turn-current", "model": "gpt-6.1-sol"}},
        usage("audit-self", 999999, 0, 999),
    ]


class TokenAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.sessions = self.base / "sessions"
        self.output_dir = self.base / "private-output"
        self.out = self.output_dir
        self.root_id = "root"
        write_jsonl(self.sessions / "root.jsonl", root_rows())

    def tearDown(self):
        self.temp.cleanup()

    def run_audit(self, *extra, session="root", turn="turn-a"):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--session-id", session, "--turn-id", turn,
             "--sessions-dir", str(self.sessions), "--output-dir", str(self.output_dir), "--run-id", "fixture-run", "--markdown", *extra],
            text=True, capture_output=True,
        )

    def report(self):
        report = json.loads((self.out / "audit.json").read_text(encoding="utf-8"))
        report["requests"] = [json.loads(line) for line in (self.out / "requests.jsonl").read_text(encoding="utf-8").splitlines()]
        report["events"] = [json.loads(line) for line in (self.out / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        return report

    def test_default_markdown_is_one_table_of_sol_input_equivalents(self):
        rows = root_rows()[:3]
        for index, (model, input_tokens, cached, output) in enumerate([
            ("gpt-6.1-sol", 3_000_000, 2_000_000, 200_000),
            ("gpt-6.1-terra", 6_000_000, 4_000_000, 400_000),
            ("gpt-6-luna", 40_000_000, 20_000_000, 4_000_000),
        ]):
            rows.append({"type": "turn_context", "payload": {"turn_id": "turn-a", "model": model, "reasoning_effort": "high"}})
            rows.append(usage(f"priced-{index}", input_tokens, cached, output, cache_write_input_tokens=0))
        rows.append(root_rows()[7])
        write_jsonl(self.sessions / "root.jsonl", rows)
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        markdown = (self.out / "audit.md").read_text(encoding="utf-8")
        self.assertEqual(markdown.count("| Agent / model / effort |"), 1)
        self.assertIn("millions of ordinary input-token equivalents", markdown)
        self.assertIn("gpt-6.1-sol", markdown)
        for model in ("gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-luna"):
            self.assertIn(f"| root / {model} / high | 1.0 | 0.1 | 1.1 | 1.0 | 2.1 |", markdown)
        self.assertIn("| **Total** | 3.0 | 0.3 | 3.3 | 3.0 | 6.3 |", markdown)
        self.assertNotIn("Raw totals", markdown)
        report = self.report()
        self.assertEqual(report["raw_totals"]["total_tokens"], 53_600_000)
        self.assertEqual(float(report["normalized_total"]["values"]["total_tokens"]), 6_300_000)
        self.assertEqual(report["provenance"]["rates"]["verified_at"], "2026-10-28")

    def test_markdown_labels_each_api_price_weight(self):
        self.assertEqual(self.run_audit().returncode, 0)
        markdown = (self.out / "audit.md").read_text(encoding="utf-8")
        self.assertIn("| Model | New input | Cached input | Output |", markdown)
        self.assertIn("| gpt-6.1-sol | 1 | 0.05 | 5 |", markdown)
        self.assertIn("| gpt-6.1-terra | 0.5 | 0.025 | 2.5 |", markdown)
        self.assertIn("| gpt-6-luna | 0.05 | 0.005 | 0.25 |", markdown)
        self.assertIn("2026-10-28", markdown)
        self.assertIn("ordinary input token", markdown)

    def test_equivalent_total_is_rounded_after_aggregation(self):
        rows = root_rows()
        rows[3] = usage("r1", 26_000, 0, 0, cache_write_input_tokens=0)
        rows[6] = usage("r2", 520_000, 0, 0, cache_write_input_tokens=0)
        write_jsonl(self.sessions / "root.jsonl", rows)
        self.assertEqual(self.run_audit().returncode, 0)
        markdown = (self.out / "audit.md").read_text(encoding="utf-8")
        self.assertIn("| root / gpt-6.1-sol / high | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |", markdown)
        self.assertIn("| root / gpt-6-luna / low | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |", markdown)
        self.assertIn("| **Total** | 0.1 | 0.0 | 0.1 | 0.0 | 0.1 |", markdown)
        self.assertEqual(float(self.report()["normalized_total"]["values"]["total_tokens"]), 52_000)

    def test_scopes_root_turn_models_compaction_and_excludes_current_audit(self):
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report()
        self.assertEqual(report["scope"]["session_id"], "root")
        self.assertEqual(report["scope"]["turn_id"], "turn-a")
        self.assertEqual(len(report["requests"]), 2)
        self.assertNotIn("audit-self", json.dumps(report))
        self.assertEqual([r["model"] for r in report["requests"]], ["gpt-6.1-sol", "gpt-6-luna"])
        self.assertEqual(report["requests"][0]["new_tokens"], 50)
        self.assertEqual(report["requests"][0]["input_tokens"], 110)
        self.assertEqual(report["requests"][0]["output_tokens"], 14)
        self.assertEqual(report["requests"][0]["reasoning_output_tokens"], 4)
        self.assertEqual(len([e for e in report["events"] if e["event_type"] == "compacted"]), 1)
        self.assertIn("/", (self.out / "audit.md").read_text(encoding="utf-8"))

    def test_nested_and_archived_children_are_bounded_to_task_interval(self):
        child = [
            {"type": "session_meta", "payload": {"session_id": "child", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "root"}}}, "agent_path": "root/researcher"}},
            event("task_started", turn_id="child-turn", timestamp="2026-10-28T10:00:30Z"),
            {"type": "turn_context", "payload": {"turn_id": "child-turn", "model": "gpt-6.1-terra", "reasoning_effort": "medium"}},
            {**usage("child-r", 25, 10, 5, total_tokens=30), "timestamp": "2026-10-28T10:01:00Z"},
            event("task_complete", turn_id="child-turn", timestamp="2026-10-28T10:01:30Z"),
            event("task_started", turn_id="later", timestamp="2026-10-28T10:04:00Z"),
            usage("child-later", 300, 0, 20),
        ]
        write_jsonl(self.sessions / "child.jsonl", child)
        nested = [
            {"type": "session_meta", "payload": {"session_id": "nested", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "child"}}}}},
            event("task_started", turn_id="n", timestamp="2026-10-28T10:00:45Z"),
            {"type": "turn_context", "payload": {"turn_id": "n", "model": "unknown-model"}},
            {**usage("nested-r", 8, 2, 3), "timestamp": "2026-10-28T10:01:00Z"},
            event("task_complete", turn_id="n", timestamp="2026-10-28T10:01:20Z"),
        ]
        write_jsonl(self.sessions / "archived_sessions" / "nested.jsonl", nested)
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report()
        self.assertEqual({r["response_id"] for r in report["requests"]}, {"r1", "r2", "child-r", "nested-r"})
        self.assertEqual(len({r["agent"] for r in report["requests"]}), 3)

    def test_duplicate_and_revised_response_keeps_latest_chronological_revision(self):
        root = root_rows()
        revised = usage("r1", 130, 80, 18, reasoning_output_tokens=5, total_tokens=148)
        revised["payload"]["timestamp"] = "2026-10-28T10:00:20Z"
        root.insert(4, revised)
        write_jsonl(self.sessions / "root.jsonl", root)
        write_jsonl(self.sessions / "archived_sessions" / "copy.jsonl", root)
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report()
        r1 = [r for r in report["requests"] if r["response_id"] == "r1"]
        self.assertEqual(len(r1), 1)
        self.assertEqual(r1[0]["input_tokens"], 130)
        self.assertTrue(any("revised" in str(w).lower() or "revision" in str(w).lower() for w in report["coverage"]["warnings"]))

    def test_unknown_and_invalid_fields_are_incomplete_not_zero(self):
        rows = root_rows()
        rows.insert(7, {"type": "token_usage_record", "payload": {"response_id": "missing", "turn_id": "turn-a", "usage": {"input_tokens": 10, "output_tokens": 1}}})
        rows.insert(8, {"type": "token_usage_record", "payload": {"response_id": "bool", "turn_id": "turn-a", "usage": {"input_tokens": True, "cached_input_tokens": 0, "output_tokens": 1}}})
        write_jsonl(self.sessions / "root.jsonl", rows)
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report()
        self.assertEqual(len(report["requests"]), 2)
        self.assertIn("incomplete", report["coverage"]["status"])

    def test_rates_use_exact_independent_factors_and_unknown_model_is_partial(self):
        rates = self.base / "rates.json"
        rates.write_text(json.dumps({"scenario": "fixture", "reference_model": "ref", "models": {
            "ref": {"input": 2, "cached": 1, "output": 4},
            "gpt-6.1-sol": {"input": 1, "cached": 0.5, "output": 2},
            "gpt-6-luna": {"input": 0.1, "cached": 0.01, "output": 0.5},
        }}), encoding="utf-8")
        # Inject an unknown-model request into the selected turn.
        rows = root_rows()
        rows[5]["payload"]["model"] = "unknown-model"
        rows[3]["payload"]["usage"]["cache_write_input_tokens"] = 0
        rows[6]["payload"]["usage"]["cache_write_input_tokens"] = 0
        write_jsonl(self.sessions / "root.jsonl", rows)
        result = self.run_audit("--rates", str(rates))
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report()
        first = report["requests"][0]
        self.assertEqual(first["normalized"]["new_tokens"], "25")
        self.assertEqual(float(first["normalized"]["cached_tokens"]), 15.0)
        self.assertIsNone(report["requests"][1]["normalized"])

    def test_unknown_cache_write_prevents_complete_normalized_cost(self):
        rates = self.base / "rates.json"
        rates.write_text(json.dumps({"reference_model": "m", "models": {"gpt-6.1-sol": {"input": 1, "cached": 0.1, "output": 2}, "gpt-6-luna": {"input": 1, "cached": 0.1, "output": 2}, "m": {"input": 1, "cached": 0.1, "output": 2}}}), encoding="utf-8")
        result = self.run_audit("--rates", str(rates))
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report()
        self.assertIn("cache-write", json.dumps(report["coverage"]).lower())

    def test_malformed_and_truncated_lines_warn_without_leaking_source(self):
        path = self.sessions / "root.jsonl"
        path.write_text(path.read_text(encoding="utf-8") + '{"secret":"PROMPT_NEVER_LEAK"}\nnot-json-PROMPT_NEVER_LEAK', encoding="utf-8")
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = (self.out / "audit.json").read_text(encoding="utf-8") + (self.out / "audit.md").read_text(encoding="utf-8")
        self.assertNotIn("PROMPT_NEVER_LEAK", rendered)
        self.assertIn("malformed", rendered.lower())
        self.assertIn("truncated", rendered.lower())

    def test_output_private_no_overwrite_and_rejects_symlink(self):
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.stat(self.out).st_mode & 0o777, 0o700)
        for name in ("audit.json", "audit.md"):
            self.assertEqual(os.stat(self.out / name).st_mode & 0o777, 0o600)
        result = self.run_audit()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr.lower())
        link = self.base / "out-link"
        link.symlink_to(self.base, target_is_directory=True)
        result = subprocess.run([sys.executable, str(SCRIPT), "--session-id", "root", "--turn-id", "turn-a", "--sessions-dir", str(self.sessions), "--output-dir", str(link / "x")], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)

    def test_list_turns_has_no_prompt_text(self):
        rows = root_rows()
        rows.insert(1, {"type": "turn_context", "payload": {"turn_id": "t", "text": "UNIQUE_PROMPT_TEXT"}})
        write_jsonl(self.sessions / "root.jsonl", rows)
        result = subprocess.run([sys.executable, str(SCRIPT), "--list-turns", "--session-id", "root", "--sessions-dir", str(self.sessions)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("UNIQUE_PROMPT_TEXT", result.stdout)
        self.assertIn("turn-a", result.stdout)

    def test_root_only_omits_descendant_usage(self):
        child = [
            {"type": "session_meta", "payload": {"session_id": "child", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "root"}}}}},
            event("task_started", turn_id="c", timestamp="2026-10-28T10:00:30Z"),
            {"type": "turn_context", "payload": {"turn_id": "c", "model": "m", "reasoning_effort": "low"}},
            {"type": "token_usage_record", "payload": {"response_id": "child", "turn_id": "c", "usage": {"input_tokens": 5, "cached_input_tokens": 1, "output_tokens": 1}}},
            event("task_complete", turn_id="c", timestamp="2026-10-28T10:01:00Z"),
        ]
        write_jsonl(self.sessions / "child.jsonl", child)
        result = self.run_audit("--root-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({r["response_id"] for r in self.report()["requests"]}, {"r1", "r2"})

    def test_default_discovery_includes_sibling_archived_sessions(self):
        codex_home = self.base / "codex-home"
        write_jsonl(codex_home / "sessions" / "root.jsonl", root_rows())
        child = [
            {"type": "session_meta", "payload": {"session_id": "archived-child", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "root"}}}}},
            event("task_started", turn_id="c", timestamp="2026-10-28T10:00:30Z"),
            {"type": "turn_context", "payload": {"turn_id": "c", "model": "m", "reasoning_effort": "low"}},
            {"type": "token_usage_record", "timestamp": "2026-10-28T10:00:45Z", "payload": {"response_id": "archived", "turn_id": "c", "usage": {"input_tokens": 5, "cached_input_tokens": 1, "output_tokens": 1}}},
            event("task_complete", turn_id="c", timestamp="2026-10-28T10:01:00Z"),
        ]
        write_jsonl(codex_home / "archived_sessions" / "archived-child.jsonl", child)
        out = self.base / "default-out"
        env = dict(os.environ, CODEX_HOME=str(codex_home))
        result = subprocess.run([sys.executable, str(SCRIPT), "--session-id", "root", "--turn-id", "turn-a", "--output-dir", str(out)], text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("archived", {json.loads(line)["response_id"] for line in (out / "requests.jsonl").read_text().splitlines()})

    def test_huge_tool_result_is_not_copied_into_reports(self):
        rows = root_rows()
        rows.insert(3, {"type": "event_msg", "payload": {"type": "tool_call", "name": "safe_tool", "arguments": {"result": "PRIVATE_RESULT_" + "x" * 200000}}})
        write_jsonl(self.sessions / "root.jsonl", rows)
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = (self.out / "audit.json").read_text(encoding="utf-8") + (self.out / "audit.md").read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE_RESULT_", rendered)
        self.assertIn("safe_tool", (self.out / "events.jsonl").read_text(encoding="utf-8"))

    def test_missing_start_is_labeled_as_inferred(self):
        rows = root_rows()
        rows.pop(1)
        write_jsonl(self.sessions / "root.jsonl", rows)
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any("inferred" in w.lower() for w in self.report()["coverage"]["warnings"]))

    def test_actual_session_metadata_and_envelope_timestamps_scope_descendant(self):
        # Codex child metadata can identify its own session with id while retaining the
        # parent root's session_id; timestamps are on the JSONL envelope.
        rows = [
            {"type": "session_meta", "payload": {"id": "actual-child", "session_id": "root", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "root", "agent_path": "root/planner"}}}}},
            {"type": "event_msg", "timestamp": "2026-10-28T10:00:30Z", "payload": {"type": "task_started", "turn_id": "child-turn"}},
            {"type": "turn_context", "payload": {"turn_id": "child-turn", "model": "gpt-6-luna", "reasoning_effort": "low"}},
            {"type": "token_usage_record", "timestamp": "2026-10-28T10:00:45Z", "payload": {"response_id": "actual-child-response", "session_id": "root", "turn_id": "child-turn", "root_turn_id": "turn-a", "usage": {"input_tokens": 18, "cached_input_tokens": 4, "output_tokens": 3, "total_tokens": 21, "cache_write_input_tokens": 0}}},
            {"type": "event_msg", "timestamp": "2026-10-28T10:01:00Z", "payload": {"type": "task_complete", "turn_id": "child-turn"}},
        ]
        rows.insert(3, {"type": "token_usage_record", "timestamp": "2026-10-28T10:00:46Z", "payload": {"response_id": "wrong-root", "session_id": "root", "turn_id": "child-turn", "root_turn_id": "other-turn", "usage": {"input_tokens": 999, "cached_input_tokens": 0, "output_tokens": 1}}})
        write_jsonl(self.sessions / "actual-child.jsonl", rows)
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report()
        child = next(r for r in report["requests"] if r["response_id"] == "actual-child-response")
        self.assertEqual(child["session_id"], "actual-child")
        self.assertEqual(child["agent"], "root/planner")
        self.assertEqual(child["timestamp"], 1793181645.0)
        self.assertNotIn("wrong-root", {r["response_id"] for r in report["requests"]})
        self.assertEqual(report["raw_totals"]["total_tokens"], sum(r["total_tokens"] for r in report["requests"]))
        markdown = (self.out / "audit.md").read_text(encoding="utf-8")
        self.assertIn("Known subtotal (partial)", markdown)
        self.assertIn("Pricing coverage: 1/3 requests priced; 2 unpriced", markdown)

    def test_exact_child_root_turn_and_envelope_timestamp_work_without_child_lifecycle(self):
        child = [
            {"type": "session_meta", "payload": {"id": "unclosed-child", "session_id": "root", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "root", "agent_path": "root/worker"}}}}},
            {"type": "turn_context", "payload": {"turn_id": "child-turn", "model": "gpt-6-luna", "reasoning_effort": "low"}},
            {"type": "token_usage_record", "timestamp": "2026-10-28T10:00:04Z", "payload": {"response_id": "in-bound", "turn_id": "child-turn", "root_turn_id": "turn-a", "usage": {"input_tokens": 12, "cached_input_tokens": 2, "output_tokens": 4}}},
            {"type": "token_usage_record", "timestamp": "2026-10-28T10:02:12Z", "payload": {"response_id": "out-of-bound", "turn_id": "child-turn", "root_turn_id": "turn-a", "usage": {"input_tokens": 120, "cached_input_tokens": 20, "output_tokens": 40}}},
        ]
        write_jsonl(self.sessions / "unclosed-child.jsonl", child)
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        ids = {r["response_id"] for r in self.report()["requests"]}
        self.assertIn("in-bound", ids)
        self.assertNotIn("out-of-bound", ids)

    def test_jsonl_reader_discards_large_unallowlisted_payload_fields(self):
        path = self.sessions / "huge.jsonl"
        write_jsonl(path, [{"type": "event_msg", "timestamp": "2026-10-28T10:00:00Z", "payload": {"type": "tool_result", "content": "DO_NOT_RETAIN_" + "x" * 200000}}])
        spec = importlib.util.spec_from_file_location("audit_usage_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        retained = json.dumps(module._read(path))
        self.assertNotIn("DO_NOT_RETAIN_", retained)
        self.assertLess(len(retained), 1000)

    def test_total_and_cache_write_counters_are_validated(self):
        rows = root_rows()
        rows[3]["payload"]["usage"]["total_tokens"] = 999
        rows[3]["payload"]["usage"]["cache_write_input_tokens"] = 51
        write_jsonl(self.sessions / "root.jsonl", rows)
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report()
        self.assertEqual({r["response_id"] for r in report["requests"]}, {"r2"})
        self.assertEqual(report["coverage"]["invalid_records"], 1)

    def test_first_root_turn_reconciles_cumulative_snapshot_when_scope_is_complete(self):
        rows = root_rows()
        rows[3]["payload"]["thread_token_usage"] = {"input_tokens": 110, "cached_input_tokens": 60, "output_tokens": 14, "total_tokens": 124}
        rows[6]["payload"]["thread_token_usage"] = {"input_tokens": 190, "cached_input_tokens": 90, "output_tokens": 34, "total_tokens": 224}
        write_jsonl(self.sessions / "root.jsonl", rows)
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        reconciliation = self.report()["reconciliation"]
        self.assertEqual(reconciliation["status"], "matched")
        self.assertEqual(reconciliation["compared_sessions"], 1)

    def test_review_normalization_uses_ordinary_reference_input(self):
        rates = self.base / "rates.json"
        rates.write_text(json.dumps({"reference_model": "ref", "models": {"ref": {"input": 2, "cached": 0.1, "output": 10}, "gpt-6.1-sol": {"input": 2, "cached": 0.1, "output": 10}}}))
        rows = root_rows()[:4] + [root_rows()[7]]
        rows[3]["payload"]["usage"]["cache_write_input_tokens"] = 0
        write_jsonl(self.sessions / "root.jsonl", rows)
        self.assertEqual(self.run_audit("--rates", str(rates)).returncode, 0)
        report = self.report()
        # 50 new + 60*0.1/2 cached + 14*10/2 output = 123.
        self.assertEqual(float(report["normalized_total"]["values"]["total_tokens"]), 123)
        self.assertEqual(float(report["normalized_rows"][0]["normalized"]["output_tokens"]), 70)

    def test_review_child_cutoff_identity_grouping_and_markers(self):
        for sid, label in (("child1", "root"), ("child2", "root")):
            meta = {"type": "session_meta", "payload": {"id": sid, "agent_path": label, "source": {"subagent": {"thread_spawn": {"parent_thread_id": "root"}}}}}
            child = [meta, event("task_started", turn_id="c", timestamp="2026-10-28T09:59:00Z"),
                     {"type": "turn_context", "payload": {"turn_id": "c", "model": "m", "reasoning_effort": "low"}}]
            for suffix, stamp in (("before", "09:59:30"), ("inside", "10:01:00"), ("after", "10:02:30")):
                r = usage(sid + suffix, 10, 2, 1); r["payload"]["turn_id"] = "c"; r["timestamp"] = "2026-10-28T" + stamp + "Z"; child.append(r)
            child += [{"type": "compacted", "timestamp": "2026-10-28T10:01:30Z"}, event("task_complete", turn_id="c", timestamp="2026-10-28T10:03:00Z"), {"type": "compacted", "timestamp": "2026-10-28T12:00:00Z"}]
            write_jsonl(self.sessions / (sid + ".jsonl"), child)
        write_jsonl(self.sessions / "copy.jsonl", child)
        self.assertEqual(self.run_audit().returncode, 0)
        report = self.report()
        self.assertEqual({r["response_id"] for r in report["requests"]}, {"r1", "r2", "child1inside", "child2inside"})
        self.assertEqual(report["root_context_metrics"]["calls"], 2)
        self.assertEqual({r["session_id"] for r in report["rows"]}, {"root", "child1", "child2"})
        self.assertEqual(len([e for e in report["events"] if e["event_type"] == "compacted"]), 3)
        self.assertEqual(report["reconciliation"]["status"], "not_comparable")

    def test_review_excluded_child_prior_usage_prevents_reconciliation(self):
        rows = root_rows(); rows[6]["payload"]["thread_token_usage"] = {"input_tokens": 190, "cached_input_tokens": 90, "output_tokens": 34, "total_tokens": 224}
        write_jsonl(self.sessions / "root.jsonl", rows)
        child = [{"type": "session_meta", "payload": {"id": "child", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "root"}}}}}, event("task_started", turn_id="c", timestamp="2026-10-28T09:59:00Z"), {"type": "turn_context", "payload": {"turn_id": "c", "model": "m", "reasoning_effort": "low"}}]
        old = usage("old", 100, 20, 10); old["payload"]["turn_id"] = "c"; old["timestamp"] = "2026-10-28T09:59:30Z"
        current = usage("current", 100, 20, 10); current["payload"]["turn_id"] = "c"; current["timestamp"] = "2026-10-28T10:01:00Z"; current["payload"]["thread_token_usage"] = {"input_tokens": 200, "cached_input_tokens": 40, "output_tokens": 20, "total_tokens": 220}
        child += [old, current, event("task_complete", turn_id="c", timestamp="2026-10-28T10:01:30Z")]
        write_jsonl(self.sessions / "child.jsonl", child)
        self.assertEqual(self.run_audit().returncode, 0)
        self.assertEqual(self.report()["reconciliation"]["status"], "not_comparable")

    def test_review_child_missing_timestamp_is_excluded_with_warning(self):
        child = [{"type": "session_meta", "payload": {"id": "child", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "root"}}}}}, event("task_started", turn_id="c", timestamp="2026-10-28T10:00:30Z"), {"type": "turn_context", "payload": {"turn_id": "c", "model": "m", "reasoning_effort": "low"}}, usage("no-time", 5, 1, 1), event("task_complete", turn_id="c", timestamp="2026-10-28T10:01:30Z")]
        child[3]["payload"]["turn_id"] = "c"
        write_jsonl(self.sessions / "child.jsonl", child)
        self.assertEqual(self.run_audit().returncode, 0)
        report = self.report()
        self.assertNotIn("no-time", {r["response_id"] for r in report["requests"]})
        self.assertEqual(report["coverage"]["status"], "incomplete")

    def test_review_foreign_thread_rejected(self):
        rows = root_rows(); rows[3]["payload"]["thread_id"] = "foreign"
        write_jsonl(self.sessions / "root.jsonl", rows)
        result = self.run_audit()
        self.assertNotEqual(result.returncode, 0)

    def test_review_late_child_does_not_block_reconciliation(self):
        rows = root_rows(); rows[6]["payload"]["thread_token_usage"] = {"input_tokens": 190, "cached_input_tokens": 90, "output_tokens": 34, "total_tokens": 224}
        write_jsonl(self.sessions / "root.jsonl", rows)
        child = [{"type": "session_meta", "payload": {"id": "late", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "root"}}}}}, event("task_started", turn_id="late", timestamp="2026-10-28T11:00:00Z"), event("task_complete", turn_id="late", timestamp="2026-10-28T11:01:00Z")]
        write_jsonl(self.sessions / "late.jsonl", child)
        self.assertEqual(self.run_audit().returncode, 0)
        report = self.report()
        self.assertEqual(report["coverage"]["descendant_files_outside_or_unproven"], 0)
        self.assertEqual(report["reconciliation"]["status"], "matched")

    def test_lifecycle_error_reports_numeric_source_line(self):
        rows = root_rows()
        rows = [row for row in rows if not (row.get("type") == "event_msg" and row.get("payload", {}).get("type") == "task_complete")]
        write_jsonl(self.sessions / "root.jsonl", rows)
        result = self.run_audit()
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(result.stderr, r"line \d+")

    def test_unknown_lifecycle_and_unusable_identity_fail_closed(self):
        rows = root_rows()
        rows.insert(8, event("task_complete", turn_id="turn-a", timestamp="2026-10-28T10:02:30Z"))
        write_jsonl(self.sessions / "root.jsonl", rows)
        result = self.run_audit()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("PROMPT", result.stderr)
        write_jsonl(self.sessions / "wrong.jsonl", [{"type": "session_meta", "payload": {"session_id": "other"}}])
        result = self.run_audit(session="wrong")
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
