"""Extract and validate an explicit semantic release marker from a commit message."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path


VERSION = r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})"
SEMANTIC_VERSION = re.compile(VERSION)
RELEASE_TAG = re.compile(r"^v({})$".format(VERSION))
RELEASE_LINE = re.compile(r"^Release: v({})$".format(VERSION))


def release_version(message: str) -> str | None:
    matches = [match.group(1) for line in message.splitlines() if (match := RELEASE_LINE.fullmatch(line))]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("The commit message must contain exactly one Release: vX.Y.Z line")
    return matches[0]


def project_version(path: Path) -> str:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
        version = value["project"]["version"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("Cannot read the project version from {}".format(path)) from exc
    if not isinstance(version, str) or SEMANTIC_VERSION.fullmatch(version) is None:
        raise ValueError("The project version must use canonical X.Y.Z syntax")
    return version


def version_key(version: str) -> tuple[int, int, int]:
    if SEMANTIC_VERSION.fullmatch(version) is None:
        raise ValueError("Version must use canonical X.Y.Z syntax")
    return tuple(int(part) for part in version.split("."))


def require_newest(version: str, tags: list[str]) -> None:
    requested = version_key(version)
    newer = []
    for tag in tags:
        match = RELEASE_TAG.fullmatch(tag.strip())
        if match and version_key(match.group(1)) > requested:
            newer.append(tag.strip())
    if newer:
        raise ValueError(
            "Release v{} is older than existing release {}".format(
                version, max(newer, key=lambda item: version_key(item[1:]))
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--require-newest")
    args = parser.parse_args()
    try:
        if args.require_newest:
            require_newest(args.require_newest, sys.stdin.read().splitlines())
            return 0
        version = release_version(sys.stdin.read())
        if version is None:
            return 0
        configured = project_version(args.project)
        if version != configured:
            raise ValueError(
                "Release marker v{} does not match project version {}".format(
                    version, configured
                )
            )
    except ValueError as exc:
        parser.error(str(exc))
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
