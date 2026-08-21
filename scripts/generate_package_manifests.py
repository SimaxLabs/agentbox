"""Generate in-repository Homebrew and Scoop definitions for a release."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

try:
    from .release_version import VERSION
except ImportError:  # Direct script execution places scripts/ on sys.path.
    from release_version import VERSION


SHA256 = re.compile(r"^[0-9a-f]{64}$")
TARGETS = (
    "macos-arm64",
    "linux-x86_64",
    "linux-arm64",
    "windows-x86_64",
)


def load_checksums(path: Path) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for line in path.read_text(encoding="ascii").splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        digest, name = fields
        name = name.lstrip("*")
        if SHA256.fullmatch(digest) is None or Path(name).name != name:
            continue
        if name in checksums:
            raise ValueError("Duplicate checksum entry for {}".format(name))
        checksums[name] = digest
    return checksums


def archive_name(version: str, target: str) -> str:
    extension = ".zip" if target == "windows-x86_64" else ".tar.gz"
    return "agentbox-{}-{}{}".format(version, target, extension)


def required_hashes(version: str, checksums: dict[str, str]) -> dict[str, str]:
    resolved = {}
    for target in TARGETS:
        name = archive_name(version, target)
        digest = checksums.get(name)
        if digest is None:
            raise ValueError("Missing checksum for {}".format(name))
        resolved[target] = digest
    return resolved


def homebrew_formula(version: str, repository: str, hashes: dict[str, str]) -> str:
    release = "https://github.com/{}/releases/download/v{}".format(repository, version)
    return '''class Agentbox < Formula
  desc "Back up and restore AI coding-agent resources"
  homepage "https://github.com/{repository}"
  version "{version}"
  license "GPL-3.0-only"

  on_macos do
    depends_on arch: :arm64
    url "{release}/agentbox-{version}-macos-arm64.tar.gz"
    sha256 "{macos}"
  end

  on_linux do
    on_intel do
      depends_on arch: :x86_64
      url "{release}/agentbox-{version}-linux-x86_64.tar.gz"
      sha256 "{linux_x86}"
    end

    on_arm do
      depends_on arch: :arm64
      url "{release}/agentbox-{version}-linux-arm64.tar.gz"
      sha256 "{linux_arm}"
    end
  end

  def install
    libexec.install "agentbox"
    (bin/"agentbox").write_env_script libexec/"agentbox",
                                       AGENTBOX_INSTALL_CHANNEL: "homebrew"
  end

  test do
    assert_match "AgentBox v{version}", shell_output("#{{bin}}/agentbox --version")
  end
end
'''.format(
        repository=repository,
        version=version,
        release=release,
        macos=hashes["macos-arm64"],
        linux_x86=hashes["linux-x86_64"],
        linux_arm=hashes["linux-arm64"],
    )


def scoop_manifest(version: str, repository: str, hashes: dict[str, str]) -> str:
    package = "agentbox-{}-windows-x86_64".format(version)
    value = {
        "version": version,
        "description": "Back up and restore AI coding-agent resources.",
        "homepage": "https://github.com/{}".format(repository),
        "license": "GPL-3.0-only",
        "architecture": {
            "64bit": {
                "url": "https://github.com/{}/releases/download/v{}/{}.zip".format(
                    repository, version, package
                ),
                "hash": hashes["windows-x86_64"],
                "extract_dir": package,
            }
        },
        "bin": [["agentbox-scoop.cmd", "agentbox"]],
    }
    return json.dumps(value, indent=2) + "\n"


def write_manifests(root: Path, version: str, repository: str, checksums: Path) -> None:
    if re.fullmatch(VERSION, version) is None:
        raise ValueError("Version must use canonical X.Y.Z syntax")
    hashes = required_hashes(version, load_checksums(checksums))
    formula = root / "Formula/agentbox.rb"
    bucket = root / "bucket/agentbox.json"
    formula.parent.mkdir(parents=True, exist_ok=True)
    bucket.parent.mkdir(parents=True, exist_ok=True)
    formula.write_text(homebrew_formula(version, repository, hashes), encoding="utf-8")
    bucket.write_text(scoop_manifest(version, repository, hashes), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--checksums", required=True, type=Path)
    parser.add_argument("--repository", default="SimaxLabs/agentbox")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        write_manifests(
            args.root.resolve(), args.version, args.repository, args.checksums.resolve()
        )
    except (OSError, UnicodeError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
