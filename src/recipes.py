"""Validated finite Python actions for the trusted component catalog."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import hashlib
import http.client
import io
import gzip
import stat
import tarfile
import zipfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

_PLATFORMS = {"linux", "wsl", "windows"}
_ACTIONS = {"npm", "release", "uv-tool", "own-mcp", "skill", "managed-plugin"}
_OPERATIONS = ("install", "update", "verify")
_EXA_TOOLS = {"web_search_exa", "web_fetch_exa", "web_search_advanced_exa"}
_OWN_IDS = {"x-4pda", "x-genius"}
_TYPES = {"mcp", "framework", "skill", "cli"}
_PLUGIN_FILES = ("index.ts", "models.ts", "runtime.ts", "package.json", "providers.json")
_RELEASE_LIMIT = 128 * 1024 * 1024
_SKILL_LIMIT = 4 * 1024 * 1024
_MEMBER_LIMIT = 4096
# codebase-memory 0.12.1 ships a ~316 MiB executable; keep bounded headroom.
_BINARY_LIMIT = 512 * 1024 * 1024
_ARCHIVE_LIMIT = 768 * 1024 * 1024
_RELEASE_REPOSITORIES = {
    "engram": "Gentleman-Programming/engram",
    "codebase-memory-mcp": "DeusData/codebase-memory-mcp",
    "taskmd": "driangle/taskmd",
}
_EXTERNAL_SKILLS = {
    "grilling": ("mattpocock/skills", "skills/productivity/grilling", "SKILL.md agents/openai.yaml", "LICENSE"),
    "prototype": ("mattpocock/skills", "skills/engineering/prototype", "SKILL.md LOGIC.md UI.md agents/openai.yaml", "LICENSE"),
    "frontend-design": ("anthropics/skills", "skills/frontend-design", "SKILL.md LICENSE.txt", ""),
    "mcp-builder": ("anthropics/skills", "skills/mcp-builder", "SKILL.md LICENSE.txt reference/evaluation.md reference/mcp_best_practices.md reference/node_mcp_server.md reference/node_mcp_server_v2.md reference/python_mcp_server.md scripts/connections.py scripts/evaluation.py scripts/example_evaluation.xml scripts/requirements.txt", ""),
    "security-threat-model": ("openai/skills", "skills/.curated/security-threat-model", "SKILL.md LICENSE.txt agents/openai.yaml references/prompt-template.md references/security-controls-and-assets.md", ""),
    "security-best-practices": ("openai/skills", "skills/.curated/security-best-practices", "SKILL.md LICENSE.txt agents/openai.yaml references/golang-general-backend-security.md references/javascript-express-web-server-security.md references/javascript-general-web-frontend-security.md references/javascript-jquery-web-frontend-security.md references/javascript-typescript-nextjs-web-server-security.md references/javascript-typescript-react-web-frontend-security.md references/javascript-typescript-vue-web-frontend-security.md references/python-django-web-server-security.md references/python-fastapi-web-server-security.md references/python-flask-web-server-security.md", ""),
    "egm-drift-report": ("google/elephant-goldfish-model", "skills/egm-drift-report", "SKILL.md agents/openai.yaml", "LICENSE"),
    "untangle-codebase": ("caldoai/skills", "skills/untangle-codebase", "SKILL.md", "LICENSE"),
}


def _approved_skill_urls(ident):
    if ident not in _EXTERNAL_SKILLS:
        return set()
    repo, prefix, files, license_file = _EXTERNAL_SKILLS[ident]
    paths = [prefix + "/" + name for name in files.split()]
    if license_file:
        paths.append(license_file)
    return {f"https://raw.githubusercontent.com/{repo}/main/{path}" for path in paths}


class CatalogError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise CatalogError(f"{field} must be a non-empty trimmed string")
    return value


def _url(value: object, field: str, *, allow_query: bool = False) -> None:
    text = _text(value, field)
    try:
        parsed = urlsplit(text)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port is not None
                or (parsed.query and not allow_query) or parsed.fragment or "\\" in text
                or any(ord(char) < 33 or ord(char) == 127 for char in text)):
            raise ValueError
    except ValueError:
        raise CatalogError(f"{field} must be an HTTPS URL without credentials or a port") from None


def _remote_url(value: object) -> None:
    _url(value, "remote MCP url", allow_query=True)
    parsed = urlsplit(value)
    if not parsed.query:
        return
    allowed = "|".join(sorted(_EXA_TOOLS))
    if (parsed.hostname != "mcp.exa.ai" or parsed.path != "/mcp"
            or not re.fullmatch(rf"tools=(?:{allowed})(?:,(?:{allowed}))*", parsed.query)):
        raise CatalogError("remote MCP query is unsupported")
    tools = parsed.query.removeprefix("tools=").split(",")
    if len(tools) != len(set(tools)):
        raise CatalogError("remote MCP tools query contains duplicates")


def _string_list(value: object, field: str, *, empty: bool = False) -> None:
    if not isinstance(value, list) or (not value and not empty):
        raise CatalogError(f"{field} must be a {'possibly empty' if empty else 'non-empty'} list")
    seen = set()
    for item in value:
        item = _text(item, field)
        if item in seen:
            raise CatalogError(f"{field} contains a duplicate value")
        seen.add(item)


def _integration(value: object) -> str:
    if not isinstance(value, dict) or value.get("kind") not in (
        "plugin", "managed-plugin", "mcp", "remote-mcp", "cli"
    ):
        raise CatalogError("invalid integration")
    kind = value["kind"]
    if kind == "plugin":
        if (set(value) != {"kind", "package"}
                or not re.fullmatch(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*",
                                    _text(value.get("package"), "plugin package"))):
            raise CatalogError("invalid plugin integration")
    elif kind == "managed-plugin":
        if value != {"kind": "managed-plugin"}:
            raise CatalogError("invalid managed plugin integration")
    elif kind == "mcp":
        if set(value) != {"kind", "name", "arguments"}:
            raise CatalogError("invalid MCP integration")
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", _text(value["name"], "MCP name")):
            raise CatalogError("invalid MCP name")
        _string_list(value["arguments"], "MCP arguments", empty=True)
    elif kind == "remote-mcp":
        if set(value) != {"kind", "name", "url", "headers", "oauth"}:
            raise CatalogError("invalid remote MCP integration")
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", _text(value["name"], "remote MCP name")):
            raise CatalogError("invalid remote MCP name")
        _remote_url(value["url"])
        if not isinstance(value["headers"], dict) or type(value["oauth"]) is not bool:
            raise CatalogError("invalid remote MCP headers or oauth setting")
        for name, item in value["headers"].items():
            if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", name)
                    or not isinstance(item, str)
                    or not re.fullmatch(r"(?:Bearer )?\{env:[A-Z][A-Z0-9_]*\}", item)):
                raise CatalogError("remote MCP headers must contain environment references only")
    elif set(value) != {"kind"}:
        raise CatalogError(f"invalid {kind} integration")
    return kind


def _recipe(value: object, operation: str) -> None:
    if not isinstance(value, dict) or not value or set(value) - _PLATFORMS:
        raise CatalogError(f"{operation} must map supported platforms to recipes")
    for platform, recipe in value.items():
        if not isinstance(recipe, dict) or set(recipe) != {"actions"}:
            raise CatalogError(f"invalid {operation} recipe for {platform}")
        actions = recipe["actions"]
        if not isinstance(actions, list) or not actions:
            raise CatalogError("actions must be a non-empty list")
        for action in actions:
            _validate_action(action, operation)
        if len({json.dumps(action, sort_keys=True) for action in actions}) != len(actions):
            raise CatalogError("duplicate recipe action")


def _validate_action(action, operation=None):
    if not isinstance(action, dict) or action.get("action") not in _ACTIONS:
        raise CatalogError("unknown recipe action")
    kind = action["action"]
    if kind == "release" and "repository" in action:
        if (operation != "update" or set(action) != {"action", "repository", "asset_pattern", "binary"}
                or action["repository"] not in _RELEASE_REPOSITORIES.values()):
            raise CatalogError("unsupported latest release action")
        for field in ("asset_pattern", "binary"):
            pattern = _text(action[field], field)
            expanded = pattern.replace("{version}", "1.2.3").replace("{tag}", "v1.2.3").replace("{architecture}", "amd64")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", expanded):
                raise CatalogError("invalid release asset pattern")
        if not action["asset_pattern"].endswith((".tar.gz", ".zip")):
            raise CatalogError("unsupported release archive")
        return
    fields = {"action", "package", "version"} if kind == "npm" else {"action", "assets"} if kind == "release" else {"action"}
    if set(action) != fields:
        raise CatalogError("unsupported action fields")
    if kind == "npm":
        if (not re.fullmatch(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*", _text(action["package"], "npm package"))
                or not re.fullmatch(r"(?:latest|[0-9]+\.[0-9]+\.[0-9]+)", _text(action["version"], "npm version"))):
            raise CatalogError("invalid npm action")
    if kind == "release":
        if not isinstance(action["assets"], dict) or set(action["assets"]) != {"x86_64", "arm64"}:
            raise CatalogError("release requires explicit supported architectures")
        for asset in action["assets"].values():
            if not isinstance(asset, dict) or set(asset) != {"url", "sha256", "binary"}:
                raise CatalogError("invalid release asset")
            _url(asset["url"], "release URL")
            if (not isinstance(asset["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", asset["sha256"])
                    or not isinstance(asset["binary"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", asset["binary"])):
                raise CatalogError("invalid release hash or binary")


def load_catalog(path: str | Path) -> list[dict]:
    try:
        with Path(path).open(encoding="utf-8") as file:
            data = json.load(file, object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogError(f"cannot read catalog: {exc}") from exc
    if not isinstance(data, dict) or set(data) != {"packages"} or not isinstance(data["packages"], list):
        raise CatalogError("catalog must contain only a packages list")

    required = {"id", "type", "purpose", "repository", "documentation", "version", "requirements", "integration"}
    ids = set()
    integrations = set()
    skill_targets = set()
    for package in data["packages"]:
        if not isinstance(package, dict):
            raise CatalogError("each package must be an object")
        fields = set(package)
        if (not required <= fields or fields - (required | set(_OPERATIONS) | {"optional", "skill"})):
            raise CatalogError("package has missing or unsupported fields")
        ident = _text(package["id"], "id")
        if "skill" in package:
            skill = package["skill"]
            if (not isinstance(skill, dict) or set(skill) != {"id", "files"}
                    or not isinstance(skill["id"], str)
                    or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", skill["id"])
                    or not isinstance(skill["files"], dict) or "SKILL.md" not in skill["files"]):
                raise CatalogError(f"{package['id']} has invalid skill declaration")
            targets = set()
            for name, entry in skill["files"].items():
                if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*", name)
                        or any(part in (".", "..") for part in name.split("/"))
                        or not isinstance(entry, dict) or set(entry) - {"source", "sha256"}
                        or "source" not in entry or not isinstance(entry["source"], str)
                        or any(part in (".", "..") for part in entry["source"].replace("\\", "/").split("/"))):
                    raise CatalogError(f"{package['id']} has invalid skill file")
                if entry["source"].startswith("https://"):
                    _url(entry["source"], "skill source")
                    if entry["source"] not in _approved_skill_urls(package["id"]):
                        raise CatalogError("unapproved external skill URL")
                    if "sha256" in entry:
                        raise CatalogError("upstream main skill files use local integrity receipts")
                elif (not re.fullmatch(r"setup/skills/[A-Za-z0-9._/-]+", entry["source"])
                      or any(part in (".", "..") for part in entry["source"].split("/"))
                      or "sha256" in entry):
                    raise CatalogError(f"{package['id']} has invalid own skill source")
                targets.add(f"skills/{skill['id']}/{name}")
            if targets & skill_targets:
                raise CatalogError("skill target paths overlap")
            skill_targets.update(targets)
            external = {entry["source"] for entry in skill["files"].values() if entry["source"].startswith("https://")}
            if external and (external != _approved_skill_urls(package["id"]) or len(external) != len(skill["files"])):
                raise CatalogError("external skill declaration must include every approved supporting file and license")
        if not re.fullmatch(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", ident) or ident in ids:
            raise CatalogError(f"invalid or duplicate package id: {ident!r}")
        ids.add(ident)
        if not isinstance(package["type"], str) or package["type"] not in _TYPES:
            raise CatalogError(f"{ident} has an invalid package type")
        for field in ("purpose", "version"):
            _text(package[field], field)
        for field in ("repository", "documentation"):
            value = _text(package[field], field)
            if ident in _OWN_IDS and value.startswith("local:"):
                if not re.fullmatch(r"local:setup/mcp/[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", value):
                    raise CatalogError(f"invalid local {field} reference")
            else:
                _url(value, field)
        _string_list(package["requirements"], "requirements", empty=True)
        if "optional" in package and type(package["optional"]) is not bool:
            raise CatalogError("optional must be a boolean")

        kind = _integration(package["integration"])
        if kind == "managed-plugin" and ident != "x-custom-providers":
            raise CatalogError("unknown managed plugin")
        if kind == "remote-mcp" and package.get("optional") is not True:
            raise CatalogError(f"{kind} components must be optional")
        integration_name = package["integration"].get("package", package["integration"].get("name"))
        if integration_name is not None:
            key = ("plugin" if kind == "plugin" else "mcp", integration_name)
            if key in integrations:
                raise CatalogError("duplicate integration")
            integrations.add(key)
        for operation in _OPERATIONS:
            if operation in package:
                _recipe(package[operation], operation)
                for recipe in package[operation].values():
                    for action in recipe["actions"]:
                        if (action.get("repository") is not None
                                and action["repository"] != _RELEASE_REPOSITORIES.get(ident)):
                            raise CatalogError("release repository does not match component")
            elif kind != "remote-mcp":
                raise CatalogError(f"{ident} requires a {operation} recipe")

    return data["packages"]


def recipe_for(package: dict, operation: str, platform: str) -> dict | None:
    """Select a recipe; WSL inherits Linux only when it has no explicit branch."""
    if operation not in _OPERATIONS or platform not in _PLATFORMS:
        raise CatalogError("unsupported recipe operation or platform")
    recipes = package.get(operation)
    if recipes is None:
        return None
    return recipes.get(platform) or (recipes.get("linux") if platform == "wsl" else None)


_SECRET_NAME = re.compile(r"(?:KEY|TOKEN|PASSWORD|SECRET|CREDENTIAL)", re.IGNORECASE)


@dataclass(frozen=True)
class RecipeResult:
    completed: tuple[int, ...]
    failed_step: int | None
    returncode: int
    error: str | None
    commands: tuple[str, ...]
    preview: bool = False

    @property
    def ok(self) -> bool:
        return not self.preview and self.returncode == 0


def _safe_error(output: str, env: dict[str, str]) -> str:
    for name, value in sorted(env.items(), key=lambda item: len(item[1]), reverse=True):
        if value and (name.startswith("WORKKIT_") or _SECRET_NAME.search(name)):
            output = output.replace(value, "[redacted]")
    return "\n".join(output.splitlines()[-8:])[-1200:].strip()


def run_recipe(
    component_id: str,
    operation: str,
    recipe: dict,
    env: dict[str, str],
    *,
    dry_run: bool = False,
    cwd: str | Path | None = None,
    package: dict | None = None,
) -> RecipeResult:
    """Execute finite actions in order; stop and report the first failed action."""
    if (not isinstance(component_id, str) or not re.fullmatch(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", component_id)
            or operation not in _OPERATIONS or not isinstance(env, dict) or type(dry_run) is not bool
            or any(not isinstance(key, str) or not isinstance(value, str) for key, value in env.items())
            or (package is not None and (not isinstance(package, dict) or package.get("id") != component_id))):
        raise ValueError("invalid recipe execution input")
    _recipe({"linux": recipe}, operation)
    commands = tuple(json.dumps(action, sort_keys=True) for action in recipe["actions"])
    if dry_run:
        return RecipeResult((), None, 0, None, commands, preview=True)
    environment = os.environ.copy()
    environment.update(env)
    root = Path(cwd or env.get("WORKKIT_DATA_ROOT", "."))
    completed = []
    for index, action in enumerate(recipe["actions"], 1):
        try:
            _run_action(component_id, operation, action, environment, root, package)
        except (OSError, RuntimeError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
            code = exc.returncode if isinstance(exc, ActionError) else 1
            error = _safe_error(str(exc), environment)
            if operation != "verify":
                error = "recipe resources may be partial; inspect before retrying: " + error
            return RecipeResult(tuple(completed), index, code, error, commands)
        completed.append(index)
    return RecipeResult(tuple(completed), None, 0, None, commands)


class ActionError(ValueError):
    def __init__(self, message, returncode=1):
        super().__init__(message)
        self.returncode = returncode


def _execute(arguments, root, env):
    result = subprocess.run([str(arg) for arg in arguments], cwd=root, env=env,
                            text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
    if result.returncode:
        raise ActionError(result.stderr or result.stdout or "action subprocess failed", result.returncode)
    return result.stdout.strip()


def _program(name):
    path = shutil.which(name)
    if not path:
        raise ActionError(f"{name} is unavailable", 127)
    return path


def _node(root, env, *, minimum=False):
    node = _program("node")
    if minimum:
        version = _execute([node, "--version"], root, env).removeprefix("v")
        if not re.fullmatch(r"\d+\.\d+\.\d+", version) or tuple(map(int, version.split("."))) < (22, 18, 0):
            raise ActionError("managed plugin requires Node >=22.18.0")
    return node


def _npm(root, env, *, minimum=False):
    node = _node(root, env, minimum=minimum)
    npm = Path(_program("npm.cmd" if os.name == "nt" else "npm"))
    if os.name != "nt":
        return [str(npm)]
    # Do not execute a .cmd shim through an implicit command interpreter.
    cli = npm.parent / "node_modules/npm/bin/npm-cli.js"
    info = cli.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or cli.resolve(strict=True) != cli:
        raise ActionError("npm-cli.js is missing or unsafe")
    metadata = json.loads((cli.parent.parent / "package.json").read_bytes())
    if metadata.get("name") != "npm" or metadata.get("bin", {}).get("npm") != "bin/npm-cli.js":
        raise ActionError("npm-cli.js does not belong to npm")
    return [node, str(cli)]


def _directory(root, relative, *, dry_run=False):
    """Walk owned directories without following links; create only absent ones."""
    root = Path(root)
    for cursor in (root, *(root.joinpath(*Path(relative).parts[:i]) for i in range(1, len(Path(relative).parts) + 1))):
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            if dry_run:
                continue
            if os.name == "nt":
                from storage import validate_windows_directory
                validate_windows_directory(cursor, create=True)
            else:
                cursor.mkdir(mode=0o700)
            info = cursor.lstat()
        if (not stat.S_ISDIR(info.st_mode) or cursor.resolve(strict=True) != cursor
                or (os.name != "nt" and info.st_uid != os.geteuid())
                or (os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o022)):
            raise ActionError("managed directory is unsafe")
        if os.name == "nt":
            from storage import validate_windows_directory
            validate_windows_directory(cursor)
    return root / relative


class _HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _url(newurl, "download redirect URL", allow_query=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _bounded_read(stream, limit):
    content = stream.read(limit + 1)
    if len(content) > limit:
        raise ActionError("download or archive payload exceeds size limit")
    return content


def _download(url, *, limit=_RELEASE_LIMIT):
    _url(url, "download URL")
    try:
        with urllib.request.build_opener(_HTTPSRedirect()).open(url, timeout=60) as response:
            final = response.geturl()
            _url(final, "download redirect URL", allow_query=True)
            content = _bounded_read(response, limit)
            if getattr(response, "length", None):
                raise ActionError("incomplete HTTPS response")
    except http.client.HTTPException as exc:
        raise ActionError("incomplete HTTPS response") from exc
    if not content:
        raise ActionError("download returned an empty body")
    return content


def _read_owned(root, relative, digest):
    from storage import _parent, _existing, apply_file
    with _parent(Path(root) / relative) as (fd, name):
        entry = _existing(fd, name)
    if entry is None or digest is None:
        raise ActionError("managed file or integrity receipt is missing")
    apply_file(root, relative, entry[0], digest, dry_run=True)
    return entry[0]


def _skill_location(package, data_root):
    ident = package["skill"]["id"]
    if ident not in _EXTERNAL_SKILLS:
        raise ActionError("unknown external skill")
    return Path(data_root) / "skills" / ident / "main"


def verify_skill_files(package, data_root) -> dict[str, bytes]:
    """Offline integrity against the cache receipt (not upstream authenticity)."""
    from storage import load_receipt
    base = _skill_location(package, data_root)
    _directory(data_root, str(base.relative_to(data_root)), dry_run=True)
    receipt = load_receipt(base / "receipt.json")
    if set(receipt.files) != set(package["skill"]["files"]):
        raise ActionError("skill cache receipt is incomplete")
    return {name: _read_owned(base, name, receipt.files[name]) for name in receipt.files}


def fetch_skill_files(package, data_root, *, dry_run=False):
    """Fetch all approved main files before publishing any cache bytes."""
    from storage import apply_file, load_receipt, save_receipt
    base = _skill_location(package, data_root)
    declaration = package["skill"]["files"]
    if any(entry["source"] not in _approved_skill_urls(package["id"]) for entry in declaration.values()):
        raise ActionError("unapproved skill URL")
    _directory(data_root, str(base.relative_to(data_root)), dry_run=True)
    if dry_run:
        return
    receipt = load_receipt(base / "receipt.json") if base.is_dir() else None
    if receipt is not None and receipt.files:
        verify_skill_files(package, data_root)
    content = {name: _download(entry["source"], limit=_SKILL_LIMIT) for name, entry in declaration.items()}
    _directory(data_root, str(base.relative_to(data_root)))
    receipt = load_receipt(base / "receipt.json")
    for name in content:
        if "/" in name:
            _directory(base, str(Path(name).parent))
    for name, body in content.items():
        apply_file(base, name, body, receipt.files.get(name), dry_run=True)
    results = {name: apply_file(base, name, body, receipt.files.get(name)) for name, body in content.items()}
    save_receipt(base / "receipt.json", receipt, config=receipt.config, files={name: result.receipt for name, result in results.items()})


def _archive_files(content, url, *, binary):
    """Validate all metadata before reading the unique requested executable."""
    names = set()
    matches = []
    total = 0
    def name_for(name):
        parts = PurePosixPath(name).parts
        if (not parts or name.startswith("/") or "\\" in name or PureWindowsPath(name).drive
                or any(part in ("", ".", "..") for part in name.rstrip("/").split("/"))):
            raise ActionError("unsafe archive path")
        if name.rstrip("/") in names:
            raise ActionError("duplicate archive member")
        names.add(name.rstrip("/"))
        if len(names) > _MEMBER_LIMIT:
            raise ActionError("archive member count exceeds limit")
        return name.rstrip("/")
    def record(name, size, regular, member):
        nonlocal total
        if size < 0 or size > _BINARY_LIMIT:
            raise ActionError("archive member size exceeds limit")
        total += size
        if total > _ARCHIVE_LIMIT:
            raise ActionError("archive total size exceeds limit")
        if regular and PurePosixPath(name).name == binary:
            matches.append((name, member))
    def selected():
        if len(matches) != 1:
            raise ActionError("release must contain exactly one executable")
        return matches[0]
    if url.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for member in archive.infolist():
                name = name_for(member.filename)
                mode = member.external_attr >> 16
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ActionError("archive links or special files are forbidden")
                record(name, member.file_size, not member.is_dir(), member)
            name, member = selected()
            with archive.open(member) as stream:
                body = _bounded_read(stream, _BINARY_LIMIT)
    elif url.endswith(".tar.gz"):
        # Tar metadata is interleaved with payloads; bound even padding and PAX headers.
        class BoundedGzip(gzip.GzipFile):
            remaining = _ARCHIVE_LIMIT

            def read(self, size=-1):
                chunk = super().read(min(size, self.remaining + 1) if size >= 0 else self.remaining + 1)
                self.remaining -= len(chunk)
                if self.remaining < 0:
                    raise ActionError("archive expansion exceeds size limit")
                return chunk

        with BoundedGzip(fileobj=io.BytesIO(content), mode="rb") as stream:
            with tarfile.open(fileobj=stream, mode="r|") as archive:
                for member in archive:
                    name = name_for(member.name)
                    if not member.isfile() and not member.isdir():
                        raise ActionError("archive links or special files are forbidden")
                    record(name, member.size, member.isfile(), member)
        name, member = selected()
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
            with archive.extractfile(member) as stream:
                body = _bounded_read(stream, _BINARY_LIMIT)
    else:
        raise ActionError("unsupported archive format")
    expected_size = member.size if url.endswith(".tar.gz") else member.file_size
    if len(body) != expected_size:
        raise ActionError("archive payload size disagrees with metadata")
    return {name: body}


def _latest_release(package, action, architecture):
    """Resolve one approved stable asset; require GitHub's SHA-256 digest."""
    _validate_action(action, "update")
    repo = _RELEASE_REPOSITORIES.get(package["id"])
    if action.get("repository") != repo or architecture not in ("x86_64", "arm64"):
        raise ActionError("unapproved release repository or architecture")
    payload = json.loads(_download(f"https://api.github.com/repos/{repo}/releases/latest", limit=4 * 1024 * 1024),
                         object_pairs_hook=_unique_object)
    if (not isinstance(payload, dict) or payload.get("draft") is not False
            or payload.get("prerelease") is not False or not isinstance(payload.get("assets"), list)):
        raise ActionError("malformed or unstable GitHub release")
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not re.fullmatch(r"v?[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise ActionError("invalid release tag")
    values = {"tag": tag, "version": tag.removeprefix("v"),
              "architecture": "amd64" if architecture == "x86_64" else "arm64"}
    name = action["asset_pattern"].format(**values)
    assets = payload["assets"]
    if any(not isinstance(asset, dict) or not isinstance(asset.get("name"), str) for asset in assets):
        raise ActionError("malformed release assets")
    matches = [asset for asset in assets if asset["name"] == name]
    if len(matches) != 1:
        raise ActionError("release must contain exactly one matching asset")
    asset = matches[0]
    url = f"https://github.com/{repo}/releases/download/{tag}/{name}"
    if asset.get("browser_download_url") != url:
        raise ActionError("release asset URL is outside the approved repository")
    digest = asset.get("digest")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest):
        raise ActionError("release asset requires a valid GitHub SHA-256 digest")
    return {"url": url, "sha256": digest[7:].lower(), "binary": action["binary"].format(**values)}


def _release(ident, operation, action, root, env):
    from paths import detect_platform
    from storage import apply_file, load_receipt, save_receipt
    target = detect_platform()
    if target.architecture not in ("x86_64", "arm64"):
        raise ActionError("unsupported architecture")
    binary = ident + (".exe" if target.platform == "windows" else "")
    if operation == "verify":
        receipt = load_receipt(root / "bin" / (ident + "-receipt.json"))
        _read_owned(root / "bin", binary, receipt.files.get(binary))
        _execute([root / "bin" / binary, "--version"], root, env)
        return
    if operation == "install":
        directory = _directory(root, "bin", dry_run=True)
        if directory.is_dir():
            receipt = load_receipt(directory / (ident + "-receipt.json"))
            executable = directory / binary
            if binary in receipt.files or executable.exists() or executable.is_symlink():
                _read_owned(directory, binary, receipt.files.get(binary))
                _execute([executable, "--version"], root, env)
                return
    asset = (_latest_release({"id": ident}, action, target.architecture)
             if operation == "update" else action["assets"][target.architecture])
    body = _download(asset["url"])
    if hashlib.sha256(body).hexdigest() != asset["sha256"]:
        raise ActionError("release SHA-256 mismatch")
    files = _archive_files(body, asset["url"], binary=asset["binary"])
    directory = _directory(root, "bin")
    receipt = load_receipt(directory / (ident + "-receipt.json"))
    result = apply_file(directory, binary, next(iter(files.values())), receipt.files.get(binary), mode=0o700)
    save_receipt(directory / (ident + "-receipt.json"), receipt, config=receipt.config, files={binary: result.receipt})


def _plugin_dependency(directory):
    dependency = directory / "node_modules/@opencode/plugin/package.json"
    _directory(directory, "node_modules/@opencode/plugin", dry_run=True)
    info = dependency.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ActionError("managed plugin dependency is unsafe")
    metadata = json.loads(dependency.read_bytes())
    if (not isinstance(metadata, dict) or metadata.get("name") != "@opencode/plugin"
            or not isinstance(metadata.get("version"), str)
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", metadata["version"])):
        raise ActionError("managed plugin dependency has an invalid name or version")
    return metadata["version"]


def _dependency_files(directory):
    """Only regular owned dependency files may be replaced or receipted."""
    result = {}
    _directory(directory, "node_modules", dry_run=True)
    def fail(error):
        raise error
    for base, dirs, files in os.walk(directory / "node_modules", followlinks=False, onerror=fail):
        for name in dirs:
            _directory(directory, str((Path(base) / name).relative_to(directory)), dry_run=True)
        for name in files:
            path = Path(base) / name
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or path.resolve(strict=True) != path
                    or (os.name != "nt" and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022))):
                raise ActionError("managed plugin dependency tree is unsafe")
            result[str(path.relative_to(directory)).replace(os.sep, "/")] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _managed_plugin(package, operation, root, env):
    from storage import apply_file, load_receipt, save_receipt
    directory = root / "plugins/x-custom-providers"
    receipt_path = root / "plugins/x-custom-providers-receipt.json"
    dependency_receipt_path = root / "plugins/x-custom-providers-dependencies.json"
    _directory(root, "plugins/x-custom-providers", dry_run=True)
    receipt = load_receipt(receipt_path) if directory.parent.is_dir() else None
    if operation == "verify":
        if receipt is None or set(receipt.files) != set(_PLUGIN_FILES):
            raise ActionError("managed plugin receipt is incomplete")
        for name in _PLUGIN_FILES:
            _read_owned(directory, name, receipt.files[name])
        _plugin_dependency(directory)
        dependencies = load_receipt(dependency_receipt_path)
        if dependencies.files and _dependency_files(directory) != dependencies.files:
            raise ActionError("managed plugin dependencies conflict with their integrity receipt")
        _execute([_node(root, env, minimum=True), "--input-type=module", "--eval",
                  'await import("@opencode/plugin")'], directory, env)
        return
    source = Path(env["WORKKIT_SETUP_DIR"]) / "plugins/x-custom-providers"
    content = {}
    for name in _PLUGIN_FILES:
        path = source / name
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.resolve(strict=True) != path:
            raise ActionError("bundled plugin source is unsafe")
        content[name] = path.read_bytes()
    if receipt is not None:
        for name, body in content.items():
            apply_file(directory, name, body, receipt.files.get(name), dry_run=True)
    dependency_receipt = load_receipt(dependency_receipt_path) if directory.parent.is_dir() else None
    existing = (directory / "node_modules").exists() or (directory / "node_modules").is_symlink()
    if existing and operation == "install":
        _plugin_dependency(directory)
        dependencies = _dependency_files(directory)
        if dependency_receipt.files and dependencies != dependency_receipt.files:
            raise ActionError("managed plugin dependencies conflict with their integrity receipt")
        _execute([_node(root, env, minimum=True), "--input-type=module", "--eval",
                  'await import("@opencode/plugin")'], directory, env)
        receipt = load_receipt(receipt_path)
        results = {name: apply_file(directory, name, body, receipt.files.get(name)) for name, body in content.items()}
        save_receipt(receipt_path, receipt, config=receipt.config, files={name: result.receipt for name, result in results.items()})
        return
    if existing and operation == "update":
        if dependency_receipt is None or not dependency_receipt.files or _dependency_files(directory) != dependency_receipt.files:
            raise ActionError("existing plugin dependencies conflict with their ownership receipt; not overwritten")
    expected_version = None
    if operation == "install":
        manifest = json.loads(content["package.json"])
        if not isinstance(manifest, dict) or not isinstance(manifest.get("dependencies"), dict):
            raise ActionError("bundled plugin manifest has invalid dependencies")
        expected_version = _text(manifest["dependencies"].get("@opencode/plugin"), "plugin dependency version")
    # npm operates only on private staging files, never the registered plugin.
    npm = _npm(root, env, minimum=True)
    with tempfile.TemporaryDirectory(prefix=".workkit-plugin-", dir=root) as temporary:
        stage = Path(temporary)
        _directory(root, stage.name)
        for name, body in content.items():
            apply_file(stage, name, body, None)
        arguments = [*npm, "install", "--omit=dev", "--package-lock=false", "--bin-links=false"]
        if operation == "update":
            arguments.extend(("--no-save", "@opencode/plugin@latest"))
        _execute(arguments, stage, env)
        version = _plugin_dependency(stage)
        _execute([_node(root, env, minimum=True), "--input-type=module", "--eval",
                  'await import("@opencode/plugin")'], stage, env)
        if operation == "install" and version != expected_version:
            raise ActionError("npm installed an unexpected managed plugin dependency")
        dependencies = _dependency_files(stage)
        _directory(root, "plugins/x-custom-providers")
        receipt = load_receipt(receipt_path)
        dependency_receipt = load_receipt(dependency_receipt_path)
        if existing and operation == "update":
            if _dependency_files(directory) != dependency_receipt.files:
                raise ActionError("plugin dependencies changed before publication")
            (directory / "node_modules").rename(stage / "previous-node_modules")
            try:
                (stage / "node_modules").rename(directory / "node_modules")
            except OSError:
                (stage / "previous-node_modules").rename(directory / "node_modules")
                raise
        else:
            (stage / "node_modules").rename(directory / "node_modules")
        if not existing or operation == "update":
            save_receipt(dependency_receipt_path, dependency_receipt, config=dependency_receipt.config, files=dependencies)
        results = {name: apply_file(directory, name, body, receipt.files.get(name)) for name, body in content.items()}
        save_receipt(receipt_path, receipt, config=receipt.config, files={name: result.receipt for name, result in results.items()})


def _run_action(ident, operation, action, env, root, package):
    kind = action["action"]
    if kind == "release":
        return _release(ident, operation, action, root, env)
    if kind == "npm":
        directory = root / "npm" / ident
        if operation == "verify":
            path = directory / "node_modules" / action["package"] / "package.json"
            _directory(root, str(path.parent.relative_to(root)), dry_run=True)
            if path.is_symlink() or not path.is_file() or json.loads(path.read_bytes()).get("name") != action["package"]:
                raise ActionError("npm package is missing or unsafe")
            if ident == "rea":
                script = path.parent / "scripts/rea.mjs"
                _directory(root, str(script.parent.relative_to(root)), dry_run=True)
                info = script.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or script.resolve(strict=True) != script:
                    raise ActionError("REA entry point is missing or unsafe")
                _execute([_node(root, env), script, "--version"], root, env)
        else:
            _directory(root, "npm/" + ident)
            _execute([*_npm(root, env), "install", "--prefix", directory, "--no-save",
                      action["package"] + "@" + action["version"]], root, env)
        return
    if kind == "managed-plugin":
        if ident != "x-custom-providers":
            raise ActionError("unknown managed plugin")
        return _managed_plugin(package, operation, root, env)
    if kind == "uv-tool":
        binary = {"duckduckgo": "duckduckgo-mcp-server", "semgrep": "semgrep", "markitdown-mcp": "markitdown-mcp"}.get(ident)
        if binary is None:
            raise ActionError("unknown uv tool")
        if operation == "verify":
            from paths import detect_platform
            executable = root / "bin" / (binary + (".exe" if detect_platform().platform == "windows" else ""))
            if executable.is_symlink():
                tool_root = root / "uv-tools" / binary
                if tool_root.resolve(strict=True) != tool_root or not executable.resolve(strict=True).is_relative_to(tool_root):
                    raise ActionError("uv executable escapes its managed tool directory")
            info = executable.stat()
            if not stat.S_ISREG(info.st_mode):
                raise ActionError("uv executable is not a regular file")
            return _execute([executable, "--version" if ident == "semgrep" else "--help"], root, env)
        env = {**env, "UV_TOOL_DIR": str(root / "uv-tools"), "UV_TOOL_BIN_DIR": str(root / "bin")}
        return _execute([_program("uv"), "tool", "upgrade" if operation == "update" else "install", binary], root, env)
    if kind == "own-mcp":
        if ident not in _OWN_IDS:
            raise ActionError("unknown own MCP")
        arguments = [_program("uv"), "run", "--no-project", "--isolated"]
        if operation == "verify":
            arguments.append("--offline")
        for dependency in ("mcp>=1.6,<2", "httpx", "beautifulsoup4"):
            arguments.extend(("--with", dependency))
        return _execute([*arguments, "--", "python", "-c", "import mcp, httpx, bs4"], root, env)
    if kind == "skill":
        if package is None or "skill" not in package:
            raise ActionError("skill action requires package metadata")
        if any(entry["source"].startswith("https://") for entry in package["skill"]["files"].values()):
            return verify_skill_files(package, root) if operation == "verify" else fetch_skill_files(package, root)
        for entry in package["skill"]["files"].values():
            path = Path(env["WORKKIT_SETUP_DIR"]).parent / entry["source"]
            if not path.is_file() or path.is_symlink() or path.resolve(strict=True) != path:
                raise ActionError("bundled skill source is missing or unsafe")
