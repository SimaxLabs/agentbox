import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import patch

from agentbox import cli, update
from agentbox.core import AgentBoxError
from agentbox.update import (
    ReleaseInfo,
    UpdateStatus,
    _cached_failure,
    _cached_release,
    _install_channel,
    check_for_updates,
    current_build,
    fetch_latest_release,
    version_relation,
)


class AgentBoxUpdateTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        environment = patch.dict(
            os.environ,
            {"AGENTBOX_INSTALL_CHANNEL": "", "AGENTBOX_NO_UPDATE_CHECK": ""},
        )
        environment.start()
        self.addCleanup(environment.stop)

    def release(self, version="1.2.0", commit="b" * 40):
        tag = "v{}".format(version)
        return ReleaseInfo(
            "SimaxLabs/AgentBox",
            commit,
            tag,
            "https://github.com/SimaxLabs/AgentBox/releases/tag/{}".format(tag),
            version,
        )

    def test_release_identity_uses_strict_semver_and_lightweight_commit_tag(self):
        commit = "c" * 40
        payload = {
            "tag_name": "v1.2.3",
            "target_commitish": commit,
            "html_url": "https://github.com/SimaxLabs/AgentBox/releases/tag/v1.2.3",
            "draft": False,
            "prerelease": False,
        }
        reference = {
            "ref": "refs/tags/v1.2.3",
            "object": {"type": "commit", "sha": commit},
        }
        with patch("agentbox.update._read_json", side_effect=[payload, reference]) as request:
            release = fetch_latest_release("SimaxLabs/AgentBox")

        self.assertEqual(commit, release.commit)
        self.assertEqual("1.2.3", release.version)
        self.assertEqual(payload["html_url"], release.page_url)
        self.assertIn("/git/ref/tags/v1.2.3", request.call_args_list[1].args[0])

        positional = ReleaseInfo(
            release.repository,
            release.commit,
            release.tag,
            release.page_url,
        )
        self.assertEqual("1.2.3", positional.version)

    def test_release_rejects_noncanonical_semantic_tags(self):
        for tag in (
            "1.2.3",
            "v01.2.3",
            "v1.02.3",
            "v1.2.03",
            "v1.2",
            "v1.2.3-rc.1",
            "v1.2.3+build",
        ):
            with self.subTest(tag=tag), patch(
                "agentbox.update._read_json",
                return_value={
                    "tag_name": tag,
                    "target_commitish": "c" * 40,
                    "html_url": "https://github.com/SimaxLabs/AgentBox/releases/tag/{}".format(
                        tag
                    ),
                    "draft": False,
                    "prerelease": False,
                },
            ):
                with self.assertRaisesRegex(AgentBoxError, "invalid identity"):
                    fetch_latest_release("SimaxLabs/AgentBox")

    def test_release_rejects_bad_tag_identity_and_page_url(self):
        commit = "c" * 40
        payload = {
            "tag_name": "v1.2.3",
            "target_commitish": commit,
            "html_url": "https://github.com/SimaxLabs/AgentBox/releases/tag/v1.2.3",
            "draft": False,
            "prerelease": False,
        }
        annotated = {
            "ref": "refs/tags/v1.2.3",
            "object": {"type": "tag", "sha": commit},
        }
        with patch("agentbox.update._read_json", side_effect=[payload, annotated]):
            with self.assertRaisesRegex(AgentBoxError, "lightweight"):
                fetch_latest_release("SimaxLabs/AgentBox")

        mismatched = {
            "ref": "refs/tags/v1.2.3",
            "object": {"type": "commit", "sha": "d" * 40},
        }
        with patch("agentbox.update._read_json", side_effect=[payload, mismatched]):
            with self.assertRaisesRegex(AgentBoxError, "lightweight"):
                fetch_latest_release("SimaxLabs/AgentBox")

        payload["html_url"] = "https://example.invalid/release"
        lightweight = {
            "ref": "refs/tags/v1.2.3",
            "object": {"type": "commit", "sha": commit},
        }
        with patch("agentbox.update._read_json", side_effect=[payload, lightweight]):
            with self.assertRaisesRegex(AgentBoxError, "valid page URL"):
                fetch_latest_release("SimaxLabs/AgentBox")

    def test_standalone_update_awareness_provides_only_release_guidance(self):
        with patch(
            "agentbox.update.current_build",
            return_value=("SimaxLabs/AgentBox", "1.1.0", "a" * 40),
        ), patch("agentbox.update._cached_release", return_value=None), patch(
            "agentbox.update._cached_failure", return_value=None
        ), patch(
            "agentbox.update.fetch_latest_release", return_value=self.release()
        ), patch(
            "agentbox.update._store_cached_release"
        ), patch(
            "agentbox.update._failure_cache_path", return_value=self.root / "failure.json"
        ), patch.object(
            sys, "frozen", True, create=True
        ):
            status = check_for_updates()

        self.assertTrue(status.update_available)
        self.assertTrue(status.standalone)
        self.assertEqual(self.release().page_url, status.release_url)
        self.assertEqual("v1.2.0", status.latest_label)
        self.assertEqual("bbbbbbbbbbbb", status.latest_commit_label)
        self.assertFalse(hasattr(update, "install_update"))
        self.assertFalse(hasattr(update, "prepare_update_plan"))

    def test_semantic_versions_are_compared_numerically(self):
        self.assertEqual("newer", version_relation("1.9.9", "1.10.0"))
        self.assertEqual("same", version_relation("2.0.0", "2.0.0"))
        self.assertEqual("older", version_relation("10.0.0", "2.99.99"))

    def test_frozen_package_manager_paths_are_detected_without_wrapper_environment(self):
        paths = (
            ("/opt/homebrew/Cellar/agentbox/1.2.3/libexec/agentbox", "homebrew"),
            ("/Users/test/scoop/apps/agentbox/1.2.3/agentbox.exe", "scoop"),
        )
        for executable, expected in paths:
            with self.subTest(channel=expected), patch.object(
                sys, "frozen", True, create=True
            ), patch.object(sys, "executable", executable):
                channel, command = _install_channel()
            self.assertEqual(expected, channel)
            self.assertIsNotNone(command)

    def test_managed_channels_remain_update_aware(self):
        for channel, command in (
            ("homebrew", "brew upgrade agentbox"),
            ("scoop", "scoop update agentbox"),
        ):
            with self.subTest(channel=channel), patch.dict(
                os.environ, {"AGENTBOX_INSTALL_CHANNEL": channel}
            ), patch(
                "agentbox.update.current_build",
                return_value=("SimaxLabs/AgentBox", "1.1.0", "a" * 40),
            ), patch(
                "agentbox.update.fetch_latest_release", return_value=self.release()
            ), patch(
                "agentbox.update._store_cached_release"
            ), patch(
                "agentbox.update._failure_cache_path", return_value=self.root / "failure.json"
            ), patch.object(
                sys, "frozen", True, create=True
            ):
                status = check_for_updates(force=True)

            self.assertTrue(status.update_available)
            self.assertFalse(status.standalone)
            self.assertEqual(channel, status.install_channel)
            self.assertEqual(command, status.install_command)

    def test_opt_out_does_not_inspect_the_git_checkout(self):
        with patch.dict("os.environ", {"AGENTBOX_NO_UPDATE_CHECK": "1"}), patch(
            "agentbox.update._git_checkout_commit"
        ) as git_commit:
            status = check_for_updates()

        self.assertTrue(status.disabled)
        git_commit.assert_not_called()

    def test_malformed_cache_is_ignored(self):
        cache = self.root / "update-check.json"
        cache.write_text(
            json.dumps(
                {
                    "version": 3,
                    "checked_at": time.time(),
                    "repository": "SimaxLabs/AgentBox",
                    "current_version": "1.1.0",
                    "current_commit": "a" * 40,
                    "release": {
                        "version": "1.2.0",
                        "commit": 123,
                        "tag": "v1.2.0",
                        "page_url": "https://github.com/SimaxLabs/AgentBox/releases/tag/v1.2.0",
                    },
                }
            ),
            encoding="utf-8",
        )
        with patch("agentbox.update._cache_path", return_value=cache):
            self.assertIsNone(
                _cached_release("SimaxLabs/AgentBox", "1.1.0", "a" * 40)
            )

    def test_cache_is_bound_to_current_build_and_validates_release_url(self):
        cache = self.root / "update-check.json"
        value = {
            "version": 3,
            "checked_at": time.time(),
            "repository": "SimaxLabs/AgentBox",
            "current_version": "1.1.0",
            "current_commit": "a" * 40,
            "release": {
                "version": "1.2.0",
                "commit": "b" * 40,
                "tag": "v1.2.0",
                "page_url": "https://github.com/SimaxLabs/AgentBox/releases/tag/v1.2.0",
            },
        }
        cache.write_text(json.dumps(value), encoding="utf-8")
        with patch("agentbox.update._cache_path", return_value=cache):
            cached = _cached_release("SimaxLabs/AgentBox", "1.1.0", "a" * 40)
            self.assertIsNotNone(cached)
            self.assertEqual("1.2.0", cached.version)
            self.assertIsNone(
                _cached_release("SimaxLabs/AgentBox", "1.0.9", "a" * 40)
            )
            self.assertIsNone(
                _cached_release("SimaxLabs/AgentBox", "1.1.0", "c" * 40)
            )

            value["release"]["page_url"] = "https://example.invalid/release"
            cache.write_text(json.dumps(value), encoding="utf-8")
            self.assertIsNone(
                _cached_release("SimaxLabs/AgentBox", "1.1.0", "a" * 40)
            )

    def test_current_build_discovers_frozen_source_and_installed_versions(self):
        with patch.object(sys, "frozen", True, create=True), patch(
            "agentbox.update._build_info",
            return_value={
                "repository": "Owner/Repository",
                "version": "3.4.5",
                "commit": "c" * 40,
            },
        ), patch("agentbox.update._git_checkout_commit") as git_commit:
            self.assertEqual(
                ("Owner/Repository", "3.4.5", "c" * 40), current_build()
            )
            git_commit.assert_not_called()

        with patch.object(sys, "frozen", False, create=True), patch(
            "agentbox.update._source_project_version", return_value="2.3.4"
        ) as source_version, patch(
            "agentbox.update._git_checkout_commit", return_value="d" * 40
        ), patch("agentbox.update.importlib.metadata.version") as package_version:
            repository, version, commit = current_build()
            self.assertEqual("SimaxLabs/agentbox", repository)
            self.assertEqual("2.3.4", version)
            self.assertEqual("d" * 40, commit)
            source_version.assert_called_once_with()
            package_version.assert_not_called()

        with patch.object(sys, "frozen", False, create=True), patch(
            "agentbox.update._source_project_version", return_value=None
        ), patch(
            "agentbox.update.importlib.metadata.version", return_value="4.5.6"
        ):
            self.assertEqual(
                ("SimaxLabs/agentbox", "4.5.6", None), current_build()
            )

    def test_network_failure_uses_stale_release_metadata(self):
        stale = self.release()
        with patch(
            "agentbox.update.current_build",
            return_value=("SimaxLabs/AgentBox", "1.1.0", "a" * 40),
        ), patch(
            "agentbox.update._cached_release", side_effect=[None, stale, None, stale]
        ), patch(
            "agentbox.update._cached_failure", side_effect=[None, "offline"]
        ), patch(
            "agentbox.update.fetch_latest_release", side_effect=AgentBoxError("offline")
        ) as fetch, patch(
            "agentbox.update._store_failure"
        ), patch.object(
            sys, "frozen", True, create=True
        ):
            status = check_for_updates()
            repeated = check_for_updates()

        self.assertTrue(status.update_available)
        self.assertTrue(status.stale)
        self.assertTrue(repeated.stale)
        fetch.assert_called_once()

    def test_http_protocol_failure_does_not_escape_update_awareness(self):
        with patch(
            "agentbox.update.current_build",
            return_value=("SimaxLabs/AgentBox", "1.1.0", "a" * 40),
        ), patch("agentbox.update._cached_release", return_value=None), patch(
            "agentbox.update._cached_failure", return_value=None
        ), patch(
            "agentbox.update.urlopen", side_effect=IncompleteRead(b"truncated")
        ), patch(
            "agentbox.update._store_failure"
        ):
            status = check_for_updates()

        self.assertIn("Cannot contact", status.error)

    def test_invalid_utf8_failure_cache_is_ignored(self):
        failure = self.root / "failure.json"
        failure.write_bytes(b"\xff")
        with patch("agentbox.update._failure_cache_path", return_value=failure):
            self.assertIsNone(
                _cached_failure("SimaxLabs/agentbox", "1.0.0", "a" * 40)
            )

    def test_cli_announces_manual_standalone_download(self):
        release_url = "https://github.com/SimaxLabs/AgentBox/releases/tag/v1.2.0"
        status = UpdateStatus(
            "SimaxLabs/AgentBox",
            "a" * 40,
            "b" * 40,
            release_url,
            True,
            current_version="1.1.0",
            latest_version="1.2.0",
            version_relation="newer",
            standalone=True,
        )
        output = io.StringIO()
        with patch("agentbox.cli.check_for_updates", return_value=status), redirect_stderr(
            output
        ):
            cli.announce_available_update()

        self.assertIn("download it manually", output.getvalue())
        self.assertIn(release_url, output.getvalue())
        self.assertNotIn("agentbox update", output.getvalue())

    def test_cli_announces_managed_homebrew_and_scoop_commands(self):
        for channel, command in (
            ("homebrew", "brew upgrade agentbox"),
            ("scoop", "scoop update agentbox"),
        ):
            status = UpdateStatus(
                "SimaxLabs/AgentBox",
                "a" * 40,
                "b" * 40,
                "https://github.com/SimaxLabs/AgentBox/releases/tag/v1.2.0",
                True,
                current_version="1.1.0",
                latest_version="1.2.0",
                version_relation="newer",
                install_channel=channel,
                install_command=command,
            )
            output = io.StringIO()
            with self.subTest(channel=channel), patch(
                "agentbox.cli.check_for_updates", return_value=status
            ), redirect_stderr(output):
                cli.announce_available_update()
            self.assertIn(command, output.getvalue())

    def test_cli_checks_for_updates_after_a_successful_operation(self):
        calls = []
        with patch("agentbox.cli.run_operation", side_effect=lambda *_: calls.append("run")), patch(
            "agentbox.cli.announce_available_update",
            side_effect=lambda: calls.append("announce"),
        ):
            result = cli.main(
                ["status"], repository_config=self.root / "repository-agentbox.json"
            )

        self.assertEqual(0, result)
        self.assertEqual(["run", "announce"], calls)

    def test_cli_does_not_check_for_updates_after_a_failed_operation(self):
        with patch(
            "agentbox.cli.run_operation", side_effect=AgentBoxError("operation failed")
        ), patch("agentbox.cli.announce_available_update") as announce:
            with self.assertRaisesRegex(AgentBoxError, "operation failed"):
                cli.main(
                    ["status"], repository_config=self.root / "repository-agentbox.json"
                )
        announce.assert_not_called()

    def test_cli_has_no_update_command(self):
        output = io.StringIO()
        with redirect_stderr(output), self.assertRaises(SystemExit) as exit_status:
            cli.parse_args(
                ("opencode",), self.root / "agentbox.json", ["update"]
            )

        self.assertEqual(2, exit_status.exception.code)
        self.assertIn("invalid choice", output.getvalue())

    def test_cli_version_reports_the_source_build(self):
        output = io.StringIO()
        with patch(
            "agentbox.cli.current_build",
            return_value=("SimaxLabs/AgentBox", "1.2.3", "a" * 40),
        ), redirect_stdout(output), self.assertRaises(SystemExit) as exit_status:
            cli.parse_args(("opencode",), self.root / "agentbox.json", ["--version"])

        self.assertEqual(0, exit_status.exception.code)
        self.assertEqual(
            "AgentBox v1.2.3 (commit {}, SimaxLabs/AgentBox)\n".format("a" * 40),
            output.getvalue(),
        )

    def test_ui_startup_leaves_update_awareness_to_the_browser(self):
        with patch("agentbox.cli.announce_available_update") as announce, patch(
            "agentbox.web.run_browser", return_value=0
        ) as run_browser:
            result = cli.main(
                ["--config", str(self.root / "agentbox.json"), "ui", "--no-open"],
                repository_config=self.root / "repository-agentbox.json",
            )

        self.assertEqual(0, result)
        announce.assert_not_called()
        run_browser.assert_called_once()


if __name__ == "__main__":
    unittest.main()
