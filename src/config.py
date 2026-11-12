"""Conservative JSONC edits. Receipts must be persisted locally by the caller.

The caller must serialize writers and persist the returned receipt after a successful
apply. A lost or stale receipt fails closed on the next update. This module never
prints configuration or receipt contents. Parent directories must exist.

Element-wise ownership for the OpenCode V2 ``plugins`` array.

V2.0.31's Config.Plugin schema accepts a package string or an object containing
``package`` and optional ``options`` (Schema.Unknown values). Its plugin source
also treats leading ``-`` strings as removal directives. We only manage npm
package references and known local paths; unknown references fail closed."""

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

import jsonc_document as jsonc

from storage import ManagedFileError, _create_at, _parent, apply_file, _existing


class ConfigError(ValueError):
    """Invalid or ambiguous config, ownership conflict, or unsafe target."""


PathKey = tuple[str, ...]
_SAFE_REPLACE = os.name in ("posix", "nt")


@dataclass(frozen=True)
class ConfigResult:
    changed: bool
    receipt: dict[PathKey, str]


def _parse(source: str) -> jsonc.Document:
    # The library deliberately rejects trailing commas. Mask only lexer-identified
    # trailing commas so offsets remain valid; its parser still checks the grammar.
    try:
        tokens = [
            t for t in jsonc.tokenize(source=source)
            if t.kind not in (jsonc.TokenKind.WHITESPACE, jsonc.TokenKind.COMMENT)
        ]
        chars = list(source)
        for i in range(1, len(tokens) - 1):
            prev, token, following = tokens[i - 1:i + 2]
            ends_value = (prev.kind in (jsonc.TokenKind.STRING, jsonc.TokenKind.NUMBER,
                                        jsonc.TokenKind.LITERAL) or prev.text in ("}", "]"))
            if token.text == "," and following.text in ("}", "]") and ends_value:
                chars[token.start] = " "
        document = jsonc.parse(source="".join(chars))
        if not isinstance(document.value, dict):
            raise ConfigError("config root must be an object")
        return document
    except (jsonc.JSONCDecodeError, RecursionError, UnicodeError):
        raise ConfigError("invalid or ambiguous JSONC") from None


def _find(root: jsonc.Node, path: PathKey) -> tuple[jsonc.Node, int]:
    node = root
    for index, part in enumerate(path):
        if not isinstance(node.value, dict):
            raise ConfigError("a config path crosses a non-object value")
        for key, child in zip(node.keys, node.children, strict=True):
            if key.value == part:
                node = child
                break
        else:
            return node, index
    return node, len(path)


def _digest(source: str, node: jsonc.Node) -> str:
    return hashlib.sha256(source[node.start:node.end].encode("utf-8")).hexdigest()


def _encode(value: object) -> str:
    try:
        text = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        # This also rejects duplicate keys created by JSON's conversion of keys.
        jsonc.parse(source=text)
        return text
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ConfigError("new value is not unambiguous JSON") from None


def _insert(source: str, parent: jsonc.Node, key: str, value: str) -> str:
    end = parent.end - 1
    newline = "\r\n" if "\r\n" in source else "\n"
    line_start = source.rfind("\n", 0, end) + 1
    prefix = source[line_start:end]
    indent = prefix if not prefix.strip() else ""
    insertion = newline + indent + "  " + json.dumps(key, ensure_ascii=True) + ": " + value + newline + indent
    has_trailing_comma = parent.children and any(
        t.text == "," and t.kind == jsonc.TokenKind.PUNCTUATION
        for t in jsonc.tokenize(source=source[parent.children[-1].end:end])
    )
    if parent.children and not has_trailing_comma:
        pos = parent.children[-1].end
        source = source[:pos] + "," + source[pos:]
        end += 1
    return source[:end] + insertion + source[end:]


def _append_array(source: str, parent: jsonc.Node, values: list[str]) -> str:
    end = parent.end - 1
    newline = "\r\n" if "\r\n" in source else "\n"
    line_start = source.rfind("\n", 0, end) + 1
    prefix = source[line_start:end]
    indent = prefix if not prefix.strip() else ""
    separator = "," + newline + indent + "  "
    insertion = newline + indent + "  " + separator.join(values) + newline + indent
    has_trailing_comma = parent.children and any(
        t.text == "," and t.kind == jsonc.TokenKind.PUNCTUATION
        for t in jsonc.tokenize(source=source[parent.children[-1].end:end])
    )
    if parent.children and not has_trailing_comma:
        pos = parent.children[-1].end
        source = source[:pos] + "," + source[pos:]
        end += 1
    return source[:end] + insertion + source[end:]


def _edit_config_file[ResultType](
    path: str | Path,
    edit: Callable[[str, jsonc.Document, bool], tuple[str, ResultType]],
    *,
    dry_run: bool,
) -> tuple[bool, ResultType]:
    """Apply a text edit using the same guarded read and atomic publication path."""
    target = Path(path)
    if os.name == "nt":
        try:
            with _parent(target) as (directory, name):
                entry = _existing(directory, name)
                original = None if entry is None else entry[0]
                source = "{}" if original is None else original.decode("utf-8")
                source, result = edit(source, _parse(source), original is None)
                output = source.encode("utf-8")
                changed = original is None or output != original
                if changed and not dry_run:
                    if not _SAFE_REPLACE:
                        raise ConfigError("safe config replacement is unavailable")
                    apply_file(target.parent, target.name, output,
                               None if original is None else hashlib.sha256(original).hexdigest())
                return changed, result
        except (OSError, UnicodeError, ManagedFileError):
            raise ConfigError("cannot safely apply native Windows config") from None
    try:
        if os.name == "posix":
            with _parent(target):
                pass
        try:
            metadata = target.lstat()
        except FileNotFoundError:
            metadata = None
        if metadata is None:
            original = None
            source = "{}"
        else:
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ConfigError("config target must be a regular file with one link")
            if hasattr(os, "listxattr") and os.listxattr(target, follow_symlinks=False):
                raise ConfigError("config with extended attributes cannot be safely replaced")
            original = target.read_bytes()
            source = original.decode("utf-8")
    except (OSError, UnicodeError, ManagedFileError):
        raise ConfigError("cannot safely read config target as UTF-8") from None

    source, result = edit(source, _parse(source), original is None)
    output = source.encode("utf-8")
    changed = original is None or output != original
    if not changed or dry_run:
        return changed, result
    if not _SAFE_REPLACE:
        raise ConfigError("atomic replacement with metadata preservation is not supported on this OS")
    if original is None:
        try:
            with _parent(target) as (fd, name):
                _create_at(fd, name, output)
        except ManagedFileError:
            raise ConfigError("cannot safely create config; target may already exist") from None
        return True, result

    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".workkit-", dir=target.parent)
        with os.fdopen(fd, "wb") as file:
            os.chmod(temporary, stat.S_IMODE(metadata.st_mode))
            temp_metadata = os.fstat(file.fileno())
            if (temp_metadata.st_uid, temp_metadata.st_gid) != (metadata.st_uid, metadata.st_gid):
                raise ConfigError("cannot preserve config ownership")
            file.write(output)
            file.flush()
            os.fsync(file.fileno())
        current = target.lstat()
        if (not stat.S_ISREG(current.st_mode) or current.st_dev != metadata.st_dev
                or current.st_ino != metadata.st_ino or current.st_mode != metadata.st_mode
                or current.st_uid != metadata.st_uid or current.st_gid != metadata.st_gid
                or current.st_nlink != 1 or target.read_bytes() != original
                or (hasattr(os, "listxattr") and os.listxattr(target, follow_symlinks=False))):
            raise ConfigError("config changed during update")
        os.replace(temporary, target)
        temporary = None
    except OSError:
        raise ConfigError("cannot atomically apply config") from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return True, result


def apply_config(
    path: str | Path,
    changes: Mapping[PathKey, object],
    receipt: Mapping[PathKey, str],
    *,
    dry_run: bool = False,
) -> ConfigResult:
    """Set object paths without adopting existing values or overwriting foreign data.

    Receipt keys identify managed paths; values are SHA-256 hashes of their exact
    JSONC value spans. Only newly absent paths can be claimed. No deletion or
    array indexing is supported. A dry run returns the original receipt, since
    its prospective hashes must not be committed as actual ownership.
    """
    paths = list(changes)
    keys = list(receipt)
    for key in paths + keys:
        if not isinstance(key, tuple) or not key or any(not isinstance(p, str) or not p for p in key):
            raise ConfigError("paths must be non-empty tuples of non-empty strings")
        if key[0] == "plugins":
            raise ConfigError("plugins must be managed element-wise")
    if any(not isinstance(h, str) or len(h) != 64 or any(c not in "0123456789abcdef" for c in h)
           for h in receipt.values()):
        raise ConfigError("invalid receipt")
    for i, a in enumerate(paths):
        for b in paths[i + 1:] + keys:
            if a != b and (a[:len(b)] == b or b[:len(a)] == a):
                raise ConfigError("overlapping managed paths are not supported")

    def edit(source: str, document: jsonc.Document, missing: bool) -> tuple[str, dict[PathKey, str]]:
        if missing and receipt:
            raise ConfigError("previously managed config is missing")
        result_receipt = dict(receipt)
        for key, value in changes.items():
            node, found = _find(document.root, key)
            if found == len(key):
                if key not in receipt or _digest(source, node) != receipt[key]:
                    raise ConfigError("existing or modified value conflicts with receipt")
                encoded = _encode(value)
                if source[node.start:node.end] != encoded:
                    source = source[:node.start] + encoded + source[node.end:]
            else:
                if key in receipt:
                    raise ConfigError("managed value is missing")
                encoded = _encode(value)
                for part in reversed(key[found + 1:]):
                    encoded = "{" + json.dumps(part, ensure_ascii=True) + ":" + encoded + "}"
                source = _insert(source, node, key[found], encoded)
            document = _parse(source)
            updated, found = _find(document.root, key)
            if found != len(key):
                raise ConfigError("failed to locate edited value")
            result_receipt[key] = _digest(source, updated)
        return source, result_receipt

    changed, result_receipt = _edit_config_file(path, edit, dry_run=dry_run)
    return ConfigResult(changed, dict(receipt) if dry_run else result_receipt)


_PACKAGE_SEGMENT = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class PluginConfigResult:
    changed: bool
    receipt: dict[str, str]


def _package_identity(reference: str) -> tuple[str | None, bool]:
    """Return canonical npm name and remove-directive status; local refs have no name."""
    if not isinstance(reference, str) or not reference or any(ord(char) < 32 for char in reference):
        raise ConfigError("unsupported or ambiguous plugin declaration")
    remove = reference.startswith("-")
    value = reference[1:] if remove else reference
    if not value:
        raise ConfigError("unsupported or ambiguous plugin declaration")
    if value.startswith(("./", "../", "/", "file://")) or PureWindowsPath(value).is_absolute():
        return None, remove

    if value.startswith("@"):  # Scoped npm names: the leading @ is part of the name.
        scope, slash, rest = value[1:].partition("/")
        if not slash or not _PACKAGE_SEGMENT.fullmatch(scope):
            raise ConfigError("unsupported or ambiguous plugin declaration")
        name, marker, version = rest.partition("@")
        if not _PACKAGE_SEGMENT.fullmatch(name):
            raise ConfigError("unsupported or ambiguous plugin declaration")
        identity = f"@{scope}/{name}"
    else:
        name, marker, version = value.partition("@")
        if not _PACKAGE_SEGMENT.fullmatch(name):
            raise ConfigError("unsupported or ambiguous plugin declaration")
        identity = name
    if marker and (not version or "@" in version):
        raise ConfigError("unsupported or ambiguous plugin declaration")
    return identity, remove


def _plugin_identity(value: object) -> tuple[str | None, bool]:
    if isinstance(value, str):
        return _package_identity(value)
    if not isinstance(value, dict) or set(value) - {"package", "options"} or "package" not in value:
        raise ConfigError("unsupported or ambiguous plugin declaration")
    if not isinstance(value["package"], str) or (
        "options" in value and not isinstance(value["options"], dict)
    ):
        raise ConfigError("unsupported or ambiguous plugin declaration")
    return _package_identity(value["package"])


def _valid_name(name: object) -> bool:
    if not isinstance(name, str):
        return False
    try:
        identity, remove = _package_identity(name)
    except ConfigError:
        return False
    return identity == name and not remove


def _plugin_entries(root: jsonc.Node) -> tuple[jsonc.Node | None, list[tuple[str | None, bool, jsonc.Node]]]:
    plugins, found = _find_plugins(root)
    if not found:
        return None, []
    if not isinstance(plugins.value, list):
        raise ConfigError("plugins must be a V2 array")
    entries = []
    seen = set()
    for node in plugins.children:
        identity, remove = _plugin_identity(node.value)
        reference = node.value if isinstance(node.value, str) else node.value["package"]
        duplicate_key = (identity, reference if identity is None else None)
        if duplicate_key in seen:
            raise ConfigError("duplicate or ambiguous plugin declaration")
        seen.add(duplicate_key)
        entries.append((identity, remove, node))
    return plugins, entries


def _find_plugins(root: jsonc.Node) -> tuple[jsonc.Node | None, bool]:
    for key, child in zip(root.keys, root.children, strict=True):
        if key.value == "plugins":
            return child, True
    return None, False


def _object_field(node: jsonc.Node, name: str) -> jsonc.Node | None:
    for key, child in zip(node.keys, node.children, strict=True):
        if key.value == name:
            return child
    return None


def _entry_reference(node: jsonc.Node) -> str:
    return node.value if isinstance(node.value, str) else node.value["package"]


def _is_native_local_path(value: object) -> bool:
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value) or "://" in value:
        return False
    if os.name == "nt":
        path = PureWindowsPath(value)
        return path.is_absolute() and ".." not in path.parts
    path = Path(value)
    parsed = urlsplit(value)
    return (path.is_absolute() and ".." not in path.parts and not parsed.scheme and not parsed.netloc
            and not PureWindowsPath(value).drive)


def _same_json(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return (left.keys() == right.keys()
                and all(_same_json(left[key], right[key]) for key in left))
    if isinstance(left, list):
        return len(left) == len(right) and all(_same_json(a, b) for a, b in zip(left, right, strict=True))
    return left == right


def _update_owned_entry(source: str, node: jsonc.Node, desired: object, has_options: bool) -> str:
    package = desired if isinstance(desired, str) else desired["package"]
    package_text = _encode(package)
    if not isinstance(node.value, dict):
        if has_options:
            return source[:node.start] + _encode(desired) + source[node.end:]
        return source[:node.start] + package_text + source[node.end:]

    package_node = _object_field(node, "package")
    options_node = _object_field(node, "options")
    if package_node is None:
        raise ConfigError("managed plugin object has no package field")

    if has_options and options_node is None:
        source = _insert(source, node, "options", _encode(desired["options"]))

    edits = []
    if source[package_node.start:package_node.end] != package_text:
        edits.append((package_node.start, package_node.end, package_text))
    if has_options and options_node is not None and not _same_json(options_node.value, desired["options"]):
        edits.append((options_node.start, options_node.end, _encode(desired["options"])))
    for start, end, text in sorted(edits, reverse=True):
        source = source[:start] + text + source[end:]
    return source


def apply_plugins(
    path: str | Path,
    changes: Mapping[str, object],
    receipt: Mapping[str, str],
    *,
    remove=(),
    dry_run: bool = False,
) -> PluginConfigResult:
    """Add/update only explicitly owned npm plugin entries in a V2 JSONC config.

``changes`` maps an unversioned npm package name (including ``@scope/name``)
to a V2 npm reference, an absolute native plugin directory, or an object
containing one of those in ``package``. Callers must verify local directories
with the selected catalog recipe before calling; this function neither checks
nor activates them. Absolute package
    directory loading is verified for V2.2.1, not claimed for V2.0.31. Existing
    unrelated entries and their options/comments/order are never serialized anew.
    Receipts are exact hashes of each owned element and should live in the
    separate ``ReceiptSnapshot.plugins`` map. No entries are removed implicitly.
    """
    if not isinstance(changes, Mapping) or not isinstance(receipt, Mapping):
        raise ConfigError("plugin changes and receipt must be mappings")
    if isinstance(remove, (str, bytes)):
        raise ConfigError("remove must contain explicit canonical npm names")
    try:
        removals = tuple(remove)
    except TypeError:
        raise ConfigError("remove must contain explicit canonical npm names") from None
    if (any(not _valid_name(name) for name in removals) or len(set(removals)) != len(removals)
            or set(removals) & set(changes)):
        raise ConfigError("invalid, duplicate or overlapping plugin removals")
    desired: dict[str, tuple[object, str, bool, str, bool]] = {}
    for name, value in changes.items():
        if not _valid_name(name):
            raise ConfigError("plugin keys must be canonical npm package names")
        encoded = _encode(value)
        parsed = json.loads(encoded)
        identity, remove = _plugin_identity(parsed)
        package = parsed if isinstance(parsed, str) else parsed["package"]
        local = identity is None
        if remove or (local and not _is_native_local_path(package)) or (not local and identity != name):
            raise ConfigError("plugin entry does not match its ownership key")
        desired[name] = (parsed, encoded, isinstance(parsed, dict) and "options" in parsed, package, local)
    for name, digest in receipt.items():
        if not _valid_name(name) or not isinstance(digest, str) or not _HASH.fullmatch(digest):
            raise ConfigError("invalid plugin receipt")
    if len(set(receipt.values())) != len(receipt):
        raise ConfigError("plugin receipt ownership is ambiguous")

    def edit(source: str, document: jsonc.Document, missing: bool) -> tuple[str, dict[str, str]]:
        if missing and receipt:
            raise ConfigError("previously managed config is missing")
        plugins, entries = _plugin_entries(document.root)
        by_name: dict[str, list[jsonc.Node]] = {}
        by_digest: dict[str, list[jsonc.Node]] = {}
        for identity, _remove, node in entries:
            by_digest.setdefault(_digest(source, node), []).append(node)
            if identity is not None:
                by_name.setdefault(identity, []).append(node)

        # Verify every recorded entry, not only those selected for this invocation.
        owned_nodes: dict[str, jsonc.Node] = {}
        for name, digest in receipt.items():
            nodes = by_name.get(name, [])
            exact = by_digest.get(digest, [])
            if len(nodes) == 1 and len(exact) == 1 and exact[0] is nodes[0] and _digest(source, nodes[0]) == digest:
                owned_nodes[name] = nodes[0]
                continue
            if (not nodes and len(exact) == 1 and _is_native_local_path(_entry_reference(exact[0]))):
                owned_nodes[name] = exact[0]
                continue
            else:
                raise ConfigError("managed plugin is missing or differs from receipt")

        additions: list[tuple[str, str]] = []
        replacements: list[tuple[jsonc.Node, object, bool]] = []
        for name, (value, encoded, has_options, package, local) in desired.items():
            nodes = by_name.get(name, [])
            owned = owned_nodes.get(name)
            if owned is not None:
                if local and any(node is not owned and _entry_reference(node) == package for _, _, node in entries):
                    raise ConfigError("existing local plugin path conflicts with requested plugin")
                if nodes and (len(nodes) != 1 or nodes[0] is not owned):
                    raise ConfigError("existing unmanaged plugin conflicts with requested plugin")
                replacements.append((owned, value, has_options))
            elif nodes:
                raise ConfigError("existing unmanaged plugin conflicts with requested plugin")
            elif local and any(_entry_reference(node) == package for _, _, node in entries):
                raise ConfigError("existing local plugin path is not workkit-owned")
            else:
                additions.append((name, encoded))

        for node, value, has_options in sorted(replacements, key=lambda item: item[0].start, reverse=True):
            source = _update_owned_entry(source, node, value, has_options)
        if removals:
            document = _parse(source)
            plugins, current_entries = _plugin_entries(document.root)
            spans = set()
            for name in removals:
                if name not in receipt:
                    if by_name.get(name):
                        raise ConfigError("cannot remove a foreign plugin")
                    continue
                nodes = [node for _, _, node in current_entries if _digest(source, node) == receipt[name]]
                if len(nodes) != 1:
                    raise ConfigError("plugin removal conflicts with receipt")
                node = nodes[0]
                spans.add((node.start, node.end))
                # Delete punctuation only; preserve all surrounding comments.
                after = [token for token in jsonc.tokenize(source=source[node.end:plugins.end - 1])
                         if token.kind == jsonc.TokenKind.PUNCTUATION and token.text == ","]
                next_node = next((n for _, _, n in current_entries if n.start > node.start), None)
                boundary = next_node.start if next_node else plugins.end - 1
                comma = next((t for t in after if node.end + t.start < boundary), None)
                if comma is not None:
                    spans.add((node.end + comma.start, node.end + comma.end))
                else:
                    before = [t for t in jsonc.tokenize(source=source[plugins.start + 1:node.start])
                              if t.kind == jsonc.TokenKind.PUNCTUATION and t.text == ","]
                    if before:
                        comma = before[-1]
                        spans.add((plugins.start + 1 + comma.start, plugins.start + 1 + comma.end))
            for start, end in sorted(spans, reverse=True):
                source = source[:start] + source[end:]
        if additions:
            document = _parse(source)
            plugins, _found = _find_plugins(document.root)
            if plugins is None:
                values = ",".join(encoded for _name, encoded in additions)
                source = _insert(source, document.root, "plugins", "[" + values + "]")
            else:
                source = _append_array(source, plugins, [encoded for _name, encoded in additions])

        result_receipt = dict(receipt)
        for name in removals:
            result_receipt.pop(name, None)
        if desired:
            document = _parse(source)
            _plugins, final_entries = _plugin_entries(document.root)
            for name, (_value, _encoded, _has_options, package, local) in desired.items():
                matches = [node for identity, _remove, node in final_entries
                           if (_entry_reference(node) == package if local else identity == name)]
                if len(matches) != 1:
                    raise ConfigError("failed to locate edited plugin")
                result_receipt[name] = _digest(source, matches[0])
        return source, result_receipt

    changed, result_receipt = _edit_config_file(path, edit, dry_run=dry_run)
    return PluginConfigResult(changed, dict(receipt) if dry_run else result_receipt)
