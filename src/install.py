"""Run selected catalog recipes, then hand off to ownership-aware application."""

import os
import stat
from pathlib import Path

from own_mcp import OWN_MCP_IDS, OwnMcpError, own_mcp_inputs, stage_own_mcp_sources
from paths import (
    Target,
    _state_dir,
    detect_platform,
    source_root as bundled_source_root,
    validate_target_paths,
)
from recipes import CatalogError, RecipeResult, load_catalog, recipe_for, run_recipe
from selection import SelectionError, _catalog_ids, load_selection, update_selection
from storage import WriterLockError, writer_lock


def _ensure_state_dir(data_root: Path, state_dir: Path) -> None:
    try:
        if os.name == "nt":
            from storage import validate_windows_directory
            state_dir.relative_to(data_root)
            validate_windows_directory(state_dir, create=True)
            return
        parts = state_dir.relative_to(data_root).parts
        current = data_root
        for part in parts:
            current /= part
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = current.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise ValueError
    except (OSError, RuntimeError, ValueError):
        raise SelectionError("state directories must be private, owned directories") from None


def _runner_env(config_dir: Path, data_root: Path, root: Path) -> dict[str, str]:
    return {
        "WORKKIT_CONFIG_DIR": str(config_dir),
        "WORKKIT_DATA_ROOT": str(data_root),
        "WORKKIT_BIN_DIR": str(data_root / "bin"),
        "WORKKIT_NPM_ROOT": str(data_root / "npm"),
        "WORKKIT_SETUP_DIR": str(root / "setup"),
    }


def _completed_text(completed: list[tuple[str, tuple[int, ...]]]) -> str:
    if not completed:
        return "none"
    return "; ".join(f"{ident} steps {','.join(map(str, steps))}" for ident, steps in completed)


def _failure_report(operation: str, ident: str, result: RecipeResult,
                    completed: list[tuple[str, tuple[int, ...]]]) -> str:
    error = result.error or f"Python action exited with status {result.returncode}"
    return (
        f"{operation}: incomplete; recipe failed for {ident} at step {result.failed_step}\n"
        f"completed steps: {_completed_text(completed)}\n"
        f"{ident} steps run: {','.join(map(str, result.completed)) or 'none'}\n"
        f"error: {error}\n"
        "configuration and instructions were not applied; recipe resources may be partial; inspect before retrying\n"
    )


def install_components(
    component_ids,
    config_dir,
    data_root,
    target: Target,
    *,
    operation: str = "install",
    dry_run: bool = False,
    source_root: str | Path | None = None,
    catalog_path: str | Path | None = None,
    all_components: bool = False,
    env_file: str | Path | None = None,
) -> tuple[int, str]:
    """Install or update recipes; apply no configuration if any recipe fails."""
    if operation not in ("install", "update") or type(dry_run) is not bool:
        return 2, f"{operation}: blocked: invalid operation\n"
    if (type(all_components) is not bool
            or (all_components and (operation != "install" or component_ids))):
        return 2, f"{operation}: blocked: --all requires install without component IDs\n"
    try:
        source = Path(source_root or bundled_source_root()).expanduser()
        if not source.is_absolute():
            raise ValueError("catalog source root must be absolute")
        source = source.resolve(strict=True)
        packages = load_catalog(Path(catalog_path) if catalog_path is not None else source / "packages.json")
        selectable_ids = _catalog_ids(packages) & {package["id"] for package in packages}
        if (not isinstance(component_ids, (list, tuple))
                or any(not isinstance(ident, str) for ident in component_ids)
                or len(component_ids) != len(set(component_ids))
                or not set(component_ids) <= selectable_ids):
            raise ValueError("IDs must be unique selectable component IDs from packages.json")
        config, data = validate_target_paths(
            config_dir,
            data_root,
            allow_missing_data=dry_run,
            create_data_root=not dry_run,
            allow_missing_config=dry_run and operation == "install",
            create_config_dir=not dry_run and operation == "install",
        )
        host = detect_platform()
        if not dry_run and target != host:
            raise ValueError("recipes can only execute for the current platform and architecture")
        state_dir = _state_dir(config, data, target)
        selection_path = state_dir / "selection.json"

        def perform() -> tuple[int, str]:
            if not dry_run:
                _ensure_state_dir(data, state_dir)
            saved_exists = False
            selected = frozenset()
            if state_dir.is_dir():
                snapshot = load_selection(selection_path, packages)
                selected, saved_exists = snapshot.selected, snapshot.exists
            requested = list(component_ids)
            if operation == "update":
                if not saved_exists:
                    raise ValueError("saved selection is missing; install components first")
                requested = requested or [package["id"] for package in packages if package["id"] in selected]
                if not requested:
                    raise ValueError("no selected components are available to update")
                if not set(requested) <= selected:
                    raise ValueError("update IDs must already be selected; use install to add components")
                effective = selected
                run_ids = set(requested)
            else:
                if all_components:
                    requested = [package["id"] for package in packages]
                if not requested:
                    if saved_exists:
                        if not selected:
                            raise ValueError("saved selection is empty; pass component IDs explicitly")
                        requested = [package["id"] for package in packages
                                     if package["id"] in selected or package.get("optional") is False]
                    else:
                        requested = [package["id"] for package in packages if package.get("optional") is False]
                effective = frozenset(selected | set(requested))
                run_ids = set(requested)

            if not run_ids <= selectable_ids:
                raise ValueError("requested IDs must be selectable components from packages.json")

            recipes = {}
            for package in packages:
                ident = package["id"]
                if ident in run_ids:
                    recipe = recipe_for(package, operation, target.platform)
                    if recipe is None:
                        if package["integration"]["kind"] == "remote-mcp":
                            continue
                        raise ValueError(f"{ident} has no {operation} recipe for {target.platform}")
                    recipes[ident] = recipe

            actions = []
            completed: list[tuple[str, tuple[int, ...]]] = []
            env = _runner_env(config, data, source)
            for package in packages:
                ident = package["id"]
                if ident not in run_ids:
                    continue
                recipe = recipes.get(ident)
                if recipe is None:
                    if package["integration"]["kind"] == "remote-mcp":
                        actions.append(f"{ident}: no {operation} recipe (declared integration kind)")
                        continue
                    raise ValueError(f"{ident} has no {operation} recipe for {target.platform}")
                result = run_recipe(
                    ident, operation, recipe, env, dry_run=dry_run, cwd=data, package=package,
                )
                if result.preview:
                    actions.append(f"{ident} {operation} (Python actions):")
                    actions.extend(f"  {step}. {command}" for step, command in enumerate(result.commands, 1))
                    continue
                if not result.ok:
                    return 2, _failure_report(operation, ident, result, completed)
                completed.append((ident, result.completed))

            if not dry_run and (operation == "install" or snapshot.needs_migration):
                update_selection(
                    selection_path,
                    snapshot,
                    packages,
                    {ident: True for ident in requested} if operation == "install" else {},
                )
            if dry_run:
                return 0, (
                    f"{operation}: dry-run; no actions, selection, or filesystem writes performed\n"
                    f"selection: would contain {', '.join(package['id'] for package in packages if package['id'] in effective)}\n"
                    + "\n".join(actions) + "\n"
                    "configuration and instructions: would apply only after successful recipes\n"
                )

            own_selected = set(effective) & OWN_MCP_IDS
            if own_selected:
                try:
                    stage_own_mcp_sources(
                        data, target, own_selected, own_mcp_inputs(source, own_selected)
                    )
                except OwnMcpError as exc:
                    return 2, (
                        f"{operation}: recipes completed; own MCP source staging failed\n"
                        f"completed recipes: {_completed_text(completed)}\n"
                        f"error: {exc}\nconfiguration and instructions were not applied\n"
                    )

            from sync import sync_plugins

            status, apply_report = sync_plugins(
                config,
                target,
                data_root=data,
                source_root=source,
                catalog_path=catalog_path,
                lock_held=True,
                verify=True,
                env_file=env_file if operation == "install" else None,
            )
            if status:
                return 2, (
                    f"{operation}: recipes completed; application blocked or partial\n"
                    f"completed recipes: {_completed_text(completed)}\n"
                    + apply_report
                )
            return 0, (
                f"{operation}: recipes completed\n"
                f"completed recipes: {_completed_text(completed)}\n"
                + apply_report
            )

        if dry_run:
            return perform()
        with writer_lock(data, data / ".workkit.lock"):
            return perform()
    except (CatalogError, OSError, RuntimeError, SelectionError, WriterLockError, TypeError, ValueError) as exc:
        return 2, f"{operation}: blocked: {exc}\n"
