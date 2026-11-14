"""Check installable package boundaries without Claude credentials or a network."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]


class ClaudePluginPackagingTests(unittest.TestCase):
    def packages(self):
        catalog = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
        self.assertTrue(catalog["name"])
        self.assertTrue(catalog["owner"]["name"])
        self.assertTrue(catalog["plugins"])
        for entry in catalog["plugins"]:
            source = (ROOT / entry["source"]).resolve()
            self.assertTrue(source.is_relative_to(ROOT), entry["source"])
            yield entry, source

    def test_marketplace_resolves_complete_plugin_packages(self):
        for entry, source in self.packages():
            with self.subTest(plugin=entry["name"]):
                manifest = json.loads((source / ".claude-plugin/plugin.json").read_text())
                self.assertEqual(entry["name"], manifest["name"])
                self.assertRegex(manifest["version"], r"^\d+\.\d+\.\d+$")
                self.assertTrue(list((source / "skills").glob("*/SKILL.md")))
                self.assertTrue(list((source / "agents").glob("*.md")))

    def test_markdown_references_survive_copying_only_the_plugin(self):
        for entry, source in self.packages():
            with self.subTest(plugin=entry["name"]), tempfile.TemporaryDirectory() as tmp:
                # macOS temporary directories may be reached through /var symlinks.
                installed = (Path(tmp) / entry["name"]).resolve()
                shutil.copytree(source, installed)
                for document in installed.rglob("*.md"):
                    for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", document.read_text()):
                        parts = urlsplit(target)
                        if parts.scheme or parts.netloc or not parts.path:
                            continue
                        resolved = (document.parent / unquote(parts.path)).resolve()
                        self.assertTrue(resolved.is_relative_to(installed),
                                        f"{document.name}: reference escapes package: {target}")
                        self.assertTrue(resolved.exists(),
                                        f"{document.name}: missing installed reference: {target}")

    def test_cloud_project_skill_matches_plugin_skill(self):
        # Cloud sessions load .claude/skills from the clone but do not install plugins.
        plugin = ROOT / "plugins/budget-aware-swarm/skills/budget-aware-swarm"
        project = ROOT / ".claude/skills/budget-aware-swarm"
        plugin_files = sorted(p.relative_to(plugin) for p in plugin.rglob("*") if p.is_file())
        project_files = sorted(p.relative_to(project) for p in project.rglob("*") if p.is_file())
        self.assertEqual(plugin_files, project_files)
        for relative in plugin_files:
            with self.subTest(file=str(relative)):
                self.assertEqual((plugin / relative).read_bytes(), (project / relative).read_bytes(),
                                 f"copy with: cp -r {plugin.relative_to(ROOT)} .claude/skills/")

    def test_swarm_skill_is_model_visible_but_explicit_only(self):
        # disable-model-invocation hides the skill from Claude, so naming the
        # scheme in plain text would not work; the description keeps it opt-in.
        text = (ROOT / "plugins/budget-aware-swarm/skills/budget-aware-swarm/SKILL.md").read_text()
        frontmatter = text.split("---", 2)[1]
        self.assertNotIn("disable-model-invocation", frontmatter)
        description = " ".join(frontmatter.split("description:", 1)[1].split("argument-hint:", 1)[0].split())
        for phrase in ("Use only when the user explicitly invokes /budget-aware-swarm",
                       "names the Budget-Aware Swarm scheme", "Never select it on your own initiative"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, description)

    def test_swarm_skill_keeps_flat_mode_and_leaf_rules(self):
        text = (ROOT / "plugins/budget-aware-swarm/skills/budget-aware-swarm/SKILL.md").read_text()
        for phrase in ("CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH", "Flat mode", "Haiku is the default worker",
                       "Leaf contract", "100K tokens", "general-purpose", "get_session"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)
        haiku = (ROOT / "plugins/budget-aware-swarm/agents/swarm-haiku.md").read_text()
        self.assertIn("leaf contract", haiku)
        self.assertIn("100K", haiku)


if __name__ == "__main__":
    unittest.main()
