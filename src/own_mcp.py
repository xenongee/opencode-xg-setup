"""Safely stage repository-owned MCP scripts at stable paths outside checkout."""

import os
import stat
from pathlib import Path

from paths import Target
from storage import ManagedFileError, ReceiptError, _existing, _parent, apply_file, load_receipt, save_receipt

OWN_MCP = {
    "x-4pda": ("4pda.py", "x_forum_4pda", ("mcp>=1.6,<2", "httpx", "beautifulsoup4")),
    "x-genius": ("genius.py", "x_genius", ("mcp>=1.6,<2", "httpx", "beautifulsoup4")),
}
OWN_MCP_IDS = frozenset(OWN_MCP)


class OwnMcpError(ValueError):
    pass


class OwnMcpPartialError(OwnMcpError):
    pass


def _read_source(path: Path) -> bytes:
    try:
        if os.name == "nt":
            with _parent(path, private=False) as (directory, name):
                entry = _existing(directory, name, private=False)
                if entry is None:
                    raise ValueError
                return entry[0]
        if path.resolve(strict=True) != path:
            raise ValueError
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError
        content = path.read_bytes()
        after = path.lstat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise ValueError
        return content
    except (OSError, RuntimeError, ValueError):
        raise OwnMcpError(f"repository MCP source {path.name} is missing or unsafe") from None


def own_mcp_inputs(source_root: str | Path, selected=OWN_MCP_IDS) -> dict[str, bytes]:
    if not isinstance(selected, (set, frozenset)) or not selected or not selected <= OWN_MCP_IDS:
        raise OwnMcpError("invalid own MCP source selection")
    root_input = Path(source_root).expanduser()
    if not root_input.is_absolute():
        raise OwnMcpError("source checkout path must be absolute")
    try:
        root = root_input.resolve(strict=True)
        return {
            ident: _read_source(root / "setup" / "mcp" / OWN_MCP[ident][0])
            for ident in selected
        }
    except (OSError, RuntimeError, TypeError, ValueError):
        raise OwnMcpError("own MCP source is missing or unsafe") from None


def own_mcp_component(data_root: str | Path, target: Target | None = None) -> Path:
    return Path(data_root) / "bin"


def own_mcp_path(data_root: str | Path, ident: str) -> Path:
    if ident not in OWN_MCP:
        raise OwnMcpError("unknown own MCP ID")
    return own_mcp_component(data_root) / OWN_MCP[ident][0]


def _component(data_root: str | Path) -> Path:
    component = own_mcp_component(data_root)
    if os.name == "nt":
        from storage import validate_windows_directory
        try:
            validate_windows_directory(component)
        except (OSError, ValueError):
            raise OwnMcpError("stable MCP script directory is missing or unsafe") from None
        return component
    try:
        info = component.lstat()
    except OSError:
        raise OwnMcpError("stable MCP script directory is missing or unsafe") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or component.resolve(strict=True) != component:
        raise OwnMcpError("stable MCP script directory is missing or unsafe")
    return component


def stage_own_mcp_sources(
    data_root: str | Path,
    target: Target,
    selected: set[str] | frozenset[str],
    contents: dict[str, bytes],
    *,
    dry_run: bool = False,
) -> dict[str, bool]:
    """Update only selected stable copies using a private receipt and safe file writer."""
    if (not isinstance(selected, (set, frozenset)) or not selected or not selected <= OWN_MCP_IDS
            or not isinstance(contents, dict) or set(contents) != selected or type(dry_run) is not bool):
        raise OwnMcpError("invalid own MCP source selection")
    component = own_mcp_component(data_root, target)
    try:
        exists = component.is_dir() and not component.is_symlink()
        if not exists and dry_run:
            return {ident: True for ident in selected}
        component = _component(data_root)
        receipt_path = component / "sources-receipt.json"
        snapshot = load_receipt(receipt_path)
        files = dict(snapshot.files)
        changed = {}
        writes_started = False
        for ident in sorted(selected):
            filename = OWN_MCP[ident][0]
            try:
                result = apply_file(
                    component, filename, contents[ident], files.get(filename), mode=0o600, dry_run=dry_run
                )
            except ManagedFileError as exc:
                error = OwnMcpPartialError if writes_started else OwnMcpError
                raise error("own MCP source conflicts with unowned or modified data") from exc
            changed[ident] = result.changed
            if dry_run:
                continue
            if result.receipt is None:
                raise OwnMcpPartialError("own MCP source receipt is unavailable after staging")
            if files.get(filename) != result.receipt:
                writes_started = writes_started or result.changed
                files[filename] = result.receipt
                try:
                    snapshot = save_receipt(
                        receipt_path,
                        snapshot,
                        config=snapshot.config,
                        files=files,
                        plugins=snapshot.plugins,
                    )
                except ReceiptError as exc:
                    raise OwnMcpPartialError("own MCP source receipt is uncertain; inspect before retrying") from exc
        return changed
    except OwnMcpError:
        raise
    except (OSError, ReceiptError, RuntimeError, ValueError):
        raise OwnMcpError("cannot safely stage own MCP source files") from None


def verify_own_mcp_source(data_root: str | Path, target: Target, ident: str, content: bytes) -> bool:
    if ident not in OWN_MCP_IDS or not isinstance(content, bytes):
        raise OwnMcpError("invalid own MCP source identity")
    component = _component(data_root)
    try:
        snapshot = load_receipt(component / "sources-receipt.json")
        filename = OWN_MCP[ident][0]
        result = apply_file(component, filename, content, snapshot.files.get(filename), mode=0o600, dry_run=True)
        return not result.changed
    except (ManagedFileError, ReceiptError, OSError, RuntimeError, ValueError) as exc:
        raise OwnMcpError("own MCP source copy is missing, modified, or unowned") from exc


def verify_own_mcp_receipt(data_root: str | Path, target: Target, ident: str) -> bool:
    """Confirm the installed copy still matches its previous receipt before updating it."""
    if ident not in OWN_MCP_IDS:
        raise OwnMcpError("invalid own MCP source identity")
    component = _component(data_root)
    try:
        snapshot = load_receipt(component / "sources-receipt.json")
        filename = OWN_MCP[ident][0]
        with _parent(component / filename) as (fd, name):
            entry = _existing(fd, name)
        if entry is None:
            return False
        current = entry[0]
        result = apply_file(component, filename, current, snapshot.files.get(filename), mode=0o600, dry_run=True)
        return not result.changed
    except (ManagedFileError, ReceiptError, OSError, RuntimeError, ValueError) as exc:
        raise OwnMcpError("own MCP source copy is missing, modified, or unowned") from exc
