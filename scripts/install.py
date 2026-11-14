#!/usr/bin/env python3
"""Install and manage the budget-aware-swarm agent skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import uuid
from pathlib import Path
from typing import Any


MARKER_START = "<!-- budget-aware-swarm:managed:start -->"
MARKER_END = "<!-- budget-aware-swarm:managed:end -->"
PARAGRAPH = (
    "Use this skill only when the user explicitly says “$budget-aware-swarm” or "
    "“следуй схеме работы Budget-Aware Swarm”."
)
SKILL_REL = Path("skills/budget-aware-swarm")
LINK_REL = Path(".agents/skills/budget-aware-swarm")
STATE_REL = Path(".local/state/budget-aware-swarm")
STATE_FILE = "install-state.json"
CONFIG_REL = Path(".codex/config.toml")
AGENTS_REL = Path(".codex/AGENTS.md")


class InstallError(Exception):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".budget-aware-swarm-", dir=path.parent)
    tmp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def run_git(checkout: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(checkout), *args],
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    if check and result.returncode:
        raise InstallError(f"Git command failed ({' '.join(args[:2])}).")
    return result


def checkout_sha(checkout: Path) -> str:
    result = run_git(checkout, "rev-parse", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else "unavailable"


def codex_version() -> str:
    executable = shutil.which("codex")
    if not executable:
        return "unavailable"
    try:
        result = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=3)
        if result.returncode == 0:
            return (result.stdout or result.stderr).strip().splitlines()[0][:160]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        pass
    return "unavailable"


def add_managed_paragraph(original: bytes) -> bytes:
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InstallError("Codex AGENTS.md is not valid UTF-8; refusing to modify it.") from exc
    start_count = text.count(MARKER_START)
    end_count = text.count(MARKER_END)
    if start_count or end_count:
        if start_count != 1 or end_count != 1 or text.index(MARKER_START) > text.index(MARKER_END):
            raise InstallError("Managed skill markers in Codex AGENTS.md are malformed.")
        block = f"{MARKER_START}\n{PARAGRAPH}\n{MARKER_END}"
        pattern = re.compile(re.escape(MARKER_START) + r".*?" + re.escape(MARKER_END), re.S)
        return pattern.sub(block, text, count=1).encode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    sep = "" if not text or text.endswith(("\n", "\r")) else newline
    block = newline.join((MARKER_START, PARAGRAPH, MARKER_END))
    return (text + sep + block + newline).encode("utf-8")


def _replace_assignment(line: str, key: str, value: str, target_key: str | None = None) -> str:
    key_expr = rf"(?:{re.escape(key)}|\"{re.escape(key)}\"|'{re.escape(key)}')"
    match = re.match(rf"^(\s*{key_expr}\s*=\s*)([^#\r\n]*?)(\s*(?:#.*)?)$", line)
    if not match:
        return line
    prefix = match.group(1)
    if target_key is not None:
        prefix = re.sub(r"(?:max_threads|\"max_threads\"|'max_threads')(?=\s*=)", target_key, prefix)
    return prefix + value + match.group(3)


def reject_v2_override(parsed: dict[str, Any]) -> None:
    features = parsed.get("features", {})
    v2 = features.get("multi_agent_v2", {}) if isinstance(features, dict) else None
    if isinstance(v2, dict) and "max_concurrent_threads_per_session" in v2:
        raise InstallError(
            "Explicit features.multi_agent_v2 concurrency overrides the managed root-inclusive setting; remove it or manage the V2 limit explicitly."
        )


def configured_cap(parsed: dict[str, Any], *, clamp: bool = False) -> int | None:
    reject_v2_override(parsed)
    agents = parsed.get("agents", {})
    if not isinstance(agents, dict):
        raise InstallError("Codex [agents] must be a TOML table; refusing to modify it.")
    canonical = agents.get("max_concurrent_threads_per_session")
    alias = agents.get("max_threads")
    if canonical is not None and alias is not None:
        raise InstallError("Codex [agents] defines both concurrency keys; refusing an ambiguous rewrite.")
    value = canonical if canonical is not None else alias
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InstallError("Codex [agents] concurrency must be a positive integer.")
    if value > 49 and not clamp:
        raise InstallError("Codex [agents] concurrency exceeds the supported limit of 49 child threads.")
    return min(value, 49)


def legacy_original_cap(parsed: dict[str, Any]) -> int | None:
    reject_v2_override(parsed)
    agents = parsed.get("agents", {})
    if not isinstance(agents, dict):
        raise InstallError("Codex [agents] must be a TOML table; refusing migration.")
    caps = [
        value
        for key in ("max_concurrent_threads_per_session", "max_threads")
        if isinstance((value := agents.get(key)), int) and not isinstance(value, bool) and value > 0
    ]
    return min(49, *caps) if caps else None


def update_config(original: bytes, concurrency: int | None = None) -> bytes:
    try:
        text = original.decode("utf-8")
        parsed = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise InstallError("Codex config.toml is invalid TOML or not UTF-8; refusing to modify it.") from exc
    existing_cap = configured_cap(parsed, clamp=True)
    if concurrency is not None and (isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 49):
        raise InstallError("--concurrency must be an integer from 1 through 49 child threads.")
    cap = concurrency if concurrency is not None else min(existing_cap or 49, 49)

    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    table_start = None
    table_end = len(lines)
    for idx, line in enumerate(lines):
        if re.match(r"^\[agents\]\s*(?:#.*)?(?:\r?\n)?$", line):
            if table_start is not None:
                raise InstallError("Codex config.toml contains duplicate [agents] tables.")
            table_start = idx
        elif table_start is not None and re.match(r"^\s*(?:\[[^\]]+\]|\[\[[^\]]+\]\])\s*(?:#.*)?(?:\r?\n)?$", line):
            table_end = idx
            break

    if table_start is None:
        if "agents" in parsed:
            raise InstallError("Codex agents settings use an inline or implicit table; refusing an unsafe rewrite.")
        suffix = "" if not text or text.endswith(("\n", "\r")) else newline
        addition = f"{suffix}[agents]{newline}enabled = true{newline}max_concurrent_threads_per_session = {cap}{newline}"
        result = (text + addition).encode("utf-8")
        try:
            tomllib.loads(result.decode("utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise InstallError("Could not safely add Codex [agents] settings.") from exc
        return result

    found = {"enabled": False, "max_concurrent_threads_per_session": False}
    for idx in range(table_start + 1, table_end):
        line = lines[idx]
        for key, value in (("enabled", "true"), ("max_concurrent_threads_per_session", str(cap))):
            aliases = (key, "max_threads") if key == "max_concurrent_threads_per_session" else (key,)
            key_expr = "(?:" + "|".join(
                [re.escape(item) for item in aliases]
                + [f'"{item}"' for item in aliases]
                + [f"'{item}'" for item in aliases]
            ) + ")"
            if re.match(rf"^\s*{key_expr}\s*=", line):
                if found[key]:
                    raise InstallError(f"Codex [agents] has duplicate {key} keys.")
                lines[idx] = _replace_assignment(line.rstrip("\r\n"), "max_threads" if re.match(r"^\s*(?:max_threads|\"max_threads\"|'max_threads')\s*=", line) else key, value, key) + ("\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else "")
                found[key] = True
    insertions = []
    if not found["enabled"]:
        insertions.append(f"enabled = true{newline}")
    if not found["max_concurrent_threads_per_session"]:
        insertions.append(f"max_concurrent_threads_per_session = {cap}{newline}")
    if insertions:
        # Put missing settings at the end of [agents], before the next table.
        insert_at = table_end
        if insert_at > table_start + 1 and not lines[insert_at - 1].endswith(("\n", "\r")):
            insertions.insert(0, newline)
        lines[insert_at:insert_at] = insertions
    result = "".join(lines).encode("utf-8")
    try:
        tomllib.loads(result.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise InstallError("Could not safely update Codex config.toml.") from exc
    return result


def state_paths(home: Path) -> tuple[Path, Path]:
    directory = home / STATE_REL
    return directory, directory / STATE_FILE


def read_state(path: Path) -> dict[str, Any] | None:
    if path.is_symlink():
        raise InstallError("Installer state path is a symlink; refusing to follow it.")
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError("Installer state is unreadable; no files were changed.") from exc


def assert_managed_install(home: Path, state: dict[str, Any], *, allow_interrupted: bool = False) -> None:
    for rel, record in state.get("files", {}).items():
        path = home / rel
        after_matches = not path.is_symlink() and path.is_file() and digest(path.read_bytes()) == record.get("after_sha256")
        before_matches = False
        if allow_interrupted and state.get("phase") in ("applying", "rolling_back"):
            before_matches = (not path.is_symlink() and path.is_file() and record.get("exists") and digest(path.read_bytes()) == record.get("before_sha256")) or (not path.exists() and not path.is_symlink() and not record.get("exists"))
        if not after_matches and not before_matches:
            raise InstallError(f"Managed file changed since installation: {rel}")
    link = home / LINK_REL
    link_matches = link.is_symlink() and os.readlink(link) == state.get("link_target")
    interrupted_link_ok = allow_interrupted and state.get("phase") in ("applying", "rolling_back") and state.get("link_owned") and not link.exists() and not link.is_symlink()
    if not link_matches and not interrupted_link_ok:
        raise InstallError("Managed skill link changed since installation.")


def managed_invariants(home: Path, checkout: Path, state: dict[str, Any], *, allow_legacy_alias: bool = False) -> None:
    if state.get("checkout") != str(checkout):
        raise InstallError("Installed checkout does not match the requested checkout.")
    skill = checkout / SKILL_REL
    link = home / LINK_REL
    if not skill.is_dir() or not link.is_symlink() or os.readlink(link) != str(skill):
        raise InstallError("Managed skill link is invalid.")

    agents_path = home / AGENTS_REL
    config_path = home / CONFIG_REL
    if agents_path.is_symlink() or config_path.is_symlink():
        raise InstallError("A managed Codex configuration file is now a symlink.")
    try:
        agents_text = agents_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise InstallError("Managed Codex instruction file is unavailable or invalid UTF-8.") from exc
    if agents_text.count(MARKER_START) != 1 or agents_text.count(MARKER_END) != 1:
        raise InstallError("Managed skill markers are missing or duplicated.")
    managed_paragraph = re.search(
        re.escape(MARKER_START) + r"\r?\n(.*?)\r?\n" + re.escape(MARKER_END),
        agents_text,
        re.S,
    )
    if not managed_paragraph or managed_paragraph.group(1) != PARAGRAPH:
        raise InstallError("Managed skill trigger paragraph was changed.")

    try:
        parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise InstallError("Codex config.toml is unavailable, invalid UTF-8, or malformed TOML.") from exc
    agents = parsed.get("agents", {})
    if not isinstance(agents, dict):
        raise InstallError("Codex [agents] table is invalid.")
    if agents.get("enabled") is not True:
        raise InstallError("Managed Codex [agents].enabled value was changed.")
    if "max_threads" in agents:
        canonical, alias = agents.get("max_concurrent_threads_per_session"), agents.get("max_threads")
        if (
            not allow_legacy_alias
            or isinstance(canonical, bool)
            or not isinstance(canonical, int)
            or isinstance(alias, bool)
            or not isinstance(alias, int)
            or canonical != state.get("agents_cap")
            or canonical > alias
            or canonical > 4
        ):
            raise InstallError("Managed Codex config reintroduced the legacy max_threads alias.")
        reject_v2_override(parsed)
    elif configured_cap(parsed) != state.get("agents_cap"):
        raise InstallError("Managed Codex agent concurrency cap was changed.")


def full_file_hashes_match(home: Path, state: dict[str, Any]) -> bool:
    for rel, record in state.get("files", {}).items():
        path = home / rel
        if path.is_symlink() or not path.is_file() or digest(path.read_bytes()) != record.get("after_sha256"):
            return False
    return True


def validate_state_parent_paths(home: Path) -> tuple[Path, Path]:
    directory, state_file = state_paths(home)
    for path in (home / ".local", home / ".local/state", directory):
        if path.is_symlink():
            raise InstallError(f"Refusing symlinked installer state path: {path.relative_to(home)}")
        if path.exists() and not path.is_dir():
            raise InstallError(f"Installer state parent is not a directory: {path.relative_to(home)}")
    if state_file.is_symlink():
        raise InstallError("Refusing symlinked installer state file.")
    if directory.is_dir():
        for backup in directory.glob("backup-*.bin"):
            if backup.is_symlink():
                raise InstallError("Refusing symlinked private rollback backup.")
    return directory, state_file


def write_state_file(path: Path, state: dict[str, Any]) -> None:
    atomic_write(path, (json.dumps(state, sort_keys=True, indent=2) + "\n").encode(), 0o600)


def recover_migration(home: Path, state_file: Path, state: dict[str, Any]) -> dict[str, Any]:
    journal = state.get("migration_journal")
    if state.get("phase") != "migrating" or not isinstance(journal, dict):
        return state
    config = home / CONFIG_REL
    if config.is_symlink() or not config.is_file():
        raise InstallError("Interrupted config migration has an unavailable target; manual recovery is required.")
    current_hash = digest(config.read_bytes())
    backup = state_file.parent / journal["backup"]
    if backup.is_symlink() or not backup.is_file() or digest(backup.read_bytes()) != journal["before_sha256"]:
        raise InstallError("Interrupted config migration backup is missing or corrupt; preserving files for manual recovery.")
    if current_hash == journal["before_sha256"]:
        previous = journal["previous_state"]
        write_state_file(state_file, previous)
        backup.unlink(missing_ok=True)
        return previous
    if current_hash == journal["after_sha256"]:
        committed = journal["committed_state"]
        write_state_file(state_file, committed)
        backup.unlink(missing_ok=True)
        return committed
    raise InstallError("Interrupted config migration found unknown later bytes; preserving files for manual recovery.")


def remove_proven_legacy_alias(original: bytes, state_cap: int) -> bytes:
    try:
        text = original.decode("utf-8")
        parsed = tomllib.loads(text)
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise InstallError("Legacy Codex config is invalid; refusing alias normalization.") from exc
    agents = parsed.get("agents", {})
    if not isinstance(agents, dict):
        raise InstallError("Legacy Codex [agents] table is invalid.")
    canonical, alias = agents.get("max_concurrent_threads_per_session"), agents.get("max_threads")
    if (
        isinstance(canonical, bool)
        or not isinstance(canonical, int)
        or isinstance(alias, bool)
        or not isinstance(alias, int)
        or canonical != state_cap
        or canonical < 1
        or canonical > alias
        or canonical > 4
        or alias < 1
    ):
        raise InstallError("Legacy dual concurrency keys do not match the old installer output; refusing normalization.")
    lines = text.splitlines(keepends=True)
    start = end = None
    for idx, line in enumerate(lines):
        if re.match(r"^\[agents\]\s*(?:#.*)?(?:\r?\n)?$", line):
            start = idx
        elif start is not None and re.match(r"^\s*(?:\[[^\]]+\]|\[\[[^\]]+\]\])\s*(?:#.*)?(?:\r?\n)?$", line):
            end = idx
            break
    if start is None:
        raise InstallError("Legacy dual concurrency keys use an unsafe table form.")
    end = len(lines) if end is None else end
    found = 0
    for idx in range(start + 1, end):
        line = lines[idx]
        match = re.match(r"^(\s*)(?:max_threads|\"max_threads\"|'max_threads')\s*=.*?(\s*(?:#.*)?)?(\r?\n)?$", line)
        if match:
            found += 1
            comment = (match.group(2) or "").strip()
            newline = match.group(3) or ""
            lines[idx] = f"{match.group(1)}{comment}{newline}" if comment else ""
    if found != 1:
        raise InstallError("Legacy config has an ambiguous max_threads assignment; refusing normalization.")
    return "".join(lines).encode("utf-8")


def validate_legacy_alias_provenance(current: bytes, original: bytes, state_cap: int) -> None:
    try:
        original_config = tomllib.loads(original.decode("utf-8"))
        current_config = tomllib.loads(current.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise InstallError("Legacy alias provenance is invalid; refusing normalization.") from exc
    reject_v2_override(original_config)
    original_agents = original_config.get("agents", {})
    current_agents = current_config.get("agents", {})
    if not isinstance(original_agents, dict) or not isinstance(current_agents, dict):
        raise InstallError("Legacy alias provenance has an invalid [agents] table.")
    original_alias = original_agents.get("max_threads")
    current_alias = current_agents.get("max_threads")
    caps = [
        value for key in ("max_concurrent_threads_per_session", "max_threads")
        if isinstance((value := original_agents.get(key)), int) and not isinstance(value, bool) and value > 0
    ]
    expected_cap = min([4, *caps])
    if (
        isinstance(original_alias, bool)
        or not isinstance(original_alias, int)
        or original_alias < 1
        or current_alias != original_alias
        or current_agents.get("max_concurrent_threads_per_session") != expected_cap
        or state_cap != expected_cap
    ):
        raise InstallError("Legacy max_threads differs from its recorded pre-install value; preserving config for manual review.")


def migrate_managed_cap(home: Path, state_file: Path, state: dict[str, Any], concurrency: int | None) -> dict[str, Any]:
    version = state.get("version")
    if version not in (1, 2) or (version == 2 and concurrency is None):
        return state
    config_path = home / CONFIG_REL
    if config_path.is_symlink() or not config_path.is_file():
        raise InstallError("Legacy managed config is unavailable; refusing migration.")
    current = config_path.read_bytes()
    record = state.get("files", {}).get(str(CONFIG_REL))
    if not isinstance(record, dict):
        raise InstallError("Legacy installer state has no config snapshot; refusing migration.")
    original_bytes: bytes | None = None
    if record.get("exists"):
        original_backup = state_file.parent / record.get("backup", "")
        if original_backup.is_symlink() or not original_backup.is_file() or digest(original_backup.read_bytes()) != record.get("before_sha256"):
            raise InstallError("Private original config backup is missing or corrupt; refusing migration.")
        original_bytes = original_backup.read_bytes()
    if concurrency is not None and (isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 49):
        raise InstallError("--concurrency must be an integer from 1 through 49 child threads.")
    rollback_blocked = bool(state.get("rollback_blocked")) or not full_file_hashes_match(home, state)
    target = concurrency if concurrency is not None else state.get("agents_cap", 49)
    if version == 1 and concurrency is None:
        target = 49
        if record.get("exists"):
            try:
                original_config = tomllib.loads(original_bytes.decode("utf-8"))
                original_cap = legacy_original_cap(original_config)
            except (UnicodeError, tomllib.TOMLDecodeError) as exc:
                raise InstallError("Legacy private config backup is invalid; refusing migration.") from exc
            if original_cap is not None:
                target = min(49, original_cap)
    source = current
    parsed_current = tomllib.loads(current.decode("utf-8"))
    current_agents = parsed_current.get("agents", {})
    if version == 1 and isinstance(current_agents, dict) and "max_threads" in current_agents:
        if original_bytes is None:
            raise InstallError("Legacy alias has no recorded original config; refusing normalization.")
        validate_legacy_alias_provenance(current, original_bytes, state.get("agents_cap"))
        source = remove_proven_legacy_alias(current, state.get("agents_cap"))
    updated = update_config(source, target)
    if updated == current:
        state["version"] = 2
        state["rollback_blocked"] = rollback_blocked
        state["agents_cap"] = target
        write_state_file(state_file, state)
        return state
    state_dir = state_file.parent
    journal_name = f"migration-{uuid.uuid4().hex}.bin"
    atomic_write(state_dir / journal_name, current, 0o600)
    committed = json.loads(json.dumps(state))
    committed["version"] = 2
    committed["phase"] = "applied"
    committed["agents_cap"] = target
    committed["rollback_blocked"] = rollback_blocked
    committed["files"][str(CONFIG_REL)]["after_sha256"] = digest(updated)
    journaled = json.loads(json.dumps(state))
    journaled["phase"] = "migrating"
    journaled["migration_journal"] = {
        "backup": journal_name,
        "before_sha256": digest(current),
        "after_sha256": digest(updated),
        "previous_state": state,
        "committed_state": committed,
    }
    try:
        write_state_file(state_file, journaled)
    except Exception:
        (state_dir / journal_name).unlink(missing_ok=True)
        raise
    try:
        atomic_write(config_path, updated, stat.S_IMODE(config_path.stat().st_mode))
        write_state_file(state_file, committed)
        (state_dir / journal_name).unlink(missing_ok=True)
        return committed
    except Exception as exc:
        try:
            current_hash = digest(config_path.read_bytes())
            if current_hash == digest(updated):
                atomic_write(config_path, current, stat.S_IMODE(config_path.stat().st_mode))
            elif current_hash != digest(current):
                raise InstallError("Config changed during failed migration; recovery journal was retained for manual review.")
            write_state_file(state_file, state)
            (state_dir / journal_name).unlink(missing_ok=True)
        except Exception as recovery_exc:
            raise InstallError("Config migration failed; private recovery journal was retained.") from recovery_exc
        raise InstallError("Config migration failed; prior config and state were restored.") from exc


def install(home: Path, checkout: Path, concurrency: int | None = None) -> None:
    skill = checkout / SKILL_REL
    if not skill.is_dir():
        raise InstallError("Checkout does not contain skills/budget-aware-swarm.")
    state_dir, state_file = validate_state_parent_paths(home)
    existing_state = read_state(state_file)
    if existing_state:
        existing_state = recover_migration(home, state_file, existing_state)
        managed_invariants(home, checkout, existing_state, allow_legacy_alias=existing_state.get("version") == 1)
        if existing_state.get("version") == 1:
            existing_state = migrate_managed_cap(home, state_file, existing_state, concurrency)
            managed_invariants(home, checkout, existing_state)
            print(f"Updated managed concurrency cap to {existing_state['agents_cap']} child threads ({existing_state['agents_cap'] + 1} total including root). Rollback {('blocked by preserved later edits' if existing_state.get('rollback_blocked') else 'remains available')}.")
            return
        if concurrency is not None:
            existing_state = migrate_managed_cap(home, state_file, existing_state, concurrency)
            managed_invariants(home, checkout, existing_state)
            print(f"Updated managed concurrency cap to {existing_state['agents_cap']} child threads ({existing_state['agents_cap'] + 1} total including root).")
            if existing_state.get("rollback_blocked"):
                print("rollback blocked by preserved later edits")
            return
        print("Install is already valid; no files changed.")
        if not full_file_hashes_match(home, existing_state):
            print("later edits present; full rollback blocked")
        return

    link = home / LINK_REL
    if link.is_symlink():
        if os.readlink(link) != str(skill):
            raise InstallError("Skill link path is occupied by a different symlink.")
        link_owned = False
    elif link.exists():
        raise InstallError("Skill link path is occupied; refusing to replace it.")
    else:
        link_owned = True
    # Refuse file-valued ancestors before any writes.
    for parent in (home / ".agents", home / ".agents/skills", home / ".codex"):
        if parent.is_symlink():
            raise InstallError(f"Refusing to use a symlinked parent path: {parent.relative_to(home)}")
        if parent.exists() and not parent.is_dir():
            raise InstallError(f"Required parent path is not a directory: {parent.relative_to(home)}")

    targets = (AGENTS_REL, CONFIG_REL)
    before: dict[str, dict[str, Any]] = {}
    changes: dict[str, tuple[bytes, int]] = {}
    for rel in targets:
        path = home / rel
        if path.is_symlink():
            raise InstallError(f"Refusing to modify symlinked configuration file: {rel}")
        exists = path.exists()
        old = path.read_bytes() if exists else b""
        old_mode = stat.S_IMODE(path.stat().st_mode) if exists else 0o600
        new = add_managed_paragraph(old) if rel == AGENTS_REL else update_config(old, concurrency)
        mode = old_mode if exists else 0o600
        before[str(rel)] = {"exists": exists, "mode": old_mode, "backup": f"backup-{uuid.uuid4().hex}.bin"}
        before[str(rel)]["before_sha256"] = digest(old) if exists else None
        changes[str(rel)] = (new, mode)

    ensure_private_dir(state_dir)
    # Store private originals and a recovery journal before replacing user files.
    try:
        for rel, record in before.items():
            if record["exists"]:
                atomic_write(state_dir / record["backup"], (home / rel).read_bytes(), 0o600)
        state = {
            "version": 2,
            "checkout": str(checkout),
            "link_target": str(skill),
            "link_owned": link_owned,
            "phase": "applying",
            "agents_cap": tomllib.loads(changes[str(CONFIG_REL)][0].decode("utf-8"))["agents"]["max_concurrent_threads_per_session"],
            "rollback_blocked": False,
            "files": {
                rel: {
                    **before[rel],
                    "after_sha256": digest(new),
                }
                for rel, (new, _mode) in changes.items()
            },
        }
        atomic_write(state_file, (json.dumps(state, sort_keys=True, indent=2) + "\n").encode(), 0o600)
        try:
            for rel, (new, mode) in changes.items():
                atomic_write(home / rel, new, mode)
            if link_owned:
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(skill)
            state["phase"] = "applied"
            atomic_write(state_file, (json.dumps(state, sort_keys=True, indent=2) + "\n").encode(), 0o600)
        except Exception:
            # Restore all original content and modes if any operation fails.
            try:
                for rel, record in before.items():
                    path = home / rel
                    if record["exists"]:
                        atomic_write(path, (state_dir / record["backup"]).read_bytes(), record["mode"])
                    else:
                        try:
                            path.unlink()
                        except FileNotFoundError:
                            pass
                if link_owned and link.is_symlink() and os.readlink(link) == str(skill):
                    link.unlink()
            except OSError as recovery_exc:
                raise InstallError("Install failed and automatic restoration was incomplete; private recovery state was retained.") from recovery_exc
            state_file.unlink(missing_ok=True)
            for record in before.values():
                (state_dir / record["backup"]).unlink(missing_ok=True)
            try:
                state_dir.rmdir()
            except OSError:
                pass
            raise
        cap = state["agents_cap"]
        print(f"Installed budget-aware-swarm with {cap} child threads ({cap + 1} total including root). Effective V2 cap remains unverified until Codex reads this config.")
    except Exception as exc:
        if isinstance(exc, InstallError):
            raise
        raise InstallError("Install failed; original configuration was restored where possible.") from exc


def rollback(home: Path) -> None:
    state_dir, state_file = validate_state_parent_paths(home)
    state = read_state(state_file)
    if not state:
        raise InstallError("No managed installation state found.")
    state = recover_migration(home, state_file, state)
    if state.get("rollback_blocked"):
        raise InstallError("Full rollback is blocked because later user edits were preserved during migration.")
    assert_managed_install(home, state, allow_interrupted=True)
    # Validate every backup before mutating anything.
    backups: dict[str, bytes] = {}
    for rel, record in state["files"].items():
        backup = state_dir / record["backup"]
        if record["exists"]:
            if backup.is_symlink() or not backup.is_file():
                raise InstallError("A private rollback backup is missing; no files were changed.")
            backups[rel] = backup.read_bytes()
            if digest(backups[rel]) != record.get("before_sha256"):
                raise InstallError("A private rollback backup is corrupt; no files were changed.")
    state["phase"] = "rolling_back"
    atomic_write(state_file, (json.dumps(state, sort_keys=True, indent=2) + "\n").encode(), 0o600)
    for rel, record in state["files"].items():
        path = home / rel
        if record["exists"]:
            atomic_write(path, backups[rel], record["mode"])
        else:
            path.unlink()
    link = home / LINK_REL
    if state.get("link_owned") and link.is_symlink() and os.readlink(link) == state.get("link_target"):
        link.unlink()
    state_file.unlink()
    for record in state["files"].values():
        try:
            (state_dir / record["backup"]).unlink()
        except FileNotFoundError:
            pass
    try:
        state_dir.rmdir()
    except OSError:
        pass
    print("Rolled back managed changes.")


def report(home: Path, checkout: Path) -> None:
    _state_dir, state_file = validate_state_parent_paths(home)
    state = read_state(state_file)
    if state:
        state = recover_migration(home, state_file, state)
    skill = checkout / SKILL_REL
    link = home / LINK_REL
    link_ok = skill.is_dir() and link.is_symlink() and os.readlink(link) == str(skill)
    marker_ok = False
    config_ok = False
    try:
        agents_path = home / AGENTS_REL
        if agents_path.is_symlink():
            raise OSError("symlink")
        agents_text = agents_path.read_text(encoding="utf-8")
        marker_ok = (
            agents_text.count(MARKER_START) == 1
            and agents_text.count(MARKER_END) == 1
            and bool(re.search(re.escape(MARKER_START) + r"\r?\n" + re.escape(PARAGRAPH) + r"\r?\n" + re.escape(MARKER_END), agents_text))
        )
    except (OSError, UnicodeError):
        pass
    try:
        config_path = home / CONFIG_REL
        if config_path.is_symlink():
            raise OSError("symlink")
        parsed_config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        config_agents = parsed_config.get("agents", {})
        config_ok = state is not None and isinstance(config_agents, dict) and config_agents.get("enabled") is True
        if config_ok and "max_threads" in config_agents and state.get("version") == 1:
            config_ok = config_agents.get("max_concurrent_threads_per_session") == state.get("agents_cap")
        elif config_ok:
            config_ok = "max_threads" not in config_agents and configured_cap(parsed_config) == state.get("agents_cap")
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, InstallError):
        pass
    invariants_ok = False
    invariant_error: str | None = None
    if state:
        try:
            managed_invariants(home, checkout, state, allow_legacy_alias=state.get("version") == 1)
            invariants_ok = True
        except (InstallError, OSError) as exc:
            invariant_error = str(exc)
    hashes_ok = bool(state) and full_file_hashes_match(home, state)
    print(f"checkout SHA: {checkout_sha(checkout)}")
    print(f"Codex version: {codex_version()}")
    print(f"skill link: {'valid' if link_ok else 'invalid'}")
    print(f"Codex AGENTS.md markers: {'valid' if marker_ok else 'invalid'}")
    print(f"Codex config: {'valid' if config_ok else 'invalid'}")
    print(f"managed invariants: {'valid' if invariants_ok else 'invalid'}")
    print(f"rollback snapshot: {'intact' if hashes_ok else 'changed'}")
    if invariants_ok and not hashes_ok:
        print("later edits present; full rollback blocked")
    if invariants_ok and state and state.get("rollback_blocked"):
        print("rollback blocked by preserved later edits")
    if state and state.get("agents_cap"):
        cap = state["agents_cap"]
        print(f"Concurrency cap: {cap} child threads ({cap + 1} total including root); effective V2 cap unverified until Codex reads config")
    if not invariants_ok:
        raise InstallError(invariant_error or "No managed installation state found.")


def update(checkout: Path, home: Path, concurrency: int | None = None) -> None:
    _state_dir, state_file = validate_state_parent_paths(home)
    state = read_state(state_file)
    if not state:
        raise InstallError("Update requires a valid installed skill.")
    state = recover_migration(home, state_file, state)
    managed_invariants(home, checkout, state, allow_legacy_alias=state.get("version") == 1)
    if not (checkout / ".git").exists():
        raise InstallError("Update requires a Git checkout.")
    status = run_git(checkout, "status", "--porcelain", "--untracked-files=all", check=False)
    if status.returncode or status.stdout.strip():
        raise InstallError("Update requires a clean checkout.")
    branch = run_git(checkout, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if branch.returncode:
        raise InstallError("Update refuses a detached HEAD.")
    upstream = run_git(checkout, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False)
    if upstream.returncode or not upstream.stdout.strip():
        raise InstallError("Update requires a configured upstream.")
    result = run_git(checkout, "pull", "--ff-only", check=False)
    if result.returncode:
        raise InstallError("Fast-forward update failed.")
    if not (checkout / SKILL_REL).is_dir():
        raise InstallError("Update completed, but the fetched checkout no longer contains the skill directory.")
    print(f"Updated checkout to {checkout_sha(checkout)}")
    updated_installer = checkout / "scripts/install.py"
    if not updated_installer.is_file():
        raise InstallError("Update completed, but the checkout has no installer migration entrypoint.")
    command = [sys.executable, str(updated_installer), "install", "--home", str(home), "--checkout", str(checkout)]
    if concurrency is not None:
        command.extend(("--concurrency", str(concurrency)))
    migration = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    if migration.returncode:
        raise InstallError("Checkout updated, but managed configuration migration failed: " + (migration.stderr.strip() or migration.stdout.strip()))
    if migration.stdout.strip():
        print(migration.stdout.strip())
    report(home, checkout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "verify", "status", "update", "rollback"))
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--concurrency", type=int, help="managed child-thread limit (1-49)")
    args = parser.parse_args(argv)
    home = args.home.expanduser().absolute()
    checkout = args.checkout.expanduser().absolute()
    try:
        if args.concurrency is not None:
            if args.command not in ("install", "update"):
                raise InstallError("--concurrency is only valid with install or update.")
            if not 1 <= args.concurrency <= 49:
                raise InstallError("--concurrency must be an integer from 1 through 49 child threads.")
        if args.command == "install":
            install(home, checkout, args.concurrency)
        elif args.command == "rollback":
            rollback(home)
        elif args.command == "update":
            update(checkout, home, args.concurrency)
        else:
            report(home, checkout)
        return 0
    except (InstallError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
