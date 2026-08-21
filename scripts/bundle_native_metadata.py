"""Inventory PyInstaller native binaries and bundle their license notices."""

from __future__ import annotations

import argparse
import ast
import bz2
import ctypes
import decimal
import hashlib
import io
import json
import platform
import re
import sqlite3
import ssl
import subprocess
import sys
import sysconfig
import tarfile
import time
import zlib
from http.client import HTTPException
from importlib.metadata import Distribution, distributions
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import urlopen
from xml.parsers import expat


WINDOWS_STATIC_DEPENDENCIES = {
    "_bz2.pyd": ("CPython dependency bzip2", "bzip2", "bzip2-1.0.6", "LICENSE"),
    "_decimal.pyd": (
        "CPython dependency mpdecimal",
        "mpdecimal",
        "BSD-2-Clause",
        "COPYRIGHT.txt",
    ),
    "_lzma.pyd": (
        "CPython dependency XZ Utils",
        "xz",
        "LicenseRef-Public-Domain",
        "COPYING",
    ),
    "_zstd.pyd": (
        "CPython dependency Zstandard",
        "zstd",
        "BSD-3-Clause",
        "LICENSE",
    ),
    "zlib.pyd": (
        "CPython dependency zlib-ng",
        "zlib-ng",
        "Zlib",
        "LICENSE.md",
    ),
}

UBUNTU_COMPONENT_LICENSES = {
    "OpenSSL": "Apache-2.0",
    "mpdecimal": "BSD-2-Clause",
    "Zstandard": "BSD-3-Clause",
    "SQLite": "blessing",
    "Expat": "MIT",
    "bzip2": "bzip2-1.0.6",
    "zlib": "Zlib",
    "libffi": "MIT",
    "GNU Readline": "GPL-3.0-or-later",
    "ncurses": "X11-distribute-modifications-variant",
    "util-linux libuuid": "BSD-3-Clause",
    "GCC runtime": "GPL-3.0-or-later WITH GCC-exception-3.1",
    "libedit": "BSD-3-Clause",
}

LAUNCHPAD_ATTEMPTS = 4
LAUNCHPAD_TIMEOUT_SECONDS = 30


def launchpad_json(url: str) -> object:
    for attempt in range(1, LAUNCHPAD_ATTEMPTS + 1):
        try:
            with urlopen(url, timeout=LAUNCHPAD_TIMEOUT_SECONDS) as response:
                return json.load(response)
        except HTTPError as exc:
            retryable = exc.code in {408, 425, 429} or 500 <= exc.code < 600
            if not retryable:
                raise RuntimeError(f"Launchpad request failed with HTTP {exc.code}: {url}") from exc
            error: Exception = exc
        except (
            HTTPException,
            URLError,
            TimeoutError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            error = exc
        if attempt == LAUNCHPAD_ATTEMPTS:
            raise RuntimeError(
                f"Launchpad request failed after {LAUNCHPAD_ATTEMPTS} attempts: {url}"
            ) from error
        delay = 2 ** (attempt - 1)
        print(
            f"warning: Launchpad request attempt {attempt}/{LAUNCHPAD_ATTEMPTS} failed: "
            f"{error}; retrying in {delay}s",
            file=sys.stderr,
        )
        time.sleep(delay)
    raise AssertionError("unreachable")


def binary_version(path: Path, symbol: str) -> str:
    library = ctypes.CDLL(str(path))
    function = getattr(library, symbol)
    function.restype = ctypes.c_char_p
    value = function()
    if not value:
        raise RuntimeError(f"Cannot read {symbol} from {path}")
    return value.decode("ascii")


def binary_string(path: Path, symbol: str) -> str:
    library = ctypes.CDLL(str(path))
    value = ctypes.c_char_p.in_dll(library, symbol).value
    if not value:
        raise RuntimeError(f"Cannot read {symbol} from {path}")
    return value.decode("ascii")


def source_path_version(path: Path) -> str:
    for parent in (path, *path.parents):
        match = re.search(r"(?<!\d)(\d+\.\d+(?:\.\d+)?)(?!\d)", parent.name)
        if match:
            return match.group(1)
    return "unknown"


def command_output(*command: str) -> str:
    result = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def ubuntu_binary_package(path: Path) -> str:
    for query in (str(path), f"*/{path.name}"):
        try:
            output = command_output("dpkg-query", "--search", query)
        except subprocess.CalledProcessError:
            continue
        for line in output.splitlines():
            package, separator, installed_path = line.rpartition(": ")
            if not separator:
                continue
            try:
                matches = Path(installed_path).resolve() == path.resolve()
            except OSError:
                matches = False
            if matches:
                return package
    raise RuntimeError(f"Cannot identify the Ubuntu package owning {path}")


def ubuntu_package_record(path: Path) -> dict[str, str]:
    package = ubuntu_binary_package(path)
    fields = command_output(
        "dpkg-query",
        "--show",
        "--showformat=${binary:Package}\t${Version}\t${source:Package}\t${source:Version}",
        package,
    ).split("\t")
    if len(fields) != 4:
        raise RuntimeError(f"Cannot read Ubuntu package metadata for {package}")
    binary_package, binary_version, source_package, source_version = fields
    binary_name = binary_package.split(":", 1)[0]
    copyright_path = Path("/usr/share/doc") / binary_name / "copyright"
    if not copyright_path.is_file():
        raise RuntimeError(f"Cannot find the copyright notice for {binary_package}")
    return {
        "binary_package": binary_name,
        "binary_version": binary_version,
        "source_package": source_package or binary_name,
        "source_version": source_version or binary_version,
        "copyright_path": str(copyright_path),
    }


def launchpad_source_files(source_package: str, source_version: str) -> list[dict[str, object]]:
    os_release = platform.freedesktop_os_release()
    if os_release.get("ID") != "ubuntu" or not os_release.get("VERSION_CODENAME"):
        raise RuntimeError("Ubuntu package provenance requires an Ubuntu release")
    codename = os_release["VERSION_CODENAME"]
    archive_url = "https://api.launchpad.net/1.0/ubuntu/+archive/primary"
    query = urlencode(
        {
            "ws.op": "getPublishedSources",
            "source_name": source_package,
            "version": source_version,
            "exact_match": "true",
        }
    )
    publications = launchpad_json(f"{archive_url}?{query}")
    if not isinstance(publications, dict):
        raise RuntimeError("Invalid Launchpad source publication response")
    publications = publications.get("entries", [])
    matching = [
        item
        for item in publications
        if item.get("distro_series_link", "").endswith(f"/{codename}")
        and item.get("status") in {"Published", "Superseded"}
    ]
    if not matching:
        raise RuntimeError(
            f"Cannot find published Ubuntu {codename} source {source_package}={source_version}"
        )
    files_query = urlencode({"ws.op": "sourceFileUrls", "include_meta": "true"})
    files = launchpad_json(f"{matching[0]['self_link']}?{files_query}")
    if (
        not isinstance(files, list)
        or not files
        or not all(
            isinstance(item, dict) and item.get("url") and item.get("sha256")
            for item in files
        )
    ):
        raise RuntimeError(
            f"Incomplete Launchpad source metadata for {source_package}={source_version}"
        )
    return [
        {
            "kind": "ubuntu-source",
            "filename": Path(item["url"]).name,
            "url": item["url"],
            "sha256": item["sha256"],
            "size": item.get("size"),
        }
        for item in files
    ]


def ubuntu_component_metadata(binaries: list[Path]) -> dict[str, object]:
    packages = []
    for binary in binaries:
        record = ubuntu_package_record(binary)
        if record not in packages:
            packages.append(record)
    source_packages = {
        (record["source_package"], record["source_version"]) for record in packages
    }
    if len(source_packages) != 1:
        raise RuntimeError(f"Native component spans Ubuntu sources: {sorted(source_packages)}")
    source_package, source_version = source_packages.pop()
    return {
        "version": source_version,
        "distribution": f"Ubuntu {platform.freedesktop_os_release()['VERSION_CODENAME']}",
        "source_package": source_package,
        "binary_packages": [
            {"name": record["binary_package"], "version": record["binary_version"]}
            for record in packages
        ],
        "local_license_paths": sorted(
            {record["copyright_path"] for record in packages}
        ),
        "sources": launchpad_source_files(source_package, source_version),
    }


def ubuntu_native_metadata(component: str, binaries: list[Path]) -> dict[str, object]:
    metadata = ubuntu_component_metadata(binaries)
    first = binaries[0]
    if component == "XZ Utils":
        library_version = binary_version(first, "lzma_version_string")
        metadata["library_version"] = library_version
        metadata["license_expression"] = (
            "LicenseRef-Public-Domain"
            if tuple(int(part) for part in library_version.split(".")) < (5, 4)
            else "0BSD"
        )
        return metadata
    metadata["license_expression"] = UBUNTU_COMPONENT_LICENSES[component]
    if component == "OpenSSL":
        metadata["library_version"] = ssl.OPENSSL_VERSION.split()[1]
    elif component == "mpdecimal":
        metadata["library_version"] = decimal.__libmpdec_version__
    elif component == "Zstandard":
        metadata["library_version"] = binary_version(first, "ZSTD_versionString")
    elif component == "SQLite":
        metadata["library_version"] = sqlite3.sqlite_version
    elif component == "Expat":
        metadata["library_version"] = expat.EXPAT_VERSION.removeprefix("expat_").replace(
            "_", "."
        )
    elif component == "bzip2":
        metadata["library_version"] = binary_version(first, "BZ2_bzlibVersion")
    elif component == "zlib":
        metadata["library_version"] = zlib.ZLIB_RUNTIME_VERSION
    elif component == "GNU Readline":
        metadata["library_version"] = binary_string(first, "rl_library_version")
    elif component == "ncurses":
        import curses

        version_info = curses.ncurses_version
        metadata["library_version"] = f"{version_info.major}.{version_info.minor}"
    return metadata


def installed_distribution_map() -> dict[Path, list[Distribution]]:
    file_map: dict[Path, list[Distribution]] = {}
    for package in distributions():
        for entry in package.files or ():
            source = Path(package.locate_file(entry))
            if not source.is_file():
                continue
            file_map.setdefault(source.resolve(), []).append(package)
    return file_map


def extension_component(
    path: Path, package_map: dict[Path, list[Distribution]]
) -> str:
    packages = package_map.get(path.resolve(), [])
    if len(packages) > 1:
        names = sorted(package.metadata["Name"] for package in packages)
        raise RuntimeError(f"Multiple Python distributions own {path}: {names}")
    if packages:
        return f"Python package {packages[0].metadata['Name']}"

    install_paths = sysconfig.get_paths()
    for key in ("purelib", "platlib"):
        root = Path(install_paths[key]).resolve()
        if path.resolve().is_relative_to(root):
            raise RuntimeError(f"Unowned site-packages extension: {path}")
    stdlib_roots = [Path(install_paths[key]).resolve() for key in ("stdlib", "platstdlib")]
    destination_shared = sysconfig.get_config_var("DESTSHARED")
    if destination_shared:
        stdlib_roots.append(Path(destination_shared).resolve())
    stdlib_roots.extend(
        (Path(prefix) / "DLLs").resolve() for prefix in {sys.prefix, sys.base_prefix}
    )
    for root in stdlib_roots:
        if path.resolve().is_relative_to(root):
            return "CPython"
    raise RuntimeError(f"Unrecognized Python extension: {path}")


def python_package_source(name: str, version: str) -> list[dict[str, object]]:
    endpoint = "https://pypi.org/pypi/{}/{}/json".format(quote(name), quote(version))
    with urlopen(endpoint, timeout=30) as response:
        payload = json.load(response)
    sources = [
        {
            "kind": "sdist",
            "filename": item["filename"],
            "url": item["url"],
            "sha256": item.get("digests", {}).get("sha256", ""),
        }
        for item in payload.get("urls", [])
        if item.get("packagetype") == "sdist"
    ]
    if not sources or not all(item["sha256"] for item in sources):
        raise RuntimeError(f"No hashed source distribution available for {name}=={version}")
    return sources


def python_package_metadata(
    path: Path, package_map: dict[Path, list[Distribution]]
) -> dict[str, object]:
    packages = package_map.get(path.resolve(), [])
    if len(packages) != 1:
        raise RuntimeError(f"Cannot identify one Python distribution owning {path}")
    package = packages[0]
    name = package.metadata["Name"]
    license_expression = (
        package.metadata.get("License-Expression") or package.metadata.get("License") or ""
    ).strip()
    if not license_expression:
        raise RuntimeError(f"No license metadata available for {name}=={package.version}")
    license_paths = []
    for entry in package.files or ():
        if not Path(entry).name.lower().startswith(
            ("license", "copying", "notice", "authors")
        ):
            continue
        source = Path(package.locate_file(entry))
        if source.is_file():
            license_paths.append(str(source))
    if not license_paths:
        raise RuntimeError(f"No installed license notice available for {name}=={package.version}")
    return {
        "version": package.version,
        "distribution_name": name,
        "license_expression": license_expression,
        "local_license_paths": sorted(set(license_paths)),
        "sources": python_package_source(name, package.version),
    }


def cpython_source_dependency(
    name: str, license_expression: str, license_filename: str
) -> dict[str, object]:
    python_version = platform.python_version()
    pin_url = (
        f"https://raw.githubusercontent.com/python/cpython/v{python_version}/"
        "PCbuild/get_externals.bat"
    )
    with urlopen(pin_url, timeout=30) as response:
        pins = response.read().decode("utf-8")
    versions = set(re.findall(rf"\b{re.escape(name)}-(\d+(?:\.\d+)+)\b", pins))
    if len(versions) != 1:
        raise RuntimeError(
            f"Cannot identify the CPython {python_version} {name} source pin"
        )
    version = versions.pop()
    ref = f"{name}-{version}"
    commit_url = (
        "https://api.github.com/repos/python/cpython-source-deps/commits/"
        f"{quote(ref, safe='')}"
    )
    with urlopen(commit_url, timeout=30) as response:
        commit = json.load(response).get("sha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError(f"Cannot resolve the CPython source dependency {ref}")
    return {
        "version": version,
        "cpython_dependency_pin": pin_url,
        "license_expression": license_expression,
        "license_urls": [
            "https://raw.githubusercontent.com/python/cpython-source-deps/"
            f"{commit}/{license_filename}"
        ],
        "sources": [
            {
                "kind": "cpython-source-dependency",
                "commit": commit,
                "url": (
                    "https://github.com/python/cpython-source-deps/archive/"
                    f"{commit}.tar.gz"
                ),
            }
        ],
    }


def has_library_name(name: str, *bases: str) -> bool:
    return any(
        name == base or name.startswith((f"{base}.", f"{base}-", f"{base}_"))
        for base in bases
    )


def component_for(path: Path) -> str:
    name = path.name.lower()
    if has_library_name(name, "libcrypto", "libssl"):
        return "OpenSSL"
    if has_library_name(name, "libmpdec", "mpdecimal"):
        return "mpdecimal"
    if has_library_name(name, "libzstd", "zstd"):
        return "Zstandard"
    if has_library_name(name, "liblzma", "lzma", "xz"):
        return "XZ Utils"
    if has_library_name(name, "libsqlite3", "sqlite3"):
        return "SQLite"
    if has_library_name(name, "libexpat", "expat"):
        return "Expat"
    if has_library_name(name, "libbz2", "bzip2", "bz2"):
        return "bzip2"
    if has_library_name(name, "libz", "zlib", "zlib1"):
        return "zlib"
    if has_library_name(name, "libffi", "ffi"):
        return "libffi"
    if has_library_name(name, "libreadline", "readline"):
        return "GNU Readline"
    if has_library_name(name, "libtinfo", "tinfo", "libncurses", "ncurses"):
        return "ncurses"
    if has_library_name(name, "libuuid", "uuid"):
        return "util-linux libuuid"
    if has_library_name(name, "libgcc", "libstdc++", "libgomp"):
        return "GCC runtime"
    if has_library_name(name, "libedit", "edit"):
        return "libedit"
    if re.fullmatch(r"(?:lib)?python(?:\d+(?:\.\d+)*)?(?:[._-].*)?", name):
        return "CPython"
    if name.startswith(("api-ms-win-", "vcruntime", "msvcp", "ucrtbase")):
        return "Operating system runtime"
    raise RuntimeError(f"Unrecognized native release dependency: {path}")


def component_metadata(
    component: str,
    binaries: list[Path],
    package_map: dict[Path, list[Distribution]],
) -> dict[str, object]:
    first = binaries[0]
    for dependency in WINDOWS_STATIC_DEPENDENCIES.values():
        dependency_component, name, license_expression, license_filename = dependency
        if component == dependency_component:
            metadata = cpython_source_dependency(
                name, license_expression, license_filename
            )
            metadata["static_linkage"] = True
            return metadata
    if component.startswith("Python package "):
        return python_package_metadata(first, package_map)
    if platform.system() == "Linux" and (
        component == "XZ Utils" or component in UBUNTU_COMPONENT_LICENSES
    ):
        return ubuntu_native_metadata(component, binaries)
    if component == "CPython":
        version = platform.python_version()
        return {
            "version": version,
            "license_expression": "PSF-2.0",
            "license_files": [
                f"THIRD_PARTY_LICENSES/Python-{version}/LICENSE.txt"
            ],
            "sources": [
                {
                    "url": f"https://www.python.org/ftp/python/{version}/Python-{version}.tgz"
                }
            ],
        }
    if component == "OpenSSL":
        version = ssl.OPENSSL_VERSION.split()[1]
        tag = f"openssl-{version}"
        return {
            "version": version,
            "license_expression": "Apache-2.0",
            "license_urls": [
                f"https://raw.githubusercontent.com/openssl/openssl/{tag}/LICENSE.txt"
            ],
            "sources": [
                {
                    "url": f"https://github.com/openssl/openssl/releases/download/{tag}/openssl-{version}.tar.gz"
                }
            ],
        }
    if component == "mpdecimal":
        version = decimal.__libmpdec_version__
        archive = f"https://www.bytereef.org/software/mpdecimal/releases/mpdecimal-{version}.tar.gz"
        return {
            "version": version,
            "license_expression": "BSD-2-Clause",
            "archive_license": {"url": archive, "suffix": "/COPYRIGHT.txt"},
            "sources": [{"url": archive}],
        }
    if component == "Zstandard":
        version = binary_version(first, "ZSTD_versionString")
        return {
            "version": version,
            "license_expression": "BSD-3-Clause",
            "license_urls": [
                f"https://raw.githubusercontent.com/facebook/zstd/v{version}/LICENSE"
            ],
            "sources": [
                {"url": f"https://github.com/facebook/zstd/archive/refs/tags/v{version}.tar.gz"}
            ],
        }
    if component == "XZ Utils":
        version = binary_version(first, "lzma_version_string")
        version_parts = tuple(int(part) for part in version.split("."))
        if version_parts < (5, 4):
            license_expression = "LicenseRef-Public-Domain"
            license_url = (
                f"https://raw.githubusercontent.com/tukaani-project/xz/v{version}/COPYING"
            )
            source_url = f"https://tukaani.org/xz/xz-{version}.tar.gz"
        else:
            license_expression = "0BSD"
            license_url = (
                f"https://raw.githubusercontent.com/tukaani-project/xz/v{version}/COPYING.0BSD"
            )
            source_url = (
                f"https://github.com/tukaani-project/xz/releases/download/v{version}/xz-{version}.tar.gz"
            )
        return {
            "version": version,
            "license_expression": license_expression,
            "license_urls": [license_url],
            "sources": [{"url": source_url}],
        }
    if component == "SQLite":
        version = sqlite3.sqlite_version
        return {
            "version": version,
            "license_expression": "blessing",
            "license_urls": ["https://www.sqlite.org/copyright.html"],
            "sources": [{"url": "https://www.sqlite.org/download.html"}],
        }
    if component == "Expat":
        version = expat.EXPAT_VERSION.removeprefix("expat_").replace("_", ".")
        tag = "R_" + version.replace(".", "_")
        return {
            "version": version,
            "license_expression": "MIT",
            "license_urls": [
                f"https://raw.githubusercontent.com/libexpat/libexpat/{tag}/expat/COPYING"
            ],
            "sources": [
                {"url": f"https://github.com/libexpat/libexpat/releases/tag/{tag}"}
            ],
        }
    if component == "bzip2":
        try:
            version = binary_version(first, "BZ2_bzlibVersion")
        except (AttributeError, OSError, RuntimeError):
            version = source_path_version(first)
        return {
            "version": version,
            "license_expression": "bzip2-1.0.6",
            "license_urls": [
                "https://sourceware.org/git/?p=bzip2.git;a=blob_plain;f=LICENSE;hb=HEAD"
            ],
            "sources": [{"url": "https://sourceware.org/pub/bzip2/"}],
        }
    if component == "zlib":
        version = zlib.ZLIB_RUNTIME_VERSION
        return {
            "version": version,
            "license_expression": "Zlib",
            "license_urls": [
                f"https://raw.githubusercontent.com/madler/zlib/v{version}/README"
            ],
            "sources": [
                {"url": f"https://github.com/madler/zlib/releases/tag/v{version}"}
            ],
        }
    if component == "libffi":
        if platform.system() == "Windows":
            return cpython_source_dependency("libffi", "MIT", "LICENSE")
        version = source_path_version(first)
        return {
            "version": version,
            "license_expression": "MIT",
            "license_urls": [
                "https://raw.githubusercontent.com/libffi/libffi/master/LICENSE"
            ],
            "sources": [{"url": "https://github.com/libffi/libffi/releases"}],
        }
    if component == "GNU Readline":
        version = binary_string(first, "rl_library_version")
        archive = f"https://ftp.gnu.org/gnu/readline/readline-{version}.tar.gz"
        return {
            "version": version,
            "license_expression": "GPL-3.0-or-later",
            "archive_license": {"url": archive, "suffix": "/COPYING"},
            "sources": [{"url": archive}],
        }
    if component == "ncurses":
        import curses

        version_info = curses.ncurses_version
        version = "{}.{}".format(version_info.major, version_info.minor)
        archive = f"https://invisible-island.net/archives/ncurses/ncurses-{version}.tar.gz"
        return {
            "version": version,
            "license_expression": "X11-distribute-modifications-variant",
            "archive_license": {"url": archive, "suffix": "/COPYING"},
            "sources": [{"url": archive}],
        }
    if component == "util-linux libuuid":
        return {
            "version": source_path_version(first),
            "license_expression": "BSD-3-Clause",
            "license_urls": [
                "https://raw.githubusercontent.com/util-linux/util-linux/master/"
                "Documentation/licenses/COPYING.BSD-3-Clause"
            ],
            "sources": [{"url": "https://github.com/util-linux/util-linux/releases"}],
        }
    if component == "GCC runtime":
        return {
            "version": source_path_version(first),
            "license_expression": "GPL-3.0-or-later WITH GCC-exception-3.1",
            "license_urls": [
                "https://raw.githubusercontent.com/gcc-mirror/gcc/master/COPYING3",
                "https://raw.githubusercontent.com/gcc-mirror/gcc/master/COPYING.RUNTIME",
            ],
            "sources": [{"url": "https://ftp.gnu.org/gnu/gcc/"}],
        }
    if component == "libedit":
        return {
            "version": source_path_version(first),
            "license_expression": "BSD-3-Clause",
            "license_urls": [
                "https://raw.githubusercontent.com/NetBSD/src/trunk/lib/libedit/readline.c"
            ],
            "sources": [{"url": "https://www.thrysoee.dk/editline/"}],
        }
    if component == "Operating system runtime":
        return {
            "version": source_path_version(first),
            "license_expression": "system-library",
            "license_files": [],
            "sources": [{"kind": "operating-system"}],
        }
    raise RuntimeError(f"Missing native component metadata: {component}")


def write_notices(component: str, metadata: dict[str, object], destination: Path) -> None:
    if "license_files" in metadata:
        return
    notice_root = destination / "THIRD_PARTY_LICENSES" / "{}-{}".format(
        component.replace(" ", "-"), metadata["version"]
    )
    notice_root.mkdir(parents=True, exist_ok=True)
    copied = []
    for source_name in metadata.pop("local_license_paths", []):
        source = Path(str(source_name))
        target = notice_root / source.name
        counter = 2
        while target.exists():
            target = notice_root / f"{source.stem}-{counter}{source.suffix}"
            counter += 1
        target.write_bytes(source.read_bytes())
        copied.append(str(target.relative_to(destination)))
    for index, url in enumerate(metadata.pop("license_urls", []), start=1):
        with urlopen(str(url), timeout=30) as response:
            payload = response.read()
        filename = Path(str(url).split("?", 1)[0]).name or f"LICENSE-{index}.txt"
        target = notice_root / filename
        target.write_bytes(payload)
        copied.append(str(target.relative_to(destination)))
    archive_notice = metadata.pop("archive_license", None)
    if archive_notice:
        with urlopen(str(archive_notice["url"]), timeout=30) as response:
            payload = response.read()
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            member = next(
                item
                for item in archive.getmembers()
                if item.isfile() and item.name.endswith(str(archive_notice["suffix"]))
            )
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f"Cannot extract {member.name}")
            target = notice_root / Path(member.name).name
            target.write_bytes(source.read())
            copied.append(str(target.relative_to(destination)))
    if metadata["license_expression"] != "system-library" and not copied:
        raise RuntimeError(f"No license notice collected for {component}")
    metadata["license_files"] = copied


def classify_native_entries(
    entries: list[tuple[str, str, str]],
    package_map: dict[Path, list[Distribution]],
) -> dict[str, list[tuple[str, Path, str]]]:
    grouped: dict[str, list[tuple[str, Path, str]]] = {}
    for bundled_name, source_name, kind in entries:
        if kind not in {"BINARY", "EXTENSION"}:
            continue
        source = Path(source_name).resolve()
        component = (
            component_for(source)
            if kind == "BINARY"
            else extension_component(source, package_map)
        )
        grouped.setdefault(component, []).append((bundled_name, source, kind))
    if platform.system() == "Windows":
        for entry in grouped.get("CPython", []):
            bundled_name, _, kind = entry
            dependency = WINDOWS_STATIC_DEPENDENCIES.get(Path(bundled_name).name.lower())
            if kind == "EXTENSION" and dependency:
                grouped.setdefault(dependency[0], []).append(entry)
            if kind == "BINARY" and re.fullmatch(
                r"python\d{2,}[a-z]?\.dll", Path(bundled_name).name.lower()
            ):
                zlib_dependency = WINDOWS_STATIC_DEPENDENCIES["zlib.pyd"]
                grouped.setdefault(zlib_dependency[0], []).append(entry)
    return grouped


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("analysis", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    destination = args.destination.resolve()

    analysis = ast.literal_eval(args.analysis.read_text(encoding="utf-8"))
    native_entries = [
        entry for entry in analysis[15] if entry[2] in {"BINARY", "EXTENSION"}
    ]
    package_map = (
        installed_distribution_map()
        if any(entry[2] == "EXTENSION" for entry in native_entries)
        else {}
    )
    binaries = classify_native_entries(native_entries, package_map)

    records = []
    for component, entries in sorted(binaries.items()):
        metadata = component_metadata(
            component, [source for _, source, _ in entries], package_map
        )
        write_notices(component, metadata, destination)
        metadata.update(
            {
                "name": component,
                "binaries": [
                    {
                        "bundled_name": bundled_name,
                        "kind": kind,
                        "source_name": source.name,
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    }
                    for bundled_name, source, kind in entries
                ],
            }
        )
        records.append(metadata)

    (destination / "NATIVE_COMPONENTS.json").write_text(
        json.dumps({"schema": 1, "components": records}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
