#!/usr/bin/env python3
"""Read only current-session turn model/effort metadata from Codex JSONL."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import os
import sys
import uuid
from typing import Any


def result(status: str, *, session_id: str | None = None, turn_id: str | None = None,
           model: str | None = None, effort: str | None = None,
           source: str | None = None, line: int | None = None) -> dict[str, Any]:
    return {"status": status, "session_id": session_id, "turn_id": turn_id,
            "model": model, "effort": effort, "source": source, "line": line,
            "provenance": "client_telemetry"}


def read_file(path: Path, requested_id: str, requested_turn: str | None) -> tuple[str, dict[str, Any] | None]:
    identities: set[str] = set()
    contexts: dict[str, dict[str, Any]] = {}
    starts: list[str] = []
    completed: set[str] = set()
    try:
        with path.open(encoding="utf-8") as stream:
            for number, raw in enumerate(stream, 1):
                try:
                    entry = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return "malformed", None
                if not isinstance(entry, dict):
                    return "malformed", None
                payload = entry.get("payload")
                if not isinstance(payload, dict):
                    continue
                if entry.get("type") == "session_meta":
                    value = payload.get("id")
                    if isinstance(value, str):
                        identities.add(value)
                    else:
                        identities.add("")
                elif entry.get("type") == "event_msg":
                    event_type = payload.get("type")
                    event_turn = payload.get("turn_id")
                    if isinstance(event_turn, str) and event_turn.strip():
                        if event_type == "task_started":
                            starts.append(event_turn)
                        elif event_type == "task_complete":
                            completed.add(event_turn)
                elif entry.get("type") == "turn_context":
                    # Project immediately: never retain summaries or other payload fields.
                    projected = {key: payload.get(key) for key in ("turn_id", "model", "effort")}
                    projected["line"] = number
                    tid = projected["turn_id"]
                    if isinstance(tid, str) and tid.strip():
                        previous = contexts.get(tid)
                        if previous and (previous.get("model"), previous.get("effort")) != (projected.get("model"), projected.get("effort")):
                            return "conflicting_context", None
                        contexts[tid] = projected
    except (OSError, UnicodeError):
        return "unreadable", None
    if len(identities) != 1 or requested_id not in identities:
        return "identity_mismatch", None
    if requested_turn:
        if starts and (starts[-1] != requested_turn or starts[-1] in completed):
            return "inactive_turn", None
        return "ok", {"turn": contexts.get(requested_turn)}
    if not starts:
        return "no_active_turn", None
    active_turn = starts[-1]
    if active_turn in completed:
        return "no_active_turn", None
    return "ok", {"turn": contexts.get(active_turn)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--turn-id")
    parser.add_argument("--codex-home", type=Path)
    args = parser.parse_args()
    try:
        uuid.UUID(args.session_id)
        if args.turn_id:
            uuid.UUID(args.turn_id)
    except ValueError:
        output = result("invalid_id")
        print(json.dumps(output, separators=(",", ":")))
        return 1
    codex_home = args.codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    base = codex_home / "sessions"
    suffix = f"-{args.session_id}.jsonl"
    candidates = sorted(p for p in base.rglob("*.jsonl")
                        if p.name == f"{args.session_id}.jsonl" or p.name.endswith(suffix)) if base.is_dir() else []
    if not candidates:
        output = result("not_found")
    else:
        matched: list[tuple[Path, dict[str, Any] | None]] = []
        mismatch = False
        unreadable = False
        malformed = False
        conflict = False
        inactive = False
        no_active = False
        for path in candidates:
            status, data = read_file(path, args.session_id, args.turn_id)
            if status == "ok" and data is not None:
                matched.append((path, data["turn"]))
            elif status == "identity_mismatch":
                mismatch = True
            elif status == "malformed":
                malformed = True
            elif status == "conflicting_context":
                conflict = True
            elif status == "inactive_turn":
                inactive = True
            elif status == "no_active_turn":
                no_active = True
            else:
                unreadable = True
        if malformed:
            output = result("malformed")
        elif conflict:
            output = result("conflicting_context")
        elif inactive:
            output = result("inactive_turn")
        elif no_active:
            output = result("no_active_turn")
        elif unreadable:
            output = result("unreadable")
        elif len(matched) > 1:
            output = result("ambiguous")
        elif not matched:
            output = result("identity_mismatch" if mismatch else "not_found")
        else:
            path, chosen = matched[0]
            if chosen is None:
                output = result("turn_not_found", session_id=args.session_id, source=str(path))
            else:
                number = chosen["line"]
                tid, model, effort = chosen.get("turn_id"), chosen.get("model"), chosen.get("effort")
                if not all(isinstance(v, str) and v.strip() for v in (tid, model, effort)):
                    output = result("missing_fields", session_id=args.session_id, turn_id=tid if isinstance(tid, str) else None,
                                    source=str(path), line=number)
                else:
                    output = result("ok", session_id=args.session_id, turn_id=tid, model=model,
                                    effort=effort, source=str(path), line=number)
    print(json.dumps(output, separators=(",", ":")))
    return 0 if output["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
