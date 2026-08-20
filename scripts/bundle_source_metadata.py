"""Bundle dependency versions, source locations, and license notices."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
from importlib.metadata import Distribution, PackageNotFoundError, distribution
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


ROOT_DISTRIBUTIONS = ("agentbox-workbench", "pyinstaller")
LICENSE_NAMES = ("license", "copying", "notice", "authors")


def dependency_closure() -> list[Distribution]:
    resolved: dict[str, Distribution] = {}
    pending = list(ROOT_DISTRIBUTIONS)
    while pending:
        requested = pending.pop()
        name = canonicalize_name(requested)
        if name in resolved:
            continue
        try:
            package = distribution(requested)
        except PackageNotFoundError as exc:
            raise RuntimeError(f"Required release distribution is missing: {requested}") from exc
        resolved[name] = package
        for value in package.requires or ():
            requirement = Requirement(value)
            if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
                continue
            pending.append(requirement.name)
    return sorted(resolved.values(), key=lambda item: canonicalize_name(item.metadata["Name"]))


def source_records(name: str, version: str) -> list[dict[str, str]]:
    if canonicalize_name(name) == "agentbox-workbench":
        return [{"kind": "bundled", "path": "agentbox-source.zip"}]
    endpoint = "https://pypi.org/pypi/{}/{}/json".format(quote(name), quote(version))
    with urlopen(endpoint, timeout=30) as response:
        payload = json.load(response)
    records = []
    for item in payload.get("urls", ()):
        if item.get("packagetype") != "sdist":
            continue
        records.append(
            {
                "kind": "sdist",
                "filename": item["filename"],
                "url": item["url"],
                "sha256": item.get("digests", {}).get("sha256", ""),
            }
        )
    if not records:
        records.append({"kind": "project", "url": payload["info"]["package_url"]})
    return records


def copy_license_files(package: Distribution, destination: Path) -> list[str]:
    copied = []
    package_root = destination / "THIRD_PARTY_LICENSES" / "{}-{}".format(
        package.metadata["Name"], package.version
    )
    for entry in package.files or ():
        filename = Path(entry).name.lower()
        if not filename.startswith(LICENSE_NAMES):
            continue
        source = Path(package.locate_file(entry))
        if not source.is_file():
            continue
        package_root.mkdir(parents=True, exist_ok=True)
        target = package_root / Path(entry).name
        counter = 2
        while target.exists():
            target = package_root / f"{Path(entry).stem}-{counter}{Path(entry).suffix}"
            counter += 1
        shutil.copy2(source, target)
        copied.append(str(target.relative_to(destination)))
    return copied


def copy_additional_notices(destination: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    python_license = Path(os.__file__).resolve().parent / "LICENSE.txt"
    python_root = destination / "THIRD_PARTY_LICENSES" / f"Python-{platform.python_version()}"
    python_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(python_license, python_root / "LICENSE.txt")
    records.append(
        {
            "name": "CPython",
            "version": platform.python_version(),
            "license_files": [str((python_root / "LICENSE.txt").relative_to(destination))],
            "sources": [
                {
                    "kind": "source",
                    "url": "https://www.python.org/ftp/python/{0}/Python-{0}.tgz".format(
                        platform.python_version()
                    ),
                }
            ],
        }
    )

    htmx_root = destination / "THIRD_PARTY_LICENSES" / "htmx-2.0.4"
    htmx_root.mkdir(parents=True, exist_ok=True)
    htmx_license = Path("agentbox/static/vendor/HTMX-LICENSE.txt")
    shutil.copy2(htmx_license, htmx_root / htmx_license.name)
    records.append(
        {
            "name": "htmx",
            "version": "2.0.4",
            "license_expression": "0BSD",
            "vendored_sha256": "e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447",
            "license_files": [
                str((htmx_root / htmx_license.name).relative_to(destination))
            ],
            "sources": [
                {
                    "kind": "source",
                    "url": "https://github.com/bigskysoftware/htmx/releases/tag/v2.0.4",
                }
            ],
        }
    )
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    destination = args.destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)

    packages = dependency_closure()
    records = []
    for package in packages:
        name = package.metadata["Name"]
        is_agentbox = canonicalize_name(name) == "agentbox-workbench"
        record = {
            "name": name,
            "version": package.version,
            "license_expression": (
                "GPL-3.0-only"
                if is_agentbox
                else package.metadata.get("License-Expression", "")
            ),
            "license": package.metadata.get("License", ""),
            "license_files": (
                ["LICENSE"] if is_agentbox else copy_license_files(package, destination)
            ),
            "sources": source_records(name, package.version),
        }
        records.append(record)
    records.extend(copy_additional_notices(destination))
    records.sort(key=lambda item: canonicalize_name(str(item["name"])))

    (destination / "DEPENDENCIES.txt").write_text(
        "".join(f"{record['name']}=={record['version']}\n" for record in records),
        encoding="utf-8",
    )
    (destination / "SOURCE_MANIFEST.json").write_text(
        json.dumps({"schema": 1, "components": records}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
