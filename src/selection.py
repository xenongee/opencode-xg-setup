"""Private local selections, scoped to a caller-chosen backend/config path.

Callers choose a state path outside Git for each target and serialize writers.
The file contains catalog IDs plus the reserved x-4pda/x-genius IDs; this
module neither confirms a selection nor installs anything. Parent directories
must already exist.
"""

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from own_mcp import OWN_MCP_IDS
from storage import (ManagedFileError, ReceiptError, _create_at, _existing, _outside_git,
                     _parent, _temporary, _directory_info, _private_file, _stat_at,
                     _replace_at, _unlink_at, _sync_directory, _WindowsAPI)

_RENAMES = {"own-4pda": "x-4pda", "own-genius": "x-genius", "huggingface-hosted": "huggingface-remote"}

class SelectionError(ValueError):
    """Invalid, unsafe, or externally changed selection state."""


class SelectionConflictError(SelectionError):
    """The state changed after its snapshot was loaded."""


class SelectionPublicationError(SelectionError):
    """The state may have been published; reload before deciding what to do."""


class SelectionUnsupportedError(SelectionError):
    """Safe selection file operations are not supported on this platform."""


@dataclass(frozen=True)
class SelectionSnapshot:
    selected: frozenset[str]
    exists: bool
    _location: tuple[int, int, str] = field(repr=False)
    _identity: tuple | None = field(repr=False)
    needs_migration: bool = False


def _catalog_ids(catalog: list[dict]) -> frozenset[str]:
    if not isinstance(catalog, list):
        raise SelectionError("catalog must be the validated package list")
    ids = set()
    for package in catalog:
        if not isinstance(package, Mapping):
            raise SelectionError("catalog contains an invalid package")
        ident = package.get("id")
        if (not isinstance(ident, str) or not re.fullmatch(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", ident)
                or ident in ids):
            raise SelectionError("catalog contains an invalid or duplicate ID")
        ids.add(ident)
    return frozenset(ids | OWN_MCP_IDS)


def _decode(raw: bytes, catalog_ids: frozenset[str]) -> frozenset[str]:
    try:
        selected = json.loads(raw.decode("utf-8"))
        if not isinstance(selected, list):
            raise SelectionError("selection must be a JSON list")
        result = set()
        for ident in selected:
            if not isinstance(ident, str):
                raise SelectionError("selection contains an unknown or duplicate ID")
            renamed = _RENAMES.get(ident, ident)
            if renamed not in catalog_ids or renamed in result:
                raise SelectionError("selection contains an unknown ID or rename collision")
            result.add(renamed)
        return frozenset(result)
    except (ValueError, UnicodeError, RecursionError, TypeError):
        raise SelectionError("invalid or ambiguous selection") from None


def _encode(selected: set[str] | frozenset[str], catalog_ids: frozenset[str]) -> bytes:
    if (not isinstance(selected, (set, frozenset))
            or any(not isinstance(ident, str) or ident not in catalog_ids for ident in selected)):
        raise SelectionError("selection contains an unknown ID")
    return (json.dumps(sorted(selected), ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")


def _snapshot(fd: int, name: str, catalog_ids: frozenset[str]) -> SelectionSnapshot:
    if name == ".git":
        raise SelectionError("selection cannot be Git metadata")
    directory = _directory_info(fd)
    location = (directory.st_dev, directory.st_ino, name)
    entry = _existing(fd, name)
    if entry is None:
        return SelectionSnapshot(frozenset(), False, location, None)
    raw, info = entry
    if not _private_file(info):
        raise SelectionError("selection must be owned by the current user with mode 0600")
    selected = _decode(raw, catalog_ids)
    try:
        raw_ids = json.loads(raw.decode("utf-8"))
        needs_migration = any(ident in _RENAMES for ident in raw_ids)
    except (ValueError, UnicodeError, TypeError):
        needs_migration = False
    identity = (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
                info.st_size, info.st_mtime_ns, info.st_ctime_ns, hashlib.sha256(raw).digest())
    return SelectionSnapshot(selected, True, location, identity, needs_migration)


def _require_posix() -> None:
    if os.name == "nt":
        try:
            _WindowsAPI()
        except ManagedFileError as exc:
            raise SelectionUnsupportedError(str(exc)) from exc
        return
    if (os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY")
            or os.open not in os.supports_dir_fd):
        raise SelectionUnsupportedError("safe selection file operations require POSIX no-follow support")


def _path(path: str | Path) -> Path:
    if not isinstance(path, (str, Path)):
        raise SelectionError("selection path must be a string or Path")
    return Path(path)


def load_selection(path: str | Path, catalog: list[dict]) -> SelectionSnapshot:
    """Load selected catalog/own IDs; `exists` distinguishes missing from []."""
    _require_posix()
    catalog_ids = _catalog_ids(catalog)
    try:
        with _parent(_path(path)) as (fd, name):
            _outside_git(fd)
            return _snapshot(fd, name, catalog_ids)
    except SelectionError:
        raise
    except (ManagedFileError, ReceiptError, OSError) as exc:
        raise SelectionError("cannot safely load selection") from exc


def update_selection(
    path: str | Path,
    snapshot: SelectionSnapshot,
    catalog: list[dict],
    updates: Mapping[str, bool],
    *,
    dry_run: bool = False,
) -> SelectionSnapshot:
    """Apply named catalog/own ID toggles, preserving every other selection.

    Callers must serialize all writers. The pre-replace identity check cannot
    prevent an uncooperative writer racing after that check. On publication
    uncertainty, reload and resolve rather than retrying this snapshot.
    """
    _require_posix()
    catalog_ids = _catalog_ids(catalog)
    if (not isinstance(snapshot, SelectionSnapshot) or not isinstance(snapshot.selected, frozenset)
            or snapshot.exists is not (snapshot._identity is not None)
            or any(ident not in catalog_ids for ident in snapshot.selected)):
        raise SelectionError("a valid loaded selection snapshot is required")
    if not isinstance(updates, Mapping) or type(dry_run) is not bool:
        raise SelectionError("updates must map supported IDs to booleans")

    selected = set(snapshot.selected)
    for ident, value in updates.items():
        if ident not in catalog_ids or type(value) is not bool:
            raise SelectionError("updates contain an unknown ID or invalid selection state")
        if value:
            selected.add(ident)
        else:
            selected.discard(ident)
    encoded = _encode(selected, catalog_ids)

    published = False
    replace_started = False
    try:
        with _parent(_path(path)) as (fd, name):
            _outside_git(fd)
            current = _snapshot(fd, name, catalog_ids)
            if current._location != snapshot._location or current._identity != snapshot._identity:
                raise SelectionConflictError("selection changed since it was loaded")
            if current.exists and current.selected == selected and not current.needs_migration:
                return current
            if dry_run:
                return current

            if current._identity is None:
                try:
                    _create_at(fd, name, encoded)
                except FileExistsError:
                    try:
                        appeared = _snapshot(fd, name, catalog_ids)
                    except (ManagedFileError, OSError, SelectionError):
                        raise SelectionPublicationError("selection publication could not be determined") from None
                    if appeared.exists:
                        raise SelectionConflictError("selection appeared during update") from None
                    raise SelectionError("cannot stage selection") from None
                except (ManagedFileError, OSError):
                    try:
                        appeared = _snapshot(fd, name, catalog_ids)
                    except (ManagedFileError, OSError, SelectionError):
                        raise SelectionPublicationError("selection publication could not be determined") from None
                    if appeared.exists:
                        raise SelectionPublicationError("selection publication could not be determined") from None
                    raise SelectionError("cannot stage selection") from None
                published = True
            else:
                temp = _temporary(fd, encoded, 0o600)
                try:
                    info = _stat_at(fd, temp)
                    if (info.st_uid, info.st_gid) != (current._identity[3], current._identity[4]):
                        raise SelectionError("cannot preserve selection ownership")
                    fresh = _snapshot(fd, name, catalog_ids)
                    if fresh._identity != current._identity:
                        raise SelectionConflictError("selection changed during update")
                    replace_started = True
                    try:
                        _replace_at(fd, temp, name)
                    except Exception as exc:
                        raise SelectionPublicationError("selection publication cannot be confirmed") from exc
                    published = True
                finally:
                    try:
                        _unlink_at(fd, temp)
                    except FileNotFoundError:
                        pass

            try:
                _sync_directory(fd)
                result = _snapshot(fd, name, catalog_ids)
                if not result.exists or result.selected != selected:
                    raise SelectionPublicationError("selection changed after publication")
                return result
            except SelectionPublicationError:
                raise
            except (ManagedFileError, OSError, SelectionError):
                raise SelectionPublicationError("selection publication cannot be confirmed") from None
    except SelectionError:
        raise
    except (ManagedFileError, ReceiptError, OSError) as exc:
        if published or replace_started:
            raise SelectionPublicationError("selection publication cannot be confirmed") from exc
        raise SelectionError("cannot safely save selection") from exc
    except Exception as exc:
        if published or replace_started:
            raise SelectionPublicationError("selection publication cannot be confirmed") from exc
        raise
