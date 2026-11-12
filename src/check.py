"""Read-only selection and local recipe checks; runtime activation stays unknown."""

import stat
from pathlib import Path

from own_mcp import OWN_MCP_IDS, OwnMcpError, own_mcp_inputs, verify_own_mcp_source
from paths import (
    Target,
    _data_root,
    _state_dir,
    detect_platform,
    source_root as bundled_source_root,
    validate_target_paths,
)
from recipes import CatalogError, load_catalog, recipe_for, run_recipe
from selection import SelectionError, load_selection
from storage import ReceiptError, load_receipt


def _config_state(config_dir) -> str:
    directory = Path(config_dir).expanduser()
    try:
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or directory.resolve(strict=True) != directory:
            return "unknown"
        candidates = []
        for name in ("opencode.json", "opencode.jsonc"):
            path = directory / name
            try:
                entry = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
                return "unknown"
            candidates.append(path)
        if not candidates:
            return "missing"
        if len(candidates) != 1:
            return "unknown"
        from config import _parse

        _parse(candidates[0].read_text(encoding="utf-8"))
        return "valid"
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return "unknown"


def check(config_dir, target: Target, *, catalog_path=None, data_root=None):
    """Return a read-only local report; no network, server, or agent is launched."""
    if (not isinstance(target, Target) or target.platform not in ("linux", "wsl", "windows")
            or target.architecture not in ("x86_64", "arm64")):
        return 2, "target: invalid or unsupported\n"
    root = Path(catalog_path).expanduser().parent if catalog_path is not None else bundled_source_root()
    data = Path(data_root or _data_root()).expanduser()
    try:
        config, data = validate_target_paths(config_dir, data, allow_missing_data=True)
        root = root.resolve(strict=True)
        packages = load_catalog(Path(catalog_path) if catalog_path is not None else root / "packages.json")
    except (CatalogError, OSError, RuntimeError, TypeError, ValueError) as exc:
        return 2, f"check: blocked: catalog or target path is invalid\nerror: {exc}\n"

    state_dir = _state_dir(config, data, target)
    lines = [f"target: {target.platform}-{target.architecture}", f"catalog: valid ({len(packages)} components)"]
    config_state = _config_state(config)
    lines.append(f"config: {config_state}")
    try:
        selection = load_selection(state_dir / "selection.json", packages)
        lines.append(f"selection: {'valid' if selection.exists else 'missing'}")
        if getattr(selection, "needs_migration", False):
            lines.append("selection migration: available (read-only; run install or sync to save)")
    except (OSError, RuntimeError, SelectionError, ValueError):
        selection = None
        lines.append("selection: unknown (missing, invalid, or unsafe)")
    try:
        receipt = load_receipt(state_dir / "receipt.json")
        receipt_state = "valid" if receipt._identity is not None else "missing"
    except (OSError, ReceiptError, RuntimeError, ValueError):
        receipt = None
        receipt_state = "unknown (invalid or unsafe)"
    lines.append(f"ownership receipt: {receipt_state}")

    if selection is None or not selection.exists:
        lines.append("result: incomplete; no validated saved selection")
        return 2, "\n".join(lines) + "\n"

    try:
        host = detect_platform()
    except ValueError:
        host = None
    selected = selection.selected
    failures = []
    env = {
        "WORKKIT_CONFIG_DIR": str(config),
        "WORKKIT_DATA_ROOT": str(data),
        "WORKKIT_BIN_DIR": str(data / "bin"),
        "WORKKIT_NPM_ROOT": str(data / "npm"),
        "WORKKIT_SETUP_DIR": str(root / "setup"),
    }
    for package in packages:
        ident = package["id"]
        is_selected = ident in selected
        lines.extend((f"{ident}:", f"  selected: {'yes' if is_selected else 'no'}"))
        if not is_selected:
            continue
        kind = package["integration"]["kind"]
        if kind == "remote-mcp":
            lines.append("  installed: not-applicable (hosted integration)")
            continue
        if host != target:
            lines.append("  installed: unknown (verification must run on the target platform)")
            failures.append(ident)
            continue
        if not data.is_dir():
            lines.append("  installed: no (private data root or component is missing)")
            failures.append(ident)
            continue
        recipe = recipe_for(package, "verify", target.platform)
        if recipe is None:
            lines.append("  installed: unknown (no verify recipe)")
            failures.append(ident)
            continue
        result = run_recipe(ident, "verify", recipe, env, cwd=data, package=package)
        if not result.ok:
            lines.append(
                f"  installed: no (verify failed at step {result.failed_step}; "
                f"completed: {','.join(map(str, result.completed)) or 'none'}; "
                f"error: {result.error or 'action failed'})"
            )
            failures.append(ident)
            continue
        if ident in OWN_MCP_IDS:
            try:
                content = own_mcp_inputs(root, {ident})[ident]
                if not verify_own_mcp_source(data, target, ident, content):
                    raise OwnMcpError("managed source differs")
            except (OwnMcpError, OSError, RuntimeError, ValueError):
                lines.append("  installed: conflict (stable own-MCP script is missing or modified)")
                failures.append(ident)
                continue
        verified = f"  installed: yes ({len(result.completed)} local verify steps)"
        if ident == "taskmd":
            executable = data / "bin" / ("taskmd.exe" if target.platform == "windows" else "taskmd")
            verified += f"; executable: {executable}"
        lines.append(verified)

    from sync import sync_plugins

    status, config_report = sync_plugins(
        config, target, dry_run=True, data_root=data, source_root=root, verify=False,
    )
    would_update = "would update" in config_report
    if status:
        lines.append("configuration: conflict or not safely applicable")
        failures.append("configuration")
    elif would_update:
        lines.append("configuration: would update (no writes)")
        failures.append("configuration")
    else:
        lines.append("configuration: safe preview (no writes)")
    if config_report:
        lines.extend(f"  {line}" for line in config_report.rstrip().splitlines())
    lines.append("runtime-confirmed: unknown (no MCP, network, or agent launched)")
    if failures:
        lines.append("result: incomplete; resolve local verify or configuration blockers")
        return 1, "\n".join(lines) + "\n"
    lines.append("result: local state verified; runtime activation remains unknown")
    return 0, "\n".join(lines) + "\n"
