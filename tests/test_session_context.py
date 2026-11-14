"""Deterministic black-box tests for bounded current-session metadata reading."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills" / "budget-aware-swarm" / "scripts" / "read_session_context.py"
SID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
T1 = "11111111-1111-4111-8111-111111111111"
T2 = "22222222-2222-4222-8222-222222222222"


def rec(kind: str, payload: dict) -> str:
    return json.dumps({"type": kind, "payload": payload}) + "\n"


def session(path: Path, sid: str = SID, turns: list[dict] | None = None, *, markers: bool = True,
            completed: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [rec("session_meta", {"id": sid})]
    for turn in turns or []:
        if markers:
            lines.append(rec("event_msg", {"type": "task_started", "turn_id": turn.get("turn_id")}))
        lines.append(rec("turn_context", turn))
    if completed:
        lines.append(rec("event_msg", {"type": "task_complete", "turn_id": completed}))
    path.write_text("".join(lines), encoding="utf-8")


def turn(tid: str, model: str = "gpt-test", effort: str = "medium") -> dict:
    return {"turn_id": tid, "model": model, "effort": effort}


class SessionContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.sessions = self.home / "sessions" / "2026" / "01" / "01"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_cli(self, sid: str = SID, *args: str, codex_home: Path | None = None, env_home: Path | None = None) -> tuple[subprocess.CompletedProcess[str], dict]:
        env = dict(os.environ)
        if env_home is not None:
            env["CODEX_HOME"] = str(env_home)
        command = [sys.executable, str(SCRIPT), "--session-id", sid, *args]
        if codex_home is not None:
            command += ["--codex-home", str(codex_home)]
        result = subprocess.run(command, env=env, text=True, capture_output=True, check=False)
        try:
            data = json.loads(result.stdout)
        except Exception as exc:
            self.fail(f"CLI did not emit JSON: {exc}; stderr={result.stderr!r}")
        return result, data

    def test_latest_turn_and_model_switch_are_selected(self) -> None:
        session(self.sessions / f"session-{SID}.jsonl", turns=[turn(T1), turn(T2, "gpt-new", "high")])
        result, data = self.run_cli(codex_home=self.home)
        self.assertEqual(result.returncode, 0)
        self.assertEqual((data["status"], data["session_id"], data["turn_id"]), ("ok", SID, T2))
        self.assertEqual((data["model"], data["effort"]), ("gpt-new", "high"))
        self.assertEqual(data["provenance"], "client_telemetry")
        self.assertTrue(data["source"].endswith(f"session-{SID}.jsonl"))
        self.assertEqual(data["line"], 5)

    def test_explicit_turn_selects_exact_turn(self) -> None:
        session(self.sessions / f"{SID}.jsonl", turns=[turn(T1), turn(T2, "gpt-new", "high")], markers=False)
        result, data = self.run_cli(SID, "--turn-id", T1, codex_home=self.home)
        self.assertEqual(result.returncode, 0)
        self.assertEqual((data["turn_id"], data["model"], data["effort"]), (T1, "gpt-test", "medium"))

    def test_missing_explicit_turn_does_not_fall_back(self) -> None:
        session(self.sessions / f"{SID}.jsonl", turns=[turn(T1)], markers=False)
        result, data = self.run_cli(SID, "--turn-id", T2, codex_home=self.home)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(data["status"], "turn_not_found")
        self.assertIsNone(data["turn_id"])

    def test_filename_match_with_wrong_session_meta_is_rejected(self) -> None:
        session(self.sessions / f"{SID}.jsonl", sid="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", turns=[turn(T1)])
        result, data = self.run_cli(codex_home=self.home)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(data["status"], "identity_mismatch")
        self.assertIsNone(data["session_id"])

    def test_multiple_identity_matches_are_ambiguous(self) -> None:
        session(self.sessions / f"a-{SID}.jsonl", turns=[turn(T1)])
        session(self.sessions / f"b-{SID}.jsonl", turns=[turn(T2)])
        result, data = self.run_cli(codex_home=self.home)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(data["status"], "ambiguous")
        self.assertIsNone(data["model"])

    def test_missing_session_and_missing_fields_fail_closed(self) -> None:
        result, data = self.run_cli(codex_home=self.home)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(data["status"], "not_found")
        session(self.sessions / f"{SID}.jsonl", turns=[{"turn_id": T1, "model": "only-model"}])
        result, data = self.run_cli(codex_home=self.home)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(data["status"], "missing_fields")
        self.assertIsNone(data["effort"])

    def test_malformed_or_truncated_jsonl_fails_closed(self) -> None:
        path = self.sessions / f"{SID}.jsonl"
        session(path, turns=[turn(T1)])
        path.write_text(path.read_text() + '{"type":"turn_context"', encoding="utf-8")
        result, data = self.run_cli(codex_home=self.home)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(data["status"], "malformed")

    def test_unrelated_private_payload_never_appears_in_output(self) -> None:
        path = self.sessions / f"{SID}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        private = "PRIVATE_TRANSCRIPT_SENTINEL secret-token"
        path.write_text(rec("session_meta", {"id": SID}) +
                        rec("message", {"body": private, "credential": private}) +
                        rec("turn_context", turn(T1)), encoding="utf-8")
        result, _ = self.run_cli(codex_home=self.home)
        self.assertNotIn(private, result.stdout + result.stderr)
        self.assertNotIn("body", result.stdout)
        self.assertNotIn("credential", result.stdout)

    def test_codex_home_environment_is_default_and_flag_overrides_it(self) -> None:
        env_home = self.home / "env-home"
        explicit = self.home / "explicit-home"
        session(env_home / "sessions" / f"{SID}.jsonl", turns=[turn(T1)])
        session(explicit / "sessions" / f"{SID}.jsonl", turns=[turn(T2, "gpt-explicit", "high")])
        result, data = self.run_cli(env_home=env_home)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(data["turn_id"], T1)
        result, data = self.run_cli(codex_home=explicit, env_home=env_home)
        self.assertEqual(result.returncode, 0)
        self.assertEqual((data["turn_id"], data["model"]), (T2, "gpt-explicit"))

    def test_ids_must_be_uuids_and_filename_match_is_exact_suffix(self) -> None:
        session(self.sessions / f"prefix-{SID}.jsonl", turns=[turn(T1)])
        result, data = self.run_cli("partial", codex_home=self.home)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(data["status"], "invalid_id")
        result, data = self.run_cli(SID, "--turn-id", "short", codex_home=self.home)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(data["status"], "invalid_id")
        result, data = self.run_cli(codex_home=self.home)
        self.assertEqual(data["status"], "ok")
        session(self.sessions / f"{SID}-extra.jsonl", turns=[turn(T1)])
        # The valid suffix file remains the only candidate; partial-prefix files cannot match.
        self.assertEqual(self.run_cli(codex_home=self.home)[1]["status"], "ok")

    def test_duplicate_identical_turn_metadata_is_allowed(self) -> None:
        session(self.sessions / f"{SID}.jsonl", turns=[turn(T1), turn(T1)])
        result, data = self.run_cli(SID, "--turn-id", T1, codex_home=self.home)
        self.assertEqual(result.returncode, 0)
        self.assertEqual((data["model"], data["effort"]), ("gpt-test", "medium"))

    def test_conflicting_same_turn_routing_metadata_rejected(self) -> None:
        session(self.sessions / f"{SID}.jsonl", turns=[turn(T1), turn(T1, "gpt-other", "high")])
        result, data = self.run_cli(codex_home=self.home)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(data["status"], "conflicting_context")

    def test_conflicting_session_meta_ids_fail_closed(self) -> None:
        path = self.sessions / f"{SID}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rec("session_meta", {"id": SID}) +
                        rec("session_meta", {"id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"}) +
                        rec("turn_context", turn(T1)), encoding="utf-8")
        result, data = self.run_cli(codex_home=self.home)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(data["status"], "identity_mismatch")
        self.assertIsNone(data["session_id"])

    def test_latest_start_without_context_pauses_instead_of_using_stale_context(self) -> None:
        path = self.sessions / f"{SID}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rec("session_meta", {"id": SID}) +
                        rec("event_msg", {"type": "task_started", "turn_id": T1}) + rec("turn_context", turn(T1)) +
                        rec("event_msg", {"type": "task_started", "turn_id": T2}), encoding="utf-8")
        result, data = self.run_cli(codex_home=self.home)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(data["status"], "turn_not_found")
        self.assertIsNone(data["turn_id"])

    def test_no_markers_without_explicit_turn_pauses(self) -> None:
        session(self.sessions / f"{SID}.jsonl", turns=[turn(T1)], markers=False)
        result, data = self.run_cli(codex_home=self.home)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(data["status"], "no_active_turn")

    def test_completed_latest_task_pauses(self) -> None:
        session(self.sessions / f"{SID}.jsonl", turns=[turn(T1)], completed=T1)
        result, data = self.run_cli(codex_home=self.home)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(data["status"], "no_active_turn")

    def test_explicit_turn_must_match_active_when_markers_exist(self) -> None:
        session(self.sessions / f"{SID}.jsonl", turns=[turn(T1), turn(T2)])
        result, data = self.run_cli(SID, "--turn-id", T1, codex_home=self.home)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(data["status"], "inactive_turn")

    def test_explicit_turn_allowed_without_markers(self) -> None:
        session(self.sessions / f"{SID}.jsonl", turns=[turn(T1)], markers=False)
        result, data = self.run_cli(SID, "--turn-id", T1, codex_home=self.home)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(data["turn_id"], T1)


if __name__ == "__main__":
    unittest.main()
