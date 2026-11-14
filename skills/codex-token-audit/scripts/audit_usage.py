#!/usr/bin/env python3
"""Bounded, local-only token telemetry audit for Codex JSONL sessions."""

from __future__ import annotations

import argparse
import datetime as dt
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation
import runtime_evidence


class AuditError(Exception):
    pass


def _safe_identifier(value):
    return isinstance(value, (str, int)) and not isinstance(value, bool) and bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", str(value)))


_USAGE_FIELDS = (
    "input_tokens", "cached_input_tokens", "output_tokens",
    "reasoning_output_tokens", "total_tokens", "cache_write_input_tokens",
)


def _sanitize_row(row):
    """Retain only identifiers, counters, model metadata and safe tool names."""
    typ = row.get("type")
    envelope = {}
    if "timestamp" in row:
        envelope["timestamp"] = row["timestamp"]
    payload = row.get("payload")
    if "timestamp" not in envelope and isinstance(payload, dict):
        for key in ("timestamp", "created_at", "time"):
            if isinstance(payload.get(key), (str, int, float)):
                envelope["timestamp"] = payload[key]
                break
    if typ == "session_meta":
        p = row.get("payload")
        if isinstance(p, dict):
            safe = {k: p[k] for k in ("id", "session_id", "agent_path") if isinstance(p.get(k), (str, int))}
            source = p.get("source")
            if isinstance(source, dict):
                sub = source.get("subagent")
                if isinstance(sub, dict):
                    spawn = sub.get("thread_spawn")
                    if isinstance(spawn, dict):
                        safe["source"] = {"subagent": {"thread_spawn": {k: spawn[k] for k in ("parent_thread_id", "agent_path") if isinstance(spawn.get(k), (str, int))}}}
            envelope.update(type=typ, payload=safe)
        return envelope
    if typ == "turn_context":
        p = row.get("payload")
        if isinstance(p, dict):
            safe = {k: p[k] for k in ("turn_id", "local_turn_id", "model", "model_name", "reasoning_effort", "effort") if _safe_identifier(p.get(k))}
            tier = runtime_evidence.tier(p)
            if tier is not None:
                safe["configured_service_tier"] = tier
            envelope.update(type=typ, payload=safe)
        return envelope
    if typ == "token_usage_record":
        p = row.get("payload")
        if isinstance(p, dict):
            safe = {k: p[k] for k in ("response_id", "id", "session_id", "thread_id", "turn_id", "local_turn_id", "root_turn_id") if _safe_identifier(p.get(k))}
            usage = p.get("usage")
            if isinstance(usage, dict):
                safe["usage"] = {k: usage[k] for k in _USAGE_FIELDS if k in usage and isinstance(usage[k], (int, float, str)) and not isinstance(usage[k], bool)}
                tier = runtime_evidence.tier(usage)
                if tier is not None:
                    safe["observed_service_tier"] = tier
            for counter_name in ("thread_token_usage", "turn_token_usage"):
                snapshot = p.get(counter_name)
                if isinstance(snapshot, dict):
                    safe[counter_name] = {k: v for k, v in snapshot.items() if k in _USAGE_FIELDS and _integer(v)}
            envelope.update(type=typ, payload=safe)
        return envelope
    if typ == "compacted":
        envelope["type"] = typ
        return envelope
    if typ in ("event_msg", "response_item"):
        p = row.get("payload")
        if isinstance(p, dict):
            safe = {k: p[k] for k in ("type", "turn_id", "local_turn_id") if _safe_identifier(p.get(k))}
            if p.get("type") == "token_count":
                quota = runtime_evidence.quota(p.get("rate_limits", p.get("rateLimits")))
                if quota is not None:
                    safe["quota"] = quota
            if p.get("type") == "thread_settings_applied":
                tier = runtime_evidence.tier(p.get("thread_settings", p.get("threadSettings")))
                if tier is not None:
                    safe["configured_service_tier"] = tier
            if p.get("type") in ("tool_call", "tool_use"):
                name = p.get("name") or p.get("tool_name")
                if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name):
                    safe["name"] = name
            for key in ("call_id", "result_id", "id", "target_thread_id", "parent_thread_id", "fork_turns"):
                value = p.get(key)
                if _safe_identifier(value):
                    safe[key] = value
            name = p.get("name") or p.get("tool_name")
            if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name):
                safe["name"] = name
            if name in ("spawn_agent", "collaboration.spawn_agent", "collaboration__spawn_agent"):
                arguments = p.get("arguments")
                try:
                    arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
                except (ValueError, TypeError):
                    arguments = None
                if isinstance(arguments, dict):
                    if arguments.get("fork_turns") in ("none", "all"):
                        safe["fork_turns"] = arguments["fork_turns"]
                        safe["fork_provenance"] = "recorded tool call arguments"
                    for src, dest in (("model", "spawn_model"), ("reasoning_effort", "spawn_effort")):
                        value = arguments.get(src)
                        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
                            safe[dest] = value
            body = p.get("output", p.get("content"))
            if p.get("type") in ("function_call_output", "tool_result", "custom_tool_call_output"):
                try:
                    structured = json.loads(body) if isinstance(body, str) else body
                except (ValueError, TypeError):
                    structured = None
                if isinstance(structured, dict):
                    for key in ("agent_id", "thread_id", "target_thread_id"):
                        value = structured.get(key)
                        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
                            safe["target_thread_id"] = value
                            break
            if isinstance(body, str):
                safe["result_chars"] = len(body)
                safe["result_bytes"] = len(body.encode("utf-8"))
            elif body is not None:
                safe["opaque"] = True
                safe["mixed"] = isinstance(body, list)
                safe["images"] = isinstance(body, list) and any(isinstance(x, dict) and "image" in str(x.get("type", "")).lower() for x in body)
                if isinstance(body, list):
                    text_parts = [x["text"] for x in body if isinstance(x, dict) and isinstance(x.get("text"), str)]
                    if text_parts:
                        safe["result_chars"] = sum(len(x) for x in text_parts)
                        safe["result_bytes"] = sum(len(x.encode("utf-8")) for x in text_parts)
            if isinstance(p.get("truncated"), bool):
                safe["truncated"] = p["truncated"]
                if p.get("type") in ("function_call_output", "tool_result", "custom_tool_call_output"):
                    safe["result_truncated"] = p["truncated"]
            # Numeric-only legacy snapshots are enough for bounded reconciliation.
            for k in ("thread_token_usage", "turn_token_usage"):
                value = p.get(k)
                if isinstance(value, dict):
                    safe[k] = {name: n for name, n in value.items() if isinstance(name, str) and _integer(n)}
            envelope.update(type=typ, payload=safe)
        return envelope
    return {"type": typ} if isinstance(typ, str) else {}


def _json_lines(path):
    """Yield allowlisted JSONL structures without retaining message bodies."""
    try:
        with path.open("rb") as stream:
            for number, raw in enumerate(stream, 1):
                # A final record without newline may be valid, but cannot be known complete.
                truncated = not raw.endswith(b"\n")
                try:
                    value = json.loads(raw)
                    if not isinstance(value, dict):
                        yield number, None, True, truncated
                    else:
                        sanitized = _sanitize_row(value)
                        if truncated and sanitized.get("type") in ("event_msg", "response_item") and isinstance(sanitized.get("payload"), dict):
                            sanitized["payload"]["source_line_incomplete"] = True
                            sanitized["payload"]["truncated"] = True
                        yield number, sanitized, False, truncated
                except (json.JSONDecodeError, UnicodeDecodeError):
                    yield number, None, True, truncated
    except OSError:
        raise AuditError("cannot read selected session file")


def _meta(path):
    # Session metadata is expected near the beginning. Inspect only the first 200 lines
    # while identifying candidate files; full content is parsed only after linking.
    try:
        with path.open("rb") as stream:
            for n, raw in enumerate(stream, 1):
                if n > 200:
                    break
                try:
                    row = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(row, dict) and row.get("type") == "session_meta":
                    p = row.get("payload") or {}
                    if isinstance(p, dict):
                        return p
                    return {}
    except OSError:
        return {}
    return {}


def _session_files(directory, include_sibling_archive=False):
    try:
        roots = [directory]
        sibling_archive = directory.parent / "archived_sessions"
        if include_sibling_archive and sibling_archive.is_dir() and sibling_archive != directory:
            roots.append(sibling_archive)
        return sorted({p for root in roots for p in root.rglob("*.jsonl") if p.is_file() and not p.is_symlink()})
    except OSError:
        raise AuditError("cannot enumerate sessions directory")


def _meta_id(meta):
    # Child records can retain their parent's session_id while `id` identifies the file.
    val = meta.get("id") or meta.get("session_id")
    return str(val) if _safe_identifier(val) else None


def _agent_path(meta):
    direct = meta.get("agent_path")
    if isinstance(direct, str) and re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", direct) and ".." not in direct.split("/"):
        return direct
    try:
        value = meta["source"]["subagent"]["thread_spawn"].get("agent_path")
    except (KeyError, TypeError, AttributeError):
        value = None
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", value) and ".." not in value.split("/") else "agent"


def _resolve_root(files, session_id):
    exact = []
    for p in files:
        m = _meta(p)
        mid = _meta_id(m)
        if mid == session_id:
            exact.append((p, m))
        elif p.stem.endswith(session_id):
            if mid and mid != session_id:
                raise AuditError("session filename and metadata identity conflict")
            exact.append((p, m))
    if not exact:
        raise AuditError("requested session identity not found")
    # Multiple copies are expected, but only when all metadata confirms same identity.
    for _, m in exact:
        mid = _meta_id(m)
        if mid is not None and mid != session_id:
            raise AuditError("conflicting session identities")
    return exact


def _parent(meta):
    try:
        parent = meta["source"]["subagent"]["thread_spawn"]["parent_thread_id"]
        return str(parent) if _safe_identifier(parent) else None
    except (KeyError, TypeError):
        return None


def _timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if runtime_evidence.finite(value) else None
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def _row_time(payload, envelope=None):
    if isinstance(envelope, dict):
        found = _timestamp(envelope.get("timestamp"))
        if found is not None:
            return found
    for key in ("timestamp", "created_at", "time"):
        found = _timestamp(payload.get(key))
        if found is not None:
            return found
    return None


def _turn(payload):
    return payload.get("turn_id") or payload.get("local_turn_id")


def _lifecycle(rows, turn_id):
    starts, ends = [], []
    for idx, (line, row, _, _) in enumerate(rows):
        if not row or row.get("type") != "event_msg":
            continue
        p = row.get("payload") or {}
        if not isinstance(p, dict) or str(_turn(p)) != str(turn_id):
            continue
        typ = p.get("type")
        if typ == "task_started":
            starts.append((idx, _row_time(p, row), line))
        elif typ in ("task_complete", "turn_aborted"):
            ends.append((idx, _row_time(p, row), line, typ))
    if len(ends) != 1:
        pointer = ", ".join(str(e[2]) for e in ends) if ends else str(rows[-1][0] if rows else 1)
        raise AuditError(f"target turn has {len(ends)} terminal lifecycle events; relevant line {pointer}")
    if len(starts) > 1:
        raise AuditError(f"target turn has ambiguous start lifecycle at lines {', '.join(str(s[2]) for s in starts)}")
    inferred = not starts
    if inferred:
        ctx = next((i for i, (_, row, _, _) in enumerate(rows) if row and row.get("type") == "turn_context" and str(_turn(row.get("payload") or {})) == str(turn_id)), None)
        if ctx is None or ctx >= ends[0][0]:
            raise AuditError("target turn start is unavailable")
        start = (ctx, None, rows[ctx][0])
    else:
        start = starts[0]
    end = ends[0]
    if end[0] <= start[0]:
        raise AuditError("target turn lifecycle order is invalid")
    return start, end, inferred


def _read(path):
    return list(_json_lines(path))


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _dec(value):
    if isinstance(value, bool):
        raise AuditError("invalid rates file")
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise AuditError("invalid rates file")
    if not d.is_finite() or d < 0:
        raise AuditError("invalid rates file")
    return d


def _load_rates(path):
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        models = data["models"]
        ref = data["reference_model"]
        out = {str(k): {key: _dec(v[key]) for key in ("input", "cached", "output")} for k, v in models.items()}
        if ref not in out or out[ref]["input"] <= 0:
            raise AuditError("invalid rates file")
        return {"scenario": data.get("scenario"), "verified_at": data.get("verified_at"), "source": data.get("source"), "reference_model": ref, "models": out}
    except AuditError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise AuditError("invalid rates file")


def _normalization(req, rates):
    if rates is None:
        return None
    model = req["model"]
    if model not in rates["models"]:
        return None
    if req["cache_write_input_tokens"] is None or req["cache_write_input_tokens"] != 0:
        return None
    rate = rates["models"][model]
    ref = rates["models"][rates["reference_model"]]
    new = Decimal(req["new_tokens"])
    cached = Decimal(req["cached_input_tokens"])
    output = Decimal(req["output_tokens"])
    nn = new * rate["input"] / ref["input"]
    nc = cached * rate["cached"] / ref["input"]
    no = output * rate["output"] / ref["input"]
    return {"new_tokens": str(nn), "cached_tokens": str(nc), "input_tokens": str(nn + nc), "output_tokens": str(no), "total_tokens": str(nn + nc + no)}


def _safe_output_dir(path):
    out = Path(path).expanduser().absolute()
    # macOS exposes standard temporary paths through aliases such as /var -> /private/var.
    # Permit those OS aliases but reject user-controlled symlink components.
    for part in (out, *out.parents):
        if part.is_symlink() and str(part) not in ("/var", "/tmp"):
            raise AuditError("output path may not contain symlinks")
    if out.exists():
        if not out.is_dir():
            raise AuditError("output path is not a directory")
    else:
        try:
            out.mkdir(mode=0o700, parents=True)
        except FileExistsError:
            raise AuditError("output directory already exists")
    if (out / "audit.json").exists() or (out / "audit.md").exists():
        raise AuditError("output report already exists; refusing to overwrite")
    os.chmod(out, 0o700)
    return out


def _write_private(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def _fmt_millions(v):
    if v is None:
        return "unknown"
    return f"{Decimal(v) / Decimal(1_000_000):.1f}"


def _build_markdown(report, rates):
    lines = ["# Token usage audit", "", f"Session `{report['scope']['session_id']}`, turn `{report['scope']['turn_id']}`.", "", f"Coverage: **{report['coverage']['status']}**. {len(report['coverage']['warnings'])} warning(s)."]
    if rates:
        reference = rates["reference_model"]
        lines += ["", f"All usage values below are millions of ordinary input-token equivalents of `{reference}`, rounded to one decimal after aggregation.", "", "| Agent / model / effort | Новый | Кэш | Ввод | Вывод | Итого |", "|---|---:|---:|---:|---:|---:|"]
        for row in report["normalized_rows"]:
            label = " / ".join(str(row.get(k) or "unknown") for k in ("agent", "model", "effort"))
            vals = row.get("normalized") or {}
            if not row["complete"]:
                label += " (partial)"
            lines.append("| " + label + " | " + " | ".join(_fmt_millions(vals.get(k)) for k in ("new_tokens", "cached_tokens", "input_tokens", "output_tokens", "total_tokens")) + " |")
        normalized_total = report["normalized_total"]
        vals = normalized_total.get("values") or {}
        total_label = "**Total**" if normalized_total["complete"] else "**Known subtotal (partial)**"
        lines.append("| " + total_label + " | " + " | ".join(_fmt_millions(vals.get(k)) for k in ("new_tokens", "cached_tokens", "input_tokens", "output_tokens", "total_tokens")) + " |")
        priced = sum(r["normalized"] is not None for r in report["requests"])
        lines += ["", f"Pricing coverage: {priced}/{len(report['requests'])} requests priced; {len(report['requests']) - priced} unpriced. Unknown models or unsupported cache-write usage are not zero cost.", "", f"API-price scenario: {rates.get('scenario') or 'unnamed'}. Rate date: {rates.get('verified_at') or 'unknown'}; source: {rates.get('source') or 'unknown'}. This is a comparison, not subscription consumption or an invoice.", "", f"One unit equals the price of one ordinary input token of `{reference}`. Each coefficient is its category's API price divided by that reference input price:", "", "| Model | New input | Cached input | Output |", "|---|---:|---:|---:|"]
        denominator = rates["models"][reference]["input"]
        for model, prices in rates["models"].items():
            weights = [format((prices[k] / denominator).normalize(), "f") for k in ("input", "cached", "output")]
            lines.append("| " + model + " | " + " | ".join(weights) + " |")
    else:
        lines += ["", "Price equivalence unavailable; raw counts remain in the local audit.json."]
    lines += ["", "Ввод = Новый + Кэш; Итого = Ввод + Вывод. Cache is weighted once; output already includes reasoning. Rounded rows may not sum to the displayed total; 0.0 can be a small nonzero value. Exact raw counts and equivalents remain in the local audit.json.", "", "Request input counts and cumulative cached-token sums are separate measures; cache attribution across retained context is not inferred.", "", "Coverage notes:"]
    lines.extend(f"- {w}" for w in report["coverage"]["warnings"] or ["No known gaps in selected evidence."])
    lines += ["", "Reports contain numeric telemetry and source line pointers; prompts and tool outputs are omitted.", ""]
    return "\n".join(lines)


def _collect_one(args):
    sessions_dir = Path(args.sessions_dir).expanduser().absolute()
    if sessions_dir.is_symlink() or not sessions_dir.is_dir():
        raise AuditError("sessions directory is unavailable")
    files = _session_files(sessions_dir, include_sibling_archive=not args.sessions_dir_explicit)
    roots = _resolve_root(files, args.session_id)
    metas = {p: _meta(p) for p in files}
    # Follow metadata parent links. Child payload.session_id can be inherited from root,
    # so ownership is tied to the verified session metadata/file relationship.
    selected = {p: (m, "root") for p, m in roots}
    root_paths = {p for p, _ in roots}
    frontier = {args.session_id}
    while frontier and not args.root_only:
        parent_ids = frontier
        frontier = set()
        for p in files:
            if p in selected:
                continue
            m = metas[p]
            parent = _parent(m)
            if parent not in parent_ids:
                continue
            child_id = _meta_id(m)
            if not child_id:
                continue
            selected[p] = (m, _agent_path(m))
            frontier.add(child_id)
    # Identity ambiguity across copies of a child is safe only if IDs and parent agree.
    for p, (m, _) in selected.items():
        mid = _meta_id(m)
        if p not in [r[0] for r in roots] and (not mid or _parent(m) not in {args.session_id, *[_meta_id(x[0]) for x in selected.values()]}):
            raise AuditError("descendant metadata identity is conflicting")

    aliases = {path: f"source-{index:04d}" for index, path in enumerate(sorted(selected), 1)}
    root_path = roots[0][0]
    root_rows = _read(root_path)
    start, end, inferred = _lifecycle(root_rows, args.turn_id)
    start_time = start[1]
    end_time = end[1]
    warnings = []
    if inferred:
        warnings.append(f"Root task start inferred from turn_context at line {start[2]}.")
    if end[3] == "turn_aborted":
        warnings.append(f"Root task ended aborted at line {end[2]}.")
    requests = []
    malformed = 0
    truncated = 0
    invalid = 0
    no_response = 0
    duplicate_count = 0
    revised_count = 0
    missing_children = 0
    compact_markers = []
    events = []
    marker_keys = set()
    event_keys = set()
    stream_signatures = defaultdict(dict)
    tool_names = defaultdict(list)
    cumulative_by_session = {}
    prior_usage_by_session = {}

    # Process root first, then descendants. Selected child turns must have an interval
    # overlapping the root task interval; absent child timestamps are reported as gaps.
    selected_records = []
    for path, (meta, agent) in selected.items():
        rows = root_rows if path == root_path else _read(path)
        local_start, local_end = (start, end) if path not in root_paths else _lifecycle(rows, args.turn_id)[:2]
        malformed += sum(1 for _, _, bad, _ in rows if bad)
        truncated += sum(1 for _, _, _, tr in rows if tr)
        mid = _meta_id(meta)
        if path not in root_paths:
            # If this metadata references a child file that is unreadable, _read fails.
            # Its run is included only when lifecycle timestamps prove overlap.
            turns = defaultdict(list)
            for i, (line, row, _, _) in enumerate(rows):
                if not row:
                    continue
                p = row.get("payload") or {}
                if not isinstance(p, dict):
                    continue
                typ = p.get("type") if row.get("type") == "event_msg" else None
                tid = _turn(p)
                if typ in ("task_started", "task_complete", "turn_aborted") and tid is not None:
                    turns[str(tid)].append((i, _row_time(p, row), typ, line))
            accepted_turns = set()
            accepted_ranges = []
            for tid, evs in turns.items():
                starts = [e for e in evs if e[2] == "task_started"]
                ends = [e for e in evs if e[2] in ("task_complete", "turn_aborted")]
                for st in starts:
                    et = next((e for e in ends if e[0] > st[0]), None)
                    if not et:
                        continue
                    if start_time is not None and end_time is not None and st[1] is not None and et[1] is not None and st[1] <= end_time and et[1] >= start_time:
                        accepted_turns.add(tid)
                        accepted_ranges.append((st[0], et[0]))
            times = [_row_time(row.get("payload") or {}, row) for _, row, _, _ in rows if row]
            times = [t for t in times if t is not None]
            exact_in_scope = any(row and row.get("type") == "token_usage_record"
                                 and str((row.get("payload") or {}).get("root_turn_id")) == str(args.turn_id)
                                 and start_time is not None and end_time is not None
                                 and (t := _row_time(row.get("payload") or {}, row)) is not None
                                 and start_time <= t <= end_time for _, row, _, _ in rows)
            proven_outside = bool(times) and start_time is not None and end_time is not None and (min(times) > end_time or max(times) < start_time)
            if not accepted_turns and not exact_in_scope and not proven_outside:
                missing_children += 1
        else:
            accepted_turns = {str(args.turn_id)}
            accepted_ranges = [(start[0], end[0])]
        if path in root_paths:
            prior_start = local_start[0]
            prior_usage_by_session[mid or args.session_id] = prior_usage_by_session.get(mid or args.session_id, False) or any(
                row and row.get("type") == "token_usage_record" for _, row, _, _ in rows[:prior_start]
            )
        elif accepted_ranges:
            prior_start = min(lo for lo, _ in accepted_ranges)
            prior_usage_by_session[mid or args.session_id] = prior_usage_by_session.get(mid or args.session_id, False) or any(
                row and row.get("type") == "token_usage_record" for _, row, _, _ in rows[:prior_start]
            )
        else:
            # A direct root_turn_id record can be included without child lifecycle,
            # but cannot prove the child's earlier cumulative scope was empty.
            prior_usage_by_session[mid or args.session_id] = True
        contexts = {}
        configured_tier = None
        occurrences = defaultdict(int)
        signatures = []
        included_indices = []
        unbounded_usage = False
        for i, (line, row, bad, tr) in enumerate(rows):
            if not row:
                continue
            typ = row.get("type")
            p = row.get("payload") or {}
            if not isinstance(p, dict):
                continue
            tid = _turn(p)
            if typ == "turn_context" and tid is not None and (str(tid) in accepted_turns or path not in root_paths):
                contexts[str(tid)] = {"model": p.get("model") or p.get("model_name"), "effort": p.get("reasoning_effort") or p.get("effort")}
            fingerprint = None
            if typ in ("event_msg", "response_item", "compacted") or (typ == "turn_context" and "configured_service_tier" in p):
                fingerprint = hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
                occurrences[fingerprint] += 1
                event_key = (mid or args.session_id, fingerprint, occurrences[fingerprint])
                event_id = f"{mid or args.session_id}:{fingerprint[:24]}:{occurrences[fingerprint]}"
            if typ == "compacted":
                timestamp = _row_time(p, row)
                scoped = (local_start[0] <= i <= local_end[0]) if path in root_paths else (
                    start_time is not None and end_time is not None and timestamp is not None
                    and start_time <= timestamp <= end_time
                    and (any(lo < i < hi for lo, hi in accepted_ranges) or exact_in_scope))
                marker_key = event_key
                if scoped and marker_key not in marker_keys:
                    marker_keys.add(marker_key)
                    compact_markers.append({"agent": "root" if path in root_paths else agent, "session_id": mid or args.session_id, "source_file": aliases[path], "line": line, "timestamp": timestamp, "turn_id": str(tid) if tid is not None else None, "root_turn_id": args.turn_id, "event_id": event_id})
            timestamp = _row_time(p, row)
            scoped_event = (local_start[0] <= i <= local_end[0]) if path in root_paths else (
                start_time is not None and end_time is not None and timestamp is not None
                and start_time <= timestamp <= end_time
                and (any(lo <= i <= hi for lo, hi in accepted_ranges) or exact_in_scope))
            if scoped_event and (typ in ("event_msg", "response_item", "compacted") or (typ == "turn_context" and "configured_service_tier" in p)):
                signatures.append((fingerprint, occurrences[fingerprint]))
            if scoped_event and (typ in ("event_msg", "response_item", "compacted") or (typ == "turn_context" and "configured_service_tier" in p)) and event_key not in event_keys:
                event_keys.add(event_key)
                item = {"event_type": p.get("type") or typ, "session_id": mid or args.session_id,
                        "agent": "root" if path in root_paths else agent, "turn_id": str(tid) if tid is not None else (next(iter(accepted_turns)) if len(accepted_turns) == 1 else None),
                        "timestamp": timestamp, "source": {"file": aliases[path], "line": line}, "root_turn_id": args.turn_id, "event_id": event_id, "order": {"basis": "source_line", "cross_session": "unproven"}}
                for key in ("name", "call_id", "result_id", "id", "target_thread_id", "parent_thread_id", "fork_turns", "fork_provenance", "spawn_model", "spawn_effort", "result_chars", "result_bytes", "mixed", "images", "opaque", "truncated", "result_truncated", "source_line_incomplete", "quota", "configured_service_tier"):
                    if key in p:
                        item[key] = p[key]
                if p.get("type") in ("function_call_output", "tool_result", "custom_tool_call_output"):
                    item.setdefault("source_line_incomplete", bool(tr))
                events.append(item)
            if scoped_event and "configured_service_tier" in p:
                configured_tier = {"value": p["configured_service_tier"], "source": {"file": aliases[path], "line": line}, "provenance": p.get("type") or typ}
            if scoped_event and typ in ("event_msg", "response_item") and p.get("type") in ("tool_call", "tool_use", "function_call", "custom_tool_call"):
                name = p.get("name") or p.get("tool_name")
                if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name):
                    tool_names[path].append(name)
            if typ != "token_usage_record":
                continue
            # Exclude root telemetry outside the selected lifecycle interval.
            if path in root_paths and not (local_start[0] < i < local_end[0]):
                continue
            timestamp = _row_time(p, row)
            exact_root_scope = False
            if p.get("thread_id") is not None and str(p["thread_id"]) != str(mid or args.session_id):
                raise AuditError(f"usage thread identity conflicts with session metadata at line {line}")
            if path not in root_paths:
                if start_time is None or end_time is None or timestamp is None:
                    unbounded_usage = True
                    continue
                if not start_time <= timestamp <= end_time:
                    continue
                root_turn = p.get("root_turn_id")
                if root_turn is not None and str(root_turn) != str(args.turn_id):
                    continue
                if root_turn is not None and timestamp is not None:
                    exact_root_scope = start_time is not None and end_time is not None and start_time <= timestamp <= end_time
                    if not exact_root_scope:
                        continue
                elif not any(lo < i < hi for lo, hi in accepted_ranges):
                    continue
            snapshot = p.get("thread_token_usage")
            if isinstance(snapshot, dict):
                cumulative_by_session[mid or args.session_id] = snapshot
            local_turn = str(tid) if tid is not None else str(args.turn_id)
            if local_turn not in accepted_turns and not exact_root_scope:
                # Descendant records may carry root_turn_id without a local turn id.
                if path not in root_paths and len(accepted_turns) == 1 and (local_turn == str(args.turn_id) or tid is None):
                    local_turn = next(iter(accepted_turns))
                else:
                    continue
            u = p.get("usage")
            if not isinstance(u, dict):
                invalid += 1
                continue
            required = ("input_tokens", "cached_input_tokens", "output_tokens")
            if any(not _integer(u.get(k)) for k in required):
                invalid += 1
                continue
            inp, cached, out = (u[k] for k in required)
            if cached > inp or not _integer(u.get("reasoning_output_tokens")) and u.get("reasoning_output_tokens") is not None or (u.get("reasoning_output_tokens") is not None and u["reasoning_output_tokens"] > out):
                invalid += 1
                continue
            cache_write = u.get("cache_write_input_tokens")
            total = u.get("total_tokens")
            if cache_write is not None and (not _integer(cache_write) or cache_write > inp - cached):
                invalid += 1
                continue
            if total is not None and (not _integer(total) or total != inp + out):
                invalid += 1
                continue
            context = contexts.get(local_turn, {})
            response_id = p.get("response_id") or p.get("id")
            if response_id is None:
                no_response += 1
            req = {
                "response_id": str(response_id) if response_id is not None else None,
                "session_id": mid or args.session_id,
                "agent": "root" if path in root_paths else agent,
                "model": context.get("model"),
                "effort": context.get("effort"),
                "input_tokens": inp,
                "cached_input_tokens": cached,
                "new_tokens": inp - cached,
                "output_tokens": out,
                "reasoning_output_tokens": u.get("reasoning_output_tokens"),
                "total_tokens": inp + out,
                "cache_write_input_tokens": cache_write,
                "turn_id": local_turn,
                "root_turn_id": args.turn_id,
                "parent_id": _parent(meta),
                "model_provenance": "preceding turn_context",
                "configured_service_tier": configured_tier,
                "observed_service_tier": ({"value": p["observed_service_tier"], "source": {"file": aliases[path], "line": line}, "provenance": "response usage metadata"} if "observed_service_tier" in p else None),
                "timestamp": timestamp,
                "source": {"file": aliases[path], "line": line},
                "preceding_tool_names": list(tool_names[path][-5:]),
            }
            selected_records.append(req)
            tool_names[path] = []
            included_indices.append(i)
        stream_signatures[mid or args.session_id][aliases[path]] = signatures
        if unbounded_usage:
            missing_children += 1
        if included_indices:
            last = max(included_indices)
            excluded_prior = any(row and row.get("type") == "token_usage_record" and i not in included_indices
                                 for i, (_, row, _, _) in enumerate(rows[:last]))
            prior_usage_by_session[mid or args.session_id] = prior_usage_by_session.get(mid or args.session_id, False) or excluded_prior
    # Calls and results share a call ID within a session; only attach fork settings
    # to a child when its independently recorded parent link confirms the target.
    calls = {(e["session_id"], e.get("call_id")): e for e in events
             if e.get("name") in ("spawn_agent", "collaboration.spawn_agent", "collaboration__spawn_agent") and e.get("call_id")}
    spawn_records = {}
    known_sessions = {_meta_id(meta): _parent(meta) for _, (meta, _) in selected.items()}
    for event in events:
        target = event.get("target_thread_id")
        call = calls.get((event["session_id"], event.get("call_id")))
        if not target or call is None or event is call:
            continue
        if target not in known_sessions:
            if not args.root_only:
                missing_children += 1
                warnings.append("A recorded spawned child is missing a linked local session log; descendant coverage is incomplete.")
            continue
        if known_sessions[target] != event["session_id"]:
            warnings.append("A recorded spawn target conflicts with session parent metadata; fork parameters remain unknown.")
            continue
        spawn_records[target] = call
    uncertain_sessions = set()
    for item in events:
        if len(stream_signatures.get(item["session_id"], {})) > 1:
            item["source_copies_ambiguous"] = True
    for sid, streams in stream_signatures.items():
        if len(streams) > 1 and len({tuple(seq) for seq in streams.values()}) > 1:
            uncertain_sessions.add(sid)
    if uncertain_sessions:
        warnings.append("Session copies have differing event sequences; ordering across their source aliases is unproven.")
        for item in events:
            if item["session_id"] in uncertain_sessions:
                item["order"]["cross_source"] = "unproven"
        for marker in compact_markers:
            if marker["session_id"] in uncertain_sessions:
                marker["order_uncertain"] = True
    if missing_children:
        warnings.append(f"{missing_children} linked descendant session(s) had no timestamp-bounded completed turn in the root interval; descendant coverage is incomplete.")
    if malformed:
        warnings.append(f"{malformed} malformed JSONL line(s) were observed; source text omitted.")
    if truncated:
        warnings.append(f"{truncated} JSONL line(s) lacked a final newline and may be truncated.")
    if invalid:
        warnings.append(f"{invalid} usage record(s) had missing or invalid required counters and were excluded.")
    if no_response:
        warnings.append(f"{no_response} request(s) lacked response IDs; duplicate coverage is incomplete.")
    # Source-line order is proven only within one source; timestamps alone do not
    # establish an order for missing or equal timestamps across sessions.
    selected_records.sort(key=lambda r: (r["session_id"] != args.session_id, r["session_id"], r["source"]["file"], r["source"]["line"]))
    dedup = {}
    output = []
    for req in selected_records:
        rid = req["response_id"]
        if rid is None:
            output.append(req)
            continue
        previous = dedup.get(rid)
        if previous is None:
            dedup[rid] = req
            output.append(req)
            continue
        identity_fields = ("session_id", "agent", "turn_id")
        if any(previous[k] != req[k] for k in identity_fields):
            raise AuditError("response ID has conflicting session or turn ownership")
        counts = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "cache_write_input_tokens")
        if all(previous[k] == req[k] for k in counts):
            duplicate_count += 1
            continue
        revised_count += 1
        # Timestamp wins when available; otherwise later encountered record is latest.
        if previous["timestamp"] is None or (req["timestamp"] is not None and req["timestamp"] >= previous["timestamp"]):
            output[output.index(previous)] = req
            dedup[rid] = req
    if duplicate_count:
        warnings.append(f"{duplicate_count} identical duplicate response record(s) counted once.")
    if revised_count:
        warnings.append(f"{revised_count} revised response record(s) deduplicated to the latest chronological version.")
    for req in output:
        streams = stream_signatures.get(req["session_id"], {})
        if streams and req["source"]["file"] != next(iter(streams)):
            uncertain_sessions.add(req["session_id"])
    if uncertain_sessions:
        warning = "Session copies have differing event sequences; ordering across their source aliases is unproven."
        if warning not in warnings:
            warnings.append(warning)
        for event in events:
            if event["session_id"] in uncertain_sessions:
                event["order"]["cross_source"] = "unproven"
        for marker in compact_markers:
            if marker["session_id"] in uncertain_sessions:
                marker["order_uncertain"] = True
    rates = _load_rates(args.rates)
    for req in output:
        req["normalized"] = _normalization(req, rates)
    for req in output:
        if req["session_id"] in uncertain_sessions:
            req["order_uncertain"] = True
    # Timelines contain numeric metadata and evidence pointers only.
    output.sort(key=lambda r: (r["session_id"] != args.session_id, r["session_id"], r["source"]["file"], r["source"]["line"]))
    rows = defaultdict(lambda: {"session_id": None, "agent": None, "model": None, "effort": None, "new_tokens": 0, "cached_input_tokens": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
    for req in output:
        key = (req["session_id"], req["model"], req["effort"])
        agg = rows[key]
        agg.update(dict(zip(("session_id", "model", "effort"), key)))
        agg["agent"] = req["agent"]
        for field in ("new_tokens", "cached_input_tokens", "input_tokens", "output_tokens", "total_tokens"):
            agg[field] += req[field]
    normalized_rows = []
    for key, agg in rows.items():
        known = [r for r in output if (r["session_id"], r["model"], r["effort"]) == key and r["normalized"] is not None]
        vals = {k: str(sum((Decimal(r["normalized"][k]) for r in known), Decimal(0))) for k in ("new_tokens", "cached_tokens", "input_tokens", "output_tokens", "total_tokens")}
        normalized_rows.append({"session_id": key[0], "agent": agg["agent"], "model": key[1], "effort": key[2], "normalized": vals if known else None, "complete": len(known) == sum(1 for r in output if (r["session_id"], r["model"], r["effort"]) == key)})
    if rates:
        for row in normalized_rows:
            if not row["complete"]:
                warnings.append(f"Normalized total for {row['agent']} / {row['model'] or 'unknown'} is partial: unknown model or cache-write retention split.")
    # Root-only context metrics exclude descendants and any unresolved counters.
    root_reqs = [r for r in output if r["session_id"] == args.session_id]
    root_growth = []
    for previous, current in zip(root_reqs, root_reqs[1:]):
        delta = current["input_tokens"] - previous["input_tokens"]
        if delta > 0:
            root_growth.append({"increase_tokens": delta, "from": previous["source"], "to": current["source"], "preceding_tool_names": current["preceding_tool_names"]})
    root_growth.sort(key=lambda event: event["increase_tokens"], reverse=True)
    inputs = [r["input_tokens"] for r in root_reqs]
    ordered = sorted(inputs)
    mean = (sum(inputs) / len(inputs)) if inputs else None
    median = ((ordered[(len(ordered)-1)//2] + ordered[len(ordered)//2]) / 2) if ordered else None
    cache_den = sum(r["input_tokens"] for r in root_reqs)
    root_metrics = {"calls": len(root_reqs), "first_input_tokens": inputs[0] if inputs else None, "last_input_tokens": inputs[-1] if inputs else None, "mean_input_tokens": mean, "median_input_tokens": median, "max_input_tokens": max(inputs) if inputs else None, "cache_hit_fraction": (sum(r["cached_input_tokens"] for r in root_reqs) / cache_den) if cache_den else None, "counts_at_least": {str(n): sum(1 for x in inputs if x >= n) for n in (100000, 200000, 300000, 400000)}, "cached_input_sum_tokens": sum(r["cached_input_tokens"] for r in root_reqs)}
    for marker in compact_markers:
        before = [r for r in output if r["source"]["file"] == marker["source_file"] and r["source"]["line"] < marker["line"]]
        after = [r for r in output if r["source"]["file"] == marker["source_file"] and r["source"]["line"] > marker["line"]]
        marker["before_requests"] = len(before)
        marker["after_requests"] = len(after)
        marker["before_input_tokens"] = sum(r["input_tokens"] for r in before)
        marker["after_input_tokens"] = sum(r["input_tokens"] for r in after)
        marker["before_cached_input_tokens"] = sum(r["cached_input_tokens"] for r in before)
        marker["after_cached_input_tokens"] = sum(r["cached_input_tokens"] for r in after)

    if not output:
        raise AuditError("no usable request-level token usage found in selected task")
    if any(r["model"] is None or r["effort"] is None for r in output):
        warnings.append("One or more requests lack a model or effort in preceding local turn_context; no defaults were inferred.")
    reconciliation = {"status": "not_comparable", "reason": "Cumulative counter scope cannot be proven to match selected request records."}
    raw_totals = {field: sum(r[field] for r in output) for field in ("new_tokens", "cached_input_tokens", "input_tokens", "output_tokens", "total_tokens")}
    normalized_requests = [r for r in output if r["normalized"] is not None]
    normalized_values = {field: str(sum((Decimal(r["normalized"][field]) for r in normalized_requests), Decimal(0))) for field in ("new_tokens", "cached_tokens", "input_tokens", "output_tokens", "total_tokens")}
    normalized_total = {"values": normalized_values, "complete": len(normalized_requests) == len(output)} if rates else None
    root_task_started_before = any(
        row and row.get("type") == "event_msg"
        and (row.get("payload") or {}).get("type") == "task_started"
        and i < start[0]
        for i, (_, row, _, _) in enumerate(root_rows)
    )
    session_totals = defaultdict(lambda: {k: 0 for k in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")})
    for req in output:
        for field in session_totals[req["session_id"]]:
            session_totals[req["session_id"]][field] += req[field]
    scope_complete = (not root_task_started_before and all(not prior_usage_by_session.get(sid, True) for sid in session_totals)
                      and missing_children == 0 and invalid == 0 and malformed == 0 and truncated == 0 and no_response == 0)
    comparisons = []
    if scope_complete and set(cumulative_by_session) >= set(session_totals):
        for sid, totals in session_totals.items():
            snapshot = cumulative_by_session.get(sid) or {}
            compared = {field: {"requests": totals[field], "cumulative": snapshot.get(field)} for field in totals}
            matches = all(_integer(snapshot.get(field)) and snapshot[field] == totals[field] for field in totals)
            comparisons.append({"session_id": sid, "status": "matched" if matches else "mismatch", "counters": compared})
        reconciliation = {"status": "matched" if all(c["status"] == "matched" for c in comparisons) else "mismatch", "reason": "First root task, no excluded prior requests in compared sessions, and complete discoverable linked-session snapshots." if all(c["status"] == "matched" for c in comparisons) else "At least one thread cumulative snapshot differs from the selected request total.", "compared_sessions": len(comparisons), "sessions": comparisons}
    coverage = {"status": "incomplete" if warnings else "complete", "warnings": warnings, "descendant_scope": "Only linked session files available under the selected sessions directory are discoverable; absent logs cannot be distinguished from no descendants.", "malformed_lines": malformed, "truncated_lines": truncated, "invalid_records": invalid, "missing_response_ids": no_response, "identical_duplicates": duplicate_count, "revised_responses": revised_count, "descendant_files_outside_or_unproven": missing_children}
    report = {"schema_version": 1, "scope": {"session_id": args.session_id, "turn_id": args.turn_id, "task_start_line": start[2], "task_terminal_line": end[2], "terminal_type": end[3], "root_only": args.root_only, "source_file": aliases[root_path], "start_timestamp": start_time, "terminal_timestamp": end_time}, "provenance": {"models": "client turn_context telemetry; not server attestation", "usage": "token_usage_record.payload.usage only", "rates": ({"scenario": rates["scenario"], "verified_at": rates["verified_at"], "source": rates["source"], "reference_model": rates["reference_model"]} if rates else None)}, "coverage": coverage, "agents": sorted({r["agent"] for r in output}), "rows": list(rows.values()), "raw_totals": raw_totals, "normalized_rows": normalized_rows, "normalized_total": normalized_total, "requests": output, "root_context_metrics": root_metrics, "top_input_growth_events": root_growth[:10], "compaction_segments": compact_markers, "reconciliation": reconciliation}
    report["agent_metadata"] = [{"session_id": _meta_id(meta) or args.session_id, "agent": agent,
                                  "parent_id": _parent(meta), "parent_provenance": "session_meta.source.subagent.thread_spawn" if _parent(meta) else None,
                                  "fork_turns": None, "fork_provenance": None}
                                 for path, (meta, agent) in selected.items() if path == root_path or path not in root_paths]
    report["agent_metadata"] = list({item["session_id"]: item for item in report["agent_metadata"]}.values())
    for item in report["agent_metadata"]:
        spawn = spawn_records.get(item["session_id"], {})
        item["fork_turns"] = spawn.get("fork_turns")
        item["fork_provenance"] = spawn.get("fork_provenance")
        item["spawn_model"] = spawn.get("spawn_model")
        item["spawn_effort"] = spawn.get("spawn_effort")
        item["spawn_source"] = spawn.get("source")
    report["events"] = events
    report["compaction_markers"] = copy.deepcopy(compact_markers)
    return report


def collect_task(args):
    turns = args.turn_id if isinstance(args.turn_id, list) else [args.turn_id]
    if not _safe_identifier(args.session_id) or any(not _safe_identifier(turn) for turn in turns):
        raise AuditError("session and turn IDs must be safe identifiers")
    if not turns or any(not turn for turn in turns) or len(set(turns)) != len(turns):
        raise AuditError("explicit unique turn IDs are required")
    reports = []
    for turn in turns:
        local = copy.copy(args)
        local.turn_id = turn
        reports.append(_collect_one(local))
    window_coverages = [{"turn_id": turn, **copy.deepcopy(report["coverage"])} for turn, report in zip(turns, reports)]
    window_reconciliations = [{"turn_id": turn, **copy.deepcopy(report["reconciliation"])} for turn, report in zip(turns, reports)]
    result = reports[0]
    result["scope"]["windows"] = [dict(r["scope"]) for r in reports]
    if len(reports) > 1:
        # Deduplicate shared descendants across the independently bounded windows.
        requests = []
        seen = {}
        for report in reports:
            for req in report["requests"]:
                rid = req["response_id"]
                key = (req["session_id"], rid) if rid else (req["session_id"], req["source"]["file"], req["source"]["line"])
                if key in seen:
                    old = seen[key]
                    if old["turn_id"] != req["turn_id"]:
                        raise AuditError("response ID has conflicting session or turn ownership")
                    old.setdefault("root_turn_ids", [old["root_turn_id"]])
                    if req["root_turn_id"] not in old["root_turn_ids"]:
                        old["root_turn_ids"].append(req["root_turn_id"])
                    if req["timestamp"] is not None and (old["timestamp"] is None or req["timestamp"] > old["timestamp"]):
                        req["root_turn_ids"] = old["root_turn_ids"]
                        requests[requests.index(old)] = req
                        seen[key] = req
                    continue
                seen[key] = req
                requests.append(req)
        requests.sort(key=lambda r: (r["session_id"] != args.session_id, r["session_id"], r["source"]["file"], r["source"]["line"]))
        result["requests"] = requests
        result["raw_totals"] = {k: sum(r[k] for r in requests) for k in ("new_tokens", "cached_input_tokens", "input_tokens", "output_tokens", "total_tokens")}
        groups = defaultdict(list)
        for req in requests:
            groups[(req["session_id"], req["model"], req["effort"])].append(req)
        result["rows"] = []
        result["normalized_rows"] = []
        for (sid, model, effort), group in groups.items():
            result["rows"].append({"session_id": sid, "agent": group[0]["agent"], "model": model, "effort": effort, **{k: sum(r[k] for r in group) for k in result["raw_totals"]}})
            known = [r["normalized"] for r in group if r["normalized"] is not None]
            fields = ("new_tokens", "cached_tokens", "input_tokens", "output_tokens", "total_tokens")
            values = {k: str(sum((Decimal(n[k]) for n in known), Decimal(0))) for k in fields}
            result["normalized_rows"].append({"session_id": sid, "agent": group[0]["agent"], "model": model, "effort": effort, "normalized": values if known else None, "complete": len(known) == len(group)})
        known = [r["normalized"] for r in requests if r["normalized"] is not None]
        if args.rates:
            result["normalized_total"] = {"values": {k: str(sum((Decimal(n[k]) for n in known), Decimal(0))) for k in ("new_tokens", "cached_tokens", "input_tokens", "output_tokens", "total_tokens")}, "complete": len(known) == len(requests)}
        for key in ("events", "compaction_markers"):
            merged = {}
            for report in reports:
                for item in report[key]:
                    source = item.get("source", {})
                    identity = item["event_id"]
                    if identity in merged:
                        old = merged[identity]
                        old.setdefault("root_turn_ids", [old["root_turn_id"]])
                        if item["root_turn_id"] not in old["root_turn_ids"]:
                            old["root_turn_ids"].append(item["root_turn_id"])
                    else:
                        merged[identity] = item
            result[key] = list(merged.values())
        result["compaction_segments"] = result["compaction_markers"]
        result["coverage"]["warnings"] = list(dict.fromkeys(w for r in reports for w in r["coverage"]["warnings"]))
        result["coverage"]["status"] = "incomplete" if result["coverage"]["warnings"] else "complete"
        for key in ("malformed_lines", "truncated_lines", "invalid_records", "missing_response_ids", "identical_duplicates", "revised_responses", "descendant_files_outside_or_unproven"):
            result["coverage"][key] = None
        result["coverage"]["counter_scope_note"] = "Per-window counts may overlap and cannot be summed into a union without record identity."
        result["reconciliation"] = {"status": "not_comparable", "reason": "Selected separate turn windows do not establish cumulative counter scope.", "per_window": window_reconciliations}
    result["coverage"]["per_window"] = window_coverages
    result["task_label"] = getattr(args, "task_label", None)
    try:
        runtime_evidence.validate_new_fields([], [], result["task_label"])
    except ValueError as exc:
        raise AuditError(str(exc)) from exc
    for req in result["requests"]:
        req["event_ids"] = []
    for event in result["events"]:
        source = event["source"]
        local_requests = [r for r in result["requests"] if r["session_id"] == event["session_id"]
                          and r["source"]["file"] == source["file"]
                          and event["root_turn_id"] in r.get("root_turn_ids", [r["root_turn_id"]])]
        before = [r for r in local_requests if r["source"]["line"] < source["line"]]
        after = [r for r in local_requests if r["source"]["line"] > source["line"]]
        event["previous_response_id"] = max(before, key=lambda r: r["source"]["line"])["response_id"] if before else None
        next_request = min(after, key=lambda r: r["source"]["line"]) if after else None
        event["next_response_id"] = next_request["response_id"] if next_request else None
        if next_request is not None:
            next_request["event_ids"].append(event["event_id"])
    if getattr(args, "markdown", False):
        result["_markdown"] = _build_markdown(result, _load_rates(args.rates))
    return result


def _list_turns(args):
    if not _safe_identifier(args.session_id):
        raise AuditError("session ID must be a safe identifier")
    files = _session_files(Path(args.sessions_dir).expanduser().absolute(), include_sibling_archive=not args.sessions_dir_explicit)
    candidates = _resolve_root(files, args.session_id)
    # Report lifecycle ids and safe timestamps only. No prompt-bearing fields.
    turns = {}
    for path, _ in candidates:
        for line, row, bad, _ in _read(path):
            if not row or row.get("type") != "event_msg":
                continue
            p = row.get("payload") or {}
            if not isinstance(p, dict) or p.get("type") not in ("task_started", "task_complete", "turn_aborted"):
                continue
            tid = _turn(p)
            if tid is None:
                continue
            item = turns.setdefault(str(tid), {"turn_id": str(tid), "started_at": None, "terminal_at": None, "terminal": None, "lines": []})
            t = _row_time(p, row)
            if p.get("type") == "task_started":
                item["started_at"] = t
            else:
                item["terminal_at"] = t
                item["terminal"] = p.get("type")
            item["lines"].append(line)
    if not turns:
        raise AuditError("no lifecycle turns found for requested session")
    print(json.dumps({"session_id": args.session_id, "turns": list(turns.values())}, ensure_ascii=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Audit bounded Codex token telemetry")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--turn-id", action="append")
    parser.add_argument("--sessions-dir")
    parser.add_argument("--output-dir")
    parser.add_argument("--rates", default=str(Path(__file__).resolve().parents[1] / "references/standard-short-2026-10-28.json"), help="API-price scenario; defaults to the bundled dated Sol comparison")
    parser.add_argument("--root-only", action="store_true")
    parser.add_argument("--list-turns", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--task-label", help="optional caller-supplied single-line label (1–120 characters)")
    parser.add_argument("--markdown", action="store_true")
    parser.add_argument("--drive-folder-id")
    parser.add_argument("--drive-transport", choices=("auto", "gws", "connector"))
    args = parser.parse_args(argv)
    args.sessions_dir_explicit = any(arg == "--sessions-dir" or arg.startswith("--sessions-dir=") for arg in (argv if argv is not None else sys.argv[1:]))
    if not args.sessions_dir:
        args.sessions_dir = os.path.join(os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex")), "sessions")
    try:
        if args.list_turns:
            _list_turns(args)
            return 0
        if not args.turn_id:
            raise AuditError("--turn-id is required unless --list-turns is used")
        if not args.output_dir:
            raise AuditError("--output-dir is required unless --list-turns is used")
        import bundle
        report = collect_task(args)
        try:
            out = bundle.write_bundle(report, args.output_dir, rates_path=args.rates, run_id=args.run_id, markdown=args.markdown)
        except bundle.BundleError as exc:
            raise AuditError(str(exc)) from exc
        print(f"Collection: {report['coverage']['status']}; package: {out}")
        import delivery_config
        try:
            delivery = delivery_config.resolve_delivery(args.drive_folder_id, args.drive_transport)
        except delivery_config.ConfigError:
            print("Delivery: blocked; invalid or inaccessible delivery configuration")
            return 0
        if delivery["drive_folder_id"] and delivery["transport"] == "connector":
            import drive_export
            print(drive_export.connector_instruction(out, delivery["drive_folder_id"]))
        elif delivery["drive_folder_id"]:
            import drive_export
            receipt = drive_export.upload_bundle(out, delivery["drive_folder_id"])
            if receipt.get("fallback_transport") == "connector":
                print(drive_export.connector_instruction(out, delivery["drive_folder_id"]))
            elif receipt.get("status") == "complete":
                location = receipt.get("folder_link") or receipt.get("folder_id") or "verified folder"
                print(f"Delivery: complete; {location}")
            else:
                print(f"Delivery: {receipt.get('status', 'blocked')}; {receipt.get('reason', 'delivery was not confirmed')}")
        else:
            print("Delivery: NOT CONFIGURED (no Drive folder ID)")
        return 0
    except AuditError as exc:
        print(f"audit_usage.py: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print("audit_usage.py: audit could not safely process the selected evidence", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
