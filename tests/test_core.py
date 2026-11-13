import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import recipes

from config import ConfigError, _parse, apply_config, apply_plugins
from environment import EnvironmentError, discover_env_file, import_environment
from install import install_components
from instructions import _COMPONENTS, render_instructions
from paths import _DATA_FILES, _state_dir, detect_platform
from recipes import load_catalog
from storage import ManagedFileError, apply_file, load_receipt, remove_file
from sync import _INSTRUCTIONS, _desired_config

ROOT = Path(__file__).resolve().parents[1]
BASE = {"oh-my-opencode-slim", "ponytail", "engram", "codebase-memory-mcp",
        "semgrep", "taskmd", "playwright-cli", "x-custom-providers", "grilling",
        "x-diagnosing-bugs", "x-writing-for-agents", "prototype", "frontend-design",
        "mcp-builder", "security-threat-model", "security-best-practices", "egm-drift-report",
        "x-bro", "markitdown-mcp"}
COMPONENTS = ("slim", "ponytail", "engram", "cbm", "semgrep", "markitdown", "taskmd")


class CoreTests(unittest.TestCase):
    def test_surgical_jsonc_preserves_foreign_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.jsonc"
            foreign = '"plugins": [{"package":"foreign@1","options":{"token":"{env:TOKEN}"}}'
            path.write_text('{\n // keep comment\n "model": "user-model",\n ' + foreign +
                            '],\n "mcp":{"servers":{"foreign":{"disabled":true}}}\n}\n')
            key = ("mcp", "servers", "managed", "command")
            result = apply_config(path, {key: ["original"]}, {})
            apply_config(path, {key: ["updated"]}, result.receipt)
            plugin = apply_plugins(path, {"owned": "owned@1"}, {})
            apply_plugins(path, {"owned": "owned@2"}, plugin.receipt)
            text = path.read_text()
            for preserved in ('// keep comment', '"model": "user-model"', foreign,
                              '"foreign":{"disabled":true}'):
                self.assertIn(preserved, text)
            self.assertEqual(_parse(text).value["mcp"]["servers"]["managed"]["command"], ["updated"])

    def test_manual_file_and_config_changes_block_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "AGENTS.md"
            receipt = apply_file(root, path.name, b"managed", None).receipt
            path.write_bytes(b"manual")
            config = root / "opencode.jsonc"
            key = ("mcp", "servers", "owned", "command")
            config_receipt = apply_config(config, {key: ["managed"]}, {}).receipt
            config.write_text(config.read_text().replace('"managed"', '"manual"'))
            before = config.read_bytes()
            for dry_run in (True, False):
                with self.assertRaises(ManagedFileError):
                    apply_file(root, path.name, b"replacement", receipt, dry_run=dry_run)
                with self.assertRaises(ManagedFileError):
                    remove_file(root, path.name, receipt, dry_run=dry_run)
                self.assertEqual(path.read_bytes(), b"manual")
                with self.assertRaises(ConfigError):
                    apply_config(config, {key: ["replacement"]}, config_receipt, dry_run=dry_run)
                self.assertEqual(config.read_bytes(), before)
            path.unlink()
            with self.assertRaises(ManagedFileError):
                apply_file(root, path.name, b"replacement", receipt)

    @unittest.skipUnless(os.name == "posix", "symlink testing requires POSIX environment")
    def test_symlink_files_and_parents_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actual = root / "actual"
            actual.mkdir()
            target = actual / "config.jsonc"
            target.write_text("{}")
            (root / "link").symlink_to(actual, target_is_directory=True)
            (root / "config.jsonc").symlink_to(target)
            for relative in ("config.jsonc", "link/config.jsonc"):
                for dry_run in (True, False):
                    with self.assertRaises(ConfigError):
                        apply_config(root / relative, {("owned",): 1}, {}, dry_run=dry_run)
                    with self.assertRaises(ManagedFileError):
                        apply_file(root, relative, b"replacement", None, dry_run=dry_run)
            self.assertEqual(target.read_text(), "{}")

    def test_agents_all_components_and_platform_layering(self):
        self.assertEqual(_COMPONENTS, COMPONENTS)
        self.assertEqual(set(_INSTRUCTIONS.values()), set(COMPONENTS))
        for platform in ("linux", "wsl", "windows"):
            with self.subTest(platform=platform):
                sources = ["common.md", "windows.md" if platform == "windows" else "linux.md"]
                if platform == "wsl":
                    sources.append("wsl.md")
                sources.append("skills.md")
                sources += [f"components/{name}.md" for name in COMPONENTS]
                text = render_instructions(ROOT / "setup/instructions", platform,
                                           reversed(COMPONENTS), local_addition="Local last.")
                labels = [f"<!-- Source: setup/instructions/{name} -->" for name in sources]
                self.assertEqual([line for line in text.splitlines()
                                  if line.startswith("<!-- Source: setup/")], labels)
                for name, label in zip(sources, labels):
                    self.assertIn(label + "\n" + (ROOT / "setup/instructions" / name).read_text().strip(), text)
                self.assertTrue(text.endswith("Local last.\n"))

    def test_real_catalog_base_and_build_resources(self):
        packages = load_catalog(ROOT / "packages.json")
        self.assertEqual({package["id"] for package in packages if not package["optional"]}, BASE)
        # Release checks compare this required-file contract with the actual wheel.
        manifest = {"packages.json"} | {path.relative_to(ROOT).as_posix()
                                      for path in (ROOT / "setup").rglob("*") if path.is_file()}
        self.assertTrue(set(_DATA_FILES) <= manifest)
        action = {"action": "release", "repository": "Gentleman-Programming/engram",
                  "asset_pattern": "engram_{version}_linux_{architecture}.tar.gz", "binary": "engram"}
        url = "https://github.com/Gentleman-Programming/engram/releases/download/v3.7.1/engram_3.7.1_linux_amd64.tar.gz"
        payload = {"tag_name": "v3.7.1", "draft": False, "prerelease": False, "assets": [
            {"name": "engram_3.7.1_linux_amd64.tar.gz", "browser_download_url": url, "digest": "sha256:" + "a" * 64}]}
        with patch("recipes._download", side_effect=lambda *args, **kwargs: json.dumps(payload).encode()):
            resolved = recipes._latest_release({"id": "engram"}, action, "x86_64")
            self.assertEqual((resolved["sha256"], resolved["url"]), ("a" * 64, url))
            payload["assets"][0].pop("digest")
            with self.assertRaises(ValueError):
                recipes._latest_release({"id": "engram"}, action, "x86_64")

    def test_semgrep_initial_active_and_user_toggles_preserved(self):
        packages = load_catalog(ROOT / "packages.json")
        selected = [package for package in packages if package["id"] == "semgrep"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "opencode.jsonc"
            snapshot = load_receipt(root / "receipt.json")
            desired, disabled, _ = _desired_config(path, snapshot, packages, selected, root, detect_platform())
            snapshot.config.update(apply_config(path, desired, {}).receipt)
            key = ("mcp", "servers", "semgrep", "disabled")
            toggle = apply_config(path, {key: True}, {}).receipt
            for value in (True, False):
                toggle = apply_config(path, {key: value}, toggle).receipt
                desired, disabled, _ = _desired_config(path, snapshot, packages, selected, root, detect_platform())
                self.assertNotIn(key, desired)
                apply_config(path, desired, snapshot.config)
                self.assertEqual(_parse(path.read_text()).value["mcp"]["servers"]["semgrep"]["disabled"], value)

    def test_bare_cli_dry_run_selects_base_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config"
            config.mkdir()
            (root / ".env").write_text('TOKEN="unclosed')
            before = sorted(root.rglob("*"))
            env = dict(os.environ, HOME=str(root), XDG_DATA_HOME=str(root / "data"),
                       PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1", TMPDIR="/tmp/opencode")
            result = subprocess.run([sys.executable, "-m", "workkit", "install", "--dry-run",
                                     "--config-dir", str(config)], env=env, cwd=root,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            selected = result.stdout.split("selection: would contain ")[1].splitlines()[0]
            self.assertEqual(set(selected.split(", ")), BASE)
            self.assertEqual(sorted(root.rglob("*")), before)

    def test_saved_selection_gets_new_defaults_but_empty_stays_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, config, data = root / "source", root / "config", root / "data"
            shutil.copytree(ROOT / "setup/instructions", source / "setup/instructions")
            skill = source / "setup/skills/fixture/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("Fixture skill.")
            target = detect_platform()
            packages = [package for package in load_catalog(ROOT / "packages.json")
                        if package["id"] in {"ponytail", "taskmd", "grilling"}]
            for package in packages:
                package.update(optional=package["id"] == "grilling", integration={"kind": "cli"},
                               skill={"id": package["id"], "files": {"SKILL.md": {"source": "setup/skills/fixture/SKILL.md"}}})
                for operation in ("install", "update", "verify"):
                    package[operation] = {target.platform: {"actions": [{"action": "skill"}]}}
            (source / "packages.json").write_text(json.dumps({"packages": packages}))
            for ids in (["grilling"], []):
                status, report = install_components(ids, config, data, target, source_root=source)
                self.assertEqual(status, 0, report)
            selection = _state_dir(config, data, target) / "selection.json"
            self.assertEqual(set(json.loads(selection.read_text())), {"ponytail", "taskmd", "grilling"})
            selection.write_text("[]")
            before = {path: path.read_bytes() for path in config.rglob("*") if path.is_file()}
            self.assertEqual(install_components([], config, data, target, source_root=source)[0], 2)
            self.assertEqual(selection.read_text(), "[]")
            self.assertEqual(before, {path: path.read_bytes() for path in before})

    @unittest.skipUnless(os.name == "posix", "profile and /bin/sh shell imports require POSIX environment")
    def test_environment_formats_literal_roundtrip_and_idempotency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, backend = root / "private.env", root / "backend"
            value = 'synthetic $HOME `id` \\ " single\' %'
            source.write_text('ROUNDTRIP="' + value + '"\nEMPTY=\nINVALID-NAME=ignored\n')
            source.chmod(0o600)
            backend.write_text("# keep\nOTHER=keep\nROUNDTRIP=old\nROUNDTRIP=duplicate\n")
            backend.chmod(0o640)
            block = b'# >>> opencode-workkit env >>>\n[ -f "$HOME/.config/opencode/workkit.env" ] && . "$HOME/.config/opencode/workkit.env"\n# <<< opencode-workkit env <<<\n'
            (root / ".profile").write_bytes(b"# legacy \xff\n" + block * 2)
            report = import_environment(source, home=root, backend=backend)
            self.assertEqual(report["names"], ("ROUNDTRIP",))
            shell = root / ".config/opencode/workkit.env"
            result = subprocess.run(["/bin/sh", "-c", '. "$1"; printf %s "$ROUNDTRIP"', "sh", str(shell)], capture_output=True, check=True)
            self.assertEqual(result.stdout.decode(), value)
            self.assertIn("$$HOME", (root / ".config/environment.d/opencode-workkit.conf").read_text())
            self.assertIn('\\$HOME', backend.read_text())
            self.assertIn("# keep\nOTHER=keep\n", backend.read_text())
            self.assertEqual(backend.read_text().count("ROUNDTRIP="), 1)
            self.assertEqual((root / ".profile").read_bytes(), b"# legacy \xff\n" + block)
            self.assertEqual(shell.stat().st_mode & 0o777, 0o600)
            self.assertEqual(backend.stat().st_mode & 0o777, 0o640)
            snapshot = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in root.rglob("*") if path.is_file()}
            self.assertFalse(import_environment(source, home=root, backend=backend)["changed"])
            self.assertEqual(snapshot, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in snapshot})

    @unittest.skipUnless(os.name == "posix", "POSIX file permission bit tests and backend paths")
    def test_environment_discovery_and_unsafe_preflight_never_leak(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.env"
            source.write_text('TOKEN="synthetic-secret\n')
            source.chmod(0o600)
            with self.assertRaises(EnvironmentError) as error:
                import_environment(source, home=root, backend=None)
            self.assertNotIn("synthetic-secret", str(error.exception))
            self.assertFalse((root / ".config").exists())
            source.write_text("TOKEN=synthetic-secret\n")
            source.chmod(0o644)
            with self.assertRaises(EnvironmentError):
                discover_env_file(source)
            source.chmod(0o600)
            backend = root / "backend"
            backend.write_text('OTHER="multiline\nTOKEN=x\n..."\n')
            with self.assertRaises(EnvironmentError):
                import_environment(source, home=root, backend=backend)
            self.assertEqual(backend.read_text(), 'OTHER="multiline\nTOKEN=x\n..."\n')
            self.assertFalse((root / ".config/opencode/workkit.env").exists())
            (root / ".profile").write_text("# >>> opencode-workkit env >>>\ncustom\n")
            with self.assertRaises(EnvironmentError):
                import_environment(source, home=root, backend=None)
            self.assertFalse((root / ".config/opencode/workkit.env").exists())
            (root / ".env").write_text("TOKEN=synthetic-secret\n")
            (root / ".env").chmod(0o644)
            with self.assertRaises(EnvironmentError):
                discover_env_file(cwd=root, checkout=ROOT)

    def test_environment_parse_and_leak_prevention(self):
        from environment import _parse, _upsert
        values = _parse(b'ROUNDTRIP="synthetic value"\n')
        updated = _upsert(b"OTHER=keep\n", values, "shell")
        self.assertEqual(_upsert(updated, values, "shell"), updated)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.env"
            source.write_text('TOKEN="synthetic-secret\n')
            source.chmod(0o600)
            with self.assertRaises(EnvironmentError) as error:
                import_environment(source, home=root, backend=None)
            self.assertNotIn("synthetic-secret", str(error.exception))
            self.assertFalse((root / ".config").exists())

    def test_env_discovery_falls_back_to_installed_checkout(self):
        from paths import is_checkout
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout, elsewhere = root / "checkout", root / "elsewhere"
            (checkout / "src").mkdir(parents=True)
            elsewhere.mkdir()
            (checkout / "src/workkit.py").write_text("")
            (checkout / "packages.json").write_text("{}")
            self.assertTrue(is_checkout(checkout))
            self.assertFalse(is_checkout(elsewhere))
            self.assertIsNone(discover_env_file(cwd=elsewhere, checkout=checkout))
            private = checkout / ".env"
            private.write_text("TOKEN=synthetic-value\n")
            private.chmod(0o600)
            self.assertEqual(discover_env_file(cwd=elsewhere, checkout=checkout), private)

    def test_windows_acl_allows_owner_rights_sid_and_rejects_foreign_sid(self):
        if os.name != "nt":
            self.skipTest("Windows ACL test requires native NT")
        from storage import validate_windows_directory, ManagedFileError
        with tempfile.TemporaryDirectory() as directory:
            test_dir = Path(directory) / "owner_rights_dir"
            # Python on Windows creates dirs with mode=0o700 using S-1-3-4 (OWNER RIGHTS)
            test_dir.mkdir(mode=0o700)
            validate_windows_directory(test_dir)
            import subprocess
            subprocess.run(["icacls", str(test_dir), "/grant", "*S-1-1-0:(OI)(CI)R"],
                           check=True, capture_output=True)
            with self.assertRaises(ManagedFileError):
                validate_windows_directory(test_dir)



if __name__ == "__main__":
    unittest.main()
