import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.generate_package_manifests import write_manifests
from scripts.release_version import main, release_version, require_newest


class SemanticReleaseTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_release_requires_one_exact_marker_line(self):
        self.assertEqual("1.2.3", release_version("Feature\n\nRelease: v1.2.3\n"))
        self.assertIsNone(release_version("Feature mentions v1.2.3"))
        self.assertIsNone(release_version("release: v1.2.3"))
        self.assertIsNone(release_version("Release: v01.2.3"))
        with self.assertRaisesRegex(ValueError, "exactly one"):
            release_version("Release: v1.2.3\nRelease: v1.2.3")

    def test_release_marker_must_match_project_version(self):
        project = self.root / "pyproject.toml"
        project.write_text('[project]\nversion = "1.2.4"\n', encoding="utf-8")
        with patch("sys.stdin", io.StringIO("Release: v1.2.3\n")), patch(
            "sys.argv", ["release_version.py", "--project", str(project)]
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), self.assertRaises(
            SystemExit
        ) as exit_status:
            main()
        self.assertEqual(2, exit_status.exception.code)

    def test_release_cannot_be_older_than_an_existing_stable_release(self):
        require_newest("1.10.0", ["v1.9.9", "not-a-release"])
        with self.assertRaisesRegex(ValueError, "older"):
            require_newest("1.9.0", ["v1.10.0"])

    def test_manifests_pin_all_archives_and_managed_wrappers(self):
        version = "1.2.3"
        names = {
            "macos-arm64": "a" * 64,
            "linux-x86_64": "b" * 64,
            "linux-arm64": "c" * 64,
            "windows-x86_64": "d" * 64,
        }
        checksums = self.root / "SHA256SUMS.txt"
        checksums.write_text(
            "".join(
                "{}  agentbox-{}-{}{}\n".format(
                    digest,
                    version,
                    target,
                    ".zip" if target == "windows-x86_64" else ".tar.gz",
                )
                for target, digest in names.items()
            ),
            encoding="ascii",
        )

        write_manifests(self.root, version, "SimaxLabs/agentbox", checksums)

        formula = (self.root / "Formula/agentbox.rb").read_text(encoding="utf-8")
        manifest = json.loads((self.root / "bucket/agentbox.json").read_text())
        self.assertIn('version "1.2.3"', formula)
        self.assertIn('AGENTBOX_INSTALL_CHANNEL: "homebrew"', formula)
        for digest in names.values():
            self.assertIn(digest, formula + json.dumps(manifest))
        self.assertEqual([["agentbox-scoop.cmd", "agentbox"]], manifest["bin"])
        self.assertEqual(
            "agentbox-1.2.3-windows-x86_64",
            manifest["architecture"]["64bit"]["extract_dir"],
        )


if __name__ == "__main__":
    unittest.main()
