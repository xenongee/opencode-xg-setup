"""Import literal dotenv values after sync; never change the runner's environment.

Targets are preflighted together and published individually under a shared lock.
This is not a multi-file transaction: a failed publication is safe to retry.
Unsupported security metadata is rejected rather than silently discarded.
"""

import os
import re
import shlex
import stat
from contextlib import contextmanager
from pathlib import Path

from storage import (
    _existing, _open_lock_file, _parent, _same_file, _temporary, writer_lock,
)

_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_ASSIGNMENT = re.compile(r"(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=")
_START = "# >>> opencode-workkit env >>>"
_END = "# <<< opencode-workkit env <<<"
_SOURCE = '[ -f "$HOME/.config/opencode/workkit.env" ] && . "$HOME/.config/opencode/workkit.env"'
_BACKEND = Path("/etc/openchamber-backend.env")


class EnvironmentError(ValueError):
    """Unsafe source/target or an incomplete import; contains no source values."""


def _absolute(path):
    path = Path(path).expanduser().absolute()
    if ".." in path.parts or not path.name:
        raise EnvironmentError("unsafe environment path")
    return path


def _read(path, *, owner=True, private=False):
    with _parent(path) as (fd, name):
        existing = _existing(fd, name)
    if existing is not None and os.name == "posix":
        info = existing[1]
        if ((owner and info.st_uid != os.geteuid())
                or (private and stat.S_IMODE(info.st_mode) & ~0o600)):
            raise EnvironmentError("unsafe environment file permissions")
    return existing


def _source(path):
    existing = _read(path, private=True)
    if existing is None:
        raise EnvironmentError("environment source is missing")
    return existing[0]


def discover_env_file(explicit=None, *, cwd=None, checkout=None):
    """Choose one source: explicit --env-file or the verified Workkit checkout .env."""
    try:
        if explicit is not None:
            path = _absolute(Path(cwd or Path.cwd()) / Path(explicit).expanduser())
            _source(path)
            return path
        if checkout is not None:
            root = _absolute(checkout)
            if (root / "src/workkit.py").is_file() and (root / "packages.json").is_file():
                path = root / ".env"
                try:
                    path.lstat()
                except FileNotFoundError:
                    return None
                _source(path)
                return path
        return None
    except (OSError, TypeError, ValueError, RuntimeError):
        raise EnvironmentError("unsafe or inaccessible environment source") from None


def _parse(content):
    text = content.decode("utf-8")
    text = text.replace("\r\n", "\n")
    if "\0" in text or "\r" in text:
        raise EnvironmentError("invalid environment source")
    values = {}
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not _KEY.fullmatch(key):
            continue
        if value.startswith(("'", '"')) or value.endswith(("'", '"')):
            if len(value) < 2 or value[0] != value[-1] or value[0] not in "\"'":
                raise EnvironmentError("invalid environment quoting")
            value = value[1:-1]
        if value:
            values[key] = value
    return values


def _quoted(value, *, environment_d=False):
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    value = value.replace("$", "$$" if environment_d else "\\$")
    if not environment_d:
        value = value.replace("`", "\\`")
    return '"' + value + '"'


def _lines(text):
    # Only LF ends a physical line; other Unicode separators can be literal values.
    parts = text.split("\n")
    return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _upsert(content, values, kind):
    text = content.decode("utf-8", errors="surrogateescape")
    newline = "\r\n" if "\r\n" in text else "\n"
    replacements = {}
    for key, value in values.items():
        replacements[key] = ("export " + key + "=" + shlex.quote(value) if kind == "shell"
                             else key + "=" + _quoted(value, environment_d=kind == "environment.d"))
    lines, seen = [], set()
    for line in _lines(text):
        match = _ASSIGNMENT.match(line.lstrip())
        key = match[1] if match else None
        # Reject every multiline assignment before an interior line can look like a key.
        if match:
            rhs = line.lstrip()[match.end():].rstrip("\r\n")
            try:
                shlex.split(rhs, comments=False)
            except ValueError:
                raise EnvironmentError("unsupported multi-line environment target") from None
        if key not in replacements:
            lines.append(line)
            continue
        if key not in seen:
            lines.append(replacements[key] + newline)
            seen.add(key)
    for key in replacements:
        if key in seen:
            continue
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += newline
        lines.append(replacements[key] + newline)
    return "".join(lines).encode("utf-8", errors="surrogateescape")


def _profile(content):
    text = content.decode("utf-8", errors="surrogateescape")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = _lines(text)
    result, found, index = [], False, 0
    block = newline.join((_START, _SOURCE, _END)) + newline
    while index < len(lines):
        line = lines[index].rstrip("\r\n")
        if line == _START:
            if (index + 2 >= len(lines) or lines[index + 1].rstrip("\r\n") != _SOURCE
                    or lines[index + 2].rstrip("\r\n") != _END):
                raise EnvironmentError("custom or malformed environment profile block")
            if not found:
                result.append(block)
                found = True
            index += 3
            continue
        if "opencode-workkit env" in line or "workkit environment >>>" in line or "<<< workkit environment" in line:
            raise EnvironmentError("custom or malformed environment profile block")
        result.append(lines[index])
        index += 1
    if not found:
        if result and not result[-1].endswith("\n"):
            result[-1] += newline
        result.append(block)
    return "".join(result).encode("utf-8", errors="surrogateescape")


def _directory(path, home, *, create=False):
    """Validate all ancestors without following links; create only private missing dirs."""
    if os.name == "nt":
        from storage import validate_windows_directory
        try:
            validate_windows_directory(path, create=create)
        except FileNotFoundError:
            return False
        return True
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current /= part
            try:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    return False
                os.mkdir(part, mode=0o700, dir_fd=fd)
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            info = os.fstat(fd)
            if current.is_relative_to(home) and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022):
                raise EnvironmentError("unsafe environment directory")
            if current.is_relative_to(home) and os.listxattr(fd):
                raise EnvironmentError("unsupported environment directory security metadata")
        return True
    finally:
        os.close(fd)


@contextmanager
def _shared_lock(home):
    directory = home / ".config/opencode"
    _directory(directory, home, create=True)
    path = directory / ".workkit-environment.lock"
    if os.name == "nt":
        with writer_lock(directory, path):
            yield
        return
    import fcntl
    with _parent(path) as (fd, name):
        lock = _open_lock_file(fd, name)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with _parent(path) as (current, current_name):
                if (not _same_file(os.fstat(fd), os.fstat(current))
                        or not _same_file(os.fstat(lock), os.stat(current_name, dir_fd=current, follow_symlinks=False))):
                    raise EnvironmentError("environment lock changed")
            yield
        finally:
            os.close(lock)


def _unchanged(previous, current):
    if previous is None or current is None:
        return previous is current
    def identity(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
                info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return previous[0] == current[0] and identity(previous[1]) == identity(current[1])


def _publish(path, previous, content, mode):
    with _parent(path) as (fd, name):
        current = _existing(fd, name)
        if not _unchanged(previous, current):
            raise EnvironmentError("environment target changed before publication")
        temp = _temporary(fd, content, 0o600)
        try:
            if previous is not None:
                info = previous[1]
                handle = os.open(temp, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    temporary_info = os.fstat(handle)
                    if (temporary_info.st_uid, temporary_info.st_gid) != (info.st_uid, info.st_gid):
                        os.fchown(handle, info.st_uid, info.st_gid)
                    os.fchmod(handle, mode)
                    os.fsync(handle)
                finally:
                    os.close(handle)
            if not _unchanged(previous, _existing(fd, name)):
                raise EnvironmentError("environment target changed before publication")
            with _parent(path) as (current_fd, _):
                if not _same_file(os.fstat(fd), os.fstat(current_fd)):
                    raise EnvironmentError("environment target directory changed")
            if previous is None:
                os.link(temp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            else:
                os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            try:
                os.unlink(temp, dir_fd=fd)
            except FileNotFoundError:
                pass


def _windows(values):
    import ctypes
    from ctypes import wintypes as w
    import winreg
    changed = False
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
        updates = []
        unique = {name.casefold(): (name, value) for name, value in values.items()}
        for name, value in unique.values():
            try:
                previous, kind = winreg.QueryValueEx(key, name)
            except FileNotFoundError:
                previous, kind = None, None
            if previous != value or kind != winreg.REG_SZ:
                updates.append((name, value))
        for name, value in updates:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
            changed = True
    if changed:
        user = ctypes.WinDLL("user32", use_last_error=True)
        send = user.SendMessageTimeoutW
        send.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM, w.UINT, w.UINT, ctypes.POINTER(ctypes.c_size_t)]
        send.restype = w.LPARAM
        message, result = ctypes.create_unicode_buffer("Environment"), ctypes.c_size_t()
        if not send(0xffff, 0x001a, 0, ctypes.cast(message, ctypes.c_void_p).value, 2, 1000, ctypes.byref(result)):
            raise EnvironmentError("environment registry changed; broadcast incomplete")
    return ("HKCU Environment",) if changed else ()


def import_environment(path, *, home=None, backend=_BACKEND):
    """Return names/changed/skipped tuples only; backend=None disables backend access."""
    try:
        home = _absolute(home if home is not None else Path.home())
        values = _parse(_source(_absolute(path)))
        if not values:
            return {"names": (), "changed": (), "skipped": ()}
        if not _directory(home, home):
            raise EnvironmentError("environment home is missing")
        with _shared_lock(home):
            if os.name == "nt":
                return {"names": tuple(values), "changed": _windows(values), "skipped": ()}
            plans, skipped = [], []
            targets = [(home / ".config/opencode/workkit.env", "shell"),
                       (home / ".config/environment.d/opencode-workkit.conf", "environment.d"),
                       (home / ".profile", "profile"), (home / ".bashrc", "profile")]
            if backend is not None:
                targets.append((_absolute(backend), "backend"))
            for target, kind in targets:
                if kind == "backend":
                    with _parent(target) as (fd, name):
                        try:
                            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        except FileNotFoundError:
                            skipped.append("backend")
                            continue
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                            raise EnvironmentError("unsafe environment backend target")
                    if not (os.access(target, os.W_OK, effective_ids=True)
                            and os.access(target.parent, os.W_OK, effective_ids=True)):
                        skipped.append("backend")
                        continue
                parent_exists = (_directory(target.parent, home) if kind != "backend"
                                 else target.parent.is_dir())
                previous = (_read(target, owner=kind != "backend", private=kind in ("shell", "environment.d"))
                            if parent_exists else None)
                if (kind in ("shell", "environment.d") and previous is not None
                        and stat.S_IMODE(previous[1].st_mode) != 0o600):
                    raise EnvironmentError("managed environment target must have mode 0600")
                if kind in ("profile", "backend") and previous is None:
                    skipped.append(kind if kind == "backend" else target.name)
                    continue
                before = previous[0] if previous else b""
                content = _profile(before) if kind == "profile" else _upsert(before, values, kind)
                mode = stat.S_IMODE(previous[1].st_mode) if previous else 0o600
                if content != before:
                    plans.append((target, previous, content, mode, kind))
            for target, previous, content, mode, kind in plans:
                if kind != "backend":
                    _directory(target.parent, home, create=True)
                if not os.access(target.parent, os.W_OK, effective_ids=True):
                    raise EnvironmentError("environment target directory is not writable")
            changed = []
            for target, previous, content, mode, kind in plans:
                _publish(target, previous, content, mode)
                changed.append(kind if kind == "backend" else target.name)
            return {"names": tuple(values), "changed": tuple(changed), "skipped": tuple(skipped)}
    except (OSError, TypeError, ValueError, RuntimeError):
        raise EnvironmentError("environment import blocked or partially applied; inspect targets before retrying") from None
