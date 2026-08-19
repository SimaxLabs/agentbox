#!/usr/bin/env python3
"""Back up and restore AI coding-agent skills and commands."""

import ast
from contextlib import contextmanager
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl.
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX has no msvcrt.
    msvcrt = None


VERSION = 1
SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
CONFIG_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PLACEHOLDER = re.compile(r"\$(?:ARGUMENTS(?:\[[0-9]+\])?|[A-Z][A-Z0-9_]*|[0-9]+)")


class AiKitError(Exception):
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
    sources: List[Source] = field(default_factory=list)
    host: str = ""


@dataclass
class Candidate:
    name: str
    payload: Optional[Path]
    generated: Optional[bytes]
    origins: List[Dict[str, str]]

    def fingerprint(self) -> str:
        if self.generated is not None:
            return hash_generated_skill(self.generated)
        if self.payload is None:
            raise AiKitError("Restore candidate has no payload")
        return hash_path(self.payload)


@dataclass(frozen=True)
class OperationEvent:
    kind: str
    message: str
    tool: str = ""
    artifact: str = ""


Reporter = Callable[[OperationEvent], None]


def console_report(event: OperationEvent) -> None:
    print(event.message)


@dataclass(frozen=True)
class OperationRequest:
    action: str
    tool: str = "all"
    host: Optional[str] = None
    dry_run: bool = False
    prune: bool = False
    include_derived: bool = False
    source_tool: Optional[str] = None
    all_tools: bool = False
    all_hosts: bool = False
    as_backed_up: bool = False
    force: bool = False


def expand_path(value: str, base: Path) -> Path:
    expanded = Path(os.path.expandvars(os.path.expanduser(value)))
    if not expanded.is_absolute():
        expanded = base / expanded
    return expanded


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AiKitError("Cannot read {}: {}".format(path, exc))


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("{}.tmp-{}".format(path.name, uuid.uuid4().hex))
    try:
        temporary.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_config(path: Path, host_override: Optional[str] = None) -> dict:
    config = load_json(path, {})
    if config.get("version") != VERSION:
        raise AiKitError("{} must declare version {}".format(path, VERSION))
    tools = config.get("tools")
    if not isinstance(tools, dict) or not tools:
        raise AiKitError("{} must define at least one tool".format(path))
    base = path.parent
    host = (
        host_override
        or os.environ.get("AI_KIT_HOST")
        or config.get("host")
        or socket.gethostname()
    )
    if not isinstance(host, str):
        raise AiKitError("Host namespace must be a string")
    host = re.sub(r"[^A-Za-z0-9._-]+", "-", host).strip(".-_")
    if not host or not CONFIG_NAME.fullmatch(host):
        raise AiKitError("Invalid host namespace: {!r}".format(host))
    config["_base"] = base
    config["_host"] = host
    config["_catalog_root"] = expand_path(config.get("catalog", "catalog"), base)
    if config["_catalog_root"].is_symlink():
        raise AiKitError("Symlinked catalog roots are not supported: {}".format(config["_catalog_root"]))
    config["_catalog"] = config["_catalog_root"] / host
    if config["_catalog"].is_symlink():
        raise AiKitError("Symlinked host catalogs are not supported: {}".format(config["_catalog"]))
    config["_state_file"] = expand_path(
        config.get("state_file", "~/.local/state/ai-kit/state.json"), base
    )
    config["_safety_backups"] = expand_path(
        config.get("safety_backups", "~/.local/state/ai-kit/backups"), base
    )
    for tool_name, tool in tools.items():
        if not CONFIG_NAME.fullmatch(tool_name):
            raise AiKitError("Invalid tool name in {}: {}".format(path, tool_name))
        for kind in ("skills", "commands"):
            section = tool.get(kind)
            if not isinstance(section, dict):
                raise AiKitError("{}.{} is missing from {}".format(tool_name, kind, path))
            section["_target"] = expand_path(section["target"], base)
            seen = set()
            parsed_sources = []
            for source in section.get("sources", []):
                source_id = source.get("id")
                if (
                    not isinstance(source_id, str)
                    or not CONFIG_NAME.fullmatch(source_id)
                    or source_id in seen
                ):
                    raise AiKitError("Source IDs for {}.{} must be unique".format(tool_name, kind))
                seen.add(source_id)
                parsed_sources.append(
                    {"id": source_id, "path": expand_path(source["path"], base)}
                )
            section["_sources"] = parsed_sources
    return config


def config_for_host(config: dict, host: str) -> dict:
    if not CONFIG_NAME.fullmatch(host):
        raise AiKitError("Invalid host namespace: {!r}".format(host))
    selected = dict(config)
    selected["_host"] = host
    selected["_catalog"] = config["_catalog_root"] / host
    if selected["_catalog"].is_symlink():
        raise AiKitError("Symlinked host catalogs are not supported: {}".format(selected["_catalog"]))
    return selected


def catalog_hosts(config: dict) -> List[str]:
    root = config["_catalog_root"]
    if not root.exists():
        return []
    if not root.is_dir():
        raise AiKitError("Catalog root is not a directory: {}".format(root))
    hosts = []
    for entry in sorted(root.iterdir()):
        if entry.is_symlink():
            raise AiKitError("Symlinked host catalogs are not supported: {}".format(entry))
        if not entry.is_dir():
            continue
        if not CONFIG_NAME.fullmatch(entry.name):
            raise AiKitError("Invalid host catalog name: {}".format(entry.name))
        hosts.append(entry.name)
    return hosts


def hash_generated_skill(content: bytes) -> str:
    digest = hashlib.sha256()
    update_hash_entry(digest, ".", "directory", 0o755, b"")
    update_hash_entry(digest, "SKILL.md", "file", 0o644, content)
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
        raise AiKitError("Symlinked artifacts are not supported: {}".format(path))
    if path.is_file():
        return
    if not path.is_dir():
        raise AiKitError("Unsupported payload: {}".format(path))
    for current, directories, files in os.walk(str(path), followlinks=False):
        current_path = Path(current)
        for name in directories + files:
            child = current_path / name
            if child.is_symlink():
                raise AiKitError("Symlinked artifact content is not supported: {}".format(child))
            if not child.is_dir() and not child.is_file():
                raise AiKitError("Unsupported artifact content: {}".format(child))


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


def split_frontmatter(content: str) -> Tuple[Dict[str, str], str]:
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
        raise AiKitError("Cannot read {}: {}".format(skill_file, exc))
    name = metadata.get("name", skill_dir.name).strip()
    if not SKILL_NAME.fullmatch(name):
        raise AiKitError("Invalid skill name {!r} in {}".format(name, skill_file))
    return name


def command_name(relative_path: str) -> str:
    path = Path(relative_path)
    raw = "-".join(path.with_suffix("").parts).lower()
    name = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    if not name:
        raise AiKitError("Cannot derive a skill name from {}".format(relative_path))
    return name


def walk_skill_dirs(root: Path) -> Iterable[Path]:
    def walk_error(error: OSError) -> None:
        raise AiKitError("Cannot scan skill source {}: {}".format(root, error))

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
        raise AiKitError("Manifest paths must be non-empty strings")
    path = Path(value)
    if path in (Path("."), Path("")) or path.is_absolute() or ".." in path.parts:
        raise AiKitError("Unsafe relative path in manifest: {}".format(value))
    return path


def checked_join(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise AiKitError("Unsafe relative path: {}".format(relative))
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise AiKitError("Refusing path through symlink: {}".format(current))
    candidate = root / relative
    root_real = root.resolve(strict=False)
    parent_real = candidate.parent.resolve(strict=False)
    try:
        common = Path(os.path.commonpath([str(root_real), str(parent_real)]))
    except ValueError:
        raise AiKitError("Path escapes configured root: {}".format(candidate))
    if common != root_real:
        raise AiKitError("Path escapes configured root: {}".format(candidate))
    return candidate


def require_regular_root(root: Path, label: str) -> None:
    if root.is_symlink():
        raise AiKitError("Symlinked {} roots are not supported: {}".format(label, root))
    if root.exists() and not root.is_dir():
        raise AiKitError("{} root is not a directory: {}".format(label.capitalize(), root))


def load_state(config: dict) -> dict:
    state = load_json(config["_state_file"], {"version": VERSION, "deployments": {}})
    if state.get("version") != VERSION:
        return {"version": VERSION, "deployments": {}}
    state.setdefault("deployments", {})
    return state


def receipt_for(state: dict, physical_path: Path) -> Optional[dict]:
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
            host_root = config["_catalog_root"] / host
            if host_root.is_symlink():
                continue
            payload = checked_join(host_root / tool, relative)
            if not payload.exists():
                continue
            name = read_skill_name(payload) if kind == "skill" else command_name(
                payload.relative_to(host_root / tool / "commands").as_posix()
            )
            artifact = Artifact(tool, kind, name, relative.as_posix(), payload, host=host)
            if artifact_portable_fingerprint(artifact) == expected:
                return True
        except (AiKitError, ValueError):
            continue
    return False


def collect_tool_artifacts(
    config: dict,
    tool_name: str,
    state: dict,
    include_derived: bool,
    portable_index: Dict[Tuple[str, str], List[dict]],
    own_catalog_keys: set,
    report: Reporter = console_report,
) -> Tuple[List[Artifact], List[str], set, List[str]]:
    tool = config["tools"][tool_name]
    artifacts: Dict[Tuple[str, str], Artifact] = {}
    errors = []
    available_sources = set()
    receipts_to_clear = []

    for kind in ("skills", "commands"):
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
                            "{} was restored by ai-kit and then modified; use --include-derived "
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
                    if (
                        current_key in own_catalog_keys
                        and matches_portable
                        and not include_derived
                    ):
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
                    if (
                        current_key not in own_catalog_keys
                        and matches_portable
                        and not include_derived
                    ):
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
                key = current_key
                existing = artifacts.get(key)
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
                    artifacts[key] = Artifact(
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
        raise AiKitError("Symlinked catalog paths are not supported for {}".format(tool_name))
    manifest = load_json(path, {"version": VERSION, "artifacts": []})
    if manifest.get("version") != VERSION:
        raise AiKitError("Unsupported manifest version for {}".format(tool_name))
    artifacts = manifest.setdefault("artifacts", [])
    if not isinstance(artifacts, list):
        raise AiKitError("Manifest artifacts for {} must be a list".format(tool_name))
    seen_paths = set()
    for entry in artifacts:
        if not isinstance(entry, dict) or entry.get("kind") not in ("skill", "command"):
            raise AiKitError("Invalid artifact entry in {} manifest".format(tool_name))
        name = entry.get("name")
        if not isinstance(name, str) or not SKILL_NAME.fullmatch(name):
            raise AiKitError("Invalid artifact name in {} manifest: {!r}".format(tool_name, name))
        entry_path = safe_relative_path(entry.get("path"))
        if entry_path in seen_paths:
            raise AiKitError("Duplicate artifact path in {} manifest: {}".format(tool_name, entry_path))
        seen_paths.add(entry_path)
        if entry["kind"] == "skill":
            expected = Path("skills") / name
            if entry_path != expected:
                raise AiKitError(
                    "Skill manifest path must be {}: {}".format(expected, entry_path)
                )
        elif entry_path.parts[0] != "commands" or entry_path.suffix != ".md":
            raise AiKitError("Command manifest path must be under commands/: {}".format(entry_path))
        sources = entry.get("sources", [])
        if not isinstance(sources, list):
            raise AiKitError("Artifact sources must be a list: {}".format(entry_path))
        for source in sources:
            if (
                not isinstance(source, dict)
                or not isinstance(source.get("id"), str)
                or not CONFIG_NAME.fullmatch(source["id"])
            ):
                raise AiKitError("Invalid source entry for {}".format(entry_path))
            safe_relative_path(source.get("relative_path"))
    return manifest


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(str(path))


def install_staged(staged: Path, destination: Path, token: str) -> None:
    previous = destination.with_name(".{}.ai-kit-old-{}".format(destination.name, token))
    had_destination = destination.exists() or destination.is_symlink()
    if had_destination:
        destination.rename(previous)
    try:
        staged.rename(destination)
    except Exception:
        if had_destination and previous.exists():
            previous.rename(destination)
        raise
    if had_destination and previous.exists():
        remove_path(previous)


def copy_exact(source: Path, destination: Path) -> None:
    validate_regular_payload(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    staged = destination.with_name(".{}.ai-kit-new-{}".format(destination.name, token))
    try:
        if source.is_dir():
            shutil.copytree(str(source), str(staged), symlinks=False)
        else:
            shutil.copy2(str(source), str(staged))
        install_staged(staged, destination, token)
    finally:
        if staged.exists() or staged.is_symlink():
            remove_path(staged)


def write_generated_skill(content: bytes, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    staged = destination.with_name(".{}.ai-kit-new-{}".format(destination.name, token))
    try:
        staged.mkdir(mode=0o755)
        staged.chmod(0o755)
        skill_file = staged / "SKILL.md"
        skill_file.write_bytes(content)
        skill_file.chmod(0o644)
        install_staged(staged, destination, token)
    finally:
        if staged.exists() or staged.is_symlink():
            remove_path(staged)


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
            raise AiKitError(
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


def scan_catalog(config: dict, source_tools: Sequence[str]) -> List[Artifact]:
    artifacts = []
    for tool_name in source_tools:
        root = config["_catalog"] / tool_name
        if root.is_symlink():
            raise AiKitError("Symlinked catalog paths are not supported for {}".format(tool_name))
        skills = root / "skills"
        if skills.exists():
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
                    )
                )
        commands = root / "commands"
        if commands.exists():
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
                    )
                )
    return artifacts


def scan_catalog_hosts(
    config: dict, source_hosts: Sequence[str], source_tools: Sequence[str]
) -> List[Artifact]:
    artifacts = []
    for host in source_hosts:
        artifacts.extend(scan_catalog(config_for_host(config, host), source_tools))
    return artifacts


def convert_command(artifact: Artifact) -> bytes:
    try:
        content = artifact.payload.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AiKitError("Cannot convert {}: {}".format(artifact.payload, exc))
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


def build_candidates(artifacts: Sequence[Artifact]) -> Tuple[List[Candidate], List[str]]:
    candidates: Dict[str, Candidate] = {}
    errors = []
    for artifact in artifacts:
        origin = {
            "host": artifact.host,
            "tool": artifact.tool,
            "kind": artifact.kind,
            "path": artifact.catalog_path,
        }
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
    source_hosts: Optional[Sequence[str]] = None,
) -> Dict[Tuple[str, str], List[dict]]:
    index: Dict[Tuple[str, str], List[dict]] = {}
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
) -> List[Tuple[Candidate, Path, str, bool]]:
    artifacts = scan_catalog_hosts(config, source_hosts, source_tools)
    candidates, errors = build_candidates(artifacts)
    if errors:
        raise AiKitError("\n".join(errors))
    target_root = config["tools"][target_tool]["skills"]["_target"]
    require_regular_root(target_root, "restore target")
    operations = []
    for candidate in candidates:
        destination = checked_join(target_root, Path(candidate.name))
        if destination.is_symlink() and not force:
            raise AiKitError(
                "Refusing to replace symlink {} without --force".format(destination)
            )
        candidate_hash = candidate.fingerprint()
        unchanged = (
            destination.exists()
            and not destination.is_symlink()
            and hash_path(destination) == candidate_hash
        )
        operations.append((candidate, destination, candidate_hash, unchanged))
    return operations


def restore_portable(
    config: dict,
    target_tool: str,
    dry_run: bool,
    state: dict,
    operations: Sequence[Tuple[Candidate, Path, str, bool]],
    report: Reporter = console_report,
) -> int:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
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
    prepared: Dict[str, Sequence[Tuple[Candidate, Path, str, bool]]]
) -> None:
    destinations = {}
    for target_tool, operations in prepared.items():
        for _, destination, _, _ in operations:
            key = str(destination.absolute())
            previous = destinations.get(key)
            if previous and previous != target_tool:
                raise AiKitError(
                    "Restore targets {} and {} both map to {}; restore them separately "
                    "or configure distinct target roots".format(
                        previous, target_tool, destination
                    )
                )
            destinations[key] = target_tool


def configured_source_root(config: dict, tool_name: str, kind: str, source_id: str) -> Optional[Path]:
    section = config["tools"][tool_name]["skills" if kind == "skill" else "commands"]
    for source in section["_sources"]:
        if source["id"] == source_id:
            return source["path"]
    return None


def prepare_exact_restore(
    config: dict,
    tool_name: str,
    force: bool,
) -> List[Tuple[dict, Path, Path, bool, str]]:
    manifest = load_manifest(config, tool_name)
    tool_root = config["_catalog"] / tool_name
    operations = []
    claimed_destinations = {}
    for entry in manifest["artifacts"]:
        entry_path = safe_relative_path(entry["path"])
        payload = checked_join(tool_root, entry_path)
        if not payload.exists():
            raise AiKitError("Catalog payload is missing: {}".format(payload))
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
                raise AiKitError(
                    "Refusing to replace symlink {} without --force".format(destination)
                )
            previous_payload = claimed_destinations.get(destination_key)
            if previous_payload and previous_payload != str(payload):
                raise AiKitError(
                    "Multiple exact artifacts target {}: {} and {}".format(
                        destination, previous_payload, payload
                    )
                )
            claimed_destinations[destination_key] = str(payload)
            unchanged = (
                destination.exists()
                and not destination.is_symlink()
                and hash_path(destination) == payload_hash
            )
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
    operation_groups: Sequence[Sequence[Tuple[dict, Path, Path, bool, str]]]
) -> List[Tuple[dict, Path, Path, bool, str]]:
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
                    raise AiKitError(
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
    operations: Sequence[Tuple[dict, Path, Path, bool, str]],
    report: Reporter = console_report,
) -> int:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
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
    portable_index: Dict[Tuple[str, str], List[dict]],
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
    for key, artifact in sorted(source_entries.items()):
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
def operation_guard(config_path: Path):
    """Serialize operations that use the same configuration file."""
    resolved = config_path.expanduser().resolve()
    if hasattr(os, "getuid"):
        lock_root = Path("/tmp") / "ai-kit-locks-{}".format(os.getuid())
    else:  # pragma: no cover - exercised on Windows.
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path(tempfile.gettempdir())
        lock_root = base / "AI Kit" / "locks"
    try:
        lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if lock_root.is_symlink() or not lock_root.is_dir():
            raise AiKitError("Operation lock root is not a regular directory: {}".format(lock_root))
        if hasattr(os, "getuid") and lock_root.stat().st_uid != os.getuid():
            raise AiKitError("Operation lock root has an unexpected owner: {}".format(lock_root))
        lock_name = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest() + ".lock"
        lock_handle = (lock_root / lock_name).open("a+b")
    except OSError as exc:
        raise AiKitError("Cannot lock {}: {}".format(resolved, exc))
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
            raise AiKitError("This platform does not provide filesystem operation locking")
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - exercised on Windows.
                lock_handle.seek(0)
                msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)


def run_operation(
    config_path: Path,
    request: OperationRequest,
    report: Reporter = console_report,
    acquire_lock: bool = True,
    pre_apply: Optional[Callable[[], None]] = None,
) -> int:
    """Run one validated operation and return its change or drift count."""
    config_path = config_path.expanduser().resolve()
    if acquire_lock:
        with operation_guard(config_path):
            return run_operation(
                config_path,
                request,
                report,
                acquire_lock=False,
                pre_apply=pre_apply,
            )
    config = load_config(config_path, request.host)
    tool_names = sorted(config["tools"])
    if request.action not in ("backup", "restore", "status"):
        raise AiKitError("Unknown action: {}".format(request.action))
    if request.tool != "all" and request.tool not in tool_names:
        raise AiKitError("Unknown tool: {}".format(request.tool))
    if request.source_tool and request.source_tool not in tool_names:
        raise AiKitError("Unknown source tool: {}".format(request.source_tool))
    if request.source_tool and request.all_tools:
        raise AiKitError("A source tool and all tools cannot both be selected")

    state = load_state(config)
    selected = tool_names if request.tool == "all" else [request.tool]
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
            raise AiKitError("\n".join(all_errors))
        for tool_name, artifacts, available_sources in prepared:
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
            raise AiKitError(
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
                if request.source_tool:
                    source_tools = [request.source_tool]
                elif request.all_tools:
                    source_tools = tool_names
                else:
                    source_tools = [tool_name]
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

    if request.action == "status":
        portable_index = build_portable_index(config, tool_names)
        for tool_name in selected:
            total += status_tool(config, tool_name, state, portable_index, report)
        return total
    raise AiKitError("Unknown action: {}".format(request.action))
