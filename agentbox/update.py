"""Release awareness and installation-channel guidance."""

from __future__ import annotations

import importlib.metadata
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import uuid
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen

from .core import AgentBoxError, external_program_environment, user_state_root


DEFAULT_UPDATE_REPOSITORY = "SimaxLabs/agentbox"
UPDATE_CACHE_VERSION = 3
UPDATE_CACHE_TTL_SECONDS = 6 * 60 * 60
UPDATE_FAILURE_TTL_SECONDS = 10 * 60
NETWORK_TIMEOUT_SECONDS = 8
MAX_METADATA_BYTES = 2 * 1024 * 1024
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


@dataclass(frozen=True)
class ReleaseInfo:
    repository: str
    commit: str
    tag: str
    page_url: str
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
    error: str | None = None
    disabled: bool = False
    stale: bool = False
    current_version: str | None = None
    latest_version: str | None = None
    version_relation: str | None = None
    install_channel: str | None = None
    install_command: str | None = None
    standalone: bool = False

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


def _request(url: str) -> Request:
    return Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
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
    except (HTTPError, URLError, HTTPException, OSError, ValueError) as exc:
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


def _valid_release_page(repository: str, tag: str, url: object) -> bool:
    return _valid_github_url(url, "/{}/releases/tag/{}".format(repository, tag))


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
    page_url = payload.get("html_url")
    if not _valid_release_page(repository, tag, page_url):
        raise AgentBoxError("The latest AgentBox release has no valid page URL")
    return ReleaseInfo(repository, commit, tag, page_url, version)


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
) -> ReleaseInfo | None:
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
        if (
            release_version is None
            or _full_commit(commit) is None
            or tag != "v{}".format(release_version)
            or not _valid_release_page(repository, tag, page_url)
        ):
            return None
        return ReleaseInfo(
            repository,
            commit,
            tag,
            page_url,
            release_version,
        )
    except (OSError, AttributeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _store_cached_release(
    release: ReleaseInfo,
    current_version: str | None,
    current_commit: str | None,
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
                    "release": {
                        "version": release.version,
                        "commit": release.commit,
                        "tag": release.tag,
                        "page_url": release.page_url,
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
        if not isinstance(value, dict):
            return None
        error = value.get("error")
        if (
            GITHUB_REPOSITORY.fullmatch(repository) is None
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


def _release_status(
    repository: str,
    current_version: str | None,
    current_commit: str | None,
    release: ReleaseInfo,
    channel: str | None,
    command: str | None,
    *,
    stale: bool = False,
) -> UpdateStatus:
    semantic_relation = (
        version_relation(current_version, release.version) if current_version else None
    )
    available = semantic_relation == "newer" if semantic_relation is not None else None
    return UpdateStatus(
        repository,
        current_commit,
        release.commit,
        release.page_url,
        available,
        stale=stale,
        current_version=current_version,
        latest_version=release.version,
        version_relation=semantic_relation,
        install_channel=channel,
        install_command=command,
        standalone=bool(getattr(sys, "frozen", False)) and channel is None,
    )


def check_for_updates(force: bool = False) -> UpdateStatus:
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
            disabled=True,
            current_version=current_version,
            install_channel=channel,
            install_command=command,
            standalone=bool(getattr(sys, "frozen", False)) and channel is None,
        )
    repository, current_version, current_commit = current_build()

    def unavailable_status(error: str) -> UpdateStatus:
        return UpdateStatus(
            repository,
            current_commit,
            None,
            None,
            None,
            error=error,
            current_version=current_version,
            install_channel=channel,
            install_command=command,
            standalone=bool(getattr(sys, "frozen", False)) and channel is None,
        )

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
                return _release_status(
                    repository,
                    current_version,
                    current_commit,
                    stale,
                    channel,
                    command,
                    stale=True,
                )
            return unavailable_status(failure)
    try:
        if cached is None:
            release = fetch_latest_release(repository)
            _store_cached_release(release, current_version, current_commit)
            try:
                _failure_cache_path().unlink(missing_ok=True)
            except OSError:
                pass
        else:
            release = cached
    except AgentBoxError as exc:
        stale = (
            None
            if force
            else _cached_release(repository, current_version, current_commit, True)
        )
        if stale is not None:
            _store_failure(repository, current_version, current_commit, str(exc))
            return _release_status(
                repository,
                current_version,
                current_commit,
                stale,
                channel,
                command,
                stale=True,
            )
        if not force:
            _store_failure(repository, current_version, current_commit, str(exc))
        return unavailable_status(str(exc))
    return _release_status(
        repository,
        current_version,
        current_commit,
        release,
        channel,
        command,
    )
