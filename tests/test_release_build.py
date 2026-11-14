"""Release boundaries, reproducibility, and real-package offline acceptance."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('build_release', ROOT / 'scripts/build_release.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def zip_files(path):
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


class ReleaseBuildTests(unittest.TestCase):
    def fixture(self, root):
        (root / 'releases').mkdir()
        (root / 'skills/example').mkdir(parents=True)
        (root / 'skills/example/SKILL.md').write_text('# Example\n')
        catalog = {'schema': 1, 'skills': {'example': {'packages': {
            'codex': {'source': 'skills/example', 'files': ['SKILL.md']}}}}}
        (root / 'releases/catalog.json').write_text(json.dumps(catalog))
        return catalog

    def test_every_skill_and_host_has_identical_content_in_all_archive_variants(self):
        catalog = json.loads((ROOT / 'releases/catalog.json').read_text())['skills']
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            manifest = builder.build(ROOT, out, version='0.3.42')
            universal = zip_files(out / manifest['bundle']['asset'])
            host_bundles = {h: zip_files(out / p['asset']) for h, p in manifest['bundles'].items()}
            self.assertEqual(set(host_bundles), {'codex', 'claude'})
            self.assertEqual({n.split('/')[0] for n in host_bundles['claude']} - {'.claude-plugin'}, {'budget-aware-swarm'})
            for skill, definition in manifest['skills'].items():
                single = zip_files(out / definition['asset'])
                for host, package in definition['packages'].items():
                    with self.subTest(skill=skill, host=host):
                        files = zip_files(out / package['asset'])
                        self.assertEqual(set(files), set(catalog[skill]['packages'][host]['files']) | {'release-info.json'})
                        for name, data in files.items():
                            self.assertEqual(single[f'{host}/{name}'], data)
                            self.assertEqual(universal[f'{skill}/{host}/{name}'], data)
                            self.assertEqual(host_bundles[host][f'{skill}/{name}'], data)
                        info = json.loads(files['release-info.json'])
                        self.assertEqual((info['version'], info['skill'], info['host']), ('0.3.42', skill, host))
                        self.assertEqual(info['source_commit'], manifest['source_commit'])
                        if host == 'claude':
                            self.assertEqual(json.loads(files['.claude-plugin/plugin.json'])['version'], '0.3.42')
            for name in universal:
                self.assertFalse(any(part in ('.git', 'tests', 'docs') for part in Path(name).parts))

    def test_build_is_reproducible_and_does_not_mutate_source_versions(self):
        plugin_path = ROOT / 'plugins/budget-aware-swarm/.claude-plugin/plugin.json'
        before = plugin_path.read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / 'a', Path(tmp) / 'b'
            builder.build(ROOT, first, version='0.3.42')
            builder.build(ROOT, second, version='0.3.42')
            self.assertEqual({p.name: p.read_bytes() for p in first.iterdir()},
                             {p.name: p.read_bytes() for p in second.iterdir()})
        self.assertEqual(plugin_path.read_bytes(), before)

    def test_unlisted_working_files_never_leave_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            (root / 'skills/example/private-notes.md').write_text('private')
            (root / 'credentials').write_text('not a real credential')
            manifest = builder.build(root, root / 'out', source_commit='a' * 40)
            files = zip_files(root / 'out' / manifest['skills']['example']['packages']['codex']['asset'])
            self.assertEqual(set(files), {'SKILL.md', 'release-info.json'})

    def test_symlink_and_path_escape_inputs_fail(self):
        for name in ('../private.md', '/absolute', 'nested/../../private.md', 'bad\\name'):
            with self.subTest(path=name), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(builder.BuildError):
                    builder.safe_file(Path(tmp), name)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            p = root / 'skills/example/SKILL.md'
            p.unlink()
            (root / 'real.md').write_text('outside package')
            p.symlink_to(root / 'real.md')
            with self.assertRaisesRegex(builder.BuildError, 'Symlink'):
                builder.build(root, root / 'out', source_commit='a' * 40)

    def test_missing_reference_invalid_version_and_unapproved_skill_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            with self.assertRaisesRegex(builder.BuildError, 'Version'):
                builder.build(root, root / 'out', version='latest')
            with self.assertRaisesRegex(builder.BuildError, 'not approved'):
                builder.build(root, root / 'out', skills=['private-skill'])
            (root / 'skills/example/SKILL.md').write_text('[missing](references/missing.md)\n')
            with self.assertRaisesRegex(builder.BuildError, 'Unpackaged reference'):
                builder.build(root, root / 'out', source_commit='a' * 40)

    def test_release_needs_no_custom_installer_or_consumer_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            manifest = builder.build(root, root / 'out', source_commit='a' * 40)
            self.assertNotIn('installer_sha256', manifest)
            assets = {p.name for p in (root / 'out').iterdir()}
            self.assertTrue(all(name.endswith('.zip') or name in ('release.json', 'SHA256SUMS') for name in assets))
            self.assertFalse(any('install' in name for name in assets))

    def test_claude_bundles_are_native_marketplaces_without_generated_consumer_scripts(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            manifest = builder.build(ROOT, out, version='1.2.3')
            for entry in (manifest['bundle'], manifest['bundles']['claude']):
                with self.subTest(asset=entry['asset']):
                    files = zip_files(out / entry['asset'])
                    marketplace = json.loads(files['.claude-plugin/marketplace.json'])
                    self.assertEqual(marketplace['name'], 'tired-tools-releases')
                    self.assertEqual([p['name'] for p in marketplace['plugins']], ['budget-aware-swarm'])
                    for plugin in marketplace['plugins']:
                        prefix = plugin['source'].removeprefix('./')
                        metadata = json.loads(files[prefix + '/.claude-plugin/plugin.json'])
                        self.assertEqual((metadata['name'], metadata['version']), (plugin['name'], '1.2.3'))
                        self.assertIn(prefix + '/skills/' + plugin['name'] + '/SKILL.md', files)


if __name__ == '__main__':
    unittest.main()
