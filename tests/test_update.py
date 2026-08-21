import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agentbox import cli
from agentbox.core import AgentBoxError
from agentbox.update import (
    ReleaseAsset,
    ReleaseInfo,
    UpdatePlan,
    UpdateStatus,
    _cached_failure,
    _consume_update_warning,
    _install_channel,
    _reserve_windows_update,
    _schedule_windows_replacement,
    _cached_release,
    _download_archive,
    _stage_executable,
    check_for_updates,
    current_build,
    fetch_latest_release,
    install_update,
    prepare_update_plan,
    release_relation,
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
            (
                ReleaseAsset(
                    "agentbox-{}-macos-arm64.tar.gz".format(version),
                    "https://github.com/SimaxLabs/AgentBox/releases/download/{}/agentbox-{}-macos-arm64.tar.gz".format(
                        tag, version
                    ),
                ),
                ReleaseAsset(
                    "SHA256SUMS.txt",
                    "https://github.com/SimaxLabs/AgentBox/releases/download/{}/SHA256SUMS.txt".format(
                        tag
                    ),
                ),
            ),
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
            "assets": [
                {
                    "name": "SHA256SUMS.txt",
                    "browser_download_url": "https://github.com/SimaxLabs/AgentBox/releases/download/v1.2.3/SHA256SUMS.txt",
                }
            ],
        }
        reference = {
            "ref": "refs/tags/v1.2.3",
            "object": {"type": "commit", "sha": commit},
        }
        with patch("agentbox.update._read_json", side_effect=[payload, reference]) as request:
            release = fetch_latest_release("SimaxLabs/AgentBox")

        self.assertEqual(commit, release.commit)
        self.assertEqual("1.2.3", release.version)
        self.assertEqual("SHA256SUMS.txt", release.assets[0].name)
        self.assertIn("/git/ref/tags/v1.2.3", request.call_args_list[1].args[0])

        positional = ReleaseInfo(
            release.repository,
            release.commit,
            release.tag,
            release.page_url,
            release.assets,
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
                    "assets": [],
                },
            ):
                with self.assertRaisesRegex(AgentBoxError, "invalid identity"):
                    fetch_latest_release("SimaxLabs/AgentBox")

    def test_release_rejects_annotated_or_mismatched_tag_and_foreign_assets(self):
        commit = "c" * 40
        payload = {
            "tag_name": "v1.2.3",
            "target_commitish": commit,
            "html_url": "https://github.com/SimaxLabs/AgentBox/releases/tag/v1.2.3",
            "draft": False,
            "prerelease": False,
            "assets": [],
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

        payload["assets"] = [
            {
                "name": "SHA256SUMS.txt",
                "browser_download_url": "https://example.invalid/SHA256SUMS.txt",
            }
        ]
        lightweight = {
            "ref": "refs/tags/v1.2.3",
            "object": {"type": "commit", "sha": commit},
        }
        with patch("agentbox.update._read_json", side_effect=[payload, lightweight]):
            with self.assertRaisesRegex(AgentBoxError, "invalid assets"):
                fetch_latest_release("SimaxLabs/AgentBox")

    def test_update_awareness_compares_embedded_commits(self):
        with patch("agentbox.update.current_build", return_value=("SimaxLabs/AgentBox", "1.1.0", "a" * 40)), patch(
            "agentbox.update.release_target", return_value="macos-arm64"
        ), patch("agentbox.update._cached_release", return_value=None), patch(
            "agentbox.update._cached_failure", return_value=None
        ), patch(
            "agentbox.update.fetch_latest_release", return_value=self.release()
        ), patch(
            "agentbox.update.release_relation", return_value="ahead"
        ), patch("agentbox.update._store_cached_release"), patch(
            "agentbox.update._failure_cache_path", return_value=self.root / "failure.json"
        ), patch.object(
            sys, "frozen", True, create=True
        ):
            status = check_for_updates()

        self.assertTrue(status.update_available)
        self.assertTrue(status.can_self_update)
        self.assertEqual("v1.2.0", status.latest_label)
        self.assertEqual("bbbbbbbbbbbb", status.latest_commit_label)

    def test_prepare_update_plan_pins_asset_checksum_and_current_executable(self):
        executable = self.root / "agentbox"
        executable.write_bytes(b"old executable")
        checksum = "d" * 64
        with patch("agentbox.update.current_build", return_value=("SimaxLabs/AgentBox", "1.1.0", "a" * 40)), patch(
            "agentbox.update.release_target", return_value="macos-arm64"
        ), patch("agentbox.update.fetch_latest_release", return_value=self.release()), patch(
            "agentbox.update.release_relation", return_value="ahead"
        ), patch(
            "agentbox.update._checksums",
            return_value={"agentbox-1.2.0-macos-arm64.tar.gz": checksum},
        ), patch.object(sys, "frozen", True, create=True), patch.object(
            sys, "executable", str(executable)
        ):
            plan = prepare_update_plan()

        self.assertEqual(checksum, plan.archive_sha256)
        self.assertEqual("agentbox-1.2.0-macos-arm64.tar.gz", plan.archive_name)
        self.assertEqual("1.1.0", plan.current_version)
        self.assertEqual("1.2.0", plan.latest_version)
        self.assertEqual(hashlib.sha256(b"old executable").hexdigest(), plan.executable_sha256)
        self.assertEqual(str(executable.resolve()), plan.executable_path)

    def test_prepare_update_plan_refuses_a_downgrade(self):
        executable = self.root / "agentbox"
        executable.write_bytes(b"newer executable")
        with patch(
            "agentbox.update.current_build",
            return_value=("SimaxLabs/AgentBox", "2.0.0", "a" * 40),
        ), patch("agentbox.update.release_target", return_value="macos-arm64"), patch(
            "agentbox.update.fetch_latest_release", return_value=self.release()
        ), patch("agentbox.update.release_relation") as relation, patch.object(
            sys, "frozen", True, create=True
        ), patch.object(sys, "executable", str(executable)):
            with self.assertRaisesRegex(AgentBoxError, "semantic downgrade"):
                prepare_update_plan()
        relation.assert_not_called()

    def test_update_awareness_refuses_a_release_that_is_not_ahead(self):
        with patch(
            "agentbox.update.current_build",
            return_value=("SimaxLabs/AgentBox", "1.1.0", "a" * 40),
        ), patch("agentbox.update.release_target", return_value="macos-arm64"), patch(
            "agentbox.update._cached_release", return_value=None
        ), patch("agentbox.update._cached_failure", return_value=None), patch(
            "agentbox.update.fetch_latest_release", return_value=self.release()
        ), patch("agentbox.update.release_relation", return_value="behind"), patch(
            "agentbox.update._store_cached_release"
        ), patch.object(sys, "frozen", True, create=True):
            status = check_for_updates()

        self.assertTrue(status.update_available)
        self.assertFalse(status.can_self_update)
        self.assertEqual("behind", status.relation)

    def test_release_relation_uses_github_compare_status(self):
        with patch("agentbox.update._read_json", return_value={"status": "ahead"}) as request:
            relation = release_relation("SimaxLabs/AgentBox", "a" * 40, "b" * 40)

        self.assertEqual("ahead", relation)
        self.assertIn(
            "/compare/{}...{}".format("a" * 40, "b" * 40),
            request.call_args.args[0],
        )

    def test_semantic_versions_are_compared_numerically(self):
        self.assertEqual("newer", version_relation("1.9.9", "1.10.0"))
        self.assertEqual("same", version_relation("2.0.0", "2.0.0"))
        self.assertEqual("older", version_relation("10.0.0", "2.99.99"))

    def test_prepare_update_plan_requires_ahead_commit_ancestry(self):
        executable = self.root / "agentbox"
        executable.write_bytes(b"executable")
        for relation in ("behind", "diverged", "identical"):
            with self.subTest(relation=relation), patch(
                "agentbox.update.current_build",
                return_value=("SimaxLabs/AgentBox", "1.1.0", "a" * 40),
            ), patch(
                "agentbox.update.release_target", return_value="macos-arm64"
            ), patch(
                "agentbox.update.fetch_latest_release", return_value=self.release()
            ), patch(
                "agentbox.update.release_relation", return_value=relation
            ), patch.object(
                sys, "frozen", True, create=True
            ), patch.object(
                sys, "executable", str(executable)
            ):
                with self.assertRaisesRegex(AgentBoxError, relation):
                    prepare_update_plan()

    def test_managed_channels_disable_direct_updates_before_network(self):
        for channel, command in (
            ("homebrew", "brew upgrade agentbox"),
            ("scoop", "scoop update agentbox"),
            ("custom-manager", None),
        ):
            with self.subTest(channel=channel), patch.dict(
                os.environ, {"AGENTBOX_INSTALL_CHANNEL": channel}
            ), patch("agentbox.update.fetch_latest_release") as fetch:
                with self.assertRaisesRegex(AgentBoxError, "managed by") as error:
                    prepare_update_plan()
                fetch.assert_not_called()
                if command:
                    self.assertIn(command, str(error.exception))

    def test_frozen_package_manager_paths_fail_closed_without_wrapper_environment(self):
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
                "agentbox.update.release_target", return_value="macos-arm64"
            ), patch(
                "agentbox.update.fetch_latest_release", return_value=self.release()
            ), patch(
                "agentbox.update.release_relation", return_value="ahead"
            ), patch.object(
                sys, "frozen", True, create=True
            ):
                status = check_for_updates(force=True)

            self.assertTrue(status.update_available)
            self.assertFalse(status.can_self_update)
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
                    "version": 2,
                    "checked_at": time.time(),
                    "repository": "SimaxLabs/AgentBox",
                    "current_version": "1.1.0",
                    "current_commit": "a" * 40,
                    "relation": "ahead",
                    "release": {
                        "version": "1.2.0",
                        "commit": 123,
                        "tag": "v1.2.0",
                        "page_url": "https://github.com/SimaxLabs/AgentBox/releases/tag/v1.2.0",
                        "assets": [],
                    },
                }
            ),
            encoding="utf-8",
        )
        with patch("agentbox.update._cache_path", return_value=cache):
            self.assertIsNone(
                _cached_release("SimaxLabs/AgentBox", "1.1.0", "a" * 40)
            )

    def test_cache_is_bound_to_current_version_and_commit_and_validates_urls(self):
        cache = self.root / "update-check.json"
        value = {
            "version": 2,
            "checked_at": time.time(),
            "repository": "SimaxLabs/AgentBox",
            "current_version": "1.1.0",
            "current_commit": "a" * 40,
            "relation": "ahead",
            "release": {
                "version": "1.2.0",
                "commit": "b" * 40,
                "tag": "v1.2.0",
                "page_url": "https://github.com/SimaxLabs/AgentBox/releases/tag/v1.2.0",
                "assets": [
                    {
                        "name": "SHA256SUMS.txt",
                        "url": "https://github.com/SimaxLabs/AgentBox/releases/download/v1.2.0/SHA256SUMS.txt",
                    }
                ],
            },
        }
        cache.write_text(json.dumps(value), encoding="utf-8")
        with patch("agentbox.update._cache_path", return_value=cache):
            cached = _cached_release("SimaxLabs/AgentBox", "1.1.0", "a" * 40)
            self.assertIsNotNone(cached)
            self.assertEqual("1.2.0", cached[0].version)
            self.assertIsNone(
                _cached_release("SimaxLabs/AgentBox", "1.0.9", "a" * 40)
            )
            self.assertIsNone(
                _cached_release("SimaxLabs/AgentBox", "1.1.0", "c" * 40)
            )

            value["release"]["assets"][0]["url"] = (
                "https://example.invalid/SHA256SUMS.txt"
            )
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
            "agentbox.update._git_checkout_commit", return_value="d" * 40
        ), patch("agentbox.update.importlib.metadata.version") as package_version:
            repository, version, commit = current_build()
            self.assertEqual("SimaxLabs/agentbox", repository)
            self.assertEqual("0.2.0", version)
            self.assertEqual("d" * 40, commit)
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
        stale = (self.release(), "ahead")
        with patch(
            "agentbox.update.current_build",
            return_value=("SimaxLabs/AgentBox", "1.1.0", "a" * 40),
        ), patch("agentbox.update.release_target", return_value="macos-arm64"), patch(
            "agentbox.update._cached_release", side_effect=[None, stale, None, stale]
        ), patch(
            "agentbox.update._cached_failure", side_effect=[None, "offline"]
        ), patch(
            "agentbox.update.fetch_latest_release", side_effect=AgentBoxError("offline")
        ) as fetch, patch("agentbox.update._store_failure"), patch.object(
            sys, "frozen", True, create=True
        ):
            status = check_for_updates()
            repeated = check_for_updates()

        self.assertTrue(status.update_available)
        self.assertTrue(status.stale)
        self.assertTrue(repeated.stale)
        fetch.assert_called_once()

    def test_invalid_utf8_update_state_is_ignored(self):
        failure = self.root / "failure.json"
        result = self.root / "result.json"
        failure.write_bytes(b"\xff")
        result.write_bytes(b"\xff")
        with patch("agentbox.update._failure_cache_path", return_value=failure):
            self.assertIsNone(
                _cached_failure("SimaxLabs/agentbox", "1.0.0", "a" * 40)
            )
        with patch("agentbox.update._update_result_path", return_value=result):
            self.assertIsNone(_consume_update_warning())

    def test_install_rechecks_executable_immediately_before_replacement(self):
        target = self.root / "agentbox"
        target.write_bytes(b"old")
        staged = self.root / ".agentbox.update"
        staged.write_bytes(b"new")
        reviewed = UpdatePlan(
            "SimaxLabs/AgentBox",
            "a" * 40,
            "b" * 40,
            "https://example.invalid/release",
            "macos-arm64",
            "agentbox-1.2.0-macos-arm64.tar.gz",
            "https://example.invalid/archive",
            "0" * 64,
            str(target),
            hashlib.sha256(b"old").hexdigest(),
        )

        def change_executable(*_):
            target.write_bytes(b"changed")
            return staged

        with patch("agentbox.update.prepare_update_plan", return_value=reviewed), patch(
            "agentbox.update.operation_guard", return_value=nullcontext()
        ), patch("agentbox.update._download_archive"), patch(
            "agentbox.update._stage_executable", side_effect=change_executable
        ):
            with self.assertRaisesRegex(AgentBoxError, "being staged"):
                install_update(reviewed)

    def test_windows_update_marker_blocks_a_second_pending_update(self):
        target = self.root / "agentbox.exe"
        target.write_bytes(b"old")
        marker = _reserve_windows_update(target)
        self.addCleanup(marker.unlink, missing_ok=True)

        with self.assertRaisesRegex(AgentBoxError, "already pending"):
            _reserve_windows_update(target)

    def test_windows_helper_revalidates_and_persists_failures(self):
        target = self.root / "agentbox.exe"
        staged = self.root / ".agentbox.exe.update"
        marker = self.root / ".agentbox.exe.update-pending"
        for path in (target, staged, marker):
            path.write_bytes(b"data")
        with patch("agentbox.update.user_state_root", return_value=self.root), patch(
            "agentbox.update.shutil.which", return_value="powershell.exe"
        ), patch("agentbox.update.subprocess.Popen") as process, patch(
            "agentbox.update.subprocess.DETACHED_PROCESS", 1, create=True
        ), patch(
            "agentbox.update.subprocess.CREATE_NEW_PROCESS_GROUP", 2, create=True
        ), patch("agentbox.update.subprocess.CREATE_NO_WINDOW", 4, create=True):
            _schedule_windows_replacement(target, staged, marker, "a" * 64)

        script = Path(process.call_args.args[0][-1])
        text = script.read_text(encoding="utf-8")
        self.assertIn("Get-FileHash", text)
        self.assertIn("status = 'failed'", text)
        environment = process.call_args.kwargs["env"]
        self.assertEqual("a" * 64, environment["AGENTBOX_UPDATE_EXPECTED_SHA256"])

    def test_windows_helper_setup_failure_clears_the_pending_marker(self):
        target = self.root / "agentbox.exe"
        staged = self.root / ".agentbox.exe.update"
        marker = self.root / ".agentbox.exe.update-pending"
        state_root = self.root / "not-a-directory"
        for path in (target, staged, marker, state_root):
            path.write_bytes(b"data")

        with patch("agentbox.update.user_state_root", return_value=state_root):
            with self.assertRaisesRegex(AgentBoxError, "Cannot prepare"):
                _schedule_windows_replacement(target, staged, marker, "a" * 64)

        self.assertFalse(marker.exists())

    def test_staging_reads_only_the_exact_regular_executable_member(self):
        target = self.root / "agentbox"
        target.write_bytes(b"old executable")
        target.chmod(0o755)
        archive = self.root / "update.tar.gz"
        payload = b"new executable"
        with tarfile.open(archive, "w:gz") as output:
            member = tarfile.TarInfo("agentbox-1.2.0-macos-arm64/agentbox")
            member.size = len(payload)
            member.mode = 0o755
            output.addfile(member, io.BytesIO(payload))
            unrelated = tarfile.TarInfo("../../outside")
            unrelated.size = 4
            output.addfile(unrelated, io.BytesIO(b"evil"))
        plan = UpdatePlan(
            "SimaxLabs/AgentBox",
            "a" * 40,
            "b" * 40,
            "https://example.invalid/release",
            "macos-arm64",
            "agentbox-1.2.0-macos-arm64.tar.gz",
            "https://example.invalid/archive",
            "c" * 64,
            str(target),
            hashlib.sha256(target.read_bytes()).hexdigest(),
        )

        staged = _stage_executable(plan, archive)
        self.addCleanup(staged.unlink, missing_ok=True)

        self.assertEqual(payload, staged.read_bytes())
        self.assertFalse((self.root.parent / "outside").exists())

    def test_staging_rejects_an_executable_changed_after_review(self):
        target = self.root / "agentbox"
        target.write_bytes(b"changed")
        plan = UpdatePlan(
            "SimaxLabs/AgentBox",
            "a" * 40,
            "b" * 40,
            "https://example.invalid/release",
            "macos-arm64",
            "agentbox-1.2.0-macos-arm64.tar.gz",
            "https://example.invalid/archive",
            "c" * 64,
            str(target),
            hashlib.sha256(b"old").hexdigest(),
        )

        with self.assertRaisesRegex(AgentBoxError, "changed after"):
            _stage_executable(plan, self.root / "missing.tar.gz")

    def test_staging_uses_the_versioned_zip_archive_root(self):
        target = self.root / "agentbox.exe"
        target.write_bytes(b"old executable")
        archive = self.root / "agentbox-1.2.0-windows-x86_64.zip"
        payload = b"new executable"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("agentbox-1.2.0-windows-x86_64/agentbox.exe", payload)
            output.writestr("wrong-root/agentbox.exe", b"wrong")
        plan = UpdatePlan(
            "SimaxLabs/AgentBox",
            "a" * 40,
            "b" * 40,
            "https://example.invalid/release",
            "windows-x86_64",
            archive.name,
            "https://example.invalid/archive",
            "c" * 64,
            str(target),
            hashlib.sha256(target.read_bytes()).hexdigest(),
            "1.1.0",
            "1.2.0",
        )

        staged = _stage_executable(plan, archive)
        self.addCleanup(staged.unlink, missing_ok=True)
        self.assertEqual(payload, staged.read_bytes())

    def test_download_rejects_an_archive_with_the_wrong_checksum(self):
        class Response(io.BytesIO):
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.close()

        destination = self.root / "update.tar.gz"
        plan = UpdatePlan(
            "SimaxLabs/AgentBox",
            "a" * 40,
            "b" * 40,
            "https://example.invalid/release",
            "macos-arm64",
            destination.name,
            "https://example.invalid/archive",
            "0" * 64,
            str(self.root / "agentbox"),
            "1" * 64,
        )
        with patch("agentbox.update.urlopen", return_value=Response(b"not verified")):
            with self.assertRaisesRegex(AgentBoxError, "SHA-256"):
                _download_archive(plan, destination)

    def test_install_rejects_a_release_changed_after_review(self):
        reviewed = UpdatePlan(
            "SimaxLabs/AgentBox",
            "a" * 40,
            "b" * 40,
            "https://example.invalid/release",
            "macos-arm64",
            "agentbox-1.2.0-macos-arm64.tar.gz",
            "https://example.invalid/archive",
            "0" * 64,
            str(self.root / "agentbox"),
            "1" * 64,
        )
        changed = UpdatePlan(
            **{**reviewed.__dict__, "latest_commit": "c" * 40}
        )
        with patch("agentbox.update.prepare_update_plan", return_value=changed):
            with self.assertRaisesRegex(AgentBoxError, "changed; review"):
                install_update(reviewed)

    def test_install_revalidates_a_managed_channel(self):
        reviewed = UpdatePlan(
            "SimaxLabs/AgentBox",
            "a" * 40,
            "b" * 40,
            "https://example.invalid/release",
            "macos-arm64",
            "agentbox-1.2.0-macos-arm64.tar.gz",
            "https://example.invalid/archive",
            "0" * 64,
            str(self.root / "agentbox"),
            "1" * 64,
            "1.1.0",
            "1.2.0",
        )
        with patch.dict(
            os.environ, {"AGENTBOX_INSTALL_CHANNEL": "homebrew"}
        ), patch(
            "agentbox.update.operation_guard", return_value=nullcontext()
        ), patch(
            "agentbox.update.fetch_latest_release"
        ) as fetch:
            with self.assertRaisesRegex(AgentBoxError, "brew upgrade agentbox"):
                install_update(reviewed)
        fetch.assert_not_called()

    def test_python_install_update_command_gives_manual_guidance(self):
        status = UpdateStatus(
            "SimaxLabs/AgentBox",
            "a" * 40,
            "b" * 40,
            "https://example.invalid/release",
            True,
            False,
            "macos-arm64",
            current_version="1.1.0",
            latest_version="1.2.0",
            version_relation="newer",
        )
        output = io.StringIO()
        with patch("agentbox.cli.check_for_updates", return_value=status), redirect_stdout(output):
            result = cli.run_update()

        self.assertEqual(0, result)
        self.assertIn("Automatic updates are available only for standalone", output.getvalue())

    def test_cli_startup_announces_only_an_available_update(self):
        status = UpdateStatus(
            "SimaxLabs/AgentBox",
            "a" * 40,
            "b" * 40,
            "https://example.invalid/release",
            True,
            False,
            "macos-arm64",
            current_version="1.1.0",
            latest_version="1.2.0",
            version_relation="newer",
        )
        output = io.StringIO()
        with patch("agentbox.cli.check_for_updates", return_value=status), redirect_stderr(output):
            cli.announce_available_update()

        self.assertIn("run 'agentbox update'", output.getvalue())

    def test_cli_uses_managed_homebrew_and_scoop_commands(self):
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
                False,
                "macos-arm64",
                current_version="1.1.0",
                latest_version="1.2.0",
                version_relation="newer",
                relation="ahead",
                install_channel=channel,
                install_command=command,
            )
            output = io.StringIO()
            with patch(
                "agentbox.cli.check_for_updates", return_value=status
            ), patch("agentbox.cli.prepare_update_plan") as prepare, redirect_stdout(
                output
            ):
                self.assertEqual(0, cli.run_update())
            self.assertIn(command, output.getvalue())
            prepare.assert_not_called()

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
