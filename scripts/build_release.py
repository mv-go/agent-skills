#!/usr/bin/env python3
"""Build deterministic, allowlisted skill packages; never archive a repository."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


class BuildError(ValueError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_file(root: Path, relative: str) -> Path:
    """Refuse traversal and symlinks, including a symlink in a parent directory."""
    parts = PurePosixPath(relative).parts
    if (not parts or relative != PurePosixPath(relative).as_posix()
            or relative.startswith("/") or "\\" in relative or ".." in parts):
        raise BuildError(f"Unsafe package path: {relative}")
    candidate = root
    for part in parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise BuildError(f"Symlink is not a release input: {relative}")
    if not candidate.is_file():
        raise BuildError(f"Missing release input: {relative}")
    return candidate


def check_links(files: dict[str, bytes]) -> None:
    """Local Markdown references must survive installing just this package."""
    import posixpath

    for name, data in files.items():
        if not name.endswith(".md"):
            continue
        for link in re.findall(r"\[[^\]]*\]\(([^)]+)\)", data.decode("utf-8")):
            parsed = urlsplit(link)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            target = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(parsed.path)))
            if target not in files:
                raise BuildError(f"Unpackaged reference in {name}: {link}")


def package_bytes(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(files.items()):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, data, compresslevel=9)
    return stream.getvalue()


def marketplace_bytes(sources: dict[str, str]) -> bytes:
    marketplace = {
        "name": "tired-tools-releases", "owner": {"name": "tired-tools"},
        "description": "Released tired-tools skills for Claude Code.",
        "plugins": [{"name": skill, "source": source} for skill, source in sorted(sources.items())]}
    return (json.dumps(marketplace, indent=2) + "\n").encode()


def build(root: Path, output: Path, *, version: str | None = None,
          skills: list[str] | None = None, source_commit: str | None = None) -> dict:
    catalog = json.loads((root / "releases/catalog.json").read_text())
    if catalog.get("schema") != 1 or not catalog.get("skills"):
        raise BuildError("Unsupported or empty catalog")
    selected = version or "0.0.0"
    if not VERSION.fullmatch(selected):
        raise BuildError("Version must be X.Y.Z")
    chosen = sorted(skills if skills is not None else catalog["skills"])
    if not chosen or len(set(chosen)) != len(chosen):
        raise BuildError("Select a nonempty, unique set of skills")
    for skill in chosen:
        if not SLUG.fullmatch(skill) or skill not in catalog["skills"]:
            raise BuildError(f"Skill is not approved for publication: {skill}")
    if source_commit is None:
        source_commit = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source_commit):
        raise BuildError("Source commit must be a full Git object ID")
    assets = {}
    manifest = {"schema": 2, "version": selected, "source_commit": source_commit,
                "skills": {}, "bundles": {}}
    universal_files: dict[str, bytes] = {}
    host_files: dict[str, dict[str, bytes]] = {}
    for skill in chosen:
        definition = catalog["skills"][skill]
        if not definition["packages"]:
            raise BuildError(f"No platforms for {skill}")
        entry = {"asset": f"{skill}-{selected}.zip", "packages": {}}
        skill_files = {}
        for host, package in sorted(definition["packages"].items()):
            if host not in ("codex", "claude"):
                raise BuildError(f"Unsupported host: {host}")
            source = package["source"]
            expected_source = f"skills/{skill}" if host == "codex" else f"plugins/{skill}"
            if source != expected_source:
                raise BuildError(f"Unexpected source root: {source}")
            names = package["files"]
            if not names or len(set(names)) != len(names) or "release-info.json" in names:
                raise BuildError("Package file allowlist is empty, duplicated, or includes reserved release-info.json")
            for name in names:
                safe_file(root / source, name)
            files = {name: safe_file(root, f"{source}/{name}").read_bytes() for name in names}
            required = "SKILL.md" if host == "codex" else f"skills/{skill}/SKILL.md"
            if required not in files:
                raise BuildError(f"Missing required entrypoint: {required}")
            if host == "claude":
                plugin = json.loads(files[".claude-plugin/plugin.json"])
                if plugin.get("name") != skill or not VERSION.fullmatch(plugin.get("version", "")):
                    raise BuildError("Claude plugin must declare the correct identity and a semantic version")
                # Stamp build outputs only; automatic releases never commit version changes.
                plugin["version"] = selected
                files[".claude-plugin/plugin.json"] = (json.dumps(plugin, indent=2) + "\n").encode()
            info = {"schema": 1, "skill": skill, "version": selected, "host": host,
                    "source_commit": source_commit}
            files["release-info.json"] = (json.dumps(info, indent=2, sort_keys=True) + "\n").encode()
            check_links(files)
            asset = f"{skill}-{selected}-{host}.zip"
            assets[asset] = package_bytes(files)
            entry["packages"][host] = {
                "asset": asset, "sha256": sha256(assets[asset]),
                "files": {name: sha256(data) for name, data in sorted(files.items())}}
            skill_files.update({f"{host}/{name}": data for name, data in files.items()})
            host_files.setdefault(host, {}).update({f"{skill}/{name}": data for name, data in files.items()})
        assets[entry["asset"]] = package_bytes(skill_files)
        entry["sha256"] = sha256(assets[entry["asset"]])
        manifest["skills"][skill] = entry
        universal_files.update({f"{skill}/{name}": data for name, data in skill_files.items()})
    claude_skills = [skill for skill, entry in manifest["skills"].items() if "claude" in entry["packages"]]
    if claude_skills:
        universal_files[".claude-plugin/marketplace.json"] = marketplace_bytes(
            {skill: f"./{skill}/claude" for skill in claude_skills})
        host_files["claude"][".claude-plugin/marketplace.json"] = marketplace_bytes(
            {skill: f"./{skill}" for skill in claude_skills})
    bundle = f"all-skills-{selected}.zip"
    assets[bundle] = package_bytes(universal_files)
    manifest["bundle"] = {"asset": bundle, "sha256": sha256(assets[bundle])}
    for host, files in sorted(host_files.items()):
        asset = f"all-skills-{selected}-{host}.zip"
        assets[asset] = package_bytes(files)
        manifest["bundles"][host] = {"asset": asset, "sha256": sha256(assets[asset])}
    assets["release.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    assets["SHA256SUMS"] = "".join(
        f"{sha256(data)}  {name}\n" for name, data in sorted(assets.items())).encode()
    output.mkdir(parents=True, exist_ok=True)
    if set(p.name for p in output.iterdir()) - assets.keys():
        raise BuildError("Output directory has foreign files; use an empty release directory")
    for name in assets:
        destination = output / name
        if destination.is_symlink():
            raise BuildError(f"Output asset is a symlink: {name}")
    for name, data in assets.items():
        destination = output / name
        destination.write_bytes(data)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", action="append", help="Preview selected skills; default: all approved skills")
    parser.add_argument("--version", help="Release X.Y.Z; default: 0.0.0 for local previews")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--require-clean", action="store_true", help="Required for publication")
    args = parser.parse_args()
    try:
        if args.require_clean and subprocess.check_output(
                ["git", "-C", str(ROOT), "status", "--porcelain"], text=True).strip():
            raise BuildError("Publication requires a clean source checkout")
        manifest = build(ROOT, args.output, skills=args.skill, version=args.version)
    except (BuildError, OSError, KeyError, TypeError, json.JSONDecodeError,
            subprocess.CalledProcessError) as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"skills": sorted(manifest["skills"]), "version": manifest["version"],
                      "hosts": sorted(manifest["bundles"]), "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
