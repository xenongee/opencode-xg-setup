"""Verify selected local recipes and safely apply config, instructions and own files."""

import os
import stat
from pathlib import Path

from config import ConfigError, _parse, apply_config, apply_plugins
from environment import EnvironmentError, import_environment
from instructions import InstructionsError, apply_instructions, render_instructions
from own_mcp import (
    OWN_MCP,
    OWN_MCP_IDS,
    OwnMcpError,
    OwnMcpPartialError,
    own_mcp_inputs,
    own_mcp_path,
    stage_own_mcp_sources,
    verify_own_mcp_receipt,
)
from paths import (
    Target,
    _data_root,
    _state_dir,
    detect_platform,
    source_root as bundled_source_root,
    validate_target_paths,
)
from recipes import CatalogError, load_catalog, recipe_for, run_recipe, verify_skill_files, _PLUGIN_FILES
from selection import SelectionError, load_selection, update_selection
from storage import (
    ManagedFileError,
    ManagedFileResult,
    ReceiptError,
    WriterLockError,
    apply_file,
    load_receipt,
    save_receipt,
    remove_file,
    validate_windows_directory,
    writer_lock,
)

_INSTRUCTIONS = {
    "codebase-memory-mcp": "cbm",
    "oh-my-opencode-slim": "slim",
    "ponytail": "ponytail",
    "engram": "engram",
    "semgrep": "semgrep",
    "markitdown-mcp": "markitdown",
    "taskmd": "taskmd",
}
_SLIM_APPEND = "oh-my-opencode-slim/orchestrator_append.md"
_UV_MCP_BINARIES = {"duckduckgo": "duckduckgo-mcp-server", "semgrep": "semgrep", "markitdown-mcp": "markitdown-mcp"}


def _blocked(*reasons: str) -> tuple[int, str]:
    return 2, "sync: blocked\n" + "".join(f"- {reason}\n" for reason in reasons)


def _config_path(config_dir: str | Path) -> Path:
    directory = Path(config_dir).expanduser()
    try:
        info = directory.lstat()
    except OSError:
        raise ConfigError("config directory is missing or inaccessible") from None
    if not stat.S_ISDIR(info.st_mode) or directory.resolve(strict=True) != directory:
        raise ConfigError("config directory must be an existing non-symlink directory")
    candidates = []
    for name in ("opencode.json", "opencode.jsonc"):
        candidate = directory / name
        try:
            entry = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise ConfigError("cannot inspect config") from None
        if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
            raise ConfigError("config must be a regular file with one link")
        candidates.append(candidate)
    if len(candidates) > 1:
        raise ConfigError("config is ambiguous")
    return candidates[0] if candidates else directory / "opencode.json"


def _owned_directory(path, info, *, private=False):
    if not stat.S_ISDIR(info.st_mode) or path.resolve(strict=True) != path:
        raise ManagedFileError("managed directory is unsafe")
    if os.name == "nt":
        validate_windows_directory(path)
    elif (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022
          or (private and stat.S_IMODE(info.st_mode) != 0o700)):
        raise ManagedFileError("managed directories must be owned and not writable by others")


def _slim_append(config_dir: Path, content: bytes, files: dict, *, dry_run: bool):
    directory = config_dir / "oh-my-opencode-slim"
    try:
        info = directory.lstat()
    except FileNotFoundError:
        if _SLIM_APPEND in files:
            raise ManagedFileError("managed Slim append directory is missing") from None
        if dry_run:
            return ManagedFileResult(True, None)
        if os.name == "nt":
            validate_windows_directory(directory, create=True)
        else:
            directory.mkdir(mode=0o700)
        info = directory.lstat()
    _owned_directory(directory, info, private=True)
    return apply_file(config_dir, _SLIM_APPEND, content, files.get(_SLIM_APPEND), mode=0o600, dry_run=dry_run)


def _mcp_paths(name: str, fields: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    return {field: ("mcp", "servers", name, field) for field in fields}


class SkillPartialError(ManagedFileError):
    def __init__(self, message: str, changed: bool):
        super().__init__(message)
        self.changed = changed


def _skills(config_dir: Path, data_root: Path, source: Path, selected: list[dict], files: dict, *, dry_run: bool):
    plans = []
    targets = set()
    for package in selected:
        ident = package["id"]
        declaration = package.get("skill")
        if declaration is None:
            continue
        ident = declaration["id"]
        files_decl = declaration["files"]
        cached = verify_skill_files(package, data_root) if any(entry["source"].startswith("https://") for entry in files_decl.values()) else {}
        for name, entry in files_decl.items():
            external = entry["source"].startswith("https://")
            relative_source = Path(name) if external else Path(entry["source"])
            if relative_source.is_absolute() or any(part in ("", ".", "..") for part in relative_source.parts):
                raise ManagedFileError("unsafe skill source path")
            base = data_root / "skills" / ident / "main" if external else source
            src = base / relative_source if external else source / relative_source
            origin = data_root if external else source
            try:
                components = base.relative_to(origin).parts + relative_source.parts if external else relative_source.parts
            except ValueError:
                raise ManagedFileError("skill source escapes its root") from None
            cursor = origin
            unsafe = False
            for part in components:
                cursor = cursor / part
                if cursor.is_symlink():
                    unsafe = True
                    break
            if unsafe or not src.is_file():
                raise ManagedFileError(f"required skill source is missing or unsafe: {ident}/{name}")
            relative = f"skills/{ident}/{name}"
            if relative in targets:
                raise ManagedFileError("selected skills have overlapping target paths")
            targets.add(relative)
            content = cached[name] if external else src.read_bytes()
            # Preflight ownership and file safety for every target before the first write.
            plan = apply_file(config_dir, relative, content, files.get(relative), dry_run=True) if (config_dir / relative).parent.is_dir() else None
            if plan is not None and files.get(relative) is not None and plan.receipt is None:
                raise ManagedFileError("managed skill receipt is unavailable")
            parent = (config_dir / relative).parent
            parts = parent.relative_to(config_dir).parts
            current = config_dir
            for part in parts:
                current /= part
                try:
                    info = current.lstat()
                except FileNotFoundError:
                    if files.get(relative) is not None:
                        raise ManagedFileError("managed skill directory is missing") from None
                    if dry_run:
                        continue
                    if os.name == "nt":
                        validate_windows_directory(current, create=True)
                    else:
                        current.mkdir(mode=0o700)
                    info = current.lstat()
                _owned_directory(current, info)
            plans.append((relative, content, parent))
    if dry_run:
        results = {}
        for relative, content, parent in plans:
            if parent.is_dir():
                results[relative] = apply_file(config_dir, relative, content, files.get(relative), dry_run=True)
            elif files.get(relative) is not None:
                raise ManagedFileError("managed skill directory is missing")
            else:
                results[relative] = ManagedFileResult(True, None)
        return results
    # Make all required target directories before publishing the first file.
    for _, _, parent in plans:
        current = config_dir
        for part in parent.relative_to(config_dir).parts:
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                if os.name == "nt":
                    validate_windows_directory(current, create=True)
                else:
                    current.mkdir(mode=0o700)
                info = current.lstat()
            _owned_directory(current, info)
    results = {}
    try:
        for relative, content, _ in plans:
            results[relative] = apply_file(config_dir, relative, content, files.get(relative))
    except (ManagedFileError, OSError) as exc:
        raise SkillPartialError(f"skill set partially applied after {len(results)} file(s): {exc}", bool(results)) from exc
    return results


def _mcp_config(package: dict, data_root: Path, target: Target):
    ident = package["id"]
    integration = package["integration"]
    kind = integration["kind"]
    name = integration["name"]
    if kind == "remote-mcp":
        values = {
            "type": "remote",
            "url": integration["url"],
            "headers": integration["headers"],
            "oauth": integration["oauth"],
        }
    else:
        if ident in OWN_MCP_IDS:
            script = own_mcp_path(data_root, ident)
            dependencies = OWN_MCP[ident][2]
            command = ["uv", "run", "--offline", "--no-project", "--isolated"]
            for dependency in dependencies:
                command.extend(("--with", dependency))
            command.extend(("--script", str(script)))
        elif ident == "rea":
            command = ["node", str(data_root / "npm/rea/node_modules/rea-agents/scripts/rea.mjs"), "mcp"]
        else:
            binary = _UV_MCP_BINARIES.get(ident, ident)
            executable = data_root / "bin" / (binary + (".exe" if target.platform == "windows" else ""))
            command = [str(executable), *integration["arguments"]]
        values = {"type": "local", "command": command}
    fields = tuple(values)
    return name, values, _mcp_paths(name, fields)


def _desired_config(config_file: Path, receipt, packages: list[dict], selected: list[dict],
                    data_root: Path, target: Target):
    try:
        current = _parse(config_file.read_text(encoding="utf-8") if config_file.exists() else "{}").value
    except (OSError, UnicodeError, ConfigError):
        raise ConfigError("cannot safely inspect target config") from None
    mcp = current.get("mcp", {})
    servers = mcp.get("servers", {}) if isinstance(mcp, dict) else None
    if not isinstance(servers, dict):
        raise ConfigError("MCP server map is ambiguous")

    desired = {}
    initially_disabled = set()
    plugins = {}
    for package in selected:
        integration = package["integration"]
        kind = integration["kind"]
        ident = package["id"]
        if kind == "plugin":
            plugin_name = integration["package"]
            plugin_path = data_root / "npm" / ident / "node_modules" / Path(*plugin_name.split("/"))
            plugins[plugin_name] = str(plugin_path)
        elif kind == "managed-plugin":
            plugins[ident] = str(data_root / "plugins" / ident)
        elif kind in ("mcp", "remote-mcp"):
            name, values, paths = _mcp_config(package, data_root, target)
            environment_path = ("mcp", "servers", name, "environment")
            if ident == "engram" and environment_path in receipt.config:
                server = servers.get(name)
                if not isinstance(server, dict) or "environment" not in server:
                    raise ConfigError("managed Engram environment is missing")
                values["environment"] = server["environment"]
                paths["environment"] = environment_path
            owned_paths = {path for path in paths.values() if path in receipt.config}
            if owned_paths and owned_paths != set(paths.values()):
                raise ConfigError(f"MCP ownership is incomplete for {name}")
            exists = name in servers
            if exists and not owned_paths:
                raise ConfigError(f"MCP server {name} already exists and is not managed by Workkit")
            for field, value in values.items():
                desired[paths[field]] = value
            if not exists and not owned_paths and package.get("optional") is True:
                disabled_path = ("mcp", "servers", name, "disabled")
                desired[disabled_path] = True
                initially_disabled.add(disabled_path)

    return desired, initially_disabled, plugins


def _verify_selected(packages: list[dict], selected: list[dict], target: Target,
                     config: Path, data: Path, root: Path) -> tuple[str, ...]:
    env = {
        "WORKKIT_CONFIG_DIR": str(config),
        "WORKKIT_DATA_ROOT": str(data),
        "WORKKIT_BIN_DIR": str(data / "bin"),
        "WORKKIT_NPM_ROOT": str(data / "npm"),
        "WORKKIT_SETUP_DIR": str(root / "setup"),
    }
    completed = []
    for package in selected:
        kind = package["integration"]["kind"]
        ident = package["id"]
        if kind == "remote-mcp":
            completed.append(f"{ident}: hosted; no local verifier")
            continue
        recipe = recipe_for(package, "verify", target.platform)
        if recipe is None:
            raise ValueError(f"{ident} has no verify recipe for {target.platform}")
        result = run_recipe(ident, "verify", recipe, env, cwd=data, package=package)
        if not result.ok:
            raise ValueError(
                f"{ident} verify failed at step {result.failed_step}; "
                f"completed steps: {','.join(map(str, result.completed)) or 'none'}; "
                f"error: {result.error or 'action failed'}"
            )
        if ident in OWN_MCP_IDS:
            if not verify_own_mcp_receipt(data, target, ident):
                raise ValueError(f"{ident} stable source copy differs from its managed receipt")
        elif kind == "plugin":
            plugin_name = package["integration"]["package"]
            plugin_path = data / "npm" / ident / "node_modules" / Path(*plugin_name.split("/"))
            try:
                if not plugin_path.is_dir() or plugin_path.is_symlink() or plugin_path.resolve(strict=True) != plugin_path:
                    raise ValueError
            except (OSError, RuntimeError, ValueError):
                raise ValueError(f"{ident} plugin path is missing or unsafe") from None
        elif ident == "rea":
            from recipes import _directory
            directory = data / "npm/rea"
            _directory(directory, "node_modules/rea-agents/scripts", dry_run=True)
            script = directory / "node_modules/rea-agents/scripts/rea.mjs"
            info = script.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or script.resolve(strict=True) != script
                    or (os.name == "posix" and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022))):
                raise ValueError("REA entrypoint is missing or unsafe")
        elif kind == "mcp":
            binary = _UV_MCP_BINARIES.get(ident, ident)
            executable = data / "bin" / (binary + (".exe" if target.platform == "windows" else ""))
            try:
                if ident in _UV_MCP_BINARIES and executable.is_symlink():
                    resolved = executable.resolve(strict=True)
                    tool_root = data / "uv-tools" / binary
                    if (tool_root.resolve(strict=True) != tool_root
                            or not resolved.is_relative_to(tool_root)):
                        raise ValueError
                    info = resolved.stat()
                else:
                    info = executable.lstat()
                    if executable.resolve(strict=True) != executable:
                        raise ValueError
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError
                if target.platform != "windows" and not info.st_mode & 0o111:
                    raise ValueError
            except (OSError, RuntimeError, ValueError):
                raise ValueError(f"{ident} executable path is missing or unsafe") from None
        completed.append(f"{ident}: verified ({len(result.completed)} steps)")
    return tuple(completed)


def _sync_plugins(
    config_dir: str | Path,
    target: Target,
    *,
    dry_run: bool = False,
    data_root: str | Path | None = None,
    component_ids: tuple[str, ...] | list[str] | None = None,
    catalog_path: str | Path | None = None,
    source_root: str | Path | None = None,
    lock_held: bool = False,
    verify: bool = True,
) -> tuple[int, str]:
    if type(dry_run) is not bool or type(verify) is not bool:
        return _blocked("invalid operation flags")
    try:
        config, data = validate_target_paths(
            config_dir,
            data_root or _data_root(),
            allow_missing_data=dry_run,
        )
        root = Path(source_root or bundled_source_root()).expanduser()
        if not root.is_absolute():
            return _blocked("source root must be absolute")
        root = root.resolve(strict=True)
        packages = load_catalog(Path(catalog_path) if catalog_path is not None else root / "packages.json")
        if not dry_run and detect_platform() != target:
            return _blocked("local recipes and safe writes can only target the current platform and architecture")
        if component_ids is not None and (
            not isinstance(component_ids, (list, tuple)) or not component_ids
            or len(component_ids) != len(set(component_ids))
        ):
            return _blocked("component IDs must be a non-empty unique list")
        state_dir = _state_dir(config, data, target)
        selection = load_selection(state_dir / "selection.json", packages)
        if not selection.exists:
            return _blocked("saved selection is missing; no components are assumed selected")
        selected_ids = set(selection.selected) if component_ids is None else set(component_ids)
        if not selected_ids <= selection.selected:
            return _blocked("requested components must be a non-empty subset of the saved selection")
        package_by_id = {package["id"]: package for package in packages}
        if not selected_ids <= package_by_id.keys():
            return _blocked("selection contains an unknown component")
        selected = [package for package in packages if package["id"] in selected_ids]

        verification = ()
        if verify and not dry_run:
            if not data.is_dir():
                return _blocked("private data root is missing; sync never installs components")
            try:
                verification = _verify_selected(packages, selected, target, config, data, root)
            except (OSError, RuntimeError, ValueError) as exc:
                return _blocked(str(exc))
        elif not dry_run:
            # Even internal callers cannot register an unprepared managed plugin.
            managed = [package for package in selected if package["integration"]["kind"] == "managed-plugin"]
            if managed:
                verification = _verify_selected(packages, managed, target, config, data, root)

        config_file = _config_path(config)
        receipt_path = state_dir / "receipt.json"
        receipt = load_receipt(receipt_path)
        desired_config, initially_disabled, desired_plugins = _desired_config(
            config_file, receipt, packages, selected, data, target
        )
        remove_plugins = ("x-custom-providers",) if "x-custom-providers" in receipt.plugins and "x-custom-providers" not in selection.selected else ()
        removal_receipt = None
        plugin_directory = data / "plugins/x-custom-providers"
        if remove_plugins:
            removal_receipt = load_receipt(data / "plugins/x-custom-providers-receipt.json")
            if set(removal_receipt.files) != set(_PLUGIN_FILES):
                raise ManagedFileError("managed plugin removal receipt is incomplete")
            for name, digest in removal_receipt.files.items():
                remove_file(plugin_directory, name, digest, dry_run=True)
        instruction_components = [
            _INSTRUCTIONS[package["id"]]
            for package in selected if package["id"] in _INSTRUCTIONS
        ]

        own_selected = {package["id"] for package in selected if package["id"] in OWN_MCP_IDS}
        own_sources = own_mcp_inputs(root, own_selected) if own_selected else None
        append_content = None
        if "oh-my-opencode-slim" in selected_ids:
            append_content = (root / "setup/instructions/slim/orchestrator_append.md").read_bytes()
        skill_selected = [package for package in selected if "skill" in package]

        # Preview all managed writes before publishing any of them.
        config_preview = apply_config(config_file, desired_config, receipt.config, dry_run=True) if desired_config else None
        plugins_preview = apply_plugins(config_file, desired_plugins, receipt.plugins, remove=remove_plugins, dry_run=True) if desired_plugins or remove_plugins else None
        if dry_run and detect_platform() != target:
            # Render cross-platform previews without invoking the host-only apply path.
            instruction_preview = apply_file(
                config, "AGENTS.md",
                render_instructions(root / "setup/instructions", target.platform, instruction_components).encode("utf-8"),
                receipt.files.get("AGENTS.md"), dry_run=True,
            )
        else:
            instruction_preview = apply_instructions(
                root / "setup/instructions", config, target.platform, instruction_components,
                receipt=receipt.files.get("AGENTS.md"), dry_run=True,
            )
        append_preview = (
            _slim_append(config, append_content, receipt.files, dry_run=True)
            if append_content is not None else None
        )
        skill_preview = _skills(config, data, root, skill_selected, receipt.files, dry_run=True)
        own_preview = (
            stage_own_mcp_sources(data, target, own_selected, own_sources, dry_run=True)
            if own_selected and dry_run else {}
        )
        if dry_run:
            own_updates = [ident for ident, update in own_preview.items() if update]
            return 0, (
                f"sync: preview only; components: {', '.join(package['id'] for package in selected)}\n"
                f"selection: {'would migrate renamed IDs (no writes)' if selection.needs_migration else 'unchanged'}\n"
                f"verified: {'not run (dry-run)' if verify else 'not requested'}\n"
                f"config: {'would update' if config_preview and config_preview.changed else 'unchanged'}\n"
                f"plugins: {'would update' if plugins_preview and plugins_preview.changed else 'unchanged'}\n"
                f"own MCP sources: {'would update (' + ', '.join(own_updates) + ')' if own_updates else 'unchanged'}\n"
                f"instructions: {'would update' if instruction_preview.changed else 'unchanged'}\n"
                f"Slim append: {'would update' if append_preview and append_preview.changed else 'unchanged'}\n"
                f"Skills: {'would update' if any(item.changed for item in skill_preview.values()) else 'unchanged'}\n"
                "config, ownership receipts, and files: not written\n"
                "runtime-confirmed: unknown (not probed)\n"
            )

        if selection.needs_migration:
            selection = update_selection(state_dir / "selection.json", selection, packages, {})

        writes_started = False
        staged = {}
        try:
            if own_selected:
                staged = stage_own_mcp_sources(data, target, own_selected, own_sources)
                writes_started = any(staged.values())
            config_result = apply_config(config_file, desired_config, receipt.config) if desired_config else None
            writes_started = writes_started or bool(config_result and config_result.changed)
            plugin_result = apply_plugins(config_file, desired_plugins, receipt.plugins, remove=remove_plugins) if desired_plugins or remove_plugins else None
            writes_started = writes_started or bool(plugin_result and plugin_result.changed)
            if removal_receipt is not None:
                for name, digest in removal_receipt.files.items():
                    writes_started = remove_file(plugin_directory, name, digest) or writes_started
                save_receipt(data / "plugins/x-custom-providers-receipt.json", removal_receipt, config=removal_receipt.config, files={})
                if not any(plugin_directory.iterdir()):
                    plugin_directory.rmdir()
            append_result = (
                _slim_append(config, append_content, receipt.files, dry_run=False)
                if append_content is not None else None
            )
            writes_started = writes_started or bool(append_result and append_result.changed)
            skill_result = _skills(config, data, root, skill_selected, receipt.files, dry_run=False)
            writes_started = writes_started or any(item.changed for item in skill_result.values())
            instruction_result = apply_instructions(
                root / "setup/instructions", config, target.platform, instruction_components,
                receipt=receipt.files.get("AGENTS.md"),
            )
            writes_started = writes_started or instruction_result.changed
            files = dict(receipt.files)
            if append_result is not None:
                if append_result.receipt is None:
                    raise ManagedFileError("Slim append receipt is unavailable after application")
                files[_SLIM_APPEND] = append_result.receipt
            for path, result in skill_result.items():
                if result.receipt is None:
                    raise ManagedFileError("Playwright skill receipt is unavailable after application")
                files[path] = result.receipt
            files["AGENTS.md"] = instruction_result.receipt
            changed = any((
                bool(config_result and config_result.changed),
                bool(plugin_result and plugin_result.changed),
                bool(append_result and append_result.changed),
                any(item.changed for item in skill_result.values()),
                instruction_result.changed,
                *staged.values(),
            ))
            config_receipt = config_result.receipt if config_result is not None else receipt.config
            config_receipt = {key: value for key, value in config_receipt.items() if key not in initially_disabled}
            save_receipt(
                receipt_path,
                receipt,
                config=config_receipt,
                files=files,
                plugins=plugin_result.receipt if plugin_result is not None else receipt.plugins,
            )
        except (ConfigError, InstructionsError, ManagedFileError, OwnMcpError, ReceiptError,
                OSError, RuntimeError, ValueError) as exc:
            if writes_started or isinstance(exc, OwnMcpPartialError) or (isinstance(exc, SkillPartialError) and exc.changed):
                return 2, f"sync: partial application; receipts may not represent an atomic file set; inspect config, instructions, skills, own files, and receipt before retrying\nerror: {exc}\n"
            return _blocked(str(exc))
        return 0, (
            f"sync: {'applied' if changed else 'already matches'}; components: {', '.join(package['id'] for package in selected)}\n"
            f"verified: {'; '.join(verification) if verification else 'not run'}\n"
            "config, plugins, instructions, and ownership receipts: "
            f"{'applied safely' if changed else 'unchanged'}\n"
            "runtime-confirmed: unknown (not probed)\n"
        )
    except (CatalogError, ConfigError, InstructionsError, ManagedFileError, OwnMcpError,
            ReceiptError, SelectionError, WriterLockError, OSError, RuntimeError,
            UnicodeError, TypeError, ValueError) as exc:
        return _blocked(str(exc))


def sync_plugins(
    config_dir: str | Path,
    target: Target,
    *,
    dry_run: bool = False,
    data_root: str | Path | None = None,
    component_ids: tuple[str, ...] | list[str] | None = None,
    catalog_path: str | Path | None = None,
    source_root: str | Path | None = None,
    lock_held: bool = False,
    verify: bool = True,
    env_file: str | Path | None = None,
) -> tuple[int, str]:
    """Verify local state and then safely apply the selected composition."""
    if type(lock_held) is not bool:
        return _blocked("invalid lock state")
    try:
        _config, data = validate_target_paths(
            config_dir,
            data_root or _data_root(),
            allow_missing_data=dry_run,
        )
        def apply():
            status, report = _sync_plugins(
                config_dir, target, dry_run=dry_run, data_root=data,
                component_ids=component_ids, catalog_path=catalog_path,
                source_root=source_root, lock_held=lock_held, verify=verify,
            )
            if status or dry_run or env_file is None:
                return status, report
            try:
                imported = import_environment(env_file)
            except EnvironmentError:
                return 2, report + "environment: blocked or partially applied; safe to retry after resolving the environment targets\n"
            return 0, report + (
                f"environment: {len(imported['names'])} names; {len(imported['changed'])} targets updated\n"
                f"environment skipped: {', '.join(imported['skipped']) or 'none'}\n"
            )

        if dry_run or lock_held:
            return apply()
        with writer_lock(data, data / ".workkit.lock"):
            return apply()
    except (OSError, RuntimeError, ValueError, WriterLockError) as exc:
        return _blocked(str(exc))
