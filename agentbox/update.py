"""Update awareness and verified standalone self-updates."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import uuid
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen

from .core import (
    AgentBoxError,
    application_lock_identity,
    external_program_environment,
    operation_guard,
    user_state_root,
)


DEFAULT_UPDATE_REPOSITORY = "SimaxLabs/agentbox"
UPDATE_CACHE_VERSION = 2
UPDATE_CACHE_TTL_SECONDS = 6 * 60 * 60
UPDATE_FAILURE_TTL_SECONDS = 10 * 60
NETWORK_TIMEOUT_SECONDS = 8
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_EXECUTABLE_BYTES = 256 * 1024 * 1024
SEMANTIC_VERSION = re.compile(
    r"^(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})$"
)
RELEASE_TAG = re.compile(
    r"^v(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})$"
)
FULL_COMMIT = re.compile(r"^[0-9a-f]{40}$")
GITHUB_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str


@dataclass(frozen=True)
class ReleaseInfo:
    repository: str
    commit: str
    tag: str
    page_url: str
    assets: tuple[ReleaseAsset, ...]
    version: str = ""

    def __post_init__(self) -> None:
        match = RELEASE_TAG.fullmatch(self.tag) if isinstance(self.tag, str) else None
        version = self.version or (self.tag[1:] if match else "")
        if (
            match is None
            or not isinstance(version, str)
            or SEMANTIC_VERSION.fullmatch(version) is None
            or self.tag != "v{}".format(version)
            or not isinstance(self.commit, str)
            or FULL_COMMIT.fullmatch(self.commit) is None
        ):
            raise ValueError("release identity must contain a stable version and full commit")
        object.__setattr__(self, "version", version)


@dataclass(frozen=True)
class UpdateStatus:
    repository: str
    current_commit: str | None
    latest_commit: str | None
    release_url: str | None
    update_available: bool | None
    can_self_update: bool
    target: str | None
    error: str | None = None
    disabled: bool = False
    relation: str | None = None
    stale: bool = False
    warning: str | None = None
    current_version: str | None = None
    latest_version: str | None = None
    version_relation: str | None = None
    install_channel: str | None = None
    install_command: str | None = None

    @property
    def current_label(self) -> str:
        return "v{}".format(self.current_version) if self.current_version else "unknown"

    @property
    def latest_label(self) -> str:
        return "v{}".format(self.latest_version) if self.latest_version else "unknown"

    @property
    def current_commit_label(self) -> str:
        return self.current_commit[:12] if self.current_commit else "unknown"

    @property
    def latest_commit_label(self) -> str:
        return self.latest_commit[:12] if self.latest_commit else "unknown"


@dataclass(frozen=True)
class UpdatePlan:
    repository: str
    current_commit: str
    latest_commit: str
    release_url: str
    target: str
    archive_name: str
    archive_url: str
    archive_sha256: str
    executable_path: str
    executable_sha256: str
    current_version: str = ""
    latest_version: str = ""

    def identity(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class UpdateResult:
    message: str
    restart_required: bool


def _request(url: str) -> Request:
    return Request(
        url,
        headers={
            "Accept": "application/vnd.github+json, application/octet-stream",
            "User-Agent": "AgentBox-update-check",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def _read_url(url: str, maximum: int = MAX_METADATA_BYTES) -> bytes:
    try:
        with urlopen(_request(url), timeout=NETWORK_TIMEOUT_SECONDS) as response:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > maximum:
                raise AgentBoxError("Update metadata exceeded the allowed size")
            payload = response.read(maximum + 1)
    except (HTTPError, URLError, OSError, ValueError) as exc:
        raise AgentBoxError("Cannot contact the AgentBox release service: {}".format(exc))
    if len(payload) > maximum:
        raise AgentBoxError("Update metadata exceeded the allowed size")
    return payload


def _read_json(url: str) -> dict:
    try:
        value = json.loads(_read_url(url).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentBoxError("The AgentBox release service returned invalid metadata") from exc
    if not isinstance(value, dict):
        raise AgentBoxError("The AgentBox release service returned invalid metadata")
    return value


def _semantic_version(value: object) -> str | None:
    if isinstance(value, str) and SEMANTIC_VERSION.fullmatch(value):
        return value
    return None


def version_relation(current: str, latest: str) -> str:
    current_match = SEMANTIC_VERSION.fullmatch(current)
    latest_match = SEMANTIC_VERSION.fullmatch(latest)
    if current_match is None or latest_match is None:
        raise ValueError("semantic versions must use MAJOR.MINOR.PATCH")
    current_parts = tuple(int(part) for part in current_match.groups())
    latest_parts = tuple(int(part) for part in latest_match.groups())
    if latest_parts > current_parts:
        return "newer"
    if latest_parts < current_parts:
        return "older"
    return "same"


def _full_commit(value: object) -> str | None:
    if isinstance(value, str) and FULL_COMMIT.fullmatch(value):
        return value
    return None


def _install_channel() -> tuple[str | None, str | None]:
    channel = os.environ.get("AGENTBOX_INSTALL_CHANNEL")
    if not channel and getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve().as_posix().lower()
        if "/cellar/agentbox/" in executable:
            channel = "homebrew"
        elif "/scoop/apps/agentbox/" in executable:
            channel = "scoop"
    if not channel:
        return None, None
    command = {
        "homebrew": "brew upgrade agentbox",
        "scoop": "scoop update agentbox",
    }.get(channel.strip().lower())
    return channel, command


def _valid_github_url(url: object, expected_path: str) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and parsed.username is None
        and parsed.password is None
        and port is None
        and unquote(parsed.path) == expected_path
        and not parsed.query
        and not parsed.fragment
    )


def _valid_asset_name(name: object) -> bool:
    return (
        isinstance(name, str)
        and bool(name)
        and name not in {".", ".."}
        and "/" not in name
        and "\\" not in name
        and Path(name).name == name
        and not any(ord(character) < 32 or ord(character) == 127 for character in name)
    )


def _valid_release_page(repository: str, tag: str, url: object) -> bool:
    return _valid_github_url(url, "/{}/releases/tag/{}".format(repository, tag))


def _valid_release_asset(repository: str, tag: str, asset: ReleaseAsset) -> bool:
    return _valid_asset_name(asset.name) and _valid_github_url(
        asset.url,
        "/{}/releases/download/{}/{}".format(repository, tag, asset.name),
    )


def _build_info() -> dict[str, str]:
    path = Path(__file__).resolve().with_name("build.json")
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        key: item
        for key in ("commit", "repository", "version")
        if isinstance((item := value.get(key)), str) and item.strip()
    }


def _git_checkout_commit() -> str | None:
    root = Path(__file__).resolve().parent.parent
    if not (root / ".git").exists():
        return None
    with external_program_environment() as environment:
        git = shutil.which("git", path=environment.get("PATH"))
        if git is None:
            return None
        try:
            result = subprocess.run(
                [git, "-C", str(root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError):
            return None
    commit = result.stdout.strip().lower()
    return _full_commit(commit)


def _source_project_version() -> str | None:
    path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not path.is_file():
        return None
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    project = value.get("project") if isinstance(value, dict) else None
    return _semantic_version(project.get("version")) if isinstance(project, dict) else None


def _installed_version() -> str | None:
    try:
        return _semantic_version(importlib.metadata.version("agentbox-workbench"))
    except importlib.metadata.PackageNotFoundError:
        return None


def current_build(*, inspect_git: bool = True) -> tuple[str, str | None, str | None]:
    if getattr(sys, "frozen", False):
        metadata = _build_info()
        return (
            metadata.get("repository", DEFAULT_UPDATE_REPOSITORY),
            _semantic_version(metadata.get("version")),
            _full_commit(metadata.get("commit")),
        )
    version = _source_project_version()
    if version is not None:
        return (
            DEFAULT_UPDATE_REPOSITORY,
            version,
            _git_checkout_commit() if inspect_git else None,
        )
    return DEFAULT_UPDATE_REPOSITORY, _installed_version(), None


def release_target() -> str | None:
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Darwin" and machine in {"arm64", "aarch64"}:
        return "macos-arm64"
    if system == "Linux" and machine in {"x86_64", "amd64"}:
        return "linux-x86_64"
    if system == "Linux" and machine in {"arm64", "aarch64"}:
        return "linux-arm64"
    if system == "Windows" and machine in {"amd64", "x86_64"}:
        return "windows-x86_64"
    return None


def fetch_latest_release(repository: str) -> ReleaseInfo:
    if GITHUB_REPOSITORY.fullmatch(repository) is None:
        raise AgentBoxError("The AgentBox update repository is invalid")
    endpoint = "https://api.github.com/repos/{}/releases/latest".format(
        quote(repository, safe="/")
    )
    payload = _read_json(endpoint)
    tag = payload.get("tag_name")
    match = RELEASE_TAG.fullmatch(tag) if isinstance(tag, str) else None
    commit = _full_commit(payload.get("target_commitish"))
    if (
        match is None
        or commit is None
        or payload.get("draft") is not False
        or payload.get("prerelease") is not False
    ):
        raise AgentBoxError("The latest AgentBox release has an invalid identity")
    version = tag.removeprefix("v")
    reference = _read_json(
        "https://api.github.com/repos/{}/git/ref/tags/{}".format(
            quote(repository, safe="/"), quote(tag, safe="")
        )
    )
    reference_object = reference.get("object")
    if (
        reference.get("ref") != "refs/tags/{}".format(tag)
        or not isinstance(reference_object, dict)
        or reference_object.get("type") != "commit"
        or reference_object.get("sha") != commit
    ):
        raise AgentBoxError("The latest AgentBox release tag is not a matching lightweight tag")
    asset_values = payload.get("assets")
    if not isinstance(asset_values, list):
        raise AgentBoxError("The latest AgentBox release has invalid assets")
    assets = []
    names = set()
    for item in asset_values:
        if not isinstance(item, dict):
            raise AgentBoxError("The latest AgentBox release has invalid assets")
        name = item.get("name")
        url = item.get("browser_download_url")
        asset = ReleaseAsset(name, url) if isinstance(name, str) and isinstance(url, str) else None
        if (
            asset is None
            or asset.name in names
            or not _valid_release_asset(repository, tag, asset)
        ):
            raise AgentBoxError("The latest AgentBox release has invalid assets")
        names.add(asset.name)
        assets.append(asset)
    page_url = payload.get("html_url")
    if not _valid_release_page(repository, tag, page_url):
        raise AgentBoxError("The latest AgentBox release has no valid page URL")
    return ReleaseInfo(repository, commit, tag, page_url, tuple(assets), version)


def release_relation(repository: str, current_commit: str, latest_commit: str) -> str:
    if current_commit == latest_commit:
        return "identical"
    endpoint = "https://api.github.com/repos/{}/compare/{}...{}".format(
        quote(repository, safe="/"),
        quote(current_commit, safe=""),
        quote(latest_commit, safe=""),
    )
    status = _read_json(endpoint).get("status")
    if status not in {"ahead", "behind", "diverged", "identical"}:
        raise AgentBoxError("The AgentBox release service returned an invalid commit relation")
    return str(status)


def _cache_path() -> Path:
    return user_state_root() / "update-check.json"


def _valid_cache_time(value: object, maximum_age: int, allow_expired: bool) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    checked_at = float(value)
    if not math.isfinite(checked_at):
        return False
    age = time.time() - checked_at
    return age >= 0 and (allow_expired or age <= maximum_age)


def _cached_release(
    repository: str,
    current_version: str | None,
    current_commit: str | None,
    allow_expired: bool = False,
) -> tuple[ReleaseInfo, str | None] | None:
    path = _cache_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return None
        if (
            GITHUB_REPOSITORY.fullmatch(repository) is None
            or value.get("version") != UPDATE_CACHE_VERSION
            or value.get("repository") != repository
            or value.get("current_version") != current_version
            or value.get("current_commit") != current_commit
            or not _valid_cache_time(
                value.get("checked_at"), UPDATE_CACHE_TTL_SECONDS, allow_expired
            )
        ):
            return None
        if current_version is not None and _semantic_version(current_version) is None:
            return None
        if current_commit is not None and _full_commit(current_commit) is None:
            return None
        release_value = value["release"]
        if not isinstance(release_value, dict):
            return None
        release_version = _semantic_version(release_value.get("version"))
        commit = release_value.get("commit")
        tag = release_value.get("tag")
        page_url = release_value.get("page_url")
        asset_values = release_value.get("assets")
        if (
            release_version is None
            or _full_commit(commit) is None
            or tag != "v{}".format(release_version)
            or not _valid_release_page(repository, tag, page_url)
            or not isinstance(asset_values, list)
        ):
            return None
        assets = []
        names = set()
        for item in asset_values:
            if not isinstance(item, dict):
                return None
            name = item.get("name")
            url = item.get("url")
            if not isinstance(name, str) or not isinstance(url, str):
                return None
            asset = ReleaseAsset(name, url)
            if asset.name in names or not _valid_release_asset(repository, tag, asset):
                return None
            names.add(asset.name)
            assets.append(asset)
        relation = value.get("relation")
        if relation not in {None, "ahead", "behind", "diverged", "identical"}:
            return None
        if current_commit is None and relation is not None:
            return None
        if current_commit is not None and relation is None:
            return None
        if (current_commit == commit) != (relation == "identical"):
            return None
        return (
            ReleaseInfo(
                repository,
                commit,
                tag,
                page_url,
                tuple(assets),
                release_version,
            ),
            relation,
        )
    except (OSError, AttributeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _store_cached_release(
    release: ReleaseInfo,
    current_version: str | None,
    current_commit: str | None,
    relation: str | None,
) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent.is_symlink():
            return
        temporary = path.with_name("{}.tmp-{}".format(path.name, uuid.uuid4().hex))
        temporary.write_text(
            json.dumps(
                {
                    "version": UPDATE_CACHE_VERSION,
                    "checked_at": time.time(),
                    "repository": release.repository,
                    "current_version": current_version,
                    "current_commit": current_commit,
                    "relation": relation,
                    "release": {
                        "version": release.version,
                        "commit": release.commit,
                        "tag": release.tag,
                        "page_url": release.page_url,
                        "assets": [asdict(asset) for asset in release.assets],
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError:
        return


def _failure_cache_path() -> Path:
    return user_state_root() / "update-check-error.json"


def _cached_failure(
    repository: str, current_version: str | None, current_commit: str | None
) -> str | None:
    try:
        value = json.loads(_failure_cache_path().read_text(encoding="utf-8"))
        error = value.get("error") if isinstance(value, dict) else None
        if (
            not isinstance(value, dict)
            or GITHUB_REPOSITORY.fullmatch(repository) is None
            or value.get("version") != UPDATE_CACHE_VERSION
            or value.get("repository") != repository
            or value.get("current_version") != current_version
            or value.get("current_commit") != current_commit
            or not _valid_cache_time(
                value.get("checked_at"), UPDATE_FAILURE_TTL_SECONDS, False
            )
            or not isinstance(error, str)
            or not error
        ):
            return None
        if current_version is not None and _semantic_version(current_version) is None:
            return None
        if current_commit is not None and _full_commit(current_commit) is None:
            return None
        return error
    except (OSError, UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _store_failure(
    repository: str,
    current_version: str | None,
    current_commit: str | None,
    error: str,
) -> None:
    path = _failure_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name("{}.tmp-{}".format(path.name, uuid.uuid4().hex))
        temporary.write_text(
            json.dumps(
                {
                    "version": UPDATE_CACHE_VERSION,
                    "repository": repository,
                    "current_version": current_version,
                    "current_commit": current_commit,
                    "checked_at": time.time(),
                    "error": error,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError:
        return


def _update_result_path() -> Path:
    return user_state_root() / "updates/last-result.json"


def _consume_update_warning() -> str | None:
    path = _update_result_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    try:
        path.unlink()
    except OSError:
        pass
    if isinstance(value, dict) and value.get("status") == "failed" and isinstance(
        value.get("message"), str
    ):
        return "The previous Windows update failed: {}".format(value["message"])
    return None


def _release_status(
    repository: str,
    current_version: str | None,
    current_commit: str | None,
    release: ReleaseInfo,
    relation: str | None,
    target: str | None,
    channel: str | None,
    command: str | None,
    warning: str | None,
    *,
    stale: bool = False,
) -> UpdateStatus:
    semantic_relation = (
        version_relation(current_version, release.version) if current_version else None
    )
    available = semantic_relation == "newer" if semantic_relation is not None else None
    can_self_update = (
        bool(getattr(sys, "frozen", False))
        and target is not None
        and channel is None
        and semantic_relation == "newer"
        and relation == "ahead"
    )
    return UpdateStatus(
        repository,
        current_commit,
        release.commit,
        release.page_url,
        available,
        can_self_update,
        target,
        relation=relation,
        stale=stale,
        warning=warning,
        current_version=current_version,
        latest_version=release.version,
        version_relation=semantic_relation,
        install_channel=channel,
        install_command=command,
    )


def check_for_updates(force: bool = False) -> UpdateStatus:
    warning = _consume_update_warning()
    channel, command = _install_channel()
    if not force and os.environ.get("AGENTBOX_NO_UPDATE_CHECK", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        repository, current_version, current_commit = current_build(inspect_git=False)
        return UpdateStatus(
            repository,
            current_commit,
            None,
            None,
            None,
            False,
            release_target(),
            disabled=True,
            warning=warning,
            current_version=current_version,
            install_channel=channel,
            install_command=command,
        )
    repository, current_version, current_commit = current_build()
    target = release_target()
    cached = (
        None
        if force
        else _cached_release(repository, current_version, current_commit)
    )
    if cached is None and not force:
        failure = _cached_failure(repository, current_version, current_commit)
        if failure:
            stale = _cached_release(repository, current_version, current_commit, True)
            if stale is not None:
                release, relation = stale
                return _release_status(
                    repository,
                    current_version,
                    current_commit,
                    release,
                    relation,
                    target,
                    channel,
                    command,
                    warning,
                    stale=True,
                )
            return UpdateStatus(
                repository,
                current_commit,
                None,
                None,
                None,
                False,
                target,
                error=failure,
                warning=warning,
                current_version=current_version,
                install_channel=channel,
                install_command=command,
            )
    try:
        if cached is None:
            release = fetch_latest_release(repository)
            relation = (
                release_relation(repository, current_commit, release.commit)
                if current_commit
                else None
            )
            _store_cached_release(release, current_version, current_commit, relation)
            try:
                _failure_cache_path().unlink(missing_ok=True)
            except OSError:
                pass
        else:
            release, relation = cached
    except AgentBoxError as exc:
        stale = (
            None
            if force
            else _cached_release(repository, current_version, current_commit, True)
        )
        if stale is not None:
            _store_failure(repository, current_version, current_commit, str(exc))
            release, relation = stale
            return _release_status(
                repository,
                current_version,
                current_commit,
                release,
                relation,
                target,
                channel,
                command,
                warning,
                stale=True,
            )
        if not force:
            _store_failure(repository, current_version, current_commit, str(exc))
        return UpdateStatus(
            repository,
            current_commit,
            None,
            None,
            None,
            False,
            target,
            error=str(exc),
            warning=warning,
            current_version=current_version,
            install_channel=channel,
            install_command=command,
        )
    return _release_status(
        repository,
        current_version,
        current_commit,
        release,
        relation,
        target,
        channel,
        command,
        warning,
    )


def _asset(release: ReleaseInfo, name: str) -> ReleaseAsset:
    matches = [asset for asset in release.assets if asset.name == name]
    if len(matches) != 1:
        raise AgentBoxError("The latest release does not contain exactly one {}".format(name))
    return matches[0]


def _checksums(release: ReleaseInfo) -> dict[str, str]:
    asset = _asset(release, "SHA256SUMS.txt")
    try:
        text = _read_url(asset.url).decode("ascii")
    except UnicodeDecodeError as exc:
        raise AgentBoxError("The release checksum file is invalid") from exc
    checksums = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        digest, name = fields
        name = name.lstrip("*")
        if SHA256.fullmatch(digest) and Path(name).name == name:
            if name in checksums:
                raise AgentBoxError("The release checksum file contains duplicate entries")
            checksums[name] = digest
    return checksums


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_update_plan() -> UpdatePlan:
    channel, command = _install_channel()
    if channel is not None:
        guidance = (
            " Run `{}` instead.".format(command)
            if command
            else " Update it through that package manager instead."
        )
        raise AgentBoxError(
            "This AgentBox installation is managed by {}; direct replacement is disabled.{}".format(
                channel, guidance
            )
        )
    if not getattr(sys, "frozen", False):
        raise AgentBoxError(
            "Automatic updates are available only for standalone AgentBox releases"
        )
    repository, current_version, current_commit = current_build()
    if current_version is None:
        raise AgentBoxError("This standalone build has no embedded semantic version")
    if current_commit is None:
        raise AgentBoxError("This standalone build has no embedded source commit")
    target = release_target()
    if target is None:
        raise AgentBoxError("No automatic AgentBox update is published for this platform")
    release = fetch_latest_release(repository)
    semantic_relation = version_relation(current_version, release.version)
    if semantic_relation == "same":
        raise AgentBoxError("AgentBox is already up to date at v{}".format(current_version))
    if semantic_relation == "older":
        raise AgentBoxError(
            "The latest release v{} is older than this AgentBox build v{}; semantic downgrade was refused".format(
                release.version, current_version
            )
        )
    relation = release_relation(repository, current_commit, release.commit)
    if relation != "ahead":
        raise AgentBoxError(
            "The latest release commit is {} relative to this AgentBox build; automatic update was refused".format(
                relation
            )
        )
    extension = ".zip" if target == "windows-x86_64" else ".tar.gz"
    archive_name = "agentbox-{}-{}{}".format(release.version, target, extension)
    archive = _asset(release, archive_name)
    checksum = _checksums(release).get(archive_name)
    if checksum is None:
        raise AgentBoxError("The latest release has no checksum for {}".format(archive_name))
    executable = Path(sys.executable).resolve()
    if not executable.is_file():
        raise AgentBoxError("Cannot locate the running AgentBox executable")
    return UpdatePlan(
        repository,
        current_commit,
        release.commit,
        release.page_url,
        target,
        archive_name,
        archive.url,
        checksum,
        str(executable),
        _file_sha256(executable),
        current_version,
        release.version,
    )


def _download_archive(plan: UpdatePlan, destination: Path) -> None:
    digest = hashlib.sha256()
    total = 0
    try:
        with urlopen(_request(plan.archive_url), timeout=NETWORK_TIMEOUT_SECONDS) as response:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > MAX_ARCHIVE_BYTES:
                raise AgentBoxError("The AgentBox update archive is too large")
            with destination.open("xb") as output:
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise AgentBoxError("The AgentBox update archive is too large")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
    except (HTTPError, URLError, OSError, ValueError) as exc:
        raise AgentBoxError("Cannot download the AgentBox update: {}".format(exc))
    if digest.hexdigest() != plan.archive_sha256:
        raise AgentBoxError("The AgentBox update archive failed SHA-256 verification")


def _archive_member(plan: UpdatePlan) -> str:
    executable = "agentbox.exe" if plan.target == "windows-x86_64" else "agentbox"
    if plan.archive_name.endswith(".tar.gz"):
        archive_root = plan.archive_name.removesuffix(".tar.gz")
    elif plan.archive_name.endswith(".zip"):
        archive_root = plan.archive_name.removesuffix(".zip")
    else:
        raise AgentBoxError("The update archive has an invalid filename")
    return "{}/{}".format(archive_root, executable)


def _copy_limited(source: object, destination: object, size: int) -> None:
    if size < 1 or size > MAX_EXECUTABLE_BYTES:
        raise AgentBoxError("The executable in the update archive has an invalid size")
    remaining = size
    while remaining:
        chunk = source.read(min(1024 * 1024, remaining))
        if not chunk:
            raise AgentBoxError("The executable in the update archive is truncated")
        destination.write(chunk)
        remaining -= len(chunk)
    if source.read(1):
        raise AgentBoxError("The executable in the update archive exceeded its declared size")


def _stage_executable(plan: UpdatePlan, archive_path: Path) -> Path:
    target = Path(plan.executable_path)
    if not target.is_file() or target.is_symlink():
        raise AgentBoxError("The running AgentBox executable is no longer a regular file")
    if _file_sha256(target) != plan.executable_sha256:
        raise AgentBoxError("The AgentBox executable changed after the update was reviewed")
    staged = target.with_name(".{}.update-{}".format(target.name, uuid.uuid4().hex))
    member_name = _archive_member(plan)
    completed = False
    try:
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        with os.fdopen(descriptor, "wb") as destination:
            if plan.archive_name.endswith(".zip"):
                with zipfile.ZipFile(archive_path) as archive:
                    matches = [item for item in archive.infolist() if item.filename == member_name]
                    if len(matches) != 1 or matches[0].is_dir():
                        raise AgentBoxError("The update archive has no unique AgentBox executable")
                    with archive.open(matches[0]) as source:
                        _copy_limited(source, destination, matches[0].file_size)
            else:
                with tarfile.open(archive_path, mode="r:gz") as archive:
                    matches = [item for item in archive.getmembers() if item.name == member_name]
                    if len(matches) != 1 or not matches[0].isfile():
                        raise AgentBoxError("The update archive has no unique AgentBox executable")
                    source = archive.extractfile(matches[0])
                    if source is None:
                        raise AgentBoxError("Cannot read the AgentBox executable from the update")
                    with source:
                        _copy_limited(source, destination, matches[0].size)
            destination.flush()
            os.fsync(destination.fileno())
        if os.name != "nt":
            staged.chmod(stat.S_IMODE(target.stat().st_mode) | stat.S_IXUSR)
        completed = True
        return staged
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise AgentBoxError("Cannot stage the AgentBox update: {}".format(exc))
    finally:
        if not completed:
            staged.unlink(missing_ok=True)


def _reserve_windows_update(target: Path) -> Path:
    marker = target.with_name(".{}.update-pending".format(target.name))
    try:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            age = time.time() - marker.stat().st_mtime
        except OSError as exc:
            raise AgentBoxError("Cannot inspect the pending Windows update: {}".format(exc))
        if age <= 60 * 60:
            raise AgentBoxError("Another AgentBox update is already pending")
        try:
            marker.unlink()
            descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as exc:
            raise AgentBoxError("Cannot clear the stale Windows update marker: {}".format(exc))
    except OSError as exc:
        raise AgentBoxError("Cannot reserve the AgentBox executable for update: {}".format(exc))
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write("{}\n".format(os.getpid()))
        output.flush()
        os.fsync(output.fileno())
    return marker


def _schedule_windows_replacement(
    target: Path, staged: Path, marker: Path, expected_sha256: str
) -> None:
    script_root = user_state_root() / "updates"
    script = script_root / "apply-{}.ps1".format(uuid.uuid4().hex)
    result = _update_result_path()
    try:
        script_root.mkdir(parents=True, exist_ok=True)
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "try {\n"
            "  Wait-Process -Id ([int]$env:AGENTBOX_UPDATE_PID) -ErrorAction SilentlyContinue\n"
            "  $actual = (Get-FileHash -LiteralPath $env:AGENTBOX_UPDATE_TARGET "
            "-Algorithm SHA256).Hash.ToLowerInvariant()\n"
            "  if ($actual -ne $env:AGENTBOX_UPDATE_EXPECTED_SHA256) {\n"
            "    throw 'The AgentBox executable changed while the update was pending'\n"
            "  }\n"
            "  [System.IO.File]::Replace($env:AGENTBOX_UPDATE_REPLACEMENT, "
            "$env:AGENTBOX_UPDATE_TARGET, $null)\n"
            "  @{ status = 'ok' } | ConvertTo-Json | Set-Content -LiteralPath "
            "$env:AGENTBOX_UPDATE_RESULT -Encoding UTF8\n"
            "} catch {\n"
            "  @{ status = 'failed'; message = $_.Exception.Message } | ConvertTo-Json | "
            "Set-Content -LiteralPath $env:AGENTBOX_UPDATE_RESULT -Encoding UTF8\n"
            "  Remove-Item -LiteralPath $env:AGENTBOX_UPDATE_REPLACEMENT -Force "
            "-ErrorAction SilentlyContinue\n"
            "} finally {\n"
            "  Remove-Item -LiteralPath $env:AGENTBOX_UPDATE_MARKER -Force "
            "-ErrorAction SilentlyContinue\n"
            "  Remove-Item -LiteralPath $env:AGENTBOX_UPDATE_SCRIPT -Force "
            "-ErrorAction SilentlyContinue\n"
            "}\n",
            encoding="utf-8",
        )
    except OSError as exc:
        for path in (script, marker):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise AgentBoxError("Cannot prepare the Windows update helper: {}".format(exc))
    with external_program_environment() as environment:
        powershell = shutil.which("powershell.exe", path=environment.get("PATH"))
        if powershell is None:
            script.unlink(missing_ok=True)
            marker.unlink(missing_ok=True)
            raise AgentBoxError("Windows PowerShell is required to finish this update")
        environment.update(
            {
                "AGENTBOX_UPDATE_PID": str(os.getpid()),
                "AGENTBOX_UPDATE_TARGET": str(target),
                "AGENTBOX_UPDATE_REPLACEMENT": str(staged),
                "AGENTBOX_UPDATE_EXPECTED_SHA256": expected_sha256,
                "AGENTBOX_UPDATE_MARKER": str(marker),
                "AGENTBOX_UPDATE_RESULT": str(result),
                "AGENTBOX_UPDATE_SCRIPT": str(script),
            }
        )
        try:
            subprocess.Popen(
                [
                    powershell,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                env=environment,
                creationflags=(
                    subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.CREATE_NO_WINDOW
                ),
            )
        except OSError as exc:
            script.unlink(missing_ok=True)
            marker.unlink(missing_ok=True)
            raise AgentBoxError("Cannot start the Windows update helper: {}".format(exc))


def install_update(reviewed: UpdatePlan) -> UpdateResult:
    target = Path(reviewed.executable_path)
    with operation_guard(application_lock_identity(), target):
        current = prepare_update_plan()
        if current.identity() != reviewed.identity():
            raise AgentBoxError("The available update changed; review it again before installing")
        with tempfile.TemporaryDirectory(prefix="agentbox-update-") as temporary:
            archive = Path(temporary) / reviewed.archive_name
            _download_archive(reviewed, archive)
            staged = _stage_executable(reviewed, archive)
        keep_staged = False
        try:
            if os.name == "nt":
                marker = _reserve_windows_update(target)
                _schedule_windows_replacement(
                    target, staged, marker, reviewed.executable_sha256
                )
                keep_staged = True
                return UpdateResult(
                    "Update verified and scheduled. AgentBox will stop so Windows can replace the executable.",
                    True,
                )
            if (
                not target.is_file()
                or target.is_symlink()
                or _file_sha256(target) != reviewed.executable_sha256
            ):
                raise AgentBoxError(
                    "The AgentBox executable changed while the update was being staged"
                )
            os.replace(staged, target)
            return UpdateResult(
                "Update verified and installed. Restart AgentBox to run the new build.",
                True,
            )
        except OSError as exc:
            raise AgentBoxError("Cannot replace the AgentBox executable: {}".format(exc))
        finally:
            if not keep_staged and staged.exists():
                staged.unlink()
