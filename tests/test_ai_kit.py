import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "ai-kit"


class AiKitTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.paths = {
            "claude_skills": self.root / "home/claude/skills",
            "claude_commands": self.root / "home/claude/commands",
            "codex_current": self.root / "home/agents/skills",
            "codex_secondary": self.root / "home/codex/skills",
            "codex_prompts": self.root / "home/codex/prompts",
            "opencode_skills": self.root / "home/opencode/skills",
            "opencode_commands": self.root / "home/opencode/commands",
        }
        self.catalog_root = self.root / "catalog"
        self.catalog = self.catalog_root / "test-host"
        self.state = self.root / "state/state.json"
        self.safety = self.root / "state/backups"
        self.config = self.root / "ai-kit.json"
        self.environment = os.environ.copy()
        self.environment["XDG_DATA_HOME"] = str(self.root / "data")
        self.environment["HOME"] = str(self.root / "home")
        self.config.write_text(
            json.dumps(
                {
                    "version": 1,
                    "host": "test-host",
                    "storage": {"local": str(self.catalog_root)},
                    "state_file": str(self.state),
                    "safety_backups": str(self.safety),
                    "tools": {
                        "claude": self.tool_config("claude_skills", "claude_commands"),
                        "codex": {
                            "skills": {
                                "sources": [
                                    {"id": "current", "path": str(self.paths["codex_current"])},
                                    {"id": "secondary", "path": str(self.paths["codex_secondary"])},
                                ],
                                "target": str(self.paths["codex_current"]),
                            },
                            "commands": {
                                "sources": [
                                    {"id": "prompts", "path": str(self.paths["codex_prompts"])}
                                ],
                                "target": str(self.paths["codex_prompts"]),
                            },
                        },
                        "opencode": self.tool_config("opencode_skills", "opencode_commands"),
                    },
                }
            ),
            encoding="utf-8",
        )

    def tool_config(self, skills_key, commands_key):
        return {
            "skills": {
                "sources": [{"id": "skills", "path": str(self.paths[skills_key])}],
                "target": str(self.paths[skills_key]),
            },
            "commands": {
                "sources": [{"id": "commands", "path": str(self.paths[commands_key])}],
                "target": str(self.paths[commands_key]),
            },
        }

    def run_cli(self, *arguments, expected=0):
        result = subprocess.run(
            [sys.executable, str(CLI), "--config", str(self.config), *arguments],
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            check=False,
            env=self.environment,
        )
        self.assertEqual(
            expected,
            result.returncode,
            "stdout:\n{}\nstderr:\n{}".format(result.stdout, result.stderr),
        )
        return result

    def run_git(self, *arguments, expected=0):
        if shutil.which("git") is None:
            self.skipTest("git is unavailable")
        result = subprocess.run(
            ["git", *arguments],
            cwd=str(self.root),
            text=True,
            capture_output=True,
            check=False,
            env=self.environment,
        )
        self.assertEqual(
            expected,
            result.returncode,
            "stdout:\n{}\nstderr:\n{}".format(result.stdout, result.stderr),
        )
        return result

    def set_storage(self, storage):
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["storage"] = storage
        self.config.write_text(json.dumps(config), encoding="utf-8")

    def add_bare_repository(self):
        repository = self.root / "remote.git"
        self.run_git("init", "--bare", "--initial-branch=main", str(repository))
        return repository

    def test_repository_launcher_is_executable(self):
        self.assertTrue(os.access(CLI, os.X_OK))

    def add_skill(self, root_key, name, body="Follow the workflow."):
        directory = self.paths[root_key] / name
        directory.mkdir(parents=True, exist_ok=True)
        content = (
            "---\n"
            "name: {0}\n"
            "description: Use the {0} workflow.\n"
            "---\n\n"
            "{1}\n"
        ).format(name, body)
        (directory / "SKILL.md").write_text(content, encoding="utf-8")
        return directory, content

    def add_command(self, root_key, name="audit", body="Audit $ARGUMENTS"):
        path = self.paths[root_key] / (name + ".md")
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "---\n"
            "description: Audit the selected code\n"
            "agent: plan\n"
            "---\n\n"
            "{}\n"
        ).format(body)
        path.write_text(content, encoding="utf-8")
        return path, content

    def test_backup_preserves_skill_and_command_exactly(self):
        skill, skill_content = self.add_skill("codex_secondary", "jira-ticket")
        script = skill / "scripts/check.sh"
        script.parent.mkdir()
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)
        _, command_content = self.add_command("opencode_commands")

        self.run_cli("backup", "all")

        backed_skill = self.catalog / "codex/skills/jira-ticket"
        self.assertEqual(skill_content, (backed_skill / "SKILL.md").read_text(encoding="utf-8"))
        self.assertTrue((backed_skill / "scripts/check.sh").stat().st_mode & 0o100)
        self.assertEqual(
            command_content,
            (self.catalog / "opencode/commands/audit.md").read_text(encoding="utf-8"),
        )
        manifest = json.loads((self.catalog / "codex/manifest.json").read_text(encoding="utf-8"))
        self.assertEqual("secondary", manifest["artifacts"][0]["sources"][0]["id"])

    def test_converted_restore_does_not_duplicate_on_next_backup(self):
        self.add_skill("codex_secondary", "jira-ticket")
        self.add_command("opencode_commands")
        self.run_cli("backup", "all")

        self.run_cli("restore", "claude", "--all-tools")

        restored_command = self.paths["claude_skills"] / "audit/SKILL.md"
        converted = restored_command.read_text(encoding="utf-8")
        self.assertIn("name: audit", converted)
        self.assertIn('description: "Audit the selected code"', converted)
        self.assertNotIn("agent: plan", converted)
        self.assertIn("Dollar-prefixed", converted)
        self.assertIn("Audit $ARGUMENTS", converted)
        self.assertTrue((self.paths["claude_skills"] / "jira-ticket/SKILL.md").is_file())

        backup = self.run_cli("backup", "claude")
        self.assertIn("SKIP derived", backup.stdout)
        self.assertFalse((self.catalog / "claude/skills").exists())

        self.run_cli("restore", "claude", "--all-tools")

        restored_command.write_text(converted + "\nLocal change\n", encoding="utf-8")
        conflict = self.run_cli("backup", "claude", expected=2)
        self.assertIn("was restored by ai-kit and then modified", conflict.stderr)
        self.assertFalse((self.catalog / "claude/skills").exists())

    def test_as_backed_up_restores_original_content_and_location(self):
        command, original = self.add_command("opencode_commands")
        self.run_cli("backup", "opencode")
        command.unlink()

        self.run_cli("restore", "opencode", "--as-backed-up")

        self.assertEqual(original, command.read_text(encoding="utf-8"))
        self.assertFalse((self.paths["opencode_skills"] / "audit").exists())

    def test_content_deduplication_works_without_deployment_receipt(self):
        self.add_command("opencode_commands")
        self.run_cli("backup", "opencode")
        self.run_cli("restore", "claude", "--from", "opencode")
        self.state.unlink()

        self.run_cli("backup", "claude")
        self.assertFalse((self.catalog / "claude/skills").exists())
        result = self.run_cli("restore", "codex", "--all-tools")

        self.assertNotIn("divergent", result.stderr)
        self.assertTrue((self.paths["codex_current"] / "audit/SKILL.md").is_file())

    def test_divergent_names_stop_restore_and_from_resolves_it(self):
        self.add_command("opencode_commands", name="audit", body="Audit command behavior.")
        self.add_skill("codex_secondary", "audit", body="Different skill behavior.")
        self.run_cli("backup", "all")

        conflict = self.run_cli("restore", "claude", "--all-tools", expected=2)
        self.assertIn("divergent catalog entries", conflict.stderr)
        self.assertFalse((self.paths["claude_skills"] / "audit").exists())

        self.run_cli("restore", "claude", "--from", "opencode")
        restored = (self.paths["claude_skills"] / "audit/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Audit command behavior.", restored)

    def test_prune_keeps_artifacts_from_unavailable_source(self):
        self.add_skill("codex_secondary", "jira-ticket")
        self.run_cli("backup", "codex")
        shutil.rmtree(self.paths["codex_secondary"])
        self.paths["codex_current"].mkdir(parents=True)

        result = self.run_cli("backup", "codex", "--prune")

        self.assertIn("KEEP unavailable source", result.stdout)
        self.assertTrue((self.catalog / "codex/skills/jira-ticket/SKILL.md").is_file())
        manifest = json.loads((self.catalog / "codex/manifest.json").read_text(encoding="utf-8"))
        self.assertEqual("secondary", manifest["artifacts"][0]["sources"][0]["id"])

    def test_modified_derived_artifact_conflicts_even_if_origin_is_removed(self):
        self.add_command("opencode_commands")
        self.run_cli("backup", "opencode")
        self.run_cli("restore", "claude", "--from", "opencode")
        restored = self.paths["claude_skills"] / "audit/SKILL.md"
        restored.write_text(restored.read_text(encoding="utf-8") + "Changed\n", encoding="utf-8")
        (self.catalog / "opencode/commands/audit.md").unlink()

        result = self.run_cli("backup", "claude", expected=2)

        self.assertIn("was restored by ai-kit and then modified", result.stderr)

    def test_stale_receipt_preserves_old_deployed_content(self):
        command, _ = self.add_command("opencode_commands", body="Version one $ARGUMENTS")
        self.run_cli("backup", "opencode")
        self.run_cli("restore", "claude", "--from", "opencode")
        command.write_text(
            "---\ndescription: Audit the selected code\nagent: plan\n---\n\nVersion two $ARGUMENTS\n",
            encoding="utf-8",
        )
        self.run_cli("backup", "opencode")

        result = self.run_cli("backup", "claude")

        self.assertIn("BACKUP claude skills/audit", result.stdout)
        preserved = (self.catalog / "claude/skills/audit/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Version one", preserved)

    def test_include_derived_clears_receipt_after_backup(self):
        self.add_command("opencode_commands")
        self.run_cli("backup", "opencode")
        self.run_cli("restore", "claude", "--from", "opencode")
        restored = self.paths["claude_skills"] / "audit/SKILL.md"
        restored.write_text(restored.read_text(encoding="utf-8") + "Changed\n", encoding="utf-8")

        self.run_cli("backup", "claude", "--include-derived")
        second = self.run_cli("backup", "claude")

        self.assertIn("UNCHANGED claude skills/audit", second.stdout)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertNotIn(str(restored.parent.absolute()), state["deployments"])

    def test_manifest_traversal_is_rejected_before_prune(self):
        self.paths["opencode_commands"].mkdir(parents=True)
        victim = self.root / "victim.md"
        victim.write_text("keep", encoding="utf-8")
        manifest = self.catalog / "opencode/manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            json.dumps(
                {
                    "version": 1,
                    "artifacts": [
                        {
                            "kind": "command",
                            "name": "victim",
                            "path": "../../victim.md",
                            "sources": [],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        result = self.run_cli("backup", "opencode", "--prune", expected=2)

        self.assertIn("Unsafe relative path", result.stderr)
        self.assertEqual("keep", victim.read_text(encoding="utf-8"))

    def test_symlinked_source_artifact_is_rejected(self):
        external = self.root / "external.md"
        external.write_text("secret", encoding="utf-8")
        command = self.paths["opencode_commands"] / "linked.md"
        command.parent.mkdir(parents=True)
        command.symlink_to(external)

        result = self.run_cli("backup", "opencode", expected=2)

        self.assertIn("Symlinked artifacts are not supported", result.stderr)
        self.assertFalse(self.catalog.exists())

    def test_symlinked_catalog_subdirectory_is_rejected(self):
        external = self.root / "external-commands"
        external.mkdir()
        (external / "outside.md").write_text("Outside", encoding="utf-8")
        tool_catalog = self.catalog / "opencode"
        tool_catalog.mkdir(parents=True)
        (tool_catalog / "commands").symlink_to(external)

        result = self.run_cli("restore", "claude", "--all-tools", expected=2)

        self.assertIn("Symlinked artifacts are not supported", result.stderr)
        self.assertFalse(self.paths["claude_skills"].exists())

    def test_unavailable_duplicate_source_blocks_payload_replacement(self):
        _, original = self.add_skill("codex_current", "jira-ticket", body="Original")
        secondary = self.paths["codex_secondary"] / "jira-ticket"
        secondary.parent.mkdir(parents=True)
        shutil.copytree(self.paths["codex_current"] / "jira-ticket", secondary)
        self.run_cli("backup", "codex")
        shutil.rmtree(self.paths["codex_secondary"])
        current_skill = self.paths["codex_current"] / "jira-ticket/SKILL.md"
        current_skill.write_text(original.replace("Original", "Changed"), encoding="utf-8")

        result = self.run_cli("backup", "codex", expected=2)

        self.assertIn("recorded source(s) secondary are unavailable", result.stderr)
        backed = (self.catalog / "codex/skills/jira-ticket/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Original", backed)

    def test_restore_all_preflights_every_target_before_writing(self):
        self.add_command("opencode_commands")
        self.run_cli("backup", "opencode")
        external = self.root / "external-skill"
        external.mkdir()
        (external / "SKILL.md").write_text("external", encoding="utf-8")
        codex_target = self.paths["codex_current"] / "audit"
        codex_target.parent.mkdir(parents=True)
        codex_target.symlink_to(external)

        result = self.run_cli("restore", "all", "--all-tools", expected=2)

        self.assertIn("Refusing to replace symlink", result.stderr)
        self.assertFalse((self.paths["claude_skills"] / "audit").exists())

    def test_restore_defaults_to_matching_tool_and_all_tools_is_explicit(self):
        self.add_command("opencode_commands")
        self.add_skill("codex_secondary", "jira-ticket")
        self.run_cli("backup", "all")

        self.run_cli("restore", "opencode")

        self.assertTrue((self.paths["opencode_skills"] / "audit/SKILL.md").is_file())
        self.assertFalse((self.paths["opencode_skills"] / "jira-ticket").exists())

        self.run_cli("restore", "opencode", "--all-tools")

        self.assertTrue((self.paths["opencode_skills"] / "jira-ticket/SKILL.md").is_file())

    def test_restore_defaults_to_current_host_and_all_hosts_is_explicit(self):
        first, _ = self.add_command("opencode_commands", name="current-only")
        self.run_cli("backup", "opencode")
        first.unlink()
        self.add_command("opencode_commands", name="other-only")
        self.run_cli("--host", "other-host", "backup", "opencode")

        self.run_cli("restore", "opencode")

        self.assertTrue((self.paths["opencode_skills"] / "current-only/SKILL.md").is_file())
        self.assertFalse((self.paths["opencode_skills"] / "other-only").exists())

        self.run_cli("restore", "opencode", "--all-hosts")

        self.assertTrue((self.paths["opencode_skills"] / "other-only/SKILL.md").is_file())
        self.assertTrue((self.catalog_root / "other-host/opencode/manifest.json").is_file())

    def test_exact_restore_all_hosts_rejects_divergent_destination(self):
        command, _ = self.add_command("opencode_commands", body="Current host")
        self.run_cli("backup", "opencode")
        command.write_text(
            "---\ndescription: Other host\n---\n\nOther host\n", encoding="utf-8"
        )
        self.run_cli("--host", "other-host", "backup", "opencode")
        command.unlink()

        result = self.run_cli(
            "restore", "opencode", "--as-backed-up", "--all-hosts", expected=2
        )

        self.assertIn("different catalog selections both target", result.stderr)
        self.assertFalse(command.exists())

    def test_non_directory_source_cannot_prune_catalog(self):
        self.add_command("opencode_commands")
        self.run_cli("backup", "opencode")
        shutil.rmtree(self.paths["opencode_commands"])
        self.paths["opencode_commands"].write_text("not a directory", encoding="utf-8")

        result = self.run_cli("backup", "opencode", "--prune", expected=2)

        self.assertIn("Configured source is not a regular directory", result.stderr)
        self.assertTrue((self.catalog / "opencode/commands/audit.md").is_file())

    def test_missing_receipt_cannot_overwrite_unrelated_own_backup(self):
        self.add_skill("claude_skills", "audit", body="Claude original")
        self.add_command("opencode_commands", body="OpenCode restored")
        self.run_cli("backup", "claude")
        self.run_cli("backup", "opencode")
        self.run_cli("restore", "claude", "--from", "opencode")
        self.state.unlink()

        result = self.run_cli("backup", "claude", expected=2)

        self.assertIn("restore lineage is missing", result.stderr)
        backed = (self.catalog / "claude/skills/audit/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Claude original", backed)

    def test_symlinked_catalog_root_is_rejected(self):
        external = self.root / "external-catalog"
        external.mkdir()
        self.catalog_root.symlink_to(external)
        self.add_command("opencode_commands")

        result = self.run_cli("backup", "opencode", expected=2)

        self.assertIn("Symlinked catalog roots are not supported", result.stderr)
        self.assertEqual([], list(external.iterdir()))

    def test_symlinked_restore_target_root_is_rejected(self):
        self.add_command("opencode_commands")
        self.run_cli("backup", "opencode")
        external = self.root / "external-skills"
        external.mkdir()
        self.paths["claude_skills"].parent.mkdir(parents=True)
        self.paths["claude_skills"].symlink_to(external)

        result = self.run_cli("restore", "claude", "--from", "opencode", expected=2)

        self.assertIn("Symlinked restore target roots are not supported", result.stderr)
        self.assertEqual([], list(external.iterdir()))

    def test_symlinked_exact_restore_root_is_rejected(self):
        command, _ = self.add_command("opencode_commands")
        self.run_cli("backup", "opencode")
        command.unlink()
        shutil.rmtree(self.paths["opencode_commands"])
        external = self.root / "external-commands"
        external.mkdir()
        self.paths["opencode_commands"].symlink_to(external)

        result = self.run_cli("restore", "opencode", "--as-backed-up", expected=2)

        self.assertIn("Symlinked exact restore roots are not supported", result.stderr)
        self.assertEqual([], list(external.iterdir()))

    def test_identical_artifacts_are_kept_in_each_host_namespace(self):
        self.add_skill("opencode_skills", "shared-skill")
        self.run_cli("--host", "other-host", "backup", "opencode")

        self.run_cli("backup", "opencode")

        self.assertTrue(
            (self.catalog_root / "other-host/opencode/skills/shared-skill/SKILL.md").is_file()
        )
        self.assertTrue((self.catalog / "opencode/skills/shared-skill/SKILL.md").is_file())

    def test_overlapping_portable_targets_are_rejected_before_writing(self):
        self.add_skill("claude_skills", "shared", body="Claude")
        self.add_skill("codex_secondary", "shared", body="Codex")
        self.run_cli("backup", "all")
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["tools"]["claude"]["skills"]["target"] = str(self.paths["codex_current"])
        self.config.write_text(json.dumps(config), encoding="utf-8")

        result = self.run_cli("restore", "all", expected=2)

        self.assertIn("both map to", result.stderr)
        self.assertFalse((self.paths["codex_current"] / "shared").exists())

    def test_dry_run_does_not_write(self):
        self.add_command("opencode_commands")

        self.run_cli("backup", "opencode", "--dry-run")

        self.assertFalse(self.catalog.exists())

    def test_explicit_local_storage_uses_configured_folder(self):
        selected = self.root / "selected-local-catalog"
        self.set_storage({"local": str(selected)})
        self.add_command("opencode_commands")

        self.run_cli("backup", "opencode")

        self.assertTrue((selected / "test-host/opencode/commands/audit.md").is_file())
        self.assertFalse(self.catalog.exists())

    def test_git_storage_manages_clone_commit_and_push(self):
        repository = self.add_bare_repository()
        url = repository.as_uri()
        self.set_storage({"git": url})
        self.add_command("opencode_commands")

        dry_run = self.run_cli("backup", "opencode", "--dry-run")

        self.assertIn("GIT READY main@empty", dry_run.stdout)
        self.run_git(
            "--git-dir",
            str(repository),
            "rev-parse",
            "--verify",
            "refs/heads/main",
            expected=128,
        )

        result = self.run_cli("backup", "opencode")

        self.assertIn("GIT PUSH main", result.stdout)
        backed_up = self.run_git(
            "--git-dir",
            str(repository),
            "show",
            "main:catalog/test-host/opencode/commands/audit.md",
        )
        self.assertIn("Audit $ARGUMENTS", backed_up.stdout)
        checkout_id = hashlib.sha256(url.encode("utf-8")).hexdigest()
        self.assertTrue((self.root / "data/ai-kit/repositories" / checkout_id / ".git").is_dir())

    def test_git_push_failure_rolls_back_managed_catalog(self):
        repository = self.add_bare_repository()
        self.set_storage({"git": repository.as_uri()})
        command, original = self.add_command("opencode_commands")
        self.run_cli("backup", "opencode")
        command.write_text(original + "Changed\n", encoding="utf-8")
        hook = repository / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)

        failed = self.run_cli("backup", "opencode", expected=2)

        self.assertIn("pre-receive hook declined", failed.stderr)
        remote_content = self.run_git(
            "--git-dir",
            str(repository),
            "show",
            "main:catalog/test-host/opencode/commands/audit.md",
        ).stdout
        self.assertEqual(original, remote_content)
        status = self.run_cli("status", "opencode")
        self.assertIn("DIFFERENT opencode commands/audit.md", status.stdout)

    def test_git_storage_forces_ignored_catalog_files(self):
        repository = self.add_bare_repository()
        seed = self.root / "seed"
        self.run_git("clone", repository.as_uri(), str(seed))
        (seed / ".gitignore").write_text("*.md\n", encoding="utf-8")
        self.run_git("-C", str(seed), "add", ".gitignore")
        self.run_git(
            "-C",
            str(seed),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "Add ignore rule",
        )
        self.run_git("-C", str(seed), "push", "origin", "main")
        self.set_storage({"git": repository.as_uri()})
        self.add_command("opencode_commands")

        self.run_cli("backup", "opencode")

        payload = self.run_git(
            "--git-dir",
            str(repository),
            "show",
            "main:catalog/test-host/opencode/commands/audit.md",
        )
        self.assertIn("Audit $ARGUMENTS", payload.stdout)

    def test_git_status_and_dry_run_do_not_push_pending_commit(self):
        repository = self.add_bare_repository()
        url = repository.as_uri()
        self.set_storage({"git": url})
        self.add_command("opencode_commands")
        self.run_cli("backup", "opencode")
        checkout_id = hashlib.sha256(url.encode("utf-8")).hexdigest()
        checkout = self.root / "data/ai-kit/repositories" / checkout_id
        self.run_git(
            "-C",
            str(checkout),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "Pending commit",
        )
        remote_before = self.run_git(
            "--git-dir", str(repository), "rev-parse", "refs/heads/main"
        ).stdout.strip()

        status = self.run_cli("status", "opencode")
        dry_run = self.run_cli("backup", "opencode", "--dry-run")

        self.assertIn("GIT PENDING COMMIT", status.stdout)
        self.assertIn("GIT PENDING COMMIT", dry_run.stdout)
        remote_after = self.run_git(
            "--git-dir", str(repository), "rev-parse", "refs/heads/main"
        ).stdout.strip()
        self.assertEqual(remote_before, remote_after)

        self.run_cli("backup", "opencode")
        pushed = self.run_git(
            "--git-dir", str(repository), "rev-parse", "refs/heads/main"
        ).stdout.strip()
        self.assertNotEqual(remote_before, pushed)

    def test_git_storage_rejects_repository_attributes(self):
        repository = self.add_bare_repository()
        seed = self.root / "attributes-seed"
        self.run_git("clone", repository.as_uri(), str(seed))
        (seed / ".gitattributes").write_text("catalog/** text\n", encoding="utf-8")
        self.run_git("-C", str(seed), "add", ".gitattributes")
        self.run_git(
            "-C",
            str(seed),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "Add attributes",
        )
        self.run_git("-C", str(seed), "push", "origin", "main")
        self.set_storage({"git": repository.as_uri()})

        result = self.run_cli("status", expected=2)

        self.assertIn("must not define .gitattributes", result.stderr)

    def test_git_storage_rejects_embedded_repository_content(self):
        repository = self.add_bare_repository()
        self.set_storage({"git": repository.as_uri()})
        skill, _ = self.add_skill("opencode_skills", "nested-repository")
        nested = skill / ".git"
        nested.mkdir()
        (nested / "config").write_text("embedded", encoding="utf-8")

        result = self.run_cli("backup", "opencode", expected=2)

        self.assertIn("Git storage does not support artifact content", result.stderr)

    def test_local_storage_accepts_git_named_supporting_content(self):
        skill, _ = self.add_skill("opencode_skills", "local-repository-notes")
        nested = skill / ".git"
        nested.mkdir()
        (nested / "config").write_text("supporting notes", encoding="utf-8")

        self.run_cli("backup", "opencode")

        self.assertEqual(
            "supporting notes",
            (
                self.catalog
                / "opencode/skills/local-repository-notes/.git/config"
            ).read_text(encoding="utf-8"),
        )

    def test_dual_storage_initializes_git_and_keeps_local_copy(self):
        self.add_command("opencode_commands")
        self.run_cli("backup", "opencode")
        repository = self.add_bare_repository()
        self.set_storage({"local": str(self.catalog_root), "git": repository.as_uri()})

        result = self.run_cli("backup", "opencode")

        self.assertIn("INITIALIZE GIT STORAGE FROM LOCAL CATALOG", result.stdout)
        self.assertTrue((self.catalog / "opencode/commands/audit.md").is_file())
        self.run_git(
            "--git-dir",
            str(repository),
            "show",
            "main:catalog/test-host/opencode/commands/audit.md",
        )

        command = self.paths["opencode_commands"] / "audit.md"
        command.write_text(command.read_text(encoding="utf-8") + "Changed\n", encoding="utf-8")
        self.run_cli("backup", "opencode")
        self.assertIn("Changed", (self.catalog / "opencode/commands/audit.md").read_text())

    def test_dual_storage_stops_when_copies_differ(self):
        self.add_command("opencode_commands")
        self.run_cli("backup", "opencode")
        repository = self.add_bare_repository()
        self.set_storage({"local": str(self.catalog_root), "git": repository.as_uri()})
        self.run_cli("backup", "opencode")
        local_payload = self.catalog / "opencode/commands/audit.md"
        local_payload.write_text("Divergent local catalog\n", encoding="utf-8")

        result = self.run_cli("status", expected=2)

        self.assertIn("Local and Git catalogs differ", result.stderr)

    def test_dual_storage_rolls_back_local_copy_when_push_is_rejected(self):
        command, original = self.add_command("opencode_commands")
        self.run_cli("backup", "opencode")
        repository = self.add_bare_repository()
        self.set_storage({"local": str(self.catalog_root), "git": repository.as_uri()})
        self.run_cli("backup", "opencode")
        hook = repository / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        command.write_text(original + "Changed\n", encoding="utf-8")

        self.run_cli("backup", "opencode", expected=2)

        self.assertEqual(
            original,
            (self.catalog / "opencode/commands/audit.md").read_text(encoding="utf-8"),
        )

    def test_top_level_catalog_setting_is_rejected(self):
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config.pop("storage")
        config["catalog"] = str(self.root / "other")
        self.config.write_text(json.dumps(config), encoding="utf-8")

        result = self.run_cli("status", expected=2)

        self.assertIn("Use storage.local", result.stderr)

    def test_storage_command_persists_selection_after_dry_run(self):
        selected = self.root / "persistent-local"
        self.config.chmod(0o600)

        preview = self.run_cli("storage", "--local", str(selected), "--dry-run")

        self.assertIn("UPDATE STORAGE CONFIGURATION", preview.stdout)
        unchanged = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual({"local": str(self.catalog_root)}, unchanged["storage"])

        self.run_cli("storage", "--local", str(selected))
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual({"local": str(selected)}, saved["storage"])
        self.assertNotIn("catalog", saved)
        self.assertEqual(0o600, self.config.stat().st_mode & 0o777)

        self.add_command("opencode_commands")
        self.run_cli("backup", "opencode")
        self.assertTrue((selected / "test-host/opencode/commands/audit.md").is_file())

    def test_git_storage_rejects_urls_with_embedded_tokens(self):
        self.set_storage({"git": "https://example.invalid/repo.git?token=secret"})

        result = self.run_cli("status", expected=2)

        self.assertIn("must not contain embedded credentials", result.stderr)
        self.assertNotIn("secret", result.stderr)

        self.set_storage({"git": "ssh://user:secret@example.invalid/repo.git"})
        result = self.run_cli("status", expected=2)
        self.assertIn("must not contain embedded credentials", result.stderr)
        self.assertNotIn("secret", result.stderr)


if __name__ == "__main__":
    unittest.main()
