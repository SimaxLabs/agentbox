#!/usr/bin/env python3
"""Back up and restore AI coding-agent skills and commands."""

import ast
from collections.abc import Callable, Iterable, Sequence
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl.
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX has no msvcrt.
    msvcrt = None


VERSION = 1
CONFIG_VERSION = 2
PROVIDER_DEFINITIONS_VERSION = 1
HISTORY_VERSION = 1
GIT_TIMEOUT_SECONDS = 120
_GENERATED_SKILL_DIRECTORY_MODE = 0o755
_GENERATED_SKILL_FILE_MODE = 0o644
SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
CONFIG_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
REVISION_ID = re.compile(r"^[0-9]{8}T[0-9]{12}Z-[0-9a-f]{16}$")
CONTENT_HASH = re.compile(r"^[0-9a-f]{64}$")
PLACEHOLDER = re.compile(r"\$(?:ARGUMENTS(?:\[[0-9]+\])?|[A-Z][A-Z0-9_]*|[0-9]+)")


class AgentBoxError(Exception):
    pass


@dataclass
class Source:
    source_id: str
    root: Path
    relative_path: str
    physical_path: Path


@dataclass
class Artifact:
    tool: str
    kind: str
    name: str
    catalog_path: str
    payload: Path
    sources: list[Source] = field(default_factory=list)
    host: str = ""
    catalog_revision: str | None = None


@dataclass
class Candidate:
    name: str
    payload: Path | None
    generated: bytes | None
    origins: list[dict[str, str]]

    def fingerprint(self) -> str:
        if self.generated is not None:
            return hash_generated_skill(self.generated)
        if self.payload is None:
            raise AgentBoxError("Restore candidate has no payload")
        return hash_path(self.payload)


@dataclass(frozen=True)
class OperationEvent:
    kind: str
    message: str
    tool: str = ""
    artifact: str = ""


type Reporter = Callable[[OperationEvent], None]


def console_report(event: OperationEvent) -> None:
    print(event.message)


@dataclass(frozen=True)
class OperationRequest:
    action: str
    tool: str = "all"
    host: str | None = None
    dry_run: bool = False
    prune: bool = False
    include_derived: bool = False
    source_tool: str | None = None
    all_tools: bool = False
    all_hosts: bool = False
    as_backed_up: bool = False
    force: bool = False
    storage_local: str | None = None
    storage_git: str | None = None
    provider_resources: tuple[str, ...] = ()
    catalog_revision: str | None = None


@dataclass(frozen=True)
class CatalogRevisionSummary:
    revision_id: str
    created_at: str
    host: str
    tools: tuple[str, ...]
    artifact_count: int
    changes: int
    snapshot: str

    @property
    def short_id(self) -> str:
        return self.revision_id[-16:]


@dataclass(frozen=True)
class CatalogRevisionDetail:
    summary: CatalogRevisionSummary
    artifacts: tuple[dict[str, str], ...]


@dataclass
class StorageSession:
    config: dict
    local_root: Path | None
    git_root: Path | None
    canonical_root: Path
    initialize: str | None = None
    git_branch: str | None = None
    git_revision: str | None = None
    git_pending: bool = False
    git_pushed: bool = False
    git_uncertain: bool = False

    @property
    def uses_git(self) -> bool:
        return self.git_root is not None


def expand_path(value: str, base: Path) -> Path:
    expanded = Path(os.path.expandvars(os.path.expanduser(value)))
    if not expanded.is_absolute():
        expanded = base / expanded
    return expanded


def expand_provider_path(value: str, base: Path) -> Path:
    if value.startswith("$XDG_CONFIG_HOME/"):
        configured = os.environ.get("XDG_CONFIG_HOME", "")
        candidate = Path(os.path.expandvars(os.path.expanduser(configured)))
        if not configured or not candidate.is_absolute():
            value = "~/.config/" + value.removeprefix("$XDG_CONFIG_HOME/")
    return expand_path(value, base)


def user_data_root() -> Path:
    if sys.platform == "win32":  # pragma: no cover - exercised on Windows.
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidate = Path(os.path.expandvars(os.path.expanduser(local_app_data)))
            if candidate.is_absolute():
                return candidate / "AgentBox"
        return Path.home() / "AppData/Local/AgentBox"
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        candidate = Path(os.path.expandvars(os.path.expanduser(xdg_data_home)))
        if candidate.is_absolute():
            return candidate / "agentbox"
    return Path.home() / ".local/share/agentbox"


def default_local_catalog() -> Path:
    return user_data_root() / "catalog"


def catalog_history_root(local_root: Path) -> Path:
    identity = hashlib.sha256(str(local_root.expanduser().resolve()).encode("utf-8")).hexdigest()
    return user_data_root() / "history" / identity


def _history_storage(local_root: Path | None, git_url: str | None) -> tuple[bool, Path | None]:
    enabled = local_root is not None and git_url is None
    root = catalog_history_root(local_root) if local_root is not None else None
    if enabled and root is not None:
        resolved_local = local_root.expanduser().resolve()
        resolved_history = root.expanduser().resolve()
        if resolved_local.is_relative_to(resolved_history) or resolved_history.is_relative_to(
            resolved_local
        ):
            raise AgentBoxError(
                "Local catalog and managed history paths must not overlap: {} and {}".format(
                    local_root, root
                )
            )
    return enabled, root


def user_state_root() -> Path:
    if sys.platform == "win32":  # pragma: no cover - exercised on Windows.
        return user_data_root() / "state"
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    if xdg_state_home:
        candidate = Path(os.path.expandvars(os.path.expanduser(xdg_state_home)))
        if candidate.is_absolute():
            return candidate / "agentbox"
    return Path.home() / ".local/state/agentbox"


def storage_roots(config: dict) -> list[Path]:
    roots = []
    if config["_storage_local"] is not None:
        roots.append(config["_storage_local"])
    if config["_storage_git_catalog"] is not None:
        roots.append(config["_storage_git_catalog"])
    return roots


def storage_lock_identities(config: dict) -> list[Path]:
    identities = []
    if config["_storage_local"] is not None:
        identities.append(config["_storage_local"])
    if config["_storage_git_checkout"] is not None:
        identities.append(config["_storage_git_checkout"])
    if config.get("_history_enabled"):
        identities.append(config["_history_root"])
    return identities


def git_storage_revision(config: dict) -> str | None:
    checkout = config["_storage_git_checkout"]
    url = config["_storage_git_url"]
    if checkout is None or url is None or not (checkout / ".git").is_dir():
        return None
    return _git_revision(checkout, url)


def _parse_storage(config: dict, base: Path) -> tuple[Path | None, str | None, Path | None]:
    if "catalog" in config:
        raise AgentBoxError("Use storage.local to configure local catalog storage")
    storage = config.get("storage")
    if storage is None:
        local_root = default_local_catalog()
        if local_root.is_symlink():
            raise AgentBoxError("Symlinked catalog roots are not supported: {}".format(local_root))
        if local_root.exists() and not local_root.is_dir():
            raise AgentBoxError("Catalog root is not a directory: {}".format(local_root))
        return local_root, None, None
    if not isinstance(storage, dict):
        raise AgentBoxError("storage must be an object")
    unknown = set(storage) - {"local", "git"}
    if unknown:
        raise AgentBoxError("Unknown storage option(s): {}".format(", ".join(sorted(unknown))))
    if not storage:
        raise AgentBoxError("storage must enable local, git, or both")

    local_value = storage.get("local")
    git_url = storage.get("git")
    if local_value is not None and (
        not isinstance(local_value, str) or not local_value.strip()
    ):
        raise AgentBoxError("storage.local must be a non-empty path string")
    if git_url is not None and (not isinstance(git_url, str) or not git_url.strip()):
        raise AgentBoxError("storage.git must be a non-empty repository URL")
    if local_value is None and git_url is None:
        raise AgentBoxError("storage must enable local, git, or both")
    if git_url is not None:
        if git_url.startswith("-") or any(character in git_url for character in ("\0", "\n", "\r")):
            raise AgentBoxError("storage.git contains an unsafe repository URL")
        credential_url = re.match(
            r"^[A-Za-z][A-Za-z0-9+.-]*://[^/]*:[^/]*@", git_url
        ) or re.match(r"^https?://[^/]*@", git_url, re.IGNORECASE)
        if "?" in git_url or "#" in git_url or credential_url:
            raise AgentBoxError(
                "storage.git must not contain embedded credentials, query parameters, or fragments"
            )
        repository_id = hashlib.sha256(git_url.encode("utf-8")).hexdigest()
        checkout = user_data_root() / "repositories" / repository_id
    else:
        checkout = None
    local_root = expand_path(local_value, base) if local_value is not None else None
    if local_root is not None and local_root.is_symlink():
        raise AgentBoxError("Symlinked catalog roots are not supported: {}".format(local_root))
    if local_root is not None and local_root.exists() and not local_root.is_dir():
        raise AgentBoxError("Catalog root is not a directory: {}".format(local_root))
    git_catalog = checkout / "catalog" if checkout is not None else None
    if git_catalog is not None and git_catalog.is_symlink():
        raise AgentBoxError("Symlinked catalog roots are not supported: {}".format(git_catalog))
    if git_catalog is not None and git_catalog.exists() and not git_catalog.is_dir():
        raise AgentBoxError("Catalog root is not a directory: {}".format(git_catalog))
    return local_root, git_url, checkout


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentBoxError("Cannot read {}: {}".format(path, exc))


def load_provider_definitions() -> list[dict]:
    path = Path(__file__).resolve().with_name("providers.json")
    document = load_json(path, {})
    if document.get("version") != PROVIDER_DEFINITIONS_VERSION:
        raise AgentBoxError("{} must declare version {}".format(path, PROVIDER_DEFINITIONS_VERSION))
    providers = document.get("providers")
    if not isinstance(providers, list) or not providers:
        raise AgentBoxError("{} must define providers".format(path))
    seen = set()
    for provider in providers:
        provider_id = provider.get("id") if isinstance(provider, dict) else None
        if (
            not isinstance(provider_id, str)
            or not CONFIG_NAME.fullmatch(provider_id)
            or provider_id in seen
        ):
            raise AgentBoxError("Provider IDs in {} must be valid and unique".format(path))
        seen.add(provider_id)
        if provider.get("mode") not in ("managed", "detection-only"):
            raise AgentBoxError("Invalid provider mode for {}".format(provider_id))
        if not isinstance(provider.get("name"), str) or not provider["name"].strip():
            raise AgentBoxError("Provider {} must have a name".format(provider_id))
        markers = provider.get("detect")
        if not isinstance(markers, list) or not all(
            isinstance(marker, str) and marker for marker in markers
        ):
            raise AgentBoxError("Provider {} must define detection paths".format(provider_id))
        if provider["mode"] != "managed":
            continue
        resources = provider.get("resources")
        if not isinstance(resources, dict) or set(resources) != {"skills", "commands"}:
            raise AgentBoxError("Managed provider {} must define skills and commands".format(provider_id))
        source_ids = set()
        for kind, resource in resources.items():
            if not isinstance(resource, dict) or not isinstance(resource.get("target"), str):
                raise AgentBoxError("Invalid {} resource for {}".format(kind, provider_id))
            sources = resource.get("sources")
            if not isinstance(sources, list):
                raise AgentBoxError("Invalid {} sources for {}".format(kind, provider_id))
            for source in sources:
                source_id = source.get("id") if isinstance(source, dict) else None
                if (
                    not isinstance(source_id, str)
                    or not CONFIG_NAME.fullmatch(source_id)
                    or source_id in source_ids
                    or not isinstance(source.get("path"), str)
                ):
                    raise AgentBoxError("Source IDs for {} must be valid and unique".format(provider_id))
                source_ids.add(source_id)
    return providers


def managed_provider_ids() -> list[str]:
    return [
        provider["id"]
        for provider in load_provider_definitions()
        if provider["mode"] == "managed"
    ]


def _optional_expanded_path(value: str) -> Path | None:
    if value.startswith("$XDG_CONFIG_HOME/"):
        return expand_provider_path(value, Path.home())
    variables = re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", value)
    if any(variable not in os.environ for variable in variables):
        return None
    return Path(os.path.expandvars(os.path.expanduser(value)))


def provider_detection(config: dict | None = None) -> list[dict]:
    settings = config.get("providers", {}) if config is not None else {}
    results = []
    for provider in load_provider_definitions():
        markers = [
            path
            for value in provider["detect"]
            if (path := _optional_expanded_path(value)) is not None
        ]
        detected = any(
            path.exists() and not path.is_symlink() and (path.is_dir() or path.is_file())
            for path in markers
        )
        configured = settings.get(provider["id"], {})
        resources = []
        if provider["mode"] == "managed":
            selected_resources = configured.get("resources", {})
            for kind, definition in provider["resources"].items():
                sources = []
                for source in definition["sources"]:
                    path = expand_provider_path(source["path"], Path.home())
                    sources.append(
                        {
                            "id": source["id"],
                            "path": str(path),
                            "available": path.is_dir() and not path.is_symlink(),
                        }
                    )
                resources.append(
                    {
                        "id": kind,
                        "name": definition.get("name", kind.title()),
                        "selected": bool(configured.get("enabled", detected))
                        and bool(
                            selected_resources.get(
                                kind, any(source["available"] for source in sources)
                            )
                        ),
                        "sources": sources,
                        "available": any(source["available"] for source in sources),
                    }
                )
        results.append(
            {
                "id": provider["id"],
                "name": provider["name"],
                "description": provider.get("description", ""),
                "mode": provider["mode"],
                "detected": detected,
                "selected": bool(configured.get("enabled", detected)),
                "markers": [str(path) for path in markers],
                "resources": resources,
                "resource_labels": provider.get("resource_labels", []),
            }
        )
    return results


def write_json(path: Path, value: dict, create_mode: int | None = None) -> None:
    existing_mode = None
    if path.exists() and not path.is_symlink() and path.is_file():
        existing_mode = stat.S_IMODE(path.stat().st_mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("{}.tmp-{}".format(path.name, uuid.uuid4().hex))
    try:
        desired_mode = existing_mode if existing_mode is not None else create_mode
        serialized = json.dumps(value, indent=2, sort_keys=True) + "\n"
        if desired_mode is not None:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                desired_mode,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
                temporary.chmod(desired_mode)
                destination.write(serialized)
        else:
            temporary.write_text(serialized, encoding="utf-8")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_config(path: Path, host_override: str | None = None) -> dict:
    if not path.exists():
        raise AgentBoxError("No configuration found at {}; run agentbox ui to complete setup".format(path))
    config = load_json(path, {})
    if config.get("version") != CONFIG_VERSION:
        raise AgentBoxError("{} must declare configuration version {}".format(path, CONFIG_VERSION))
    provider_settings = config.get("providers")
    if not isinstance(provider_settings, dict):
        raise AgentBoxError("{} must define provider settings".format(path))
    definitions = {
        provider["id"]: provider
        for provider in load_provider_definitions()
        if provider["mode"] == "managed"
    }
    unknown = set(provider_settings) - set(definitions)
    if unknown:
        raise AgentBoxError("Unknown provider setting(s): {}".format(", ".join(sorted(unknown))))
    base = path.parent
    host = (
        host_override
        or os.environ.get("AGENTBOX_HOST")
        or config.get("host")
        or socket.gethostname()
    )
    if not isinstance(host, str):
        raise AgentBoxError("Host namespace must be a string")
    host = re.sub(r"[^A-Za-z0-9._-]+", "-", host).strip(".-_")
    if not host or not CONFIG_NAME.fullmatch(host):
        raise AgentBoxError("Invalid host namespace: {!r}".format(host))
    local_root, git_url, git_checkout = _parse_storage(config, base)
    git_catalog = git_checkout / "catalog" if git_checkout is not None else None
    catalog_root = git_catalog or local_root
    if (
        git_catalog is not None
        and local_root is not None
        and not git_catalog.exists()
        and local_root.exists()
    ):
        catalog_root = local_root
    if catalog_root is None:  # Defensive: _parse_storage requires at least one destination.
        raise AgentBoxError("No storage destination is configured")
    if local_root is not None and local_root.is_symlink():
        raise AgentBoxError("Symlinked catalog roots are not supported: {}".format(local_root))
    if git_catalog is not None and git_catalog.is_symlink():
        raise AgentBoxError("Symlinked catalog roots are not supported: {}".format(git_catalog))
    config["_base"] = base
    config["_host"] = host
    config["_storage_local"] = local_root
    config["_storage_git_url"] = git_url
    config["_storage_git_checkout"] = git_checkout
    config["_storage_git_catalog"] = git_catalog
    history = config.get("history", {})
    if not isinstance(history, dict) or set(history) - {"max_revisions"}:
        raise AgentBoxError("history must contain only max_revisions")
    max_revisions = history.get("max_revisions")
    if max_revisions is not None and (
        not isinstance(max_revisions, int)
        or isinstance(max_revisions, bool)
        or max_revisions < 1
    ):
        raise AgentBoxError("history.max_revisions must be a positive integer")
    history_enabled, history_root = _history_storage(local_root, git_url)
    config["_history_enabled"] = history_enabled
    config["_history_root"] = history_root
    config["_history_max_revisions"] = max_revisions
    config["_catalog_root"] = catalog_root
    config["_catalog"] = config["_catalog_root"] / host
    if config["_catalog"].is_symlink():
        raise AgentBoxError("Symlinked host catalogs are not supported: {}".format(config["_catalog"]))
    state_file = config.get("state_file")
    safety_backups = config.get("safety_backups")
    config["_state_file"] = (
        expand_path(state_file, base) if state_file is not None else user_state_root() / "state.json"
    )
    config["_safety_backups"] = (
        expand_path(safety_backups, base)
        if safety_backups is not None
        else user_state_root() / "backups"
    )
    tools = {}
    for tool_name, definition in definitions.items():
        setting = provider_settings.get(tool_name, {})
        if not isinstance(setting, dict) or not isinstance(setting.get("enabled", False), bool):
            raise AgentBoxError("Invalid provider setting for {}".format(tool_name))
        resource_settings = setting.get("resources", {})
        if not isinstance(resource_settings, dict) or set(resource_settings) - {
            "skills",
            "commands",
        }:
            raise AgentBoxError("Invalid resource settings for {}".format(tool_name))
        if not all(isinstance(value, bool) for value in resource_settings.values()):
            raise AgentBoxError("Resource settings for {} must be boolean".format(tool_name))
        if not setting.get("enabled", False) and any(resource_settings.values()):
            raise AgentBoxError(
                "Disabled provider {} cannot have enabled resources".format(tool_name)
            )
        if setting.get("enabled", False) and not any(resource_settings.values()):
            raise AgentBoxError("Enabled provider {} must enable a resource".format(tool_name))
        if not setting.get("enabled", False) or not any(resource_settings.values()):
            continue
        tool = {"_name": definition["name"], "_description": definition.get("description", "")}
        for kind in ("skills", "commands"):
            resource = definition["resources"][kind]
            tool[kind] = {
                "_enabled": bool(resource_settings.get(kind, False)),
                "_name": resource.get("name", kind.title()),
                "_target": expand_provider_path(resource["target"], base),
                "_sources": [
                    {
                        "id": source["id"],
                        "path": expand_provider_path(source["path"], base),
                    }
                    for source in resource["sources"]
                ],
            }
        tools[tool_name] = tool
    if not tools:
        raise AgentBoxError("{} must enable at least one provider resource".format(path))
    config["tools"] = tools
    config["_provider_definitions"] = definitions
    return config


def config_for_host(config: dict, host: str) -> dict:
    if not CONFIG_NAME.fullmatch(host):
        raise AgentBoxError("Invalid host namespace: {!r}".format(host))
    selected = dict(config)
    selected["_host"] = host
    selected["_catalog"] = config["_catalog_root"] / host
    if selected["_catalog"].is_symlink():
        raise AgentBoxError("Symlinked host catalogs are not supported: {}".format(selected["_catalog"]))
    return selected


def catalog_hosts(config: dict) -> list[str]:
    root = config["_catalog_root"]
    if not root.exists():
        return []
    if not root.is_dir():
        raise AgentBoxError("Catalog root is not a directory: {}".format(root))
    hosts = []
    for entry in sorted(root.iterdir()):
        if entry.is_symlink():
            raise AgentBoxError("Symlinked host catalogs are not supported: {}".format(entry))
        if not entry.is_dir():
            continue
        if not CONFIG_NAME.fullmatch(entry.name):
            raise AgentBoxError("Invalid host catalog name: {}".format(entry.name))
        hosts.append(entry.name)
    return hosts


def redacted_git_url(url: str) -> str:
    return re.sub(r"(?<=://)([^/:@]+):[^/@]+@", r"\1:<credentials>@", url)


def _outside_frozen_bundle(value: str, bundle_root: Path) -> bool:
    if not value:
        return False
    try:
        return not Path(value).resolve().is_relative_to(bundle_root)
    except OSError:
        return True


@contextmanager
def external_program_environment():
    """Yield an environment safe for system programs launched by PyInstaller."""
    environment = os.environ.copy()
    if not getattr(sys, "frozen", False):
        yield environment
        return

    bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)).resolve()
    for variable in ("PATH", "DYLD_LIBRARY_PATH"):
        if variable in environment:
            environment[variable] = os.pathsep.join(
                part
                for part in environment[variable].split(os.pathsep)
                if _outside_frozen_bundle(part, bundle_root)
            )
    for variable in ("LD_LIBRARY_PATH", "LIBPATH"):
        original = environment.get(f"{variable}_ORIG")
        if original is None:
            environment.pop(variable, None)
        else:
            environment[variable] = original

    set_dll_directory = None
    if sys.platform == "win32":  # pragma: no cover - exercised by release smoke tests.
        import ctypes

        set_dll_directory = ctypes.windll.kernel32.SetDllDirectoryW
        set_dll_directory.argtypes = [ctypes.c_wchar_p]
        set_dll_directory.restype = ctypes.c_bool
        set_dll_directory(None)
    try:
        yield environment
    finally:
        if set_dll_directory is not None:
            set_dll_directory(str(bundle_root))


def _git_error_output(result: subprocess.CompletedProcess[str], url: str) -> str:
    output = (result.stderr or result.stdout or "Git command failed").strip()
    return output.replace(url, "<repository>")[:2000]


def _external_executable(name: str, environment: dict[str, str]) -> str | None:
    suffixes = [""]
    if sys.platform == "win32":  # pragma: no cover - exercised by release smoke tests.
        suffixes.extend(environment.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep))
    for value in environment.get("PATH", os.defpath).split(os.pathsep):
        if not value:
            continue
        directory = Path(value.strip('"')).expanduser()
        if not directory.is_absolute():
            continue
        for suffix in suffixes:
            candidate = directory / f"{name}{suffix.lower()}"
            if candidate.is_file() and (sys.platform == "win32" or os.access(candidate, os.X_OK)):
                return str(candidate.resolve())
    return None


def _run_git(
    checkout: Path | None,
    arguments: Sequence[str],
    url: str,
    allowed: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    try:
        with external_program_environment() as environment:
            git_executable = _external_executable("git", environment)
            if git_executable is None:
                raise FileNotFoundError
            command = [git_executable]
            if checkout is not None:
                command.extend(("-C", str(checkout)))
            command.extend(arguments)
            environment["GIT_TERMINAL_PROMPT"] = "0"
            environment["GIT_ATTR_NOSYSTEM"] = "1"
            environment["GIT_CONFIG_COUNT"] = "2"
            environment["GIT_CONFIG_KEY_0"] = "core.autocrlf"
            environment["GIT_CONFIG_VALUE_0"] = "false"
            environment["GIT_CONFIG_KEY_1"] = "core.attributesFile"
            environment["GIT_CONFIG_VALUE_1"] = os.devnull
            environment["LC_ALL"] = "C"
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                check=False,
                timeout=GIT_TIMEOUT_SECONDS,
                env=environment,
            )
    except FileNotFoundError:
        raise AgentBoxError("Git storage requires the git executable") from None
    except subprocess.TimeoutExpired:
        raise AgentBoxError("Git storage operation timed out for <repository>") from None
    except OSError as exc:
        raise AgentBoxError("Cannot run Git for <repository>: {}".format(exc)) from exc
    if result.returncode not in allowed:
        raise AgentBoxError(_git_error_output(result, url))
    return result


def _git_ref_exists(checkout: Path, reference: str, url: str) -> bool:
    result = _run_git(
        checkout,
        ("show-ref", "--verify", "--quiet", reference),
        url,
        allowed=(0, 1),
    )
    return result.returncode == 0


def _git_revision(checkout: Path, url: str) -> str | None:
    result = _run_git(
        checkout,
        ("rev-parse", "--verify", "HEAD"),
        url,
        allowed=(0, 128),
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _validate_managed_repository(checkout: Path) -> None:
    if any(path.is_file() or path.is_symlink() for path in checkout.rglob(".gitattributes")):
        raise AgentBoxError("Managed Git repositories must not define .gitattributes files")
    catalog = checkout / "catalog"
    if catalog.exists() and any(catalog.rglob(".git")):
        raise AgentBoxError("Managed Git catalogs must not contain embedded repositories")


def _validate_git_tree(checkout: Path, reference: str, url: str) -> None:
    tree = _run_git(checkout, ("ls-tree", "-r", reference), url)
    for line in tree.stdout.splitlines():
        metadata, _, path = line.partition("\t")
        mode = metadata.split(" ", 1)[0]
        parts = Path(path).parts
        if ".gitattributes" in parts:
            raise AgentBoxError("Managed Git repositories must not define .gitattributes files")
        if mode == "160000" and parts and parts[0] == "catalog":
            raise AgentBoxError("Managed Git catalogs must not contain embedded repositories")
    index = _run_git(checkout, ("ls-files", "--stage", "--", "catalog"), url)
    if any(line.startswith("160000 ") for line in index.stdout.splitlines()):
        raise AgentBoxError("Managed Git catalogs must not contain embedded repositories")


def _ensure_git_checkout(config: dict) -> tuple[str, str | None, bool]:
    checkout = config["_storage_git_checkout"]
    url = config["_storage_git_url"]
    if checkout is None or url is None:
        raise AgentBoxError("Git storage is not configured")
    parent = checkout.parent
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise AgentBoxError("Cannot create managed Git storage: {}".format(exc)) from exc
    if parent.is_symlink() or not parent.is_dir():
        raise AgentBoxError("Managed Git storage root is not a regular directory: {}".format(parent))

    if not checkout.exists():
        temporary = checkout.with_name(".{}-clone-{}".format(checkout.name, uuid.uuid4().hex))
        try:
            _run_git(
                None,
                (
                    "clone",
                    "--no-local",
                    "--no-tags",
                    "--no-checkout",
                    "--origin",
                    "origin",
                    "--",
                    url,
                    str(temporary),
                ),
                url,
            )
            cloned_revision = _git_revision(temporary, url)
            if cloned_revision is not None:
                _validate_git_tree(temporary, "HEAD", url)
                _run_git(temporary, ("read-tree", "--reset", "-u", "HEAD"), url)
            temporary.rename(checkout)
        except Exception:
            if temporary.exists() or temporary.is_symlink():
                remove_path(temporary)
            raise
    if checkout.is_symlink() or not checkout.is_dir():
        raise AgentBoxError("Managed Git checkout is not a regular directory: {}".format(checkout))
    git_directory = checkout / ".git"
    if git_directory.is_symlink() or not git_directory.is_dir():
        raise AgentBoxError("Managed storage is not a Git working tree: {}".format(checkout))

    configured_remote = _run_git(checkout, ("remote", "get-url", "origin"), url).stdout.strip()
    if configured_remote != url:
        raise AgentBoxError("Managed Git checkout origin does not match the configured repository")
    dirty = _run_git(checkout, ("status", "--porcelain", "--untracked-files=all"), url)
    if dirty.stdout.strip():
        raise AgentBoxError("Managed Git checkout contains uncommitted changes")

    branch = _run_git(checkout, ("symbolic-ref", "--quiet", "--short", "HEAD"), url).stdout.strip()
    if not branch:
        raise AgentBoxError("Managed Git checkout must use an attached branch")
    _run_git(checkout, ("fetch", "--prune", "origin"), url)
    remote_ref = "refs/remotes/origin/{}".format(branch)
    local_revision = _git_revision(checkout, url)
    pending = False
    if _git_ref_exists(checkout, remote_ref, url):
        _validate_git_tree(checkout, remote_ref, url)
        if local_revision is not None:
            local_is_ancestor = _run_git(
                checkout,
                ("merge-base", "--is-ancestor", "HEAD", remote_ref),
                url,
                allowed=(0, 1),
            )
            if local_is_ancestor.returncode == 0:
                _run_git(checkout, ("merge", "--ff-only", "--quiet", remote_ref), url)
            else:
                remote_is_ancestor = _run_git(
                    checkout,
                    ("merge-base", "--is-ancestor", remote_ref, "HEAD"),
                    url,
                    allowed=(0, 1),
                )
                if remote_is_ancestor.returncode != 0:
                    raise AgentBoxError(
                        "Managed Git checkout has diverged from origin/{}".format(branch)
                    )
                pending = True
        else:
            _run_git(checkout, ("update-ref", "refs/heads/{}".format(branch), remote_ref), url)
            _run_git(checkout, ("read-tree", "--reset", "-u", "HEAD"), url)
    elif local_revision is not None:
        pending = True
    _validate_managed_repository(checkout)
    revision = _git_revision(checkout, url)
    return branch, revision, pending


def catalog_fingerprint(root: Path) -> tuple[bool, str]:
    if root.is_symlink():
        raise AgentBoxError("Symlinked catalog roots are not supported: {}".format(root))
    if not root.exists():
        return True, hashlib.sha256(b"").hexdigest()
    if not root.is_dir():
        raise AgentBoxError("Catalog root is not a directory: {}".format(root))
    entries = sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
    if not entries:
        return True, hashlib.sha256(b"").hexdigest()
    digest = hashlib.sha256()
    for entry in entries:
        relative = entry.relative_to(root).as_posix()
        if entry.is_symlink():
            raise AgentBoxError("Symlinked catalog paths are not supported: {}".format(entry))
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if entry.is_dir():
            digest.update(b"directory\0")
            continue
        if not entry.is_file():
            raise AgentBoxError("Unsupported catalog path: {}".format(entry))
        digest.update(b"file\0")
        digest.update(b"executable\0" if entry.stat().st_mode & 0o111 else b"regular\0")
        with entry.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
    return False, digest.hexdigest()


def _select_catalog_root(config: dict, root: Path) -> None:
    config["_catalog_root"] = root
    config["_catalog"] = root / config["_host"]
    if config["_catalog"].is_symlink():
        raise AgentBoxError("Symlinked host catalogs are not supported: {}".format(config["_catalog"]))


def prepare_storage(
    config: dict,
    report: Reporter = console_report,
) -> StorageSession:
    local_root = config["_storage_local"]
    git_root = config["_storage_git_catalog"]
    if git_root is None:
        if local_root is None:
            raise AgentBoxError("No storage destination is configured")
        _select_catalog_root(config, local_root)
        return StorageSession(config, local_root, None, local_root)

    branch, revision, pending = _ensure_git_checkout(config)
    report(
        OperationEvent(
            "storage",
            "GIT READY {}@{}".format(branch, revision[:8] if revision else "empty"),
        )
    )
    if pending:
        report(OperationEvent("storage", "GIT PENDING COMMIT AWAITS CONFIRMED BACKUP"))
    if git_root.is_symlink():
        raise AgentBoxError("Symlinked catalog roots are not supported: {}".format(git_root))
    if local_root is None:
        _select_catalog_root(config, git_root)
        return StorageSession(
            config,
            None,
            git_root,
            git_root,
            git_branch=branch,
            git_revision=revision,
            git_pending=pending,
        )

    local_empty, local_hash = catalog_fingerprint(local_root)
    git_empty, git_hash = catalog_fingerprint(git_root)
    initialize = None
    if not local_empty and not git_empty and local_hash != git_hash:
        raise AgentBoxError(
            "Local and Git catalogs differ; make them equal or deactivate one storage destination"
        )
    if not local_empty and git_empty:
        canonical = local_root
        initialize = "git-from-local"
        report(OperationEvent("storage-init", "INITIALIZE GIT STORAGE FROM LOCAL CATALOG"))
    else:
        canonical = git_root
        if local_empty and not git_empty:
            initialize = "local-from-git"
            report(OperationEvent("storage-init", "INITIALIZE LOCAL STORAGE FROM GIT CATALOG"))
    _select_catalog_root(config, canonical)
    return StorageSession(
        config,
        local_root,
        git_root,
        canonical,
        initialize=initialize,
        git_branch=branch,
        git_revision=revision,
        git_pending=pending,
    )


def hash_generated_skill(content: bytes) -> str:
    digest = hashlib.sha256()
    update_hash_entry(digest, ".", "directory", _GENERATED_SKILL_DIRECTORY_MODE, b"")
    update_hash_entry(digest, "SKILL.md", "file", _GENERATED_SKILL_FILE_MODE, content)
    return digest.hexdigest()


def update_hash_entry(
    digest: "hashlib._Hash", relative: str, kind: str, mode: int, content: bytes
) -> None:
    digest.update(relative.encode("utf-8"))
    digest.update(b"\0")
    digest.update(kind.encode("ascii"))
    digest.update(b"\0")
    digest.update(("{:04o}".format(mode)).encode("ascii"))
    digest.update(b"\0")
    digest.update(content)
    digest.update(b"\0")


def validate_regular_payload(path: Path) -> None:
    if path.is_symlink():
        raise AgentBoxError("Symlinked artifacts are not supported: {}".format(path))
    if path.is_file():
        return
    if not path.is_dir():
        raise AgentBoxError("Unsupported payload: {}".format(path))
    for current, directories, files in os.walk(str(path), followlinks=False):
        current_path = Path(current)
        for name in directories + files:
            child = current_path / name
            if child.is_symlink():
                raise AgentBoxError("Symlinked artifact content is not supported: {}".format(child))
            if not child.is_dir() and not child.is_file():
                raise AgentBoxError("Unsupported artifact content: {}".format(child))


def validate_git_payload(path: Path) -> None:
    if path.is_file():
        if path.name == ".gitattributes":
            raise AgentBoxError("Git storage does not support artifact content named {}".format(path))
        return
    for child in path.rglob("*"):
        if child.name in (".git", ".gitattributes"):
            raise AgentBoxError("Git storage does not support artifact content named {}".format(child))


def hash_path(path: Path) -> str:
    validate_regular_payload(path)
    digest = hashlib.sha256()
    if path.is_file():
        update_hash_entry(
            digest,
            path.name,
            "file",
            stat.S_IMODE(path.stat().st_mode),
            path.read_bytes(),
        )
        return digest.hexdigest()
    update_hash_entry(
        digest, ".", "directory", stat.S_IMODE(path.stat().st_mode), b""
    )
    for child in sorted(path.rglob("*")):
        relative = child.relative_to(path).as_posix()
        if child.is_dir():
            update_hash_entry(
                digest,
                relative,
                "directory",
                stat.S_IMODE(child.stat().st_mode),
                b"",
            )
        elif child.is_file():
            update_hash_entry(
                digest,
                relative,
                "file",
                stat.S_IMODE(child.stat().st_mode),
                child.read_bytes(),
            )
    return digest.hexdigest()


def _path_matches_fingerprint(path: Path, fingerprint: str) -> bool:
    return path.exists() and not path.is_symlink() and hash_path(path) == fingerprint


def split_frontmatter(content: str) -> tuple[dict[str, str], str]:
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, content
    closing = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            closing = index
            break
    if closing is None:
        return {}, content

    metadata = {}
    raw = lines[1:closing]
    index = 0
    while index < len(raw):
        match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):(?:\s*(.*))?$", raw[index].rstrip("\r\n"))
        if not match:
            index += 1
            continue
        key, value = match.group(1), (match.group(2) or "").strip()
        if value in (">", ">-", ">+", "|", "|-", "|+"):
            block = []
            index += 1
            while index < len(raw):
                line = raw[index].rstrip("\r\n")
                if line and not line[0].isspace():
                    break
                block.append(line.strip())
                index += 1
            metadata[key] = ("\n" if value.startswith("|") else " ").join(block).strip()
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            try:
                value = ast.literal_eval(value)
            except (SyntaxError, ValueError):
                value = value[1:-1]
        metadata[key] = str(value)
        index += 1
    return metadata, "".join(lines[closing + 1 :])


def read_skill_name(skill_dir: Path) -> str:
    skill_file = skill_dir / "SKILL.md"
    try:
        metadata, _ = split_frontmatter(skill_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise AgentBoxError("Cannot read {}: {}".format(skill_file, exc))
    name = metadata.get("name", skill_dir.name).strip()
    if not SKILL_NAME.fullmatch(name):
        raise AgentBoxError("Invalid skill name {!r} in {}".format(name, skill_file))
    return name


def command_name(relative_path: str) -> str:
    path = Path(relative_path)
    raw = "-".join(path.with_suffix("").parts).lower()
    name = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    if not name:
        raise AgentBoxError("Cannot derive a skill name from {}".format(relative_path))
    return name


def walk_skill_dirs(root: Path) -> Iterable[Path]:
    def walk_error(error: OSError) -> None:
        raise AgentBoxError("Cannot scan skill source {}: {}".format(root, error))

    for current, directories, files in os.walk(
        str(root), followlinks=False, onerror=walk_error
    ):
        current_path = Path(current)
        for name in list(directories):
            candidate = current_path / name
            if candidate.is_symlink() and (candidate / "SKILL.md").is_file():
                yield candidate
        directories[:] = [
            name
            for name in directories
            if name not in (".git", ".system", "__pycache__")
            and not (current_path / name).is_symlink()
        ]
        if "SKILL.md" in files:
            yield Path(current)
            directories[:] = []


def source_record(source: Source) -> dict:
    return {"id": source.source_id, "relative_path": source.relative_path}


def safe_relative_path(value: str) -> Path:
    if not isinstance(value, str) or not value:
        raise AgentBoxError("Manifest paths must be non-empty strings")
    path = Path(value)
    if path == Path(".") or path.is_absolute() or ".." in path.parts:
        raise AgentBoxError("Unsafe relative path in manifest: {}".format(value))
    return path


def checked_join(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise AgentBoxError("Unsafe relative path: {}".format(relative))
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise AgentBoxError("Refusing path through symlink: {}".format(current))
    candidate = root / relative
    root_real = root.resolve(strict=False)
    parent_real = candidate.parent.resolve(strict=False)
    try:
        common = Path(os.path.commonpath([str(root_real), str(parent_real)]))
    except ValueError:
        raise AgentBoxError("Path escapes configured root: {}".format(candidate))
    if common != root_real:
        raise AgentBoxError("Path escapes configured root: {}".format(candidate))
    return candidate


def require_regular_root(root: Path, label: str) -> None:
    if root.is_symlink():
        raise AgentBoxError("Symlinked {} roots are not supported: {}".format(label, root))
    if root.exists() and not root.is_dir():
        raise AgentBoxError("{} root is not a directory: {}".format(label.capitalize(), root))


def load_state(config: dict) -> dict:
    state = load_json(config["_state_file"], {"version": VERSION, "deployments": {}})
    if state.get("version") != VERSION:
        return {"version": VERSION, "deployments": {}}
    state.setdefault("deployments", {})
    return state


def receipt_for(state: dict, physical_path: Path) -> dict | None:
    return state.get("deployments", {}).get(str(physical_path.absolute()))


def artifact_portable_fingerprint(artifact: Artifact) -> str:
    if artifact.kind == "skill":
        return hash_path(artifact.payload)
    return hash_generated_skill(convert_command(artifact))


def receipt_has_current_origin(config: dict, receipt: dict) -> bool:
    expected = receipt.get("hash")
    for origin in receipt.get("origins", []):
        tool = origin.get("tool")
        kind = origin.get("kind")
        host = origin.get("host", config["_host"])
        if (
            tool not in config["tools"]
            or kind not in ("skill", "command")
            or not isinstance(host, str)
            or not CONFIG_NAME.fullmatch(host)
        ):
            continue
        try:
            relative = safe_relative_path(origin.get("path"))
            revision = origin.get("revision")

            def matches_origin(source_config: dict) -> bool:
                host_root = source_config["_catalog_root"] / host
                if host_root.is_symlink():
                    return False
                payload = checked_join(host_root / tool, relative)
                if not payload.exists():
                    return False
                name = read_skill_name(payload) if kind == "skill" else command_name(
                    payload.relative_to(host_root / tool / "commands").as_posix()
                )
                artifact = Artifact(tool, kind, name, relative.as_posix(), payload, host=host)
                return artifact_portable_fingerprint(artifact) == expected

            if revision is None:
                if matches_origin(config):
                    return True
            elif isinstance(revision, str) and REVISION_ID.fullmatch(revision):
                with materialized_catalog_revision(config, revision) as historical_root:
                    historical = dict(config)
                    _select_catalog_root(historical, historical_root)
                    if matches_origin(historical):
                        return True
        except (AgentBoxError, ValueError):
            continue
    return False


def collect_tool_artifacts(
    config: dict,
    tool_name: str,
    state: dict,
    include_derived: bool,
    portable_index: dict[tuple[str, str], list[dict]],
    own_catalog_keys: set,
    report: Reporter = console_report,
) -> tuple[list[Artifact], list[str], set, list[str]]:
    tool = config["tools"][tool_name]
    artifacts: dict[tuple[str, str], Artifact] = {}
    errors = []
    available_sources = set()
    receipts_to_clear = []

    for kind in ("skills", "commands"):
        if not tool[kind]["_enabled"]:
            continue
        for configured_source in tool[kind]["_sources"]:
            root = configured_source["path"]
            if not root.exists():
                continue
            if root.is_symlink() or not root.is_dir():
                errors.append(
                    "Configured source is not a regular directory: {}".format(root)
                )
                continue
            if not os.access(str(root), os.R_OK | os.X_OK):
                errors.append("Configured source is not readable: {}".format(root))
                continue
            artifact_kind = "skill" if kind == "skills" else "command"
            available_sources.add((artifact_kind, configured_source["id"]))
            if kind == "skills":
                entries = [(skill, skill.relative_to(root).as_posix()) for skill in walk_skill_dirs(root)]
            else:
                entries = [
                    (command, command.relative_to(root).as_posix())
                    for command in sorted(root.rglob("*.md"))
                    if command.is_file()
                ]
            for physical_path, relative_path in entries:
                receipt = receipt_for(state, physical_path)
                if receipt:
                    current_hash = hash_path(physical_path)
                    if current_hash != receipt.get("hash") and not include_derived:
                        errors.append(
                            "{} was restored by agentbox and then modified; use --include-derived "
                            "to back it up as an independent artifact".format(physical_path)
                        )
                        continue
                    if current_hash == receipt.get("hash") and receipt_has_current_origin(
                        config, receipt
                    ):
                        report(
                            OperationEvent(
                                "skip-derived",
                                "SKIP derived {}".format(physical_path),
                                tool_name,
                                str(physical_path),
                            )
                        )
                        continue
                    receipts_to_clear.append(str(physical_path.absolute()))

                if kind == "skills":
                    name = read_skill_name(physical_path)
                    catalog_path = "skills/{}".format(name)
                else:
                    name = command_name(relative_path)
                    catalog_path = "commands/{}".format(relative_path)
                current_key = (artifact_kind, catalog_path)
                if artifact_kind == "skill":
                    current_hash = hash_path(physical_path)
                    matches_portable = (name, current_hash) in portable_index
                    if matches_portable and not include_derived:
                        if current_key in own_catalog_keys:
                            own_payload = checked_join(
                                config["_catalog"] / tool_name,
                                safe_relative_path(catalog_path),
                            )
                            if not own_payload.exists() or hash_path(own_payload) != current_hash:
                                errors.append(
                                    "{} matches another catalog artifact but differs from its own "
                                    "backup; restore lineage is missing. Use --include-derived only "
                                    "if this copy should replace the {} backup.".format(
                                        physical_path, tool_name
                                    )
                                )
                                continue
                        else:
                            report(
                                OperationEvent(
                                    "skip-duplicate",
                                    "SKIP duplicate {}".format(physical_path),
                                    tool_name,
                                    str(physical_path),
                                )
                            )
                            receipts_to_clear.append(str(physical_path.absolute()))
                            continue
                source = Source(
                    configured_source["id"], root, relative_path, physical_path
                )
                existing = artifacts.get(current_key)
                if existing:
                    if hash_path(existing.payload) != hash_path(physical_path):
                        errors.append(
                            "Conflicting {} artifacts for {}: {} and {}".format(
                                artifact_kind, name, existing.payload, physical_path
                            )
                        )
                    else:
                        existing.sources.append(source)
                else:
                    artifacts[current_key] = Artifact(
                        tool_name,
                        artifact_kind,
                        name,
                        catalog_path,
                        physical_path,
                        [source],
                    )
    return list(artifacts.values()), errors, available_sources, receipts_to_clear


def manifest_path(config: dict, tool_name: str) -> Path:
    return config["_catalog"] / tool_name / "manifest.json"


def load_manifest(config: dict, tool_name: str) -> dict:
    tool_root = config["_catalog"] / tool_name
    path = manifest_path(config, tool_name)
    if tool_root.is_symlink() or path.is_symlink():
        raise AgentBoxError("Symlinked catalog paths are not supported for {}".format(tool_name))
    manifest = load_json(path, {"version": VERSION, "artifacts": []})
    if manifest.get("version") != VERSION:
        raise AgentBoxError("Unsupported manifest version for {}".format(tool_name))
    artifacts = manifest.setdefault("artifacts", [])
    if not isinstance(artifacts, list):
        raise AgentBoxError("Manifest artifacts for {} must be a list".format(tool_name))
    seen_paths = set()
    for entry in artifacts:
        if not isinstance(entry, dict) or entry.get("kind") not in ("skill", "command"):
            raise AgentBoxError("Invalid artifact entry in {} manifest".format(tool_name))
        name = entry.get("name")
        if not isinstance(name, str) or not SKILL_NAME.fullmatch(name):
            raise AgentBoxError("Invalid artifact name in {} manifest: {!r}".format(tool_name, name))
        entry_path = safe_relative_path(entry.get("path"))
        if entry_path in seen_paths:
            raise AgentBoxError("Duplicate artifact path in {} manifest: {}".format(tool_name, entry_path))
        seen_paths.add(entry_path)
        if entry["kind"] == "skill":
            expected = Path("skills") / name
            if entry_path != expected:
                raise AgentBoxError(
                    "Skill manifest path must be {}: {}".format(expected, entry_path)
                )
        elif entry_path.parts[0] != "commands" or entry_path.suffix != ".md":
            raise AgentBoxError("Command manifest path must be under commands/: {}".format(entry_path))
        sources = entry.get("sources", [])
        if not isinstance(sources, list):
            raise AgentBoxError("Artifact sources must be a list: {}".format(entry_path))
        for source in sources:
            if (
                not isinstance(source, dict)
                or not isinstance(source.get("id"), str)
                or not CONFIG_NAME.fullmatch(source["id"])
            ):
                raise AgentBoxError("Invalid source entry for {}".format(entry_path))
            safe_relative_path(source.get("relative_path"))
    return manifest


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(str(path))


def catalog_install_paths(destination: Path, token: str) -> tuple[Path, Path]:
    previous = destination.with_name(".{}.agentbox-old-{}".format(destination.name, token))
    staged = destination.with_name(".{}.agentbox-new-{}".format(destination.name, token))
    return previous, staged


def install_staged(staged: Path, destination: Path, token: str) -> None:
    previous, expected_staged = catalog_install_paths(destination, token)
    if staged != expected_staged:
        raise AgentBoxError("Staged catalog path does not match its installation token")
    had_destination = destination.exists() or destination.is_symlink()
    if had_destination:
        destination.rename(previous)
    try:
        staged.rename(destination)
    except BaseException:
        if had_destination and previous.exists():
            previous.rename(destination)
        raise
    if had_destination and previous.exists():
        remove_path(previous)


@contextmanager
def _staged_destination(destination: Path, token: str | None = None):
    destination.parent.mkdir(parents=True, exist_ok=True)
    token = token or uuid.uuid4().hex
    _, staged = catalog_install_paths(destination, token)
    try:
        yield staged, token
    finally:
        if staged.exists() or staged.is_symlink():
            remove_path(staged)


def copy_exact(source: Path, destination: Path, token: str | None = None) -> None:
    validate_regular_payload(source)
    with _staged_destination(destination, token) as (staged, install_token):
        if source.is_dir():
            shutil.copytree(str(source), str(staged), symlinks=False)
        else:
            shutil.copy2(str(source), str(staged))
        install_staged(staged, destination, install_token)


def replace_catalog(source: Path, destination: Path, token: str | None = None) -> None:
    if source.exists():
        copy_exact(source, destination, token)
    elif destination.exists() or destination.is_symlink():
        remove_path(destination)


@contextmanager
def staged_catalog(source: Path):
    with tempfile.TemporaryDirectory(prefix="agentbox-catalog-") as temporary:
        staged = Path(temporary) / "catalog"
        if source.exists():
            copy_exact(source, staged)
        yield staged


@contextmanager
def optional_catalog_snapshot(source: Path | None):
    if source is None:
        yield None, False
        return
    existed = source.exists()
    with staged_catalog(source) as snapshot:
        yield snapshot, existed


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _regular_history_root(config: dict, *, create: bool) -> Path:
    if not config.get("_history_enabled") or config.get("_history_root") is None:
        raise AgentBoxError("Local catalog history is available only with local-only storage")
    root = config["_history_root"]
    if create:
        root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise AgentBoxError("History root is not a regular directory: {}".format(root))
    return root


def _history_directory(root: Path, name: str, *, create: bool) -> Path:
    directory = root / name
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise AgentBoxError("History path is not a regular directory: {}".format(directory))
    return directory


def _stream_sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _catalog_snapshot_document(root: Path) -> tuple[dict, str, dict[str, Path]]:
    if root.is_symlink():
        raise AgentBoxError("Symlinked catalog roots are not supported: {}".format(root))
    if not root.exists():
        document = {"version": HISTORY_VERSION, "entries": []}
        return document, hashlib.sha256(_canonical_json(document)).hexdigest(), {}
    if not root.is_dir():
        raise AgentBoxError("Catalog root is not a directory: {}".format(root))

    entries = []
    objects: dict[str, Path] = {}

    def collect(directory: Path) -> None:
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            relative = child.relative_to(root).as_posix()
            metadata = child.lstat()
            mode = stat.S_IMODE(metadata.st_mode)
            if child.is_symlink():
                raise AgentBoxError("Symlinked catalog paths are not supported: {}".format(child))
            if child.is_dir():
                entries.append({"path": relative, "kind": "directory", "mode": mode})
                collect(child)
                continue
            if not child.is_file():
                raise AgentBoxError("Unsupported catalog path: {}".format(child))
            digest, size = _stream_sha256(child)
            entries.append(
                {
                    "path": relative,
                    "kind": "file",
                    "mode": mode,
                    "hash": digest,
                    "size": size,
                }
            )
            objects.setdefault(digest, child)

    collect(root)
    entries.sort(key=lambda item: item["path"])
    document = {"version": HISTORY_VERSION, "entries": entries}
    snapshot = hashlib.sha256(_canonical_json(document)).hexdigest()
    return document, snapshot, objects


def _write_immutable(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise AgentBoxError("History path is not a regular directory: {}".format(path.parent))
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise AgentBoxError("History entry is not a regular file: {}".format(path))
        if path.read_bytes() != content:
            raise AgentBoxError("Immutable history entry changed: {}".format(path))
        return
    temporary = path.with_name("{}.tmp-{}".format(path.name, uuid.uuid4().hex))
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_history_object(path: Path, source: Path, digest: str, size: int) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise AgentBoxError("History object is not a regular file: {}".format(path))
        actual_digest, actual_size = _stream_sha256(path)
        if actual_digest != digest or actual_size != size:
            raise AgentBoxError("History object failed integrity verification: {}".format(path))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise AgentBoxError("History path is not a regular directory: {}".format(path.parent))
    temporary = path.with_name("{}.tmp-{}".format(path.name, uuid.uuid4().hex))
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        written = hashlib.sha256()
        total = 0
        with source.open("rb") as input_file, os.fdopen(descriptor, "wb") as output_file:
            while chunk := input_file.read(1024 * 1024):
                written.update(chunk)
                total += len(chunk)
                output_file.write(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())
        if written.hexdigest() != digest or total != size:
            raise AgentBoxError("Catalog changed while preparing its history revision")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _catalog_artifact_count(root: Path) -> int:
    total = 0
    if not root.exists():
        return 0
    for manifest in sorted(root.glob("*/*/manifest.json")):
        value = load_json(manifest, {})
        artifacts = value.get("artifacts") if isinstance(value, dict) else None
        if not isinstance(artifacts, list):
            raise AgentBoxError("Invalid catalog manifest while creating history: {}".format(manifest))
        total += len(artifacts)
    return total


def _prepare_catalog_revision(
    config: dict,
    catalog_root: Path,
    request: OperationRequest,
    changes: int,
) -> tuple[CatalogRevisionSummary, bytes]:
    root = _regular_history_root(config, create=True)
    objects_root = _history_directory(root, "objects", create=True)
    snapshots_root = _history_directory(root, "snapshots", create=True)
    _history_directory(root, "revisions", create=True)
    document, snapshot, objects = _catalog_snapshot_document(catalog_root)
    sizes = {
        entry["hash"]: entry["size"]
        for entry in document["entries"]
        if entry["kind"] == "file"
    }
    for digest, source in objects.items():
        _write_history_object(objects_root / digest, source, digest, sizes[digest])
    _write_immutable(snapshots_root / "{}.json".format(snapshot), _canonical_json(document))

    created = datetime.now(UTC)
    created_at = created.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    revision_id = "{}-{}".format(
        created.strftime("%Y%m%dT%H%M%S%fZ"), snapshot[:16]
    )
    tools = tuple(sorted(config["tools"])) if request.tool == "all" else (request.tool,)
    summary = CatalogRevisionSummary(
        revision_id,
        created_at,
        config["_host"],
        tools,
        _catalog_artifact_count(catalog_root),
        changes,
        snapshot,
    )
    record = {
        "version": HISTORY_VERSION,
        "id": summary.revision_id,
        "created_at": summary.created_at,
        "host": summary.host,
        "tools": list(summary.tools),
        "artifact_count": summary.artifact_count,
        "changes": summary.changes,
        "snapshot": summary.snapshot,
    }
    return summary, _canonical_json(record)


def _publish_catalog_revision(
    config: dict, summary: CatalogRevisionSummary, content: bytes
) -> None:
    root = _regular_history_root(config, create=True)
    revisions = _history_directory(root, "revisions", create=True)
    _write_immutable(revisions / "{}.json".format(summary.revision_id), content)


def _revision_path(config: dict, revision_id: str) -> Path:
    if not isinstance(revision_id, str) or REVISION_ID.fullmatch(revision_id) is None:
        raise AgentBoxError("Invalid catalog revision: {!r}".format(revision_id))
    root = _regular_history_root(config, create=False)
    revisions = _history_directory(root, "revisions", create=False)
    return revisions / "{}.json".format(revision_id)


def _catalog_revision_summary(value: object, revision_id: str) -> CatalogRevisionSummary:
    required = {
        "version",
        "id",
        "created_at",
        "host",
        "tools",
        "artifact_count",
        "changes",
        "snapshot",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise AgentBoxError("Catalog revision metadata is invalid: {}".format(revision_id))
    tools = value.get("tools")
    artifact_count = value.get("artifact_count")
    changes = value.get("changes")
    if (
        value.get("version") != HISTORY_VERSION
        or value.get("id") != revision_id
        or not isinstance(value.get("created_at"), str)
        or not isinstance(value.get("host"), str)
        or CONFIG_NAME.fullmatch(value["host"]) is None
        or not isinstance(tools, list)
        or not tools
        or not all(isinstance(tool, str) and CONFIG_NAME.fullmatch(tool) for tool in tools)
        or not isinstance(artifact_count, int)
        or isinstance(artifact_count, bool)
        or artifact_count < 0
        or not isinstance(changes, int)
        or isinstance(changes, bool)
        or changes < 0
        or not isinstance(value.get("snapshot"), str)
        or CONTENT_HASH.fullmatch(value["snapshot"]) is None
        or not revision_id.endswith("-{}".format(value["snapshot"][:16]))
    ):
        raise AgentBoxError("Catalog revision metadata is invalid: {}".format(revision_id))
    try:
        created = datetime.strptime(value["created_at"], "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise AgentBoxError("Catalog revision timestamp is invalid: {}".format(revision_id)) from exc
    if not revision_id.startswith(created.strftime("%Y%m%dT%H%M%S%fZ")):
        raise AgentBoxError("Catalog revision timestamp is invalid: {}".format(revision_id))
    return CatalogRevisionSummary(
        revision_id,
        value["created_at"],
        value["host"],
        tuple(tools),
        artifact_count,
        changes,
        value["snapshot"],
    )


def _load_catalog_revision(config: dict, revision_id: str) -> CatalogRevisionSummary:
    path = _revision_path(config, revision_id)
    if path.is_symlink() or not path.is_file():
        raise AgentBoxError("Unknown catalog revision: {}".format(revision_id))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentBoxError("Catalog revision metadata is invalid: {}".format(revision_id)) from exc
    return _catalog_revision_summary(value, revision_id)


def _load_catalog_snapshot(config: dict, snapshot: str) -> dict:
    if CONTENT_HASH.fullmatch(snapshot) is None:
        raise AgentBoxError("Catalog snapshot identity is invalid")
    root = _regular_history_root(config, create=False)
    snapshots = _history_directory(root, "snapshots", create=False)
    path = snapshots / "{}.json".format(snapshot)
    if path.is_symlink() or not path.is_file():
        raise AgentBoxError("Catalog snapshot is unavailable: {}".format(snapshot))
    try:
        content = path.read_bytes()
        value = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentBoxError("Catalog snapshot is invalid: {}".format(snapshot)) from exc
    if hashlib.sha256(_canonical_json(value)).hexdigest() != snapshot:
        raise AgentBoxError("Catalog snapshot failed integrity verification: {}".format(snapshot))
    if not isinstance(value, dict) or set(value) != {"version", "entries"}:
        raise AgentBoxError("Catalog snapshot is invalid: {}".format(snapshot))
    entries = value.get("entries")
    if value.get("version") != HISTORY_VERSION or not isinstance(entries, list):
        raise AgentBoxError("Catalog snapshot is invalid: {}".format(snapshot))
    seen = set()
    directories = set()
    previous = None
    for entry in entries:
        if not isinstance(entry, dict):
            raise AgentBoxError("Catalog snapshot contains an invalid entry")
        kind = entry.get("kind")
        expected_keys = {"path", "kind", "mode"} if kind == "directory" else {
            "path",
            "kind",
            "mode",
            "hash",
            "size",
        }
        relative = safe_relative_path(entry.get("path"))
        relative_text = relative.as_posix()
        mode = entry.get("mode")
        if (
            set(entry) != expected_keys
            or kind not in {"directory", "file"}
            or relative_text in seen
            or not isinstance(mode, int)
            or isinstance(mode, bool)
            or mode < 0
            or mode > 0o7777
            or (previous is not None and relative_text <= previous)
        ):
            raise AgentBoxError("Catalog snapshot contains an invalid entry")
        for parent in relative.parents:
            if parent == Path("."):
                continue
            if parent.as_posix() not in directories:
                raise AgentBoxError("Catalog snapshot has a missing parent directory")
        if kind == "directory":
            directories.add(relative_text)
        else:
            size = entry.get("size")
            if (
                not isinstance(entry.get("hash"), str)
                or CONTENT_HASH.fullmatch(entry["hash"]) is None
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
            ):
                raise AgentBoxError("Catalog snapshot contains an invalid file")
        seen.add(relative_text)
        previous = relative_text
    return value


def _verified_history_object(config: dict, digest: str, size: int) -> Path:
    root = _regular_history_root(config, create=False)
    objects = _history_directory(root, "objects", create=False)
    path = objects / digest
    if path.is_symlink() or not path.is_file():
        raise AgentBoxError("Catalog history object is unavailable: {}".format(digest))
    actual_digest, actual_size = _stream_sha256(path)
    if actual_digest != digest or actual_size != size:
        raise AgentBoxError("Catalog history object failed integrity verification: {}".format(digest))
    return path


def _verified_catalog_revision(config: dict, summary: CatalogRevisionSummary) -> dict:
    snapshot = _load_catalog_snapshot(config, summary.snapshot)
    for entry in snapshot["entries"]:
        if entry["kind"] == "file":
            _verified_history_object(config, entry["hash"], entry["size"])
    return snapshot


def _history_state_record(snapshot: tuple[bool, bytes, int]) -> dict:
    existed, content, mode = snapshot
    return {"existed": existed, "content": content.hex(), "mode": mode}


def _history_state_snapshot(value: object, revision_id: str) -> tuple[bool, bytes, int]:
    if (
        not isinstance(value, dict)
        or set(value) != {"existed", "content", "mode"}
        or not isinstance(value.get("existed"), bool)
        or not isinstance(value.get("content"), str)
        or not isinstance(value.get("mode"), int)
        or isinstance(value.get("mode"), bool)
        or value["mode"] < 0
        or value["mode"] > 0o7777
        or (not value["existed"] and (value["content"] or value["mode"] != 0))
    ):
        raise AgentBoxError(
            "Catalog history transaction state is invalid: {}".format(revision_id)
        )
    try:
        return value["existed"], bytes.fromhex(value["content"]), value["mode"]
    except ValueError as exc:
        raise AgentBoxError(
            "Catalog history transaction state is invalid: {}".format(revision_id)
        ) from exc


def _begin_history_transaction(
    config: dict,
    summary: CatalogRevisionSummary,
    revision_content: bytes,
    before_snapshot: str,
    before_state: tuple[bool, bytes, int],
    updated_state: tuple[bool, bytes, int],
) -> Path:
    if CONTENT_HASH.fullmatch(before_snapshot) is None:
        raise AgentBoxError("Catalog transaction has an invalid starting snapshot")
    try:
        revision = json.loads(revision_content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentBoxError("Catalog transaction has invalid revision metadata") from exc
    _catalog_revision_summary(revision, summary.revision_id)
    root = _regular_history_root(config, create=True)
    transactions = _history_directory(root, "transactions", create=True)
    path = transactions / "{}.json".format(summary.revision_id)
    _write_immutable(
        path,
        _canonical_json(
            {
                "version": HISTORY_VERSION,
                "before_snapshot": before_snapshot,
                "revision": revision,
                "state_path": str(config["_state_file"].expanduser().resolve()),
                "state_before": _history_state_record(before_state),
                "state_after": _history_state_record(updated_state),
            }
        ),
    )
    return path


def _recover_catalog_installation(
    destination: Path,
    revision_id: str,
    before_snapshot: str,
    after_snapshot: str,
) -> str:
    previous, staged = catalog_install_paths(destination, revision_id)
    _, current_snapshot, _ = _catalog_snapshot_document(destination)

    def remove_copy(path: Path, expected: str) -> None:
        if not path.exists() and not path.is_symlink():
            return
        _, snapshot, _ = _catalog_snapshot_document(path)
        if snapshot != expected:
            raise AgentBoxError(
                "Interrupted catalog installation path changed; refusing to remove {}".format(
                    path
                )
            )
        remove_path(path)

    if current_snapshot == before_snapshot:
        if staged.exists() or staged.is_symlink():
            remove_path(staged)
        remove_copy(previous, before_snapshot)
        return current_snapshot
    if current_snapshot == after_snapshot:
        remove_copy(staged, after_snapshot)
        remove_copy(previous, before_snapshot)
        return current_snapshot
    if not destination.exists() and not destination.is_symlink() and previous.exists():
        _, previous_snapshot, _ = _catalog_snapshot_document(previous)
        if previous_snapshot != before_snapshot:
            raise AgentBoxError("Interrupted catalog installation backup changed")
        if staged.exists():
            _, staged_snapshot, _ = _catalog_snapshot_document(staged)
            if staged_snapshot != after_snapshot:
                raise AgentBoxError("Interrupted staged catalog changed")
            staged.rename(destination)
            remove_path(previous)
            return after_snapshot
        previous.rename(destination)
        return before_snapshot
    raise AgentBoxError(
        "Catalog changed while recovering interrupted history revision {}; refusing to guess".format(
            revision_id
        )
    )


def _recover_history_transactions(config: dict) -> None:
    root = _regular_history_root(config, create=False)
    transactions = _history_directory(root, "transactions", create=False)
    if not transactions.exists():
        return
    local_root = config.get("_storage_local")
    if local_root is None:
        raise AgentBoxError("Local history recovery requires a local catalog")
    for path in sorted(transactions.glob("*.json")):
        revision_id = path.stem
        if (
            REVISION_ID.fullmatch(revision_id) is None
            or path.is_symlink()
            or not path.is_file()
        ):
            raise AgentBoxError("Catalog history transaction is invalid: {}".format(path))
        try:
            transaction = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentBoxError(
                "Catalog history transaction is invalid: {}".format(revision_id)
            ) from exc
        if (
            not isinstance(transaction, dict)
            or set(transaction)
            != {
                "version",
                "before_snapshot",
                "revision",
                "state_path",
                "state_before",
                "state_after",
            }
            or transaction.get("version") != HISTORY_VERSION
            or not isinstance(transaction.get("before_snapshot"), str)
            or CONTENT_HASH.fullmatch(transaction["before_snapshot"]) is None
            or transaction.get("state_path")
            != str(config["_state_file"].expanduser().resolve())
        ):
            raise AgentBoxError(
                "Catalog history transaction is invalid: {}".format(revision_id)
            )
        state_before = _history_state_snapshot(transaction["state_before"], revision_id)
        state_after = _history_state_snapshot(transaction["state_after"], revision_id)
        revision = transaction["revision"]
        summary = _catalog_revision_summary(revision, revision_id)
        revision_content = _canonical_json(revision)
        published = _revision_path(config, revision_id)
        if published.exists() or published.is_symlink():
            _write_immutable(published, revision_content)
            path.unlink()
            try:
                _apply_history_retention(config, revision_id)
            except (AgentBoxError, OSError):
                pass
            continue
        current_state = _file_snapshot(config["_state_file"])
        if (
            state_before != state_after
            and current_state != state_before
            and current_state != state_after
        ):
            raise AgentBoxError(
                "State changed while recovering interrupted history revision {}; "
                "refusing to overwrite it".format(revision_id)
            )
        current_snapshot = _recover_catalog_installation(
            local_root,
            revision_id,
            transaction["before_snapshot"],
            summary.snapshot,
        )
        if current_snapshot == summary.snapshot:
            _verified_catalog_revision(config, summary)
            if state_before != state_after and current_state == state_before:
                _restore_file_snapshot(config["_state_file"], state_after)
            _publish_catalog_revision(config, summary, revision_content)
            path.unlink()
            try:
                _apply_history_retention(config, revision_id)
            except (AgentBoxError, OSError):
                pass
            continue
        elif state_before != state_after and current_state == state_after:
            _restore_file_snapshot(config["_state_file"], state_before)
        path.unlink()


def catalog_revision_signature(config: dict, revision_id: str) -> str:
    summary = _load_catalog_revision(config, revision_id)
    snapshot = _verified_catalog_revision(config, summary)
    digest = hashlib.sha256()
    digest.update(_revision_path(config, revision_id).read_bytes())
    digest.update(_canonical_json(snapshot))
    for entry in snapshot["entries"]:
        if entry["kind"] == "file":
            digest.update(entry["hash"].encode("ascii"))
            digest.update(str(entry["size"]).encode("ascii"))
    return digest.hexdigest()


@contextmanager
def materialized_catalog_revision(config: dict, revision_id: str):
    summary = _load_catalog_revision(config, revision_id)
    snapshot = _load_catalog_snapshot(config, summary.snapshot)
    with tempfile.TemporaryDirectory(prefix="agentbox-history-") as temporary:
        root = Path(temporary) / "catalog"
        root.mkdir()
        directory_modes = []
        for entry in snapshot["entries"]:
            destination = checked_join(root, safe_relative_path(entry["path"]))
            if entry["kind"] == "directory":
                destination.mkdir(parents=True, exist_ok=False)
                directory_modes.append((destination, entry["mode"]))
                continue
            source = _verified_history_object(config, entry["hash"], entry["size"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            destination.chmod(entry["mode"])
        for directory, mode in reversed(directory_modes):
            directory.chmod(mode)
        yield root


def _revision_artifacts(config: dict, revision_id: str) -> tuple[dict[str, str], ...]:
    artifacts = []
    with materialized_catalog_revision(config, revision_id) as root:
        for manifest in sorted(root.glob("*/*/manifest.json")):
            relative = manifest.relative_to(root)
            host, tool = relative.parts[:2]
            value = load_json(manifest, {})
            entries = value.get("artifacts") if isinstance(value, dict) else None
            if not isinstance(entries, list):
                raise AgentBoxError("Historical catalog manifest is invalid: {}".format(relative))
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("kind") not in {"skill", "command"}:
                    raise AgentBoxError("Historical catalog manifest is invalid: {}".format(relative))
                name = entry.get("name")
                path = entry.get("path")
                if (
                    not isinstance(name, str)
                    or SKILL_NAME.fullmatch(name) is None
                    or safe_relative_path(path).as_posix() != path
                ):
                    raise AgentBoxError("Historical catalog manifest is invalid: {}".format(relative))
                artifacts.append(
                    {"host": host, "tool": tool, "kind": entry["kind"], "name": name, "path": path}
                )
    return tuple(
        sorted(
            artifacts,
            key=lambda item: (item["host"], item["tool"], item["kind"], item["path"]),
        )
    )


def _list_catalog_revisions(config: dict) -> list[CatalogRevisionSummary]:
    root = _regular_history_root(config, create=False)
    revisions = root / "revisions"
    if not revisions.exists():
        return []
    if revisions.is_symlink() or not revisions.is_dir():
        raise AgentBoxError("History revisions path is not a regular directory")
    result = []
    for path in sorted(revisions.glob("*.json"), reverse=True):
        result.append(_load_catalog_revision(config, path.stem))
    return result


def list_catalog_revisions(
    config_path: Path,
    host_override: str | None = None,
    *,
    acquire_lock: bool = True,
) -> list[CatalogRevisionSummary]:
    config_path = config_path.expanduser().resolve()
    config = load_config(config_path, host_override)
    if acquire_lock:
        identities = storage_lock_identities(config)
        with application_operation_guard(config_path, *identities):
            return list_catalog_revisions(
                config_path, host_override, acquire_lock=False
            )
    _recover_history_transactions(config)
    return _list_catalog_revisions(config)


def inspect_catalog_revision(
    config_path: Path,
    revision_id: str,
    host_override: str | None = None,
    *,
    acquire_lock: bool = True,
) -> CatalogRevisionDetail:
    config_path = config_path.expanduser().resolve()
    config = load_config(config_path, host_override)
    if acquire_lock:
        identities = storage_lock_identities(config)
        with application_operation_guard(config_path, *identities):
            return inspect_catalog_revision(
                config_path,
                revision_id,
                host_override,
                acquire_lock=False,
            )
    _recover_history_transactions(config)
    summary = _load_catalog_revision(config, revision_id)
    catalog_revision_signature(config, revision_id)
    return CatalogRevisionDetail(summary, _revision_artifacts(config, revision_id))


def _apply_history_retention(config: dict, keep_revision: str) -> None:
    maximum = config.get("_history_max_revisions")
    if maximum is None:
        return
    revisions = _list_catalog_revisions(config)
    if len(revisions) <= maximum:
        return
    root = _regular_history_root(config, create=False)
    retained = [revision for revision in revisions if revision.revision_id == keep_revision]
    retained.extend(
        revision
        for revision in revisions
        if revision.revision_id != keep_revision
    )
    retained = retained[:maximum]
    snapshots = {revision.snapshot for revision in retained}
    objects = set()
    for snapshot in snapshots:
        document = _load_catalog_snapshot(config, snapshot)
        for entry in document["entries"]:
            if entry["kind"] == "file":
                _verified_history_object(config, entry["hash"], entry["size"])
                objects.add(entry["hash"])

    snapshot_root = _history_directory(root, "snapshots", create=False)
    object_root = _history_directory(root, "objects", create=False)
    retained_ids = {revision.revision_id for revision in retained}
    for revision in revisions:
        if revision.revision_id in retained_ids:
            continue
        (root / "revisions/{}.json".format(revision.revision_id)).unlink(missing_ok=True)
    if snapshot_root.exists():
        for path in snapshot_root.glob("*.json"):
            if path.stem not in snapshots:
                path.unlink(missing_ok=True)
    if object_root.exists():
        for path in object_root.iterdir():
            if path.is_file() and not path.is_symlink() and path.name not in objects:
                path.unlink(missing_ok=True)


def restore_catalog_snapshot(snapshot: Path, existed: bool, destination: Path) -> None:
    if existed:
        replace_catalog(snapshot, destination)
    elif destination.exists() or destination.is_symlink():
        remove_path(destination)


def _rollback_git_checkout(
    session: StorageSession,
    snapshot: Path,
    snapshot_exists: bool,
    revision: str | None,
) -> None:
    checkout = session.config["_storage_git_checkout"]
    url = session.config["_storage_git_url"]
    branch = session.git_branch
    if checkout is None or url is None or branch is None or session.git_root is None:
        return
    reference = "refs/heads/{}".format(branch)
    if revision is None:
        _run_git(checkout, ("update-ref", "-d", reference), url)
        _run_git(checkout, ("read-tree", "--empty"), url)
    else:
        _run_git(checkout, ("update-ref", reference, revision), url)
        _run_git(checkout, ("read-tree", "HEAD"), url)
    if snapshot_exists:
        replace_catalog(snapshot, session.git_root)
    elif session.git_root.exists() or session.git_root.is_symlink():
        remove_path(session.git_root)


def commit_git_catalog(
    session: StorageSession,
    source: Path,
    report: Reporter = console_report,
) -> bool:
    checkout = session.config["_storage_git_checkout"]
    url = session.config["_storage_git_url"]
    branch = session.git_branch
    git_root = session.git_root
    if checkout is None or url is None or branch is None or git_root is None:
        raise AgentBoxError("Git storage is not prepared")
    revision = _git_revision(checkout, url)
    with tempfile.TemporaryDirectory(prefix="agentbox-git-rollback-") as temporary:
        snapshot = Path(temporary) / "catalog"
        snapshot_exists = git_root.exists()
        if snapshot_exists:
            copy_exact(git_root, snapshot)
        pushed = False
        uncertain = False
        try:
            replace_catalog(source, git_root)
            _validate_managed_repository(checkout)
            _run_git(
                checkout,
                (
                    "-c",
                    "core.autocrlf=false",
                    "-c",
                    "core.attributesFile={}".format(os.devnull),
                    "add",
                    "--force",
                    "--all",
                    "--",
                    "catalog",
                ),
                url,
            )
            staged = _run_git(
                checkout,
                ("diff", "--cached", "--quiet", "--", "catalog"),
                url,
                allowed=(0, 1),
            )
            if staged.returncode == 0:
                if not session.git_pending:
                    return False
                commit = revision
            else:
                message = "AgentBox catalog backup {}".format(
                    datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                )
                _run_git(
                    checkout,
                    (
                        "-c",
                        "user.name=AgentBox",
                        "-c",
                        "user.email=agentbox@localhost",
                        "-c",
                        "commit.gpgsign=false",
                        "commit",
                        "--quiet",
                        "-m",
                        message,
                        "--",
                        "catalog",
                    ),
                    url,
                )
                commit = _git_revision(checkout, url)
            if commit is None:
                raise AgentBoxError("Git did not create a catalog commit")
            try:
                _run_git(
                    checkout,
                    ("push", "origin", "HEAD:refs/heads/{}".format(branch)),
                    url,
                )
                pushed = True
            except AgentBoxError as push_error:
                remote_ref = "refs/remotes/origin/{}".format(branch)
                try:
                    _run_git(checkout, ("fetch", "origin"), url)
                    contains_commit = False
                    if _git_ref_exists(checkout, remote_ref, url):
                        contains_commit = (
                            _run_git(
                                checkout,
                                ("merge-base", "--is-ancestor", commit, remote_ref),
                                url,
                                allowed=(0, 1),
                            ).returncode
                            == 0
                        )
                except AgentBoxError:
                    uncertain = True
                    session.git_uncertain = True
                    raise AgentBoxError(
                        "Git push outcome is uncertain; the managed commit was preserved and "
                        "will be checked on the next operation"
                    ) from push_error
                if not contains_commit:
                    raise push_error
                pushed = True
            session.git_pushed = True
            session.git_pending = False
            session.git_revision = commit
            report(OperationEvent("git-commit", "GIT COMMIT {}".format(commit[:8])))
            report(OperationEvent("git-push", "GIT PUSH {}".format(branch)))
            return True
        except Exception:
            if not pushed and not uncertain:
                try:
                    _rollback_git_checkout(session, snapshot, snapshot_exists, revision)
                except Exception as rollback_error:
                    raise AgentBoxError(
                        "Git storage failed and its managed checkout could not be restored: {}".format(
                            rollback_error
                        )
                    ) from rollback_error
            raise


def initialize_storage_for_restore(
    session: StorageSession,
    report: Reporter = console_report,
) -> None:
    if session.initialize == "git-from-local":
        if session.local_root is None:
            raise AgentBoxError("Local storage is unavailable for Git initialization")
        commit_git_catalog(session, session.local_root, report)
    elif session.initialize == "local-from-git":
        if session.local_root is None or session.git_root is None:
            raise AgentBoxError("Dual storage is unavailable for local initialization")
        replace_catalog(session.git_root, session.local_root)


def write_generated_skill(content: bytes, destination: Path) -> None:
    with _staged_destination(destination) as (staged, token):
        staged.mkdir(mode=_GENERATED_SKILL_DIRECTORY_MODE)
        staged.chmod(_GENERATED_SKILL_DIRECTORY_MODE)
        skill_file = staged / "SKILL.md"
        skill_file.write_bytes(content)
        skill_file.chmod(_GENERATED_SKILL_FILE_MODE)
        install_staged(staged, destination, token)


def validate_backup_update(
    config: dict,
    tool_name: str,
    artifacts: Sequence[Artifact],
    available_sources: set,
) -> None:
    manifest = load_manifest(config, tool_name)
    old = {(item["kind"], item["path"]): item for item in manifest["artifacts"]}
    tool_catalog = config["_catalog"] / tool_name
    for artifact in artifacts:
        key = (artifact.kind, artifact.catalog_path)
        old_entry = old.get(key)
        if not old_entry:
            continue
        unavailable = [
            source
            for source in old_entry.get("sources", [])
            if (artifact.kind, source["id"]) not in available_sources
        ]
        if not unavailable:
            continue
        destination = checked_join(
            tool_catalog, safe_relative_path(artifact.catalog_path)
        )
        if not destination.exists() or hash_path(destination) != hash_path(artifact.payload):
            labels = ", ".join(sorted({source["id"] for source in unavailable}))
            raise AgentBoxError(
                "Refusing to update {} while recorded source(s) {} are unavailable; "
                "restore or reconnect those sources first".format(artifact.catalog_path, labels)
            )


def backup_tool(
    config: dict,
    tool_name: str,
    artifacts: Sequence[Artifact],
    available_sources: set,
    dry_run: bool,
    prune: bool,
    report: Reporter = console_report,
) -> int:
    if not available_sources:
        report(
            OperationEvent(
                "no-sources",
                "{}: no configured source directories found".format(tool_name),
                tool_name,
            )
        )
        return 0
    old_manifest = load_manifest(config, tool_name)
    old = {(item["kind"], item["path"]): item for item in old_manifest["artifacts"]}
    current = {}
    changes = 0
    tool_catalog = config["_catalog"] / tool_name

    for artifact in sorted(artifacts, key=lambda item: (item.kind, item.catalog_path)):
        destination = checked_join(tool_catalog, safe_relative_path(artifact.catalog_path))
        entry = {
            "kind": artifact.kind,
            "name": artifact.name,
            "path": artifact.catalog_path,
            "sources": [source_record(item) for item in artifact.sources],
        }
        key = (artifact.kind, artifact.catalog_path)
        old_entry = old.get(key)
        if old_entry:
            known_sources = {(item["id"], item["relative_path"]) for item in entry["sources"]}
            for source in old_entry.get("sources", []):
                source_key = (artifact.kind, source["id"])
                identity = (source["id"], source["relative_path"])
                if source_key not in available_sources and identity not in known_sources:
                    entry["sources"].append(source)
                    known_sources.add(identity)
        current[key] = entry
        if destination.exists() and hash_path(destination) == hash_path(artifact.payload):
            report(
                OperationEvent(
                    "unchanged",
                    "UNCHANGED {} {}".format(tool_name, artifact.catalog_path),
                    tool_name,
                    artifact.catalog_path,
                )
            )
            continue
        changes += 1
        report(
            OperationEvent(
                "backup",
                "BACKUP {} {}".format(tool_name, artifact.catalog_path),
                tool_name,
                artifact.catalog_path,
            )
        )
        if not dry_run:
            copy_exact(artifact.payload, destination)

    if prune:
        final = dict(current)
        for key, entry in old.items():
            if key in current:
                continue
            recorded_sources = {
                (entry["kind"], source["id"]) for source in entry.get("sources", [])
            }
            if not recorded_sources or not recorded_sources.issubset(available_sources):
                final[key] = entry
                report(
                    OperationEvent(
                        "keep",
                        "KEEP unavailable source {} {}".format(tool_name, entry["path"]),
                        tool_name,
                        entry["path"],
                    )
                )
                continue
            destination = checked_join(tool_catalog, safe_relative_path(entry["path"]))
            if destination.exists():
                changes += 1
                report(
                    OperationEvent(
                        "prune",
                        "PRUNE {} {}".format(tool_name, entry["path"]),
                        tool_name,
                        entry["path"],
                    )
                )
                if not dry_run:
                    remove_path(destination)
    else:
        final = dict(old)
        final.update(current)

    new_manifest = {
        "version": VERSION,
        "artifacts": sorted(final.values(), key=lambda item: (item["kind"], item["path"])),
    }
    if not dry_run and new_manifest != old_manifest:
        write_json(manifest_path(config, tool_name), new_manifest)
    return changes


def scan_catalog(config: dict, source_tools: Sequence[str]) -> list[Artifact]:
    artifacts = []
    for tool_name in source_tools:
        root = config["_catalog"] / tool_name
        if root.is_symlink():
            raise AgentBoxError("Symlinked catalog paths are not supported for {}".format(tool_name))
        skills = root / "skills"
        if config["tools"][tool_name]["skills"]["_enabled"] and skills.exists():
            validate_regular_payload(skills)
            for skill_dir in sorted(item for item in skills.iterdir() if item.is_dir()):
                if not (skill_dir / "SKILL.md").is_file():
                    continue
                artifacts.append(
                    Artifact(
                        tool_name,
                        "skill",
                        read_skill_name(skill_dir),
                        skill_dir.relative_to(root).as_posix(),
                        skill_dir,
                        host=config["_host"],
                        catalog_revision=config.get("_catalog_revision"),
                    )
                )
        commands = root / "commands"
        if config["tools"][tool_name]["commands"]["_enabled"] and commands.exists():
            validate_regular_payload(commands)
            for command in sorted(commands.rglob("*.md")):
                relative = command.relative_to(commands).as_posix()
                artifacts.append(
                    Artifact(
                        tool_name,
                        "command",
                        command_name(relative),
                        command.relative_to(root).as_posix(),
                        command,
                        host=config["_host"],
                        catalog_revision=config.get("_catalog_revision"),
                    )
                )
    return artifacts


def scan_catalog_hosts(
    config: dict, source_hosts: Sequence[str], source_tools: Sequence[str]
) -> list[Artifact]:
    artifacts = []
    for host in source_hosts:
        artifacts.extend(scan_catalog(config_for_host(config, host), source_tools))
    return artifacts


def convert_command(artifact: Artifact) -> bytes:
    try:
        content = artifact.payload.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AgentBoxError("Cannot convert {}: {}".format(artifact.payload, exc))
    metadata, body = split_frontmatter(content)
    description = metadata.get(
        "description", "Run the {} workflow.".format(artifact.name.replace("-", " "))
    )
    description = " ".join(description.split())
    result = [
        "---",
        "name: {}".format(artifact.name),
        "description: {}".format(json.dumps(description, ensure_ascii=True)),
        "---",
        "",
    ]
    stripped_body = body.strip()
    if PLACEHOLDER.search(stripped_body):
        result.extend(
            [
                "Use the current user request as this workflow's input. Dollar-prefixed ",
                "argument placeholders in the preserved instructions refer to details supplied by the user.",
                "",
            ]
        )
    result.append(stripped_body)
    return ("\n".join(result).rstrip() + "\n").encode("utf-8")


def build_candidates(artifacts: Sequence[Artifact]) -> tuple[list[Candidate], list[str]]:
    candidates: dict[str, Candidate] = {}
    errors = []
    for artifact in artifacts:
        origin = {
            "host": artifact.host,
            "tool": artifact.tool,
            "kind": artifact.kind,
            "path": artifact.catalog_path,
        }
        if artifact.catalog_revision is not None:
            origin["revision"] = artifact.catalog_revision
        if artifact.kind == "skill":
            candidate = Candidate(artifact.name, artifact.payload, None, [origin])
        else:
            candidate = Candidate(artifact.name, None, convert_command(artifact), [origin])
        existing = candidates.get(candidate.name)
        if not existing:
            candidates[candidate.name] = candidate
        elif existing.fingerprint() == candidate.fingerprint():
            existing.origins.extend(candidate.origins)
        else:
            origin_labels = [
                "{}:{}:{}".format(item["tool"], item["kind"], item["path"])
                for item in existing.origins + candidate.origins
            ]
            origin_tools = {item["tool"] for item in existing.origins + candidate.origins}
            resolution = (
                "Use --from <tool> to select one source."
                if len(origin_tools) > 1
                else "Rename or reconcile one of these catalog entries."
            )
            errors.append(
                "Skill name {!r} has divergent catalog entries: {}. {}".format(
                    candidate.name, ", ".join(origin_labels), resolution
                )
            )
    return sorted(candidates.values(), key=lambda item: item.name), errors


def build_portable_index(
    config: dict,
    source_tools: Sequence[str],
    source_hosts: Sequence[str] | None = None,
) -> dict[tuple[str, str], list[dict]]:
    index: dict[tuple[str, str], list[dict]] = {}
    hosts = list(source_hosts) if source_hosts is not None else [config["_host"]]
    for artifact in scan_catalog_hosts(config, hosts, source_tools):
        fingerprint = artifact_portable_fingerprint(artifact)
        key = (artifact.name, fingerprint)
        index.setdefault(key, []).append(
            {
                "host": artifact.host,
                "tool": artifact.tool,
                "kind": artifact.kind,
                "path": artifact.catalog_path,
            }
        )
    return index


def safety_copy(path: Path, backup_root: Path, tool_name: str, relative: str) -> None:
    destination = checked_join(
        backup_root,
        safe_relative_path(str(Path(tool_name) / safe_relative_path(relative))),
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if path.is_dir() and not path.is_symlink():
        shutil.copytree(str(path), str(destination), symlinks=True)
    elif path.is_symlink():
        destination.symlink_to(os.readlink(str(path)))
    else:
        shutil.copy2(str(path), str(destination), follow_symlinks=False)


def prepare_portable_restore(
    config: dict,
    target_tool: str,
    source_hosts: Sequence[str],
    source_tools: Sequence[str],
    force: bool,
) -> list[tuple[Candidate, Path, str, bool]]:
    if not config["tools"][target_tool]["skills"]["_enabled"]:
        raise AgentBoxError("Portable restore requires skills enabled for {}".format(target_tool))
    artifacts = scan_catalog_hosts(config, source_hosts, source_tools)
    candidates, errors = build_candidates(artifacts)
    if errors:
        raise AgentBoxError("\n".join(errors))
    target_root = config["tools"][target_tool]["skills"]["_target"]
    require_regular_root(target_root, "restore target")
    operations = []
    for candidate in candidates:
        destination = checked_join(target_root, Path(candidate.name))
        if destination.is_symlink() and not force:
            raise AgentBoxError(
                "Refusing to replace symlink {} without --force".format(destination)
            )
        candidate_hash = candidate.fingerprint()
        unchanged = _path_matches_fingerprint(destination, candidate_hash)
        operations.append((candidate, destination, candidate_hash, unchanged))
    return operations


def restore_portable(
    config: dict,
    target_tool: str,
    dry_run: bool,
    state: dict,
    operations: Sequence[tuple[Candidate, Path, str, bool]],
    report: Reporter = console_report,
) -> int:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_root = config["_safety_backups"] / timestamp
    changes = 0

    for candidate, destination, candidate_hash, unchanged in operations:
        if unchanged:
            report(
                OperationEvent(
                    "unchanged",
                    "UNCHANGED {} skill {}".format(target_tool, candidate.name),
                    target_tool,
                    candidate.name,
                )
            )
        else:
            changes += 1
            report(
                OperationEvent(
                    "restore",
                    "RESTORE {} skill {}".format(target_tool, candidate.name),
                    target_tool,
                    candidate.name,
                )
            )
            if not dry_run:
                if destination.exists() or destination.is_symlink():
                    safety_copy(
                        destination,
                        backup_root,
                        target_tool,
                        "skills/{}".format(candidate.name),
                    )
                if candidate.generated is not None:
                    write_generated_skill(candidate.generated, destination)
                elif candidate.payload is not None:
                    copy_exact(candidate.payload, destination)
        if not dry_run:
            state["deployments"][str(destination.absolute())] = {
                "hash": candidate_hash,
                "origins": candidate.origins,
                "target": target_tool,
            }
            write_json(config["_state_file"], state)
    return changes


def validate_portable_target_operations(
    prepared: dict[str, Sequence[tuple[Candidate, Path, str, bool]]]
) -> None:
    destinations = {}
    for target_tool, operations in prepared.items():
        for _, destination, _, _ in operations:
            key = str(destination.absolute())
            previous = destinations.get(key)
            if previous and previous != target_tool:
                raise AgentBoxError(
                    "Restore targets {} and {} both map to {}; restore them separately "
                    "or configure distinct target roots".format(
                        previous, target_tool, destination
                    )
                )
            destinations[key] = target_tool


def configured_source_root(
    config: dict, tool_name: str, kind: str, source_id: str
) -> Path | None:
    section = config["tools"][tool_name]["skills" if kind == "skill" else "commands"]
    for source in section["_sources"]:
        if source["id"] == source_id:
            return source["path"]
    return None


def prepare_exact_restore(
    config: dict,
    tool_name: str,
    force: bool,
) -> list[tuple[dict, Path, Path, bool, str]]:
    manifest = load_manifest(config, tool_name)
    tool_root = config["_catalog"] / tool_name
    operations = []
    claimed_destinations = {}
    for entry in manifest["artifacts"]:
        section_name = "skills" if entry["kind"] == "skill" else "commands"
        if not config["tools"][tool_name][section_name]["_enabled"]:
            continue
        entry_path = safe_relative_path(entry["path"])
        payload = checked_join(tool_root, entry_path)
        if not payload.exists():
            raise AgentBoxError("Catalog payload is missing: {}".format(payload))
        payload_hash = hash_path(payload)
        destinations = []
        for source in entry.get("sources", []):
            root = configured_source_root(config, tool_name, entry["kind"], source["id"])
            if root is not None:
                require_regular_root(root, "exact restore")
                destinations.append(
                    (
                        checked_join(root, safe_relative_path(source["relative_path"])),
                        source["id"],
                        source["relative_path"],
                    )
                )
        if not destinations:
            section = config["tools"][tool_name][
                "skills" if entry["kind"] == "skill" else "commands"
            ]
            fallback = (
                Path(entry["name"])
                if entry["kind"] == "skill"
                else entry_path.relative_to("commands")
            )
            require_regular_root(section["_target"], "exact restore target")
            destinations.append(
                (checked_join(section["_target"], fallback), "target", entry["path"])
            )

        seen_destinations = set()
        for destination, source_id, source_relative in destinations:
            destination_key = str(destination.absolute())
            if destination_key in seen_destinations:
                continue
            seen_destinations.add(destination_key)
            if destination.is_symlink() and not force:
                raise AgentBoxError(
                    "Refusing to replace symlink {} without --force".format(destination)
                )
            previous_payload = claimed_destinations.get(destination_key)
            if previous_payload and previous_payload != str(payload):
                raise AgentBoxError(
                    "Multiple exact artifacts target {}: {} and {}".format(
                        destination, previous_payload, payload
                    )
                )
            claimed_destinations[destination_key] = str(payload)
            unchanged = _path_matches_fingerprint(destination, payload_hash)
            operations.append(
                (
                    entry,
                    payload,
                    destination,
                    unchanged,
                    "original/{}/{}/{}/{}".format(
                        config["_host"], entry["kind"], source_id, source_relative
                    ),
                )
            )
    return operations


def combine_exact_operations(
    operation_groups: Sequence[Sequence[tuple[dict, Path, Path, bool, str]]]
) -> list[tuple[dict, Path, Path, bool, str]]:
    combined = []
    destinations = {}
    for operations in operation_groups:
        for operation in operations:
            _, payload, destination, _, _ = operation
            key = str(destination.absolute())
            fingerprint = hash_path(payload)
            existing = destinations.get(key)
            if existing:
                existing_fingerprint, existing_payload = existing
                if fingerprint != existing_fingerprint:
                    raise AgentBoxError(
                        "Exact artifacts from different catalog selections both target {}: "
                        "{} and {}".format(destination, existing_payload, payload)
                    )
                continue
            destinations[key] = (fingerprint, payload)
            combined.append(operation)
    return combined


def restore_as_backed_up(
    config: dict,
    tool_name: str,
    dry_run: bool,
    operations: Sequence[tuple[dict, Path, Path, bool, str]],
    report: Reporter = console_report,
) -> int:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_root = config["_safety_backups"] / timestamp
    changes = 0

    for entry, payload, destination, unchanged, safety_relative in operations:
        if unchanged:
            report(
                OperationEvent(
                    "unchanged",
                    "UNCHANGED {} original {}".format(tool_name, destination),
                    tool_name,
                    str(destination),
                )
            )
            continue
        changes += 1
        report(
            OperationEvent(
                "restore",
                "RESTORE {} original {}".format(tool_name, destination),
                tool_name,
                str(destination),
            )
        )
        if dry_run:
            continue
        if destination.exists() or destination.is_symlink():
            safety_copy(destination, backup_root, tool_name, safety_relative)
        copy_exact(payload, destination)
    return changes


def status_tool(
    config: dict,
    tool_name: str,
    state: dict,
    portable_index: dict[tuple[str, str], list[dict]],
    report: Reporter = console_report,
) -> int:
    manifest = load_manifest(config, tool_name)
    own_keys = {(item["kind"], item["path"]) for item in manifest["artifacts"]}
    artifacts, errors, available_sources, _ = collect_tool_artifacts(
        config,
        tool_name,
        state,
        include_derived=False,
        portable_index=portable_index,
        own_catalog_keys=own_keys,
        report=report,
    )
    if not available_sources:
        report(
            OperationEvent(
                "no-sources",
                "{}: no configured source directories found".format(tool_name),
                tool_name,
            )
        )
    catalog_entries = {(item["kind"], item["path"]): item for item in manifest["artifacts"]}
    source_entries = {(item.kind, item.catalog_path): item for item in artifacts}
    drift = 0
    for _, artifact in sorted(source_entries.items()):
        catalog_payload = checked_join(
            config["_catalog"] / tool_name,
            safe_relative_path(artifact.catalog_path),
        )
        if not catalog_payload.exists():
            drift += 1
            report(
                OperationEvent(
                    "unbacked",
                    "UNBACKED {} {}".format(tool_name, artifact.catalog_path),
                    tool_name,
                    artifact.catalog_path,
                )
            )
        elif hash_path(catalog_payload) != hash_path(artifact.payload):
            drift += 1
            report(
                OperationEvent(
                    "different",
                    "DIFFERENT {} {}".format(tool_name, artifact.catalog_path),
                    tool_name,
                    artifact.catalog_path,
                )
            )
    for key, entry in sorted(catalog_entries.items()):
        if key not in source_entries:
            report(
                OperationEvent(
                    "catalog-only",
                    "CATALOG ONLY {} {}".format(tool_name, entry["path"]),
                    tool_name,
                    entry["path"],
                )
            )
    for error in errors:
        drift += 1
        report(OperationEvent("conflict", "CONFLICT {}".format(error), tool_name))
    if not drift:
        report(
            OperationEvent(
                "clean", "{}: no backup drift".format(tool_name), tool_name
            )
        )
    return drift


@contextmanager
def _operation_lock(identity: Path):
    resolved = identity.expanduser().resolve()
    if hasattr(os, "getuid"):
        lock_root = Path("/tmp") / "agentbox-locks-{}".format(os.getuid())
    else:  # pragma: no cover - exercised on Windows.
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path(tempfile.gettempdir())
        lock_root = base / "AgentBox" / "locks"
    try:
        lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if lock_root.is_symlink() or not lock_root.is_dir():
            raise AgentBoxError("Operation lock root is not a regular directory: {}".format(lock_root))
        if hasattr(os, "getuid") and lock_root.stat().st_uid != os.getuid():
            raise AgentBoxError("Operation lock root has an unexpected owner: {}".format(lock_root))
        lock_name = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest() + ".lock"
        lock_handle = (lock_root / lock_name).open("a+b")
    except OSError as exc:
        raise AgentBoxError("Cannot lock {}: {}".format(resolved, exc))
    with lock_handle:
        if fcntl is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - exercised on Windows.
            lock_handle.seek(0, os.SEEK_END)
            if lock_handle.tell() == 0:
                lock_handle.write(b"\0")
                lock_handle.flush()
            while True:
                try:
                    lock_handle.seek(0)
                    msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        else:  # pragma: no cover - supported platforms provide one implementation.
            raise AgentBoxError("This platform does not provide filesystem operation locking")
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - exercised on Windows.
                lock_handle.seek(0)
                msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def operation_guard(config_path: Path, *storage_identities: Path):
    """Serialize operations that share a configuration or storage destination."""
    identities = sorted(
        {config_path.expanduser().resolve(), *(item.expanduser().resolve() for item in storage_identities)},
        key=str,
    )
    with ExitStack() as stack:
        for identity in identities:
            stack.enter_context(_operation_lock(identity))
        yield


def application_lock_identity() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return user_state_root() / "application-operation"


@contextmanager
def application_operation_guard(*identities: Path):
    """Serialize AgentBox operations across configurations and storage roots."""
    with operation_guard(application_lock_identity(), *identities):
        yield


def _run_catalog_operation(
    config: dict,
    request: OperationRequest,
    report: Reporter = console_report,
    pre_apply: Callable[[], None] | None = None,
) -> int:
    tool_names = sorted(config["tools"])
    if request.action not in ("backup", "restore", "status"):
        raise AgentBoxError("Unknown action: {}".format(request.action))
    if request.tool != "all" and request.tool not in tool_names:
        raise AgentBoxError("Unknown tool: {}".format(request.tool))
    if request.source_tool and request.source_tool not in tool_names:
        raise AgentBoxError("Unknown source tool: {}".format(request.source_tool))
    if request.source_tool and request.all_tools:
        raise AgentBoxError("A source tool and all tools cannot both be selected")

    state = load_state(config)
    selected = tool_names if request.tool == "all" else [request.tool]
    if request.action == "restore" and not request.as_backed_up and request.tool == "all":
        selected = [
            tool_name
            for tool_name in selected
            if config["tools"][tool_name]["skills"]["_enabled"]
        ]
        if not selected:
            raise AgentBoxError("Portable restore requires at least one provider with skills enabled")
    total = 0
    if request.action == "backup":
        prepared = []
        all_errors = []
        portable_index = build_portable_index(config, tool_names)
        receipts_to_clear = []
        for tool_name in selected:
            manifest = load_manifest(config, tool_name)
            own_keys = {(item["kind"], item["path"]) for item in manifest["artifacts"]}
            artifacts, errors, available_sources, clear = collect_tool_artifacts(
                config,
                tool_name,
                state,
                request.include_derived,
                portable_index,
                own_keys,
                report,
            )
            prepared.append((tool_name, artifacts, available_sources))
            all_errors.extend(errors)
            receipts_to_clear.extend(clear)
        if all_errors:
            raise AgentBoxError("\n".join(all_errors))
        for tool_name, artifacts, available_sources in prepared:
            if config["_storage_git_url"] is not None:
                for artifact in artifacts:
                    validate_git_payload(artifact.payload)
            validate_backup_update(config, tool_name, artifacts, available_sources)
        if pre_apply is not None and not request.dry_run:
            pre_apply()
        for tool_name, artifacts, available_sources in prepared:
            total += backup_tool(
                config,
                tool_name,
                artifacts,
                available_sources,
                request.dry_run,
                request.prune,
                report,
            )
        if receipts_to_clear and not request.dry_run:
            for path in receipts_to_clear:
                state["deployments"].pop(path, None)
            write_json(config["_state_file"], state)
        return total

    if request.action == "restore":
        source_hosts = catalog_hosts(config) if request.all_hosts else [config["_host"]]
        if request.as_backed_up and (request.source_tool or request.all_tools):
            raise AgentBoxError(
                "--from and --all-tools cannot be combined with --as-backed-up"
            )
        if request.as_backed_up:
            prepared_exact = {}
            for tool_name in selected:
                host_operations = [
                    prepare_exact_restore(
                        config_for_host(config, host), tool_name, request.force
                    )
                    for host in source_hosts
                ]
                prepared_exact[tool_name] = combine_exact_operations(host_operations)
            combine_exact_operations(list(prepared_exact.values()))
            if pre_apply is not None and not request.dry_run:
                pre_apply()
            for tool_name in selected:
                total += restore_as_backed_up(
                    config,
                    tool_name,
                    request.dry_run,
                    prepared_exact[tool_name],
                    report,
                )
        else:
            prepared_portable = {}
            for tool_name in selected:
                source_tools = (
                    tool_names if request.all_tools else [request.source_tool or tool_name]
                )
                prepared_portable[tool_name] = prepare_portable_restore(
                    config,
                    tool_name,
                    source_hosts,
                    source_tools,
                    request.force,
                )
            validate_portable_target_operations(prepared_portable)
            if pre_apply is not None and not request.dry_run:
                pre_apply()
            for tool_name in selected:
                total += restore_portable(
                    config,
                    tool_name,
                    request.dry_run,
                    state,
                    prepared_portable[tool_name],
                    report,
                )
            if not request.dry_run:
                write_json(config["_state_file"], state)
        return total

    portable_index = build_portable_index(config, tool_names)
    for tool_name in selected:
        total += status_tool(config, tool_name, state, portable_index, report)
    return total


def _file_snapshot(path: Path) -> tuple[bool, bytes, int]:
    if not path.exists():
        return False, b"", 0
    if path.is_symlink() or not path.is_file():
        raise AgentBoxError("State path is not a regular file: {}".format(path))
    return True, path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def _restore_file_snapshot(path: Path, snapshot: tuple[bool, bytes, int]) -> None:
    existed, content, mode = snapshot
    if not existed:
        if path.exists() or path.is_symlink():
            remove_path(path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("{}.rollback-{}".format(path.name, uuid.uuid4().hex))
    try:
        temporary.write_bytes(content)
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _requested_storage(request: OperationRequest) -> dict[str, str]:
    storage = {}
    if request.storage_local is not None:
        storage["local"] = request.storage_local.strip()
    if request.storage_git is not None:
        storage["git"] = request.storage_git.strip()
    return storage


def _configured_storage_update(config_path: Path, request: OperationRequest) -> dict:
    candidate = load_json(config_path, {})
    candidate["storage"] = _requested_storage(request)
    local_root, git_url, _ = _parse_storage(candidate, config_path.parent)
    _history_storage(local_root, git_url)
    return candidate


def write_config(path: Path, value: dict) -> None:
    if path.is_symlink():
        raise AgentBoxError("Symlinked configuration files are not supported: {}".format(path))
    for parent in (path.parent, *path.parents):
        if parent.is_symlink():
            raise AgentBoxError("Symlinked configuration directories are not supported: {}".format(parent))
        if parent.exists() and not parent.is_dir():
            raise AgentBoxError("Configuration parent is not a directory: {}".format(parent))
    write_json(path, value, create_mode=0o600)


def _configured_provider_update(config_path: Path, request: OperationRequest) -> dict:
    definitions = {
        provider["id"]: provider
        for provider in load_provider_definitions()
        if provider["mode"] == "managed"
    }
    selected = set(request.provider_resources)
    valid = {
        "{}.{}".format(provider_id, kind)
        for provider_id in definitions
        for kind in ("skills", "commands")
    }
    if not selected or selected - valid:
        raise AgentBoxError("Select at least one valid provider resource")
    if request.storage_local is None and request.storage_git is None:
        raise AgentBoxError("Select local storage, Git storage, or both")
    candidate = load_json(config_path, {}) if config_path.exists() else {}
    candidate.pop("tools", None)
    candidate["version"] = CONFIG_VERSION
    candidate["providers"] = {
        provider_id: {
            "enabled": any(
                "{}.{}".format(provider_id, kind) in selected
                for kind in ("skills", "commands")
            ),
            "resources": {
                kind: "{}.{}".format(provider_id, kind) in selected
                for kind in ("skills", "commands")
            },
        }
        for provider_id in definitions
    }
    candidate["storage"] = _requested_storage(request)
    local_root, git_url, _ = _parse_storage(candidate, config_path.parent)
    _history_storage(local_root, git_url)
    return candidate


def configure_providers(
    config_path: Path,
    request: OperationRequest,
    report: Reporter,
    pre_plan: Callable[[], None] | None,
    pre_apply: Callable[[], None] | None,
) -> int:
    candidate = _configured_provider_update(config_path, request)
    current = load_json(config_path, {}) if config_path.exists() else {}
    if pre_plan is not None:
        pre_plan()
    selected = set(request.provider_resources)
    for provider in load_provider_definitions():
        if provider["mode"] != "managed":
            continue
        enabled = [
            provider["resources"][kind].get("name", kind.title())
            for kind in ("skills", "commands")
            if "{}.{}".format(provider["id"], kind) in selected
        ]
        state = ", ".join(enabled) if enabled else "disabled"
        report(
            OperationEvent(
                "provider-config",
                "PROVIDER {}: {}".format(provider["name"], state),
                provider["id"],
            )
        )
    storage = candidate["storage"]
    if "local" in storage:
        report(OperationEvent("storage-config", "LOCAL STORAGE {}".format(storage["local"])))
    if "git" in storage:
        report(
            OperationEvent(
                "storage-config", "GIT STORAGE {}".format(redacted_git_url(storage["git"]))
            )
        )
    if candidate == current:
        report(OperationEvent("unchanged", "PROVIDER CONFIGURATION UNCHANGED"))
        return 0
    report(OperationEvent("provider-config", "UPDATE PROVIDER CONFIGURATION"))
    if not request.dry_run:
        if pre_apply is not None:
            pre_apply()
        write_config(config_path, candidate)
    return 1


def configure_storage(
    config_path: Path,
    request: OperationRequest,
    report: Reporter,
    pre_plan: Callable[[], None] | None,
    pre_apply: Callable[[], None] | None,
) -> int:
    candidate = _configured_storage_update(config_path, request)
    current = load_json(config_path, {})
    if pre_plan is not None:
        pre_plan()
    storage = candidate["storage"]
    if "local" in storage:
        report(OperationEvent("storage-config", "LOCAL STORAGE {}".format(storage["local"])))
    else:
        report(OperationEvent("storage-config", "LOCAL STORAGE DISABLED"))
    if "git" in storage:
        report(
            OperationEvent(
                "storage-config",
                "GIT STORAGE {}".format(redacted_git_url(storage["git"])),
            )
        )
    else:
        report(OperationEvent("storage-config", "GIT STORAGE DISABLED"))
    if candidate == current:
        report(OperationEvent("unchanged", "STORAGE CONFIGURATION UNCHANGED"))
        return 0
    report(OperationEvent("storage-config", "UPDATE STORAGE CONFIGURATION"))
    if not request.dry_run:
        if pre_apply is not None:
            pre_apply()
        write_config(config_path, candidate)
    return 1


def run_operation(
    config_path: Path,
    request: OperationRequest,
    report: Reporter = console_report,
    acquire_lock: bool = True,
    pre_plan: Callable[[], None] | None = None,
    pre_apply: Callable[[], None] | None = None,
) -> int:
    """Run one validated operation and return its change or drift count."""
    config_path = config_path.expanduser().resolve()
    if request.action == "providers":
        if acquire_lock:
            preliminary = load_config(config_path, request.host) if config_path.exists() else None
            locked_identities = (
                storage_lock_identities(preliminary) if preliminary is not None else []
            )
            with application_operation_guard(config_path, *locked_identities):
                if preliminary is not None:
                    current = load_config(config_path, request.host)
                    if {
                        item.expanduser().resolve()
                        for item in storage_lock_identities(current)
                    } != {
                        item.expanduser().resolve() for item in locked_identities
                    }:
                        raise AgentBoxError(
                            "Storage configuration changed while waiting; retry the operation"
                        )
                return run_operation(
                    config_path,
                    request,
                    report,
                    acquire_lock=False,
                    pre_plan=pre_plan,
                    pre_apply=pre_apply,
                )
        if config_path.exists():
            current = load_config(config_path, request.host)
            if current.get("_history_enabled"):
                _recover_history_transactions(current)
        return configure_providers(config_path, request, report, pre_plan, pre_apply)
    if acquire_lock:
        preliminary = load_config(config_path, request.host)
        locked_identities = storage_lock_identities(preliminary)
        with application_operation_guard(config_path, *locked_identities):
            current = load_config(config_path, request.host)
            if {
                item.expanduser().resolve() for item in storage_lock_identities(current)
            } != {item.expanduser().resolve() for item in locked_identities}:
                raise AgentBoxError("Storage configuration changed while waiting; retry the operation")
            return run_operation(
                config_path,
                request,
                report,
                acquire_lock=False,
                pre_plan=pre_plan,
                pre_apply=pre_apply,
            )

    config = load_config(config_path, request.host)
    if config.get("_history_enabled"):
        _recover_history_transactions(config)
    if request.action == "storage":
        return configure_storage(config_path, request, report, pre_plan, pre_apply)
    if request.action not in ("backup", "restore", "status"):
        raise AgentBoxError("Unknown action: {}".format(request.action))
    if request.catalog_revision is not None and request.action != "restore":
        raise AgentBoxError("A catalog revision can be selected only for restore")
    session = prepare_storage(config, report)
    if pre_plan is not None:
        pre_plan()

    def apply_with_storage() -> None:
        if pre_apply is not None:
            pre_apply()
        if request.action == "restore" and session.uses_git:
            initialize_storage_for_restore(session, report)

    if request.catalog_revision is not None:
        if session.uses_git or not config.get("_history_enabled"):
            raise AgentBoxError(
                "Native catalog revisions are available only with local-only storage"
            )
        report(
            OperationEvent(
                "revision",
                "USING CATALOG REVISION {}".format(request.catalog_revision),
                artifact=request.catalog_revision,
            )
        )
        with materialized_catalog_revision(config, request.catalog_revision) as historical_root:
            historical = dict(config)
            historical["_catalog_revision"] = request.catalog_revision
            _select_catalog_root(historical, historical_root)
            return _run_catalog_operation(
                historical, request, report, apply_with_storage
            )

    if request.action == "backup" and config.get("_history_enabled"):
        report(
            OperationEvent(
                "history",
                "LOCAL HISTORY ACTIVE FOR CHANGED BACKUPS",
            )
        )
        if request.dry_run:
            return _run_catalog_operation(config, request, report, apply_with_storage)
        if session.local_root is None:
            raise AgentBoxError("Local history requires a local catalog")
        state_snapshot = _file_snapshot(config["_state_file"])
        _, before_snapshot, _ = _catalog_snapshot_document(session.local_root)
        with staged_catalog(session.local_root) as staged, optional_catalog_snapshot(
            session.local_root
        ) as (local_snapshot, local_existed):
            working_config = dict(config)
            _select_catalog_root(working_config, staged)
            staged_state = staged.parent / "state.json"
            _restore_file_snapshot(staged_state, state_snapshot)
            working_config["_state_file"] = staged_state
            local_updated = False
            revision_published = False
            transaction_path = None
            try:
                total = _run_catalog_operation(
                    working_config, request, report, apply_with_storage
                )
                updated_state = _file_snapshot(staged_state)
                _, after_snapshot, _ = _catalog_snapshot_document(staged)
                if after_snapshot == before_snapshot:
                    if updated_state != state_snapshot:
                        _restore_file_snapshot(config["_state_file"], updated_state)
                    return total
                summary, revision_content = _prepare_catalog_revision(
                    config, staged, request, total
                )
                transaction_path = _begin_history_transaction(
                    config,
                    summary,
                    revision_content,
                    before_snapshot,
                    state_snapshot,
                    updated_state,
                )
                local_updated = True
                replace_catalog(staged, session.local_root, summary.revision_id)
                _, installed_snapshot, _ = _catalog_snapshot_document(session.local_root)
                if installed_snapshot != summary.snapshot:
                    raise AgentBoxError("Installed catalog failed history verification")
                _restore_file_snapshot(config["_state_file"], updated_state)
                _publish_catalog_revision(config, summary, revision_content)
                revision_published = True
                try:
                    transaction_path.unlink()
                except OSError:
                    report(
                        OperationEvent(
                            "history-warning",
                            "CATALOG REVISION RECOVERY MARKER WILL BE CLEANED LATER",
                        )
                    )
                report(
                    OperationEvent(
                        "revision",
                        "CREATED CATALOG REVISION {}".format(summary.revision_id),
                        artifact=summary.revision_id,
                    )
                )
                try:
                    _apply_history_retention(config, summary.revision_id)
                except (AgentBoxError, OSError):
                    report(
                        OperationEvent(
                            "history-warning",
                            "CATALOG REVISION RETENTION WILL RETRY LATER",
                        )
                    )
                return total
            except Exception as operation_error:
                if revision_published:
                    raise
                rollback_errors = []
                if local_updated and local_snapshot is not None:
                    try:
                        restore_catalog_snapshot(
                            local_snapshot, local_existed, session.local_root
                        )
                    except Exception as exc:
                        rollback_errors.append(exc)
                try:
                    _restore_file_snapshot(config["_state_file"], state_snapshot)
                except Exception as exc:
                    rollback_errors.append(exc)
                if not rollback_errors and transaction_path is not None:
                    try:
                        transaction_path.unlink(missing_ok=True)
                    except Exception as exc:
                        rollback_errors.append(exc)
                if rollback_errors:
                    raise AgentBoxError(
                        "Local history operation failed and rollback was incomplete: {}".format(
                            "; ".join(str(error) for error in rollback_errors)
                        )
                    ) from operation_error
                raise

    if request.action == "backup" and session.uses_git:
        report(OperationEvent("storage", "GIT BACKUP WILL COMMIT AND PUSH CATALOG CHANGES"))
        if request.dry_run:
            return _run_catalog_operation(config, request, report, apply_with_storage)
        state_snapshot = _file_snapshot(config["_state_file"])
        with staged_catalog(session.canonical_root) as staged, optional_catalog_snapshot(
            session.local_root
        ) as (local_snapshot, local_existed):
            working_config = dict(config)
            _select_catalog_root(working_config, staged)
            local_updated = False
            try:
                total = _run_catalog_operation(
                    working_config, request, report, apply_with_storage
                )
                if session.local_root is not None:
                    local_updated = True
                    replace_catalog(staged, session.local_root)
                commit_git_catalog(session, staged, report)
                return total
            except Exception as operation_error:
                rollback_errors = []
                if (
                    local_updated
                    and not session.git_pushed
                    and not session.git_uncertain
                    and local_snapshot is not None
                    and session.local_root is not None
                ):
                    try:
                        restore_catalog_snapshot(
                            local_snapshot, local_existed, session.local_root
                        )
                    except Exception as exc:
                        rollback_errors.append(exc)
                if not session.git_pushed and not session.git_uncertain:
                    try:
                        _restore_file_snapshot(config["_state_file"], state_snapshot)
                    except Exception as exc:
                        rollback_errors.append(exc)
                if rollback_errors:
                    raise AgentBoxError(
                        "Storage operation failed and rollback was incomplete: {}".format(
                            "; ".join(str(error) for error in rollback_errors)
                        )
                    ) from operation_error
                raise

    return _run_catalog_operation(config, request, report, apply_with_storage)
