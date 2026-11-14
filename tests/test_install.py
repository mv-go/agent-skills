"""Black-box installer tests using isolated homes and temporary Git repositories."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from scripts import install as installer


REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "scripts" / "install.py"
LINK = Path(".agents/skills/budget-aware-swarm")


def run_cli(home: Path, checkout: Path, *args: str, ok: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, str(INSTALLER), *args, "--home", str(home), "--checkout", str(checkout)],
        text=True,
        capture_output=True,
        check=False,
    )
    if ok and result.returncode:
        raise AssertionError(f"CLI failed ({result.returncode}): {result.stderr}\n{result.stdout}")
    if not ok and result.returncode == 0:
        raise AssertionError(f"CLI unexpectedly succeeded: {result.stdout}")
    return result


def make_checkout(root: Path) -> Path:
    (root / "skills" / "budget-aware-swarm").mkdir(parents=True)
    (root / "skills" / "budget-aware-swarm" / "SKILL.md").write_text("# skill\n")
    return root


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.checkout = make_checkout(self.base / "checkout")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_repeated_install_then_rollback_restores_original_bytes_and_modes(self) -> None:
        codex = self.home / ".codex"
        codex.mkdir()
        original = b"# User bootstrap\r\nKeep every byte.\n"
        (codex / "AGENTS.md").write_bytes(original)
        os.chmod(codex / "AGENTS.md", 0o640)
        config = codex / "config.toml"
        config.write_bytes(b"# existing\n[agents]\nmax_concurrent_threads_per_session = 2\n\n[other]\nx = 1\n")
        os.chmod(config, 0o644)

        run_cli(self.home, self.checkout, "install")
        installed_agents = (codex / "AGENTS.md").read_bytes()
        installed_config = config.read_bytes()
        run_cli(self.home, self.checkout, "install")
        self.assertEqual((codex / "AGENTS.md").read_bytes(), installed_agents)
        self.assertEqual(config.read_bytes(), installed_config)
        self.assertEqual(installed_agents.count(b"budget-aware-swarm:managed:start"), 1)
        self.assertIn(b"max_concurrent_threads_per_session = 2", installed_config)
        self.assertIn(b"only when the user explicitly says", installed_agents)
        self.assertTrue((self.home / LINK).is_symlink())

        run_cli(self.home, self.checkout, "rollback")
        self.assertEqual((codex / "AGENTS.md").read_bytes(), original)
        self.assertEqual((codex / "AGENTS.md").stat().st_mode & 0o777, 0o640)
        self.assertEqual(config.read_bytes(), b"# existing\n[agents]\nmax_concurrent_threads_per_session = 2\n\n[other]\nx = 1\n")
        self.assertEqual(config.stat().st_mode & 0o777, 0o644)
        self.assertFalse((self.home / LINK).exists() or (self.home / LINK).is_symlink())

    def test_collision_including_dangling_symlink_is_refused_without_touching_unrelated_paths(self) -> None:
        target = self.home / LINK
        target.parent.mkdir(parents=True)
        target.symlink_to(self.base / "missing-target")
        codex = self.home / ".codex"
        codex.mkdir()
        agents = codex / "AGENTS.md"
        agents.write_bytes(b"untouched\n")
        result = run_cli(self.home, self.checkout, "install", ok=False)
        self.assertNotIn(b"untouched", result.stdout.encode() + result.stderr.encode())
        self.assertTrue(target.is_symlink())
        self.assertEqual(os.readlink(target), str(self.base / "missing-target"))
        self.assertEqual(agents.read_bytes(), b"untouched\n")
        self.assertFalse((codex / "config.toml").exists())

    def test_preexisting_exact_link_is_accepted_but_not_removed_by_rollback(self) -> None:
        target = self.home / LINK
        target.parent.mkdir(parents=True)
        target.symlink_to(self.checkout / "skills/budget-aware-swarm")
        run_cli(self.home, self.checkout, "install")
        run_cli(self.home, self.checkout, "rollback")
        self.assertTrue(target.is_symlink())
        self.assertEqual(os.readlink(target), str(self.checkout / "skills/budget-aware-swarm"))

    def test_existing_agents_section_and_multiple_tables_keep_comments_and_keys(self) -> None:
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir()
        before = b"""# preserve this comment
[other]
value = 'x'

[agents]
# user cap stays
enabled = false
max_concurrent_threads_per_session = 1

[agents.defaults]
model = 'user-choice'

[mcp]
enabled = true
"""
        config.write_bytes(before)
        run_cli(self.home, self.checkout, "install")
        after = config.read_bytes()
        self.assertIn(b"# preserve this comment", after)
        self.assertIn(b"# user cap stays", after)
        self.assertIn(b"max_concurrent_threads_per_session = 1", after)
        self.assertIn(b"[agents.defaults]\nmodel = 'user-choice'", after)
        self.assertIn(b"[mcp]\nenabled = true", after)
        self.assertIn(b"enabled = true", after)

    def test_legacy_smaller_agent_cap_is_not_increased(self) -> None:
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir()
        config.write_text("[agents]\nmax_threads = 1\n", encoding="utf-8")
        run_cli(self.home, self.checkout, "install")
        self.assertIn("max_concurrent_threads_per_session = 1", config.read_text(encoding="utf-8"))

    def test_existing_positive_cap_over_49_is_clamped_safely(self) -> None:
        config = self.home / ".codex/config.toml"
        config.parent.mkdir()
        config.write_text("[agents]\nmax_concurrent_threads_per_session = 100\n", encoding="utf-8")
        run_cli(self.home, self.checkout, "install")
        self.assertIn("max_concurrent_threads_per_session = 49", config.read_text(encoding="utf-8"))

    def test_new_install_defaults_to_49_children_and_reports_total(self) -> None:
        result = run_cli(self.home, self.checkout, "install")
        config = (self.home / ".codex/config.toml").read_text(encoding="utf-8")
        self.assertIn("max_concurrent_threads_per_session = 49", config)
        self.assertIn("49 child threads", result.stdout)
        self.assertIn("50 total including root", result.stdout)

    def test_invalid_concurrency_values_fail_closed(self) -> None:
        for value in ("0", "50", "-1", "1.5", "true"):
            with self.subTest(value=value):
                result = run_cli(self.home, self.checkout, "install", "--concurrency", value, ok=False)
                self.assertIn("concurrency", (result.stdout + result.stderr).lower())
                self.assertFalse((self.home / ".codex/config.toml").exists())

    def test_explicit_concurrency_updates_existing_v2_install(self) -> None:
        run_cli(self.home, self.checkout, "install")
        changed = run_cli(self.home, self.checkout, "install", "--concurrency", "7")
        config = (self.home / ".codex/config.toml").read_text(encoding="utf-8")
        self.assertIn("max_concurrent_threads_per_session = 7", config)
        self.assertIn("7 child threads", changed.stdout)
        state = __import__("json").loads((self.home / ".local/state/budget-aware-swarm/install-state.json").read_text())
        self.assertEqual(state["agents_cap"], 7)

    def test_legacy_max_threads_alias_is_normalized_without_duplicate_assignment(self) -> None:
        config = self.home / ".codex/config.toml"
        config.parent.mkdir()
        config.write_text("[agents]\nmax_threads = 3 # legacy\nenabled = false\n", encoding="utf-8")
        run_cli(self.home, self.checkout, "install", "--concurrency", "49")
        result = config.read_text(encoding="utf-8")
        self.assertNotIn("max_threads", result)
        self.assertEqual(result.count("max_concurrent_threads_per_session"), 1)
        self.assertIn("max_concurrent_threads_per_session = 49 # legacy", result)

    def test_explicit_v2_override_and_ambiguous_alias_conflicts_are_rejected(self) -> None:
        config = self.home / ".codex/config.toml"
        config.parent.mkdir()
        original = b"[agents]\nmax_threads = 2\nmax_concurrent_threads_per_session = 3\n"
        config.write_bytes(original)
        run_cli(self.home, self.checkout, "install", ok=False)
        self.assertEqual(config.read_bytes(), original)
        config.write_text(
            "[features.multi_agent_v2]\nmax_concurrent_threads_per_session = 7\n", encoding="utf-8"
        )
        run_cli(self.home, self.checkout, "install", ok=False)
        self.assertIn("= 7", config.read_text(encoding="utf-8"))

    def _make_v1_owned_four_install(self, original_config: bytes | None = None) -> tuple[Path, bytes, int]:
        codex = self.home / ".codex"
        codex.mkdir()
        config = codex / "config.toml"
        original = original_config if original_config is not None else b"# original config\r\n[other]\nx = 2\r\n"
        config.write_bytes(original)
        os.chmod(config, 0o640)
        agents = codex / "AGENTS.md"
        original_agents = b"# user instructions\r\n"
        agents.write_bytes(original_agents)
        os.chmod(agents, 0o600)
        # A v1 on-disk fixture, matching the pre-migration state and config format.
        installed_agents = installer.add_managed_paragraph(original_agents)
        parsed_original = __import__("tomllib").loads(original.decode("utf-8"))
        source_agents = parsed_original.get("agents", {})
        source_caps = [
            value for key in ("max_concurrent_threads_per_session", "max_threads")
            if isinstance((value := source_agents.get(key)), int) and not isinstance(value, bool) and value > 0
        ] if isinstance(source_agents, dict) else []
        legacy_cap = min([4, *source_caps])
        if "[agents]" in original.decode("utf-8"):
            installed_config = original + f"enabled = true\nmax_concurrent_threads_per_session = {legacy_cap}\n".encode()
        else:
            installed_config = original + f"\n[agents]\nenabled = true\nmax_concurrent_threads_per_session = {legacy_cap}\n".encode()
        agents.write_bytes(installed_agents)
        config.write_bytes(installed_config)
        link_target = self.checkout / "skills/budget-aware-swarm"
        link = self.home / LINK
        link.parent.mkdir(parents=True)
        link.symlink_to(link_target)
        state_dir = self.home / ".local/state/budget-aware-swarm"
        state_dir.mkdir(parents=True, mode=0o700)
        snapshots = {
            ".codex/AGENTS.md": (original_agents, installed_agents, 0o600),
            ".codex/config.toml": (original, installed_config, 0o640),
        }
        files: dict[str, dict[str, object]] = {}
        for rel, (before, after, mode) in snapshots.items():
            backup_name = "backup-v1-" + Path(rel).name + ".bin"
            (state_dir / backup_name).write_bytes(before)
            os.chmod(state_dir / backup_name, 0o600)
            files[rel] = {
                "exists": True,
                "mode": mode,
                "backup": backup_name,
                "before_sha256": installer.digest(before),
                "after_sha256": installer.digest(after),
            }
        state = {
            "version": 1,
            "checkout": str(self.checkout),
            "link_target": str(link_target),
            "link_owned": True,
            "phase": "applied",
            "agents_cap": legacy_cap,
            "files": files,
        }
        state_path = state_dir / "install-state.json"
        state_path.write_text(__import__("json").dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.chmod(state_path, 0o600)
        return config, original, 0o640

    def test_v1_owned_default_four_migrates_and_rollback_restores_original(self) -> None:
        config, original, mode = self._make_v1_owned_four_install()
        run_cli(self.home, self.checkout, "install")
        self.assertIn("= 49", config.read_text(encoding="utf-8"))
        state = __import__("json").loads((self.home / ".local/state/budget-aware-swarm/install-state.json").read_text())
        self.assertEqual(state["version"], 2)
        run_cli(self.home, self.checkout, "rollback")
        self.assertEqual(config.read_bytes(), original)
        self.assertEqual(config.stat().st_mode & 0o777, mode)

    def test_v1_dual_alias_config_from_legacy_serializer_migrates_safely(self) -> None:
        original = b"# original config\n[agents]\nmax_threads = 2\n"
        config, _original, _mode = self._make_v1_owned_four_install(original)
        state_path = self.home / ".local/state/budget-aware-swarm/install-state.json"
        self.assertIn(b"max_threads = 2", config.read_bytes())
        config.write_bytes(config.read_bytes() + b"# unrelated later comment\n")
        run_cli(self.home, self.checkout, "install")
        migrated = config.read_text(encoding="utf-8")
        self.assertNotIn("max_threads", migrated)
        self.assertIn("max_concurrent_threads_per_session = 2", migrated)
        self.assertTrue(migrated.endswith("# unrelated later comment\n"))
        run_cli(self.home, self.checkout, "rollback", ok=False)

    def test_v1_legacy_alias_changed_after_install_is_preserved_and_blocks_migration(self) -> None:
        original = b"# original config\n[agents]\nmax_threads = 2\n"
        config, _original, _mode = self._make_v1_owned_four_install(original)
        config.write_bytes(config.read_bytes().replace(b"max_threads = 2", b"max_threads = 10"))
        before = config.read_bytes()
        run_cli(self.home, self.checkout, "install", ok=False)
        self.assertEqual(config.read_bytes(), before)

    def test_v1_migration_preserves_later_edits_and_permanently_blocks_rollback(self) -> None:
        config, _original, _mode = self._make_v1_owned_four_install()
        config.write_bytes(config.read_bytes() + b"# later user edit\n")
        run_cli(self.home, self.checkout, "install")
        migrated = config.read_bytes()
        self.assertIn(b"= 49", migrated)
        self.assertTrue(migrated.endswith(b"# later user edit\n"))
        run_cli(self.home, self.checkout, "rollback", ok=False)
        self.assertEqual(config.read_bytes(), migrated)
        verify = run_cli(self.home, self.checkout, "verify")
        self.assertIn("rollback blocked by preserved later edits", verify.stdout)

    def test_concurrency_flag_is_rejected_for_non_mutating_commands(self) -> None:
        run_cli(self.home, self.checkout, "install")
        for command in ("verify", "status", "rollback"):
            with self.subTest(command=command):
                result = run_cli(self.home, self.checkout, command, "--concurrency", "7", ok=False)
                self.assertIn("only valid with install or update", result.stderr)

    def test_failed_v1_migration_restores_config_and_old_state(self) -> None:
        config, _original, _mode = self._make_v1_owned_four_install()
        state_path = self.home / ".local/state/budget-aware-swarm/install-state.json"
        before_config = config.read_bytes()
        before_state = state_path.read_bytes()
        real_atomic_write = installer.atomic_write

        def fail_config_write(path: Path, data: bytes, mode: int) -> None:
            if path == config:
                raise OSError("simulated migration failure")
            real_atomic_write(path, data, mode)

        with patch.object(installer, "atomic_write", side_effect=fail_config_write):
            with self.assertRaises(installer.InstallError):
                installer.install(self.home, self.checkout)
        self.assertEqual(config.read_bytes(), before_config)
        self.assertEqual(state_path.read_bytes(), before_state)

    def test_unknown_concurrent_bytes_after_migration_failure_keep_journal_and_state(self) -> None:
        config, _original, _mode = self._make_v1_owned_four_install()
        state_path = self.home / ".local/state/budget-aware-swarm/install-state.json"
        real_write_state = installer.write_state_file
        count = 0

        def race_config_then_fail_commit(path: Path, state: dict[str, object]) -> None:
            nonlocal count
            count += 1
            if count == 2:
                config.write_bytes(config.read_bytes() + b"# concurrent edit\n")
                raise OSError("simulated state commit failure after concurrent edit")
            real_write_state(path, state)

        with patch.object(installer, "write_state_file", side_effect=race_config_then_fail_commit):
            with self.assertRaises(installer.InstallError):
                installer.install(self.home, self.checkout)
        state = __import__("json").loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["phase"], "migrating")
        self.assertTrue(list(state_path.parent.glob("migration-*.bin")))
        preserved = config.read_bytes()
        with self.assertRaises(installer.InstallError):
            installer.install(self.home, self.checkout)
        self.assertEqual(config.read_bytes(), preserved)
        self.assertEqual(__import__("json").loads(state_path.read_text())["phase"], "migrating")

    def test_corrupt_original_backup_blocks_rollback_without_mutation(self) -> None:
        config = self.home / ".codex/config.toml"
        config.parent.mkdir()
        config.write_bytes(b"# original config\n")
        run_cli(self.home, self.checkout, "install")
        state_path = self.home / ".local/state/budget-aware-swarm/install-state.json"
        state = __import__("json").loads(state_path.read_text(encoding="utf-8"))
        backup = state_path.parent / state["files"][".codex/config.toml"]["backup"]
        backup.write_bytes(b"corrupt")
        before = config.read_bytes()
        run_cli(self.home, self.checkout, "rollback", ok=False)
        self.assertEqual(config.read_bytes(), before)

    def test_interrupted_v1_migration_recovers_known_after_bytes(self) -> None:
        config, _original, _mode = self._make_v1_owned_four_install()
        state_path = self.home / ".local/state/budget-aware-swarm/install-state.json"
        real_write_state = installer.write_state_file
        count = 0

        def interrupt_after_config(path: Path, state: dict[str, object]) -> None:
            nonlocal count
            count += 1
            if count == 2:
                raise KeyboardInterrupt("simulated process interruption")
            real_write_state(path, state)

        with patch.object(installer, "write_state_file", side_effect=interrupt_after_config):
            with self.assertRaises(KeyboardInterrupt):
                installer.install(self.home, self.checkout)
        self.assertIn(b"= 49", config.read_bytes())
        run_cli(self.home, self.checkout, "install")
        state = __import__("json").loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["version"], 2)
        self.assertEqual(state["agents_cap"], 49)
        self.assertFalse(list(state_path.parent.glob("migration-*.bin")))


    def test_rollback_refuses_later_edits_and_preserves_them(self) -> None:
        run_cli(self.home, self.checkout, "install")
        agents = self.home / ".codex" / "AGENTS.md"
        agents.write_bytes(agents.read_bytes() + b"# user edit\n")
        result = run_cli(self.home, self.checkout, "rollback", ok=False)
        self.assertTrue(result.stderr or result.stdout)
        self.assertTrue(agents.read_bytes().endswith(b"# user edit\n"))

    def test_codex_appended_project_table_is_accepted_but_blocks_full_rollback(self) -> None:
        run_cli(self.home, self.checkout, "install")
        config = self.home / ".codex" / "config.toml"
        later_table = b'\n[projects."/tmp/codex-trust-entry"]\ntrust_level = "trusted"\n'
        config.write_bytes(config.read_bytes() + later_table)

        verify = run_cli(self.home, self.checkout, "verify")
        self.assertIn("managed invariants: valid", verify.stdout)
        self.assertIn("later edits present; full rollback blocked", verify.stdout)
        before_repeat = config.read_bytes()
        repeat = run_cli(self.home, self.checkout, "install")
        self.assertIn("later edits present; full rollback blocked", repeat.stdout)
        self.assertEqual(config.read_bytes(), before_repeat)
        run_cli(self.home, self.checkout, "rollback", ok=False)
        self.assertEqual(config.read_bytes(), before_repeat)

    def test_managed_agents_value_change_fails_verify(self) -> None:
        run_cli(self.home, self.checkout, "install")
        config = self.home / ".codex" / "config.toml"
        config.write_text(config.read_text(encoding="utf-8").replace("enabled = true", "enabled = false"), encoding="utf-8")
        run_cli(self.home, self.checkout, "verify", ok=False)

    def test_verify_rejects_reintroduced_alias_and_v2_override(self) -> None:
        run_cli(self.home, self.checkout, "install")
        config = self.home / ".codex/config.toml"
        config.write_bytes(config.read_bytes() + b"max_threads = 49\n")
        run_cli(self.home, self.checkout, "verify", ok=False)
        config.write_bytes(config.read_bytes().replace(b"max_threads = 49\n", b""))
        config.write_bytes(config.read_bytes() + b"\n[features.multi_agent_v2]\nmax_concurrent_threads_per_session = 50\n")
        run_cli(self.home, self.checkout, "verify", ok=False)

    def test_inline_agents_table_refuses_install_without_clobber(self) -> None:
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir()
        original = b"agents = { enabled = false }\n"
        config.write_bytes(original)
        run_cli(self.home, self.checkout, "install", ok=False)
        self.assertEqual(config.read_bytes(), original)
        self.assertFalse((self.home / ".codex" / "AGENTS.md").exists())
        self.assertFalse((self.home / LINK).exists() or (self.home / LINK).is_symlink())

    def test_symlinked_state_ancestor_is_refused(self) -> None:
        external_state = self.base / "external-state"
        external_state.mkdir()
        (self.home / ".local").symlink_to(external_state, target_is_directory=True)
        run_cli(self.home, self.checkout, "install", ok=False)
        self.assertEqual(list(external_state.iterdir()), [])
        self.assertFalse((self.home / ".codex" / "AGENTS.md").exists())

    def test_write_failure_after_first_file_restores_every_original_and_cleans_journal(self) -> None:
        codex = self.home / ".codex"
        codex.mkdir()
        agents = codex / "AGENTS.md"
        agents.write_bytes(b"# original exactly\r\n")
        os.chmod(agents, 0o640)
        config = codex / "config.toml"
        real_atomic_write = installer.atomic_write

        def fail_config_write(path: Path, data: bytes, mode: int) -> None:
            if path == config:
                raise OSError("simulated write failure")
            real_atomic_write(path, data, mode)

        with patch.object(installer, "atomic_write", side_effect=fail_config_write):
            with self.assertRaises(installer.InstallError):
                installer.install(self.home, self.checkout)
        self.assertEqual(agents.read_bytes(), b"# original exactly\r\n")
        self.assertEqual(agents.stat().st_mode & 0o777, 0o640)
        self.assertFalse(config.exists())
        self.assertFalse((self.home / LINK).exists() or (self.home / LINK).is_symlink())
        self.assertFalse((self.home / ".local/state/budget-aware-swarm/install-state.json").exists())

    def test_interrupted_rollback_can_resume_from_private_journal(self) -> None:
        codex = self.home / ".codex"
        codex.mkdir()
        agents = codex / "AGENTS.md"
        agents.write_bytes(b"# original\n")
        config = codex / "config.toml"
        config.write_bytes(b"[other]\nkey = 'original'\n")
        run_cli(self.home, self.checkout, "install")
        real_atomic_write = installer.atomic_write

        def fail_config_restore(path: Path, data: bytes, mode: int) -> None:
            if path == config:
                raise OSError("simulated rollback write failure")
            real_atomic_write(path, data, mode)

        with patch.object(installer, "atomic_write", side_effect=fail_config_restore):
            with self.assertRaises(OSError):
                installer.rollback(self.home)
        run_cli(self.home, self.checkout, "rollback")
        self.assertEqual(agents.read_bytes(), b"# original\n")
        self.assertEqual(config.read_bytes(), b"[other]\nkey = 'original'\n")
        self.assertFalse((self.home / LINK).exists() or (self.home / LINK).is_symlink())

    def test_status_and_verify_identify_checkout_and_valid_install(self) -> None:
        run_cli(self.home, self.checkout, "install")
        verify = run_cli(self.home, self.checkout, "verify")
        status = run_cli(self.home, self.checkout, "status")
        self.assertIn("checkout", verify.stdout.lower())
        self.assertIn("skill", verify.stdout.lower())
        self.assertIn("valid", status.stdout.lower())

    def test_verify_fails_when_an_installed_file_changes(self) -> None:
        run_cli(self.home, self.checkout, "install")
        agents = self.home / ".codex" / "AGENTS.md"
        agents.write_text(agents.read_text(encoding="utf-8").replace(installer.PARAGRAPH, "changed managed paragraph"), encoding="utf-8")
        run_cli(self.home, self.checkout, "verify", ok=False)

    def test_update_fast_forwards_clean_checkout_and_refuses_dirty_or_divergent(self) -> None:
        bare = self.base / "remote.git"
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
        subprocess.run(["git", "--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
        seed = self.base / "seed"
        make_checkout(seed)
        (seed / "scripts").mkdir()
        (seed / "scripts/install.py").write_bytes(INSTALLER.read_bytes())
        subprocess.run(["git", "init", str(seed)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(seed), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(seed), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
        subprocess.run(["git", "-C", str(seed), "commit", "-m", "initial"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(seed), "branch", "-M", "main"], check=True)
        subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(bare)], check=True)
        subprocess.run(["git", "-C", str(seed), "push", "-u", "origin", "main"], check=True, capture_output=True)
        clone = self.base / "clone"
        subprocess.run(["git", "clone", str(bare), str(clone)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(seed), "remote", "set-head", "origin", "main"], check=True)
        run_cli(self.home, clone, "install")
        # Model a deployed v1 installer-owned default cap before the clean update.
        state_path = self.home / ".local/state/budget-aware-swarm/install-state.json"
        state = __import__("json").loads(state_path.read_text(encoding="utf-8"))
        state["version"] = 1
        state["agents_cap"] = 4
        config = self.home / ".codex/config.toml"
        config.write_bytes(config.read_bytes().replace(b"= 49", b"= 4"))
        state["files"][".codex/config.toml"]["after_sha256"] = installer.digest(config.read_bytes())
        state_path.write_text(__import__("json").dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        (seed / "skills" / "budget-aware-swarm" / "SKILL.md").write_text("# updated\n")
        subprocess.run(["git", "-C", str(seed), "commit", "-am", "update"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(seed), "push"], check=True, capture_output=True)
        run_cli(self.home, clone, "update")
        self.assertEqual((clone / "skills" / "budget-aware-swarm" / "SKILL.md").read_text(), "# updated\n")
        self.assertIn("= 49", config.read_text(encoding="utf-8"))
        run_cli(self.home, clone, "update", "--concurrency", "7")
        self.assertIn("= 7", config.read_text(encoding="utf-8"))

        (clone / "local-change").write_text("dirty")
        run_cli(self.home, clone, "update", ok=False)
        (clone / "local-change").unlink()
        # Create one local and one upstream commit to make the branch diverge.
        subprocess.run(["git", "-C", str(clone), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(clone), "config", "user.name", "Test"], check=True)
        (clone / "local-commit").write_text("local")
        subprocess.run(["git", "-C", str(clone), "add", "."], check=True)
        subprocess.run(["git", "-C", str(clone), "commit", "-m", "local"], check=True, capture_output=True)
        (seed / "upstream-commit").write_text("upstream")
        subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
        subprocess.run(["git", "-C", str(seed), "commit", "-m", "upstream"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(seed), "push"], check=True, capture_output=True)
        run_cli(self.home, clone, "update", ok=False)


if __name__ == "__main__":
    unittest.main()
