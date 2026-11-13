"""Host detection, bundled source discovery, and private path validation.

Host detection and OS-specific path representation need no filesystem access;
source discovery prefers installed package data; validation covers user-owned
configuration and private Workkit data directories."""

import hashlib
import importlib.metadata
import json
import os
import platform
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from urllib.request import url2pathname

PlatformName = Literal["linux", "wsl", "windows"]
Architecture = Literal["x86_64", "arm64"]


@dataclass(frozen=True)
class Target:
    platform: PlatformName
    architecture: Architecture


def detect_platform(override: str = "auto") -> Target:
    system = platform.system().lower()
    if system == "linux":
        host = "wsl" if "microsoft" in platform.release().lower() else "linux"
    elif system == "windows":
        host = "windows"
    else:
        raise ValueError(f"unsupported operating system: {system!r}")

    machine = platform.machine().lower()
    architectures = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}
    if machine not in architectures:
        raise ValueError(f"unsupported architecture: {machine!r}")
    if override not in ("auto", "linux", "wsl", "windows"):
        raise ValueError(f"unsupported platform override: {override!r}")
    return Target(host if override == "auto" else override, architectures[machine])


_DATA_FILES = (
    "packages.json",
    "setup/plugins/x-custom-providers/index.ts",
    "setup/plugins/x-custom-providers/models.ts",
    "setup/plugins/x-custom-providers/runtime.ts",
    "setup/plugins/x-custom-providers/package.json",
    "setup/plugins/x-custom-providers/providers.json",
    "setup/mcp/4pda.py",
    "setup/mcp/genius.py",
    "setup/instructions/common.md",
    "setup/instructions/linux.md",
    "setup/instructions/wsl.md",
    "setup/instructions/windows.md",
    "setup/instructions/skills.md",
    "setup/instructions/components/cbm.md",
    "setup/instructions/components/engram.md",
    "setup/instructions/components/slim.md",
    "setup/instructions/components/ponytail.md",
    "setup/instructions/components/semgrep.md",
    "setup/instructions/components/markitdown.md",
    "setup/instructions/components/taskmd.md",
    "setup/instructions/slim/orchestrator_append.md",
    "setup/skills/x-playwright-cli/SKILL.md",
    "setup/skills/x-bro/SKILL.md",
    "setup/skills/x-bro/NOTICE.md",
    "setup/skills/x-diagnosing-bugs/SKILL.md",
    "setup/skills/x-diagnosing-bugs/NOTICE.md",
    "setup/skills/x-diagnosing-bugs/LICENSE",
    "setup/skills/x-writing-for-agents/SKILL.md",
    "setup/skills/x-writing-for-agents/SKILL-MECHANICS.md",
    "setup/skills/x-writing-for-agents/NOTICE.md",
    "setup/skills/x-writing-for-agents/LICENSE",
)


def _has_workkit_data(root: Path) -> bool:
    return all((root / path).is_file() for path in _DATA_FILES)


def source_root() -> Path:
    """Prefer installed package data, falling back to a complete source checkout."""
    package = Path(__file__).resolve().parent
    packaged = package / "data"
    if _has_workkit_data(packaged):
        return packaged
    checkout = package.parent
    if _has_workkit_data(checkout):
        return checkout
    return packaged


def checkout_root() -> Path | None:
    """Source checkout recorded by the installer, so `.env` is found from any directory."""
    try:
        raw = importlib.metadata.distribution("opencode-workkit").read_text("direct_url.json")
        url = json.loads(raw)["url"]
        if urlparse(url).scheme != "file":
            return None
        path = Path(url2pathname(urlparse(url).path)).expanduser()
    except (importlib.metadata.PackageNotFoundError, KeyError, TypeError, ValueError, OSError):
        return None
    return path if path.is_absolute() and is_checkout(path) else None


def is_checkout(path) -> bool:
    return (Path(path) / "src/workkit.py").is_file() and (Path(path) / "packages.json").is_file()


def _config_dir() -> Path:
    return Path.home() / ".config" / "opencode"


def _data_root() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    else:
        base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base).expanduser() / "opencode-workkit"


def _state_dir(config_dir, data_root, target):
    config_key = hashlib.sha256(
        str(Path(config_dir).expanduser().resolve(strict=False)).encode("utf-8")
    ).hexdigest()
    return data_root / "state" / config_key / "targets" / f"{target.platform}-{target.architecture}"


def _path(value, label):
    try:
        raw = os.fspath(value)
        path = Path(raw).expanduser()
        if (not isinstance(raw, str) or "\0" in raw or not path.is_absolute()
                or ".." in path.parts or path == Path(path.anchor)):
            raise ValueError
        return path
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ValueError(f"{label} must be an absolute native directory path without traversal") from None


def _check_directory(path, label, *, allow_missing=False):
    if os.name == "nt":
        from storage import validate_windows_directory
        try:
            validate_windows_directory(path)
            return True
        except FileNotFoundError:
            if allow_missing:
                return False
            raise ValueError(f"{label} must be an existing private non-reparse directory") from None
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                if allow_missing:
                    return False
                raise
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError
        if path.resolve(strict=True) != path:
            raise ValueError
        return True
    except (OSError, RuntimeError, ValueError):
        raise ValueError(f"{label} must be an existing non-symlink directory") from None


def _create_private_directory(path):
    if os.name == "nt":
        from storage import validate_windows_directory
        validate_windows_directory(path, create=True)
        return
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current /= part
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError
        if path.resolve(strict=True) != path:
            raise ValueError
    except (OSError, RuntimeError, ValueError):
        raise ValueError("cannot safely create the private data root") from None


def validate_target_paths(config_dir, data_root, *, allow_missing_data=False, create_data_root=False,
                          allow_missing_config=False, create_config_dir=False):
    """Validate separate user config and private data directories, without a fixed root."""
    if os.name not in ("posix", "nt") or (os.name == "posix" and not hasattr(os, "geteuid")):
        raise ValueError("safe user config/data path validation is currently POSIX-only")
    if any(type(option) is not bool for option in (
            allow_missing_data, create_data_root, allow_missing_config, create_config_dir)):
        raise ValueError("directory validation options must be booleans")
    if allow_missing_data and create_data_root:
        raise ValueError("cannot both allow a missing data root and create it")
    if allow_missing_config and create_config_dir:
        raise ValueError("cannot both allow a missing config target and create it")
    config = _path(config_dir, "config target")
    data = _path(data_root, "data root")
    if config.is_relative_to(data) or data.is_relative_to(config):
        raise ValueError("config target and data root must be separate directories")
    if create_config_dir:
        _create_private_directory(config)
    if _check_directory(config, "config target", allow_missing=allow_missing_config):
        info = config.lstat()
        if os.name == "posix" and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022):
            raise ValueError("config target must be owned by the current user and not group/world-writable")

    if create_data_root:
        _create_private_directory(data)
    elif not _check_directory(data, "data root", allow_missing=allow_missing_data):
        return config, data
    info = data.lstat()
    if os.name == "posix" and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("data root must be owned by the current user with mode 0700")
    return config, data
