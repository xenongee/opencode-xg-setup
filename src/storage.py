"""Manage individual files; callers persist receipts and serialize writers.

Parent directories must already exist. POSIX no-follow directory descriptors
prevent traversal through symlinks. Native Windows pins non-reparse ancestors
with Win32 handles denying delete sharing and requires private user-owned ACLs.

Private local ownership receipts; no configuration values are stored here.

The caller must choose a path outside any Git worktree, keep identifiers free of
secrets, and serialize all writers, including config/resource writers. Load a
snapshot, apply the target changes using its hashes, then save the returned hashes.
Each receipt is scoped by the caller to one config target and one resource root.
The target and receipt are NOT one transaction: a crash or failed receipt save
after target changes leaves a stale/missing receipt. Stop and resolve that conflict
explicitly; never infer ownership, adopt existing data, or blindly retry an update.
The pre-replace check cannot prevent an uncooperative writer racing afterwards.
Windows supports local non-reparse paths only, not UNC/device paths or Unix mode
preservation. Alternate streams and special file attributes are refused. New files
inherit the verified parent ACL; directory flush durability is not promised.
Missing native security APIs or unfamiliar ACLs fail closed.

Serialize local Workkit writers with persistent flock/msvcrt advisory locks.

Callers provide an existing private data directory outside Git and use the same
dedicated lock path beneath it for every writer. The lock file is stable and is
never replaced or removed. Uncooperative writers are not serialized; a dry-run
may observe concurrent changes."""

import errno
import hashlib
import json
import os
import secrets
import stat
import ctypes
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - native Windows
    _fcntl = None


class ManagedFileError(ValueError):
    """Invalid target, conflicting ownership, or unsafe file operation."""


@dataclass(frozen=True)
class ManagedFileResult:
    changed: bool
    receipt: str | None


def _reparse(info) -> bool:
    return bool(getattr(info, "st_reparse_tag", 0) or getattr(info, "st_file_attributes", 0) & 0x400)


def _nt_component(name):
    stem = name.split(".", 1)[0].upper()
    if (not name or name in (".", "..") or name.endswith((".", " "))
            or any(ord(c) < 32 or c in '<>:"/\\|?*' for c in name)
            or stem in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
            or stem in {f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in "123456789¹²³"}):
        raise ManagedFileError("unsafe native Windows path component")


class _WindowsAPI:
    """Native local-filesystem adapter; directory handles pin every ancestor.

    No FILE_SHARE_DELETE: rename/junction substitution is blocked while pinned.
    Files inherit the verified private parent ACL; POSIX modes are not NT ACLs.
    Network/device paths are deliberately unsupported. No shell is involved.
    """

    def __init__(self):
        try:
            import msvcrt
            from ctypes import wintypes as w
            self.crt = msvcrt
            self.k = ctypes.WinDLL("kernel32", use_last_error=True)
            self.a = ctypes.WinDLL("advapi32", use_last_error=True)
            self.k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
            self.k.CreateFileW.restype = w.HANDLE
            self.k.CloseHandle.argtypes = [w.HANDLE]
            self.k.GetFinalPathNameByHandleW.argtypes = [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD]
            self.k.GetFileInformationByHandleEx.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
            self.k.SetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
            self.k.FindFirstStreamW.argtypes = [w.LPCWSTR, ctypes.c_int, w.LPVOID, w.DWORD]
            self.k.FindFirstStreamW.restype = w.HANDLE
            self.k.FindNextStreamW.argtypes = [w.HANDLE, w.LPVOID]
            self.k.FindClose.argtypes = [w.HANDLE]
            self.a.GetSecurityInfo.argtypes = [w.HANDLE, ctypes.c_int, w.DWORD, w.LPVOID, w.LPVOID, w.LPVOID, w.LPVOID, w.LPVOID]
            self.a.GetSecurityDescriptorControl.argtypes = [w.LPVOID, w.LPVOID, w.LPVOID]
            self.a.ConvertSidToStringSidW.argtypes = [w.LPVOID, w.LPVOID]
            self.a.GetAce.argtypes = [w.LPVOID, w.DWORD, w.LPVOID]
            self.a.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, w.LPVOID]
            self.a.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, w.LPVOID]
            self.k.GetCurrentProcess.restype = w.HANDLE
            self.k.LocalFree.argtypes = [w.LPVOID]
            self.w = w
        except (ImportError, AttributeError, OSError):
            raise ManagedFileError("native Windows security APIs are unavailable") from None

    def close(self, handle):
        self.k.CloseHandle(handle)

    def open(self, path, *, directory=False, write=False, create=False, lock=False):
        handle = self.k.CreateFileW(str(path), 0x80000000 | (0x40000000 if write else 0) | (0x10000 if create else 0),
                                    3 if directory or lock else 1, None, 1 if create else 3,
                                    0x00200000 | (0x02000000 if directory else 0), None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            # FileAttributeTagInfo, queried on the opened object, not its pathname.
            attrs = (self.w.DWORD * 2)()
            if not self.k.GetFileInformationByHandleEx(handle, 9, attrs, ctypes.sizeof(attrs)):
                raise ctypes.WinError(ctypes.get_last_error())
            if attrs[0] & 0x400 or attrs[1] or bool(attrs[0] & 0x10) != directory:
                raise ManagedFileError("reparse points and non-regular targets are forbidden")
            if not directory and attrs[0] & ~(0x20 | 0x80):
                raise ManagedFileError("file has unsupported Windows attributes")
            final = ctypes.create_unicode_buffer(32768)
            size = self.k.GetFinalPathNameByHandleW(handle, final, len(final), 0)
            if not size or size >= len(final) or os.path.normcase(final.value.removeprefix("\\\\?\\")) != os.path.normcase(str(path)):
                raise ManagedFileError("native handle path does not match target")
            return handle
        except BaseException:
            if create:
                self.discard_created(handle)
            else:
                self.close(handle)
            raise

    def discard_created(self, handle):
        """Delete the exact newly created object, never its potentially changed name."""
        disposition = ctypes.c_ubyte(1)  # FILE_DISPOSITION_INFO.DeleteFile (BOOLEAN).
        try:
            if not self.k.SetFileInformationByHandle(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
                raise ManagedFileError("created object cleanup uncertain; inspect before retrying")
        finally:
            if not self.k.CloseHandle(handle):
                raise ManagedFileError("created object cleanup uncertain; inspect before retrying")

    def streams(self, path):
        class StreamData(ctypes.Structure):
            _fields_ = [("size", ctypes.c_longlong), ("name", ctypes.c_wchar * 296)]
        data = StreamData()
        search = self.k.FindFirstStreamW(str(path), 0, ctypes.byref(data), 0)
        if search == ctypes.c_void_p(-1).value:
            if ctypes.get_last_error() == 38:  # ERROR_HANDLE_EOF: no streams.
                return
            raise ManagedFileError("cannot inspect Windows alternate streams")
        try:
            while True:
                if data.name != "::$DATA":
                    raise ManagedFileError("target has unsupported alternate data streams")
                if not self.k.FindNextStreamW(search, ctypes.byref(data)):
                    if ctypes.get_last_error() != 38:
                        raise ManagedFileError("cannot inspect Windows alternate streams")
                    break
        finally:
            self.k.FindClose(search)

    def private(self, handle):
        """Validate privacy; return pointer-independent owner/DACL/control metadata."""
        w = self.w
        def sid_text(sid):
            text = w.LPWSTR()
            if not self.a.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                raise ManagedFileError("cannot inspect Windows SID")
            try:
                return text.value
            finally:
                self.k.LocalFree(text)
        token = w.HANDLE()
        if not self.a.OpenProcessToken(self.k.GetCurrentProcess(), 8, ctypes.byref(token)):
            raise ManagedFileError("cannot inspect current Windows identity")
        try:
            size = w.DWORD()
            self.a.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            if not self.a.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
                raise ManagedFileError("cannot inspect current Windows identity")
            user = sid_text(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0])
        finally:
            self.close(token)
        owner, acl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        if self.a.GetSecurityInfo(handle, 1, 5, ctypes.byref(owner), None, ctypes.byref(acl), None, ctypes.byref(descriptor)):
            raise ManagedFileError("cannot inspect Windows ACL")
        try:
            owner_sid = sid_text(owner)
            if not acl.value or owner_sid != user:
                raise ManagedFileError("Windows target must have a private user-owned ACL")
            # ACL header: revision, reserved, size, ACE count, reserved.
            count = ctypes.cast(acl, ctypes.POINTER(ctypes.c_ushort))[2]
            for index in range(count):
                ace = ctypes.c_void_p()
                if not self.a.GetAce(acl, index, ctypes.byref(ace)):
                    raise ManagedFileError("cannot inspect Windows ACE")
                kind = ctypes.c_ubyte.from_address(ace.value).value
                flags = ctypes.c_ubyte.from_address(ace.value + 1).value
                if flags & 8:  # INHERIT_ONLY_ACE does not apply to this object.
                    continue
                if kind == 1:  # A deny ACE cannot grant foreign access.
                    continue
                if kind != 0 or sid_text(ace.value + 8) not in {user, "S-1-5-18", "S-1-5-32-544"}:
                    raise ManagedFileError("Windows ACL grants access outside user/SYSTEM/Administrators")
            control, revision = ctypes.c_ushort(), w.DWORD()
            if not self.a.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
                raise ManagedFileError("cannot inspect Windows descriptor control")
            acl_size = ctypes.cast(acl, ctypes.POINTER(ctypes.c_ushort))[1]
            if acl_size < 8:
                raise ManagedFileError("invalid Windows ACL size")
            # Compare file with file, including deny ACEs and inheritance protection.
            return owner_sid, ctypes.string_at(acl, acl_size), control.value & 0x150d
        finally:
            self.k.LocalFree(descriptor)

    def fd(self, handle, *, write=False):
        try:
            return self.crt.open_osfhandle(handle, (os.O_RDWR if write else os.O_RDONLY) | os.O_BINARY)
        except BaseException:
            self.close(handle)
            raise


class _NTDirectory:
    def __init__(self, path, api):
        self.path, self.api = path, api


@contextmanager
def _nt_directory(path, *, create=False, private_root=None):
    api = _WindowsAPI()
    path = path.absolute()
    if ".." in path.parts or str(path).startswith("\\\\"):
        raise ManagedFileError("unsafe or unsupported native Windows path")
    handles = []
    current = Path(path.anchor)
    try:
        for part in (None, *path.parts[1:]):
            if part is not None:
                _nt_component(part)
                current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                if not create:
                    raise
                _outside_git(_NTDirectory(current.parent, api))
                current.mkdir(mode=0o700)
                info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or _reparse(info):
                raise ManagedFileError("Windows directory is a reparse point or not a directory")
            handles.append(api.open(current, directory=True))
            if private_root is not None and current.is_relative_to(private_root):
                api.private(handles[-1])
        yield _NTDirectory(path, api)
    finally:
        for handle in reversed(handles):
            api.close(handle)


def _directory_info(fd):
    return fd.path.lstat() if isinstance(fd, _NTDirectory) else os.fstat(fd)


def _stat_at(fd, name):
    return (fd.path / name).lstat() if isinstance(fd, _NTDirectory) else os.stat(name, dir_fd=fd, follow_symlinks=False)


def _unlink_at(fd, name):
    if isinstance(fd, _NTDirectory):
        os.unlink(fd.path / name)
    else:
        os.unlink(name, dir_fd=fd)


def _replace_at(fd, source, target):
    if isinstance(fd, _NTDirectory):
        handles = []
        try:
            for name in (target, source):
                handles.append(fd.api.open(fd.path / name))
            if fd.api.private(handles[0]) != fd.api.private(handles[1]):
                raise ManagedFileError("cannot preserve existing Windows owner/DACL/control")
        finally:
            for handle in reversed(handles):
                fd.api.close(handle)
        os.replace(fd.path / source, fd.path / target)
    else:
        os.replace(source, target, src_dir_fd=fd, dst_dir_fd=fd)


def _sync_directory(fd):
    if not isinstance(fd, _NTDirectory):
        os.fsync(fd)


def _private_file(info):
    return os.name == "nt" or (stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.geteuid())


def validate_windows_directory(path, *, create=False):
    """Validate a private inherited ACL with pinned, non-reparse ancestors."""
    with _nt_directory(Path(path), create=create) as directory:
        handle = directory.api.open(directory.path, directory=True)
        try:
            directory.api.private(handle)
            _outside_git(directory)
        finally:
            directory.api.close(handle)


@contextmanager
def _nt_writer_lock(data_root, lock_path, *, dry_run=False):
    if type(dry_run) is not bool:
        raise WriterLockError("dry_run must be a boolean")
    root = _absolute_path(data_root, "writer data directory")
    target = _absolute_path(lock_path, "writer lock path")
    try:
        _nt_component(target.name)
    except ManagedFileError as exc:
        raise WriterLockError(str(exc)) from exc
    try:
        relative = target.relative_to(root)
        if not root.name or not relative.parts or target.name == ".git":
            raise ValueError
    except ValueError:
        raise WriterLockError("writer lock must be a file inside the data root") from None
    entered = False
    try:
        with _nt_directory(target.parent, private_root=root) as directory:
            _outside_git(directory)
            if dry_run:
                entered = True
                yield False
                return
            try:
                handle = directory.api.open(target, write=True, create=True, lock=True)
                created = True
            except FileExistsError:
                handle = directory.api.open(target, write=True, lock=True)
                created = False
            try:
                directory.api.private(handle)
                if not created:
                    directory.api.streams(target)
            except BaseException:
                if created:
                    directory.api.discard_created(handle)
                else:
                    directory.api.close(handle)
                raise
            descriptor = directory.api.fd(handle, write=True)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise WriterLockError("writer lock must be a single-link regular file")
                if created:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                try:
                    directory.api.crt.locking(descriptor, directory.api.crt.LK_NBLCK, 1)
                except OSError:
                    raise WriterLockError("another Workkit writer holds the lock") from None
                try:
                    if os.fstat(descriptor).st_size != 1 or os.read(descriptor, 1) != b"\0":
                        raise WriterLockError("writer lock contains foreign data")
                    entered = True
                    yield True
                finally:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    directory.api.crt.locking(descriptor, directory.api.crt.LK_UNLCK, 1)
            finally:
                os.close(descriptor)
    except (ManagedFileError, ReceiptError, OSError) as exc:
        if entered:
            raise
        raise WriterLockError("cannot safely use native Windows writer lock") from exc


@contextmanager
def _parent(path: Path, *, private=True):
    if os.name == "nt":
        if ".." in path.parts or not path.name:
            raise ManagedFileError("unsafe target path")
        _nt_component(path.name)
        with _nt_directory(path.absolute().parent) as directory:
            if private:
                handle = directory.api.open(directory.path, directory=True)
                try:
                    directory.api.private(handle)
                finally:
                    directory.api.close(handle)
            yield directory, path.name
        return
    if (os.name != "posix" or not hasattr(os, "O_NOFOLLOW")
            or not hasattr(os, "O_DIRECTORY") or os.open not in os.supports_dir_fd):
        raise ManagedFileError("safe file operations are unavailable on this OS")
    if ".." in path.parts or not path.name:
        raise ManagedFileError("unsafe target path")
    absolute = path.absolute()
    fd = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(absolute.anchor, flags)
        for part in absolute.parent.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd, absolute.name
    except OSError:
        raise ManagedFileError("unsafe or inaccessible target path") from None
    finally:
        if fd is not None:
            os.close(fd)


def _temporary(fd: int, content: bytes, mode: int) -> str:
    if isinstance(fd, _NTDirectory):
        name = ".workkit-" + secrets.token_hex(16)
        handle = fd.api.open(fd.path / name, write=True, create=True)
        try:
            fd.api.private(handle)
        except BaseException:
            fd.api.discard_created(handle)
            raise
        try:
            descriptor = fd.api.fd(handle, write=True)
        except BaseException:
            _unlink_at(fd, name)
            raise
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
        except BaseException:
            _unlink_at(fd, name)
            raise
        return name
    name = ".workkit-" + secrets.token_hex(16)
    handle = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(handle, "wb") as file:
            os.fchmod(file.fileno(), mode)
            if hasattr(os, "listxattr") and os.listxattr(file.fileno()):
                raise ManagedFileError("temporary file has unsupported extended attributes")
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
            if stat.S_IMODE(os.fstat(file.fileno()).st_mode) != mode:
                raise ManagedFileError("cannot preserve file mode")
    except BaseException:
        os.unlink(name, dir_fd=fd)
        raise
    return name


def _create_at(fd: int, name: str, content: bytes, mode: int = 0o600) -> None:
    temp = _temporary(fd, content, mode)
    try:
        # link() is an atomic no-clobber publish, unlike replace() or rename().
        if isinstance(fd, _NTDirectory):
            # Windows rename is atomic and refuses an existing destination.
            os.rename(fd.path / temp, fd.path / name)
        else:
            os.link(temp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
    finally:
        try:
            _unlink_at(fd, temp)
        except FileNotFoundError:
            pass


def _existing(fd: int, name: str, *, private=True) -> tuple[bytes, os.stat_result] | None:
    if isinstance(fd, _NTDirectory):
        try:
            before = (fd.path / name).lstat()
        except FileNotFoundError:
            return None
        if _reparse(before) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ManagedFileError("target must be a single-link non-reparse regular file")
        handle = fd.api.open(fd.path / name)
        try:
            if private:
                fd.api.private(handle)
                fd.api.streams(fd.path / name)
        except BaseException:
            fd.api.close(handle)
            raise
        with os.fdopen(fd.api.fd(handle), "rb") as file:
            info = os.fstat(file.fileno())
            if not _same_file(before, info) or info.st_nlink != 1:
                raise ManagedFileError("target changed while opening")
            data = file.read()
            after = os.fstat(file.fileno())
            if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ManagedFileError("target changed while reading")
            return data, info
    try:
        handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None
    with os.fdopen(handle, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ManagedFileError("target must be a regular file with one link")
        if stat.S_IMODE(info.st_mode) & 0o7022:
            raise ManagedFileError("target has unsafe file permissions")
        if hasattr(os, "listxattr") and os.listxattr(file.fileno()):
            raise ManagedFileError("target has unsupported extended attributes")
        data = file.read()
        after = os.fstat(file.fileno())
        if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise ManagedFileError("target changed while reading")
        return data, info


def apply_file(
    root: str | Path,
    relative: str,
    content: bytes,
    receipt: str | None,
    *,
    mode: int = 0o600,
    dry_run: bool = False,
) -> ManagedFileResult:
    """Create an absent file or replace an unchanged owned file.

    A receipt is the SHA-256 of the exact previously written bytes; the caller
    persists it only after success. Dry-run returns the original receipt.
    Existing modes are preserved; mode applies only to newly created files.
    """
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or PureWindowsPath(relative).is_absolute() or PureWindowsPath(relative).drive
            or Path(relative).is_absolute() or any(p in ("", ".", "..") for p in relative.split("/"))):
        raise ManagedFileError("target must be a safe relative path")
    if not isinstance(content, bytes):
        raise ManagedFileError("file content must be bytes")
    if (not isinstance(mode, int) or isinstance(mode, bool) or mode < 0
            or mode & ~0o777 or mode & 0o022):
        raise ManagedFileError("unsafe file mode")
    if receipt is not None and (not isinstance(receipt, str) or len(receipt) != 64
                                or any(c not in "0123456789abcdef" for c in receipt)):
        raise ManagedFileError("invalid receipt")
    with _parent(Path(root) / relative) as (fd, name):
        current = _existing(fd, name)
        if current is None:
            if receipt is not None:
                raise ManagedFileError("previously managed file is missing")
            if dry_run:
                return ManagedFileResult(True, None)
            _create_at(fd, name, content, mode)
        else:
            previous, info = current
            if receipt is None or hashlib.sha256(previous).hexdigest() != receipt:
                raise ManagedFileError("existing or modified file conflicts with receipt")
            if previous == content or dry_run:
                return ManagedFileResult(previous != content, receipt)
            temp = _temporary(fd, content, stat.S_IMODE(info.st_mode))
            try:
                temp_info = _stat_at(fd, temp)
                if (temp_info.st_uid, temp_info.st_gid) != (info.st_uid, info.st_gid):
                    raise ManagedFileError("cannot preserve file ownership")
                fresh = _existing(fd, name)
                if (fresh is None or fresh[0] != previous or fresh[1].st_ino != info.st_ino
                        or fresh[1].st_dev != info.st_dev or fresh[1].st_mode != info.st_mode
                        or fresh[1].st_uid != info.st_uid or fresh[1].st_gid != info.st_gid):
                    raise ManagedFileError("file changed during update")
                _replace_at(fd, temp, name)
            finally:
                try:
                    _unlink_at(fd, temp)
                except FileNotFoundError:
                    pass
    return ManagedFileResult(True, hashlib.sha256(content).hexdigest())


def remove_file(root, relative, receipt, *, dry_run=False) -> bool:
    """Remove only an exact, unchanged receipted file; missing is a conflict."""
    if not _valid_file(relative) or not _valid_hash(receipt) or type(dry_run) is not bool:
        raise ManagedFileError("a safe relative path and ownership receipt are required")
    with _parent(Path(root) / relative) as (fd, name):
        current = _existing(fd, name)
        if current is None or hashlib.sha256(current[0]).hexdigest() != receipt:
            raise ManagedFileError("missing, unknown or modified file conflicts with receipt")
        if not dry_run:
            fresh = _existing(fd, name)
            if fresh is None or fresh[0] != current[0] or not _same_file(fresh[1], current[1]):
                raise ManagedFileError("file changed during removal")
            _unlink_at(fd, name)
    return True


class ReceiptError(ValueError):
    """Invalid, unsafe, or externally changed receipt."""


@dataclass(frozen=True)
class ReceiptSnapshot:
    config: dict[tuple[str, ...], str]
    files: dict[str, str]
    plugins: dict[str, str]
    _location: tuple[int, int, str] = field(repr=False)
    _identity: tuple | None = field(repr=False)


def _unique(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReceiptError("duplicate receipt field")
        result[key] = value
    return result


def _valid_hash(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def _valid_part(value: object) -> bool:
    if not isinstance(value, str) or not value or "\0" in value:
        return False
    try:
        value.encode("utf-8")
        return True
    except UnicodeError:
        return False


def _valid_file(value: object) -> bool:
    if not _valid_part(value) or "\\" in value:
        return False
    return (not Path(value).is_absolute() and not PureWindowsPath(value).drive
            and not any(p in ("", ".", "..") for p in value.split("/")))


def _encode(
    config: Mapping[tuple[str, ...], str],
    files: Mapping[str, str],
    plugins: Mapping[str, str] | None = None,
) -> bytes:
    if not isinstance(config, Mapping) or not isinstance(files, Mapping):
        raise ReceiptError("receipt data must be mappings")
    if plugins is None:
        plugins = {}
    if not isinstance(plugins, Mapping):
        raise ReceiptError("receipt data must be mappings")
    for key, digest in config.items():
        if (not isinstance(key, tuple) or not key or not all(_valid_part(p) for p in key)
                or not _valid_hash(digest)):
            raise ReceiptError("invalid config receipt entry")
    for key, digest in files.items():
        if not _valid_file(key) or not _valid_hash(digest):
            raise ReceiptError("invalid file receipt entry")
    for key, digest in plugins.items():
        if not _valid_part(key) or not _valid_hash(digest):
            raise ReceiptError("invalid plugin receipt entry")
    data = {"version": 2, "config": [[list(k), v] for k, v in sorted(config.items())],
            "files": [[k, v] for k, v in sorted(files.items())], "plugins": [[k, v] for k, v in sorted(plugins.items())]}
    return (json.dumps(data, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")


def _decode(raw: bytes) -> tuple[dict[tuple[str, ...], str], dict[str, str], dict[str, str]]:
    try:
        def reject_constant(value):
            raise ReceiptError("invalid receipt constant")

        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=reject_constant)
        if (not isinstance(data, dict) or type(data.get("version")) is not int
                or data["version"] not in (1, 2)
                or set(data) != ({"version", "config", "files"} if data["version"] == 1
                                 else {"version", "config", "files", "plugins"})
                or not isinstance(data["config"], list) or not isinstance(data["files"], list)
                or (data["version"] == 2 and not isinstance(data["plugins"], list))):
            raise ReceiptError("invalid receipt structure")
        config = {}
        files = {}
        plugins = {}
        for item in data["config"]:
            if (not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], list)
                    or not item[0] or not all(_valid_part(p) for p in item[0])
                    or not _valid_hash(item[1]) or tuple(item[0]) in config):
                raise ReceiptError("invalid or duplicate config entry")
            config[tuple(item[0])] = item[1]
        for item in data["files"]:
            if (not isinstance(item, list) or len(item) != 2 or not _valid_file(item[0])
                    or not _valid_hash(item[1]) or item[0] in files):
                raise ReceiptError("invalid or duplicate file entry")
            files[item[0]] = item[1]
        for item in data.get("plugins", []):
            if (not isinstance(item, list) or len(item) != 2 or not _valid_part(item[0])
                    or not _valid_hash(item[1]) or item[0] in plugins):
                raise ReceiptError("invalid or duplicate plugin entry")
            plugins[item[0]] = item[1]
        return config, files, plugins
    except (ValueError, UnicodeError, RecursionError, TypeError):
        raise ReceiptError("invalid or ambiguous receipt") from None


def _outside_git(fd: int) -> None:
    if isinstance(fd, _NTDirectory):
        for current in (fd.path, *fd.path.parents):
            try:
                (current / ".git").lstat()
            except FileNotFoundError:
                pass
            else:
                raise ReceiptError("managed state must be outside a Git worktree")
            try:
                head, objects, refs = [(current / name).lstat() for name in ("HEAD", "objects", "refs")]
            except FileNotFoundError:
                continue
            if stat.S_ISREG(head.st_mode) and stat.S_ISDIR(objects.st_mode) and stat.S_ISDIR(refs.st_mode):
                raise ReceiptError("managed state must be outside a Git repository")
        return
    current = os.dup(fd)
    try:
        while True:
            try:
                os.stat(".git", dir_fd=current, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ReceiptError("receipt must be outside a Git worktree")
            try:
                head = os.stat("HEAD", dir_fd=current, follow_symlinks=False)
                objects = os.stat("objects", dir_fd=current, follow_symlinks=False)
                refs = os.stat("refs", dir_fd=current, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISREG(head.st_mode) and stat.S_ISDIR(objects.st_mode) and stat.S_ISDIR(refs.st_mode):
                    raise ReceiptError("receipt must be outside a Git repository")
            parent = os.open("..", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            before, after = os.fstat(current), os.fstat(parent)
            os.close(current)
            current = parent
            if (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino):
                break
    finally:
        os.close(current)


def _snapshot(fd: int, name: str) -> ReceiptSnapshot:
    if name == ".git":
        raise ReceiptError("receipt cannot be Git metadata")
    directory = _directory_info(fd)
    location = (directory.st_dev, directory.st_ino, name)
    entry = _existing(fd, name)
    if entry is None:
        return ReceiptSnapshot({}, {}, {}, location, None)
    raw, info = entry
    if not _private_file(info):
        raise ReceiptError("receipt must be owned by the current user with mode 0600")
    config, files, plugins = _decode(raw)
    identity = (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
                info.st_size, info.st_mtime_ns, info.st_ctime_ns, hashlib.sha256(raw).digest())
    return ReceiptSnapshot(config, files, plugins, location, identity)


def load_receipt(path: str | Path) -> ReceiptSnapshot:
    """Load a local snapshot; a missing receipt yields empty ownership maps."""
    try:
        with _parent(Path(path)) as (fd, name):
            _outside_git(fd)
            return _snapshot(fd, name)
    except (ManagedFileError, OSError):
        raise ReceiptError("cannot safely load receipt") from None


def save_receipt(
    path: str | Path,
    snapshot: ReceiptSnapshot,
    *,
    config: Mapping[tuple[str, ...], str],
    files: Mapping[str, str],
    plugins: Mapping[str, str] | None = None,
    dry_run: bool = False,
) -> ReceiptSnapshot:
    """Conditionally save hashes and return a new snapshot; dry-run writes nothing.

    A stale snapshot, including a newly created/deleted/replaced receipt, fails.
    Callers must serialize writers: the identity check is not an OS-level CAS.
    After an error following publication, reload and resolve rather than assuming
    whether the receipt was committed. Never use prospective dry-run hashes.
    """
    if not isinstance(snapshot, ReceiptSnapshot):
        raise ReceiptError("a loaded receipt snapshot is required")
    next_plugins = snapshot.plugins if plugins is None else plugins
    encoded = _encode(config, files, next_plugins)
    try:
        with _parent(Path(path)) as (fd, name):
            _outside_git(fd)
            current = _snapshot(fd, name)
            if current._location != snapshot._location or current._identity != snapshot._identity:
                raise ReceiptError("receipt changed since it was loaded")
            if (current.config == config and current.files == files and current.plugins == next_plugins
                    and current._identity is not None):
                return current
            if dry_run:
                return current
            if current._identity is None:
                _create_at(fd, name, encoded)
            else:
                temp = _temporary(fd, encoded, 0o600)
                try:
                    info = _stat_at(fd, temp)
                    if (info.st_uid, info.st_gid) != (current._identity[3], current._identity[4]):
                        raise ReceiptError("cannot preserve receipt ownership")
                    fresh = _snapshot(fd, name)
                    if fresh._identity != current._identity:
                        raise ReceiptError("receipt changed during update")
                    _replace_at(fd, temp, name)
                finally:
                    try:
                        _unlink_at(fd, temp)
                    except FileNotFoundError:
                        pass
            _sync_directory(fd)
            result = _snapshot(fd, name)
            if result.config != config or result.files != files or result.plugins != next_plugins:
                raise ReceiptError("receipt changed after publication")
            return result
    except (ManagedFileError, OSError):
        raise ReceiptError("cannot safely save receipt") from None


class WriterLockError(RuntimeError):
    """Invalid lock location, unsafe file, or a concurrent writer."""


class WriterLockUnsupportedError(WriterLockError):
    """The platform does not provide the required safe lock primitives."""


def _require_posix() -> None:
    if os.name != "posix":
        raise WriterLockUnsupportedError("native Windows writer locking is blocked pending task 016")
    if (_fcntl is None or not hasattr(_fcntl, "flock") or not hasattr(os, "O_NOFOLLOW")
            or not hasattr(os, "O_DIRECTORY") or os.open not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd or os.stat not in os.supports_follow_symlinks):
        raise WriterLockUnsupportedError("safe POSIX flock and no-follow operations are unavailable")


def _absolute_path(value: str | os.PathLike[str], label: str) -> Path:
    try:
        raw = os.fspath(value)
        path = Path(raw)
    except (TypeError, ValueError, OSError):
        raise WriterLockError(f"{label} must be an absolute path") from None
    if not isinstance(raw, str) or "\0" in raw or not path.is_absolute() or ".." in path.parts:
        raise WriterLockError(f"{label} must be an absolute path without parent traversal")
    return path


def _private_directory(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise WriterLockError("writer data and lock parent paths must be directories")
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise WriterLockError("writer data and lock parent directories must be owned by you with mode 0700")


def _open_directory_path(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = None
    try:
        fd = os.open(path.anchor, flags)
        for part in path.parts[1:]:
            if part in ("", ".", ".."):
                raise WriterLockError("unsafe directory path")
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except WriterLockError:
        if fd is not None:
            os.close(fd)
        raise
    except OSError:
        if fd is not None:
            os.close(fd)
        raise WriterLockError("cannot safely open writer data directory") from None


def _open_relative_parent(root_fd: int, parts: tuple[str, ...]) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.dup(root_fd)
    except OSError:
        raise WriterLockError("cannot safely open writer lock parent") from None
    try:
        for part in parts:
            if part in ("", ".", ".."):
                raise WriterLockError("unsafe writer lock path")
            next_fd = os.open(part, flags, dir_fd=fd)
            try:
                _private_directory(next_fd)
            except BaseException:
                os.close(next_fd)
                raise
            os.close(fd)
            fd = next_fd
        return fd
    except WriterLockError:
        os.close(fd)
        raise
    except OSError:
        os.close(fd)
        raise WriterLockError("cannot safely open writer lock parent") from None


def _lock_info(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise WriterLockError("writer lock must be a single-link regular file")
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise WriterLockError("writer lock must be owned by you with mode 0600")
    if info.st_size != 0:
        raise WriterLockError("writer lock file must be empty; refusing to touch foreign data")


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _open_lock_file(parent_fd: int, name: str) -> int:
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    fd = None
    keep = False
    try:
        try:
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            try:
                fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
            except FileExistsError:
                before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            else:
                os.fchmod(fd, 0o600)
                _lock_info(os.fstat(fd))
                os.fsync(fd)
                os.fsync(parent_fd)
                keep = True
                return fd

        _lock_info(before)
        fd = os.open(name, flags, dir_fd=parent_fd)
        opened = os.fstat(fd)
        _lock_info(opened)
        if not _same_file(before, opened):
            raise WriterLockError("writer lock path changed while opening")
        keep = True
        return fd
    except WriterLockError:
        raise
    except OSError:
        raise WriterLockError("cannot safely open or create writer lock") from None
    finally:
        if fd is not None and not keep:
            try:
                os.close(fd)
            except OSError:
                pass


def _writer_outside_git(root_fd: int) -> None:
    try:
        _outside_git(root_fd)
    except (OSError, ReceiptError):
        raise WriterLockError("writer data directory must be outside every Git worktree") from None


def _verify_after_lock(
    root_path: Path,
    root_fd: int,
    parent_parts: tuple[str, ...],
    parent_fd: int,
    name: str,
    lock_fd: int,
) -> None:
    current_root = _open_directory_path(root_path)
    current_parent = None
    try:
        root_info = os.fstat(root_fd)
        if not _same_file(root_info, os.fstat(current_root)):
            raise WriterLockError("writer data directory path changed after locking")
        _private_directory(current_root)
        _writer_outside_git(current_root)
        current_parent = _open_relative_parent(current_root, parent_parts)
        _writer_outside_git(current_parent)
        if not _same_file(os.fstat(parent_fd), os.fstat(current_parent)):
            raise WriterLockError("writer lock parent path changed after locking")
        entry = os.stat(name, dir_fd=current_parent, follow_symlinks=False)
        opened = os.fstat(lock_fd)
        _lock_info(opened)
        if not _same_file(entry, opened):
            raise WriterLockError("writer lock path changed after locking")
    finally:
        if current_parent is not None:
            os.close(current_parent)
        os.close(current_root)


@contextmanager
def writer_lock(
    data_root: str | os.PathLike[str],
    lock_path: str | os.PathLike[str],
    *,
    dry_run: bool = False,
) -> Iterator[bool]:
    """Hold an exclusive nonblocking lock; yield False for a read-only preview.

    The caller prepares an existing mode-0700 directory outside Git. The lock
    path must be a direct child or lie under existing private descendant
    directories. A missing lock file is created exclusively with mode 0600, but
    parent directories are never created. The persistent lock file is empty and
    is never unlinked or replaced. A dry-run validates directories but neither
    opens nor creates the lock file; callers must perform no writes when False
    is yielded.

    Native Windows uses verified private ACLs instead of POSIX modes, pinned
    directory handles, and msvcrt.locking over a persistent single NUL byte.
    """
    if os.name == "nt":
        with _nt_writer_lock(data_root, lock_path, dry_run=dry_run) as held:
            yield held
        return
    _require_posix()
    if type(dry_run) is not bool:
        raise WriterLockError("dry_run must be a boolean")
    root_path = _absolute_path(data_root, "writer data directory")
    lock_target = _absolute_path(lock_path, "writer lock path")
    if not root_path.name:
        raise WriterLockError("writer data directory must not be the filesystem root")
    try:
        relative = lock_target.relative_to(root_path)
    except ValueError:
        raise WriterLockError("writer lock path must be inside the data directory") from None
    if not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
        raise WriterLockError("writer lock path must name a file inside the data directory")
    if relative.parts[-1] == ".git":
        raise WriterLockError("writer lock cannot be Git metadata")

    root_fd = parent_fd = lock_fd = None
    try:
        root_fd = _open_directory_path(root_path)
        _private_directory(root_fd)
        _writer_outside_git(root_fd)
        parent_parts = relative.parts[:-1]
        parent_fd = _open_relative_parent(root_fd, parent_parts)
        _writer_outside_git(parent_fd)
        name = relative.parts[-1]
        if dry_run:
            yield False
            return

        lock_fd = _open_lock_file(parent_fd, name)
        try:
            _fcntl.flock(lock_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                raise WriterLockError("another Workkit writer holds the lock") from None
            raise WriterLockError("cannot acquire Workkit writer lock") from None
        _verify_after_lock(root_path, root_fd, parent_parts, parent_fd, name, lock_fd)
        yield True
    finally:
        if lock_fd is not None:
            # Closing the descriptor releases flock, including on exceptions.
            os.close(lock_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        if root_fd is not None:
            os.close(root_fd)
