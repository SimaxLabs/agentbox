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
        self.root = Path(self.temporary.name)
        self.paths = {
            "claude_skills": self.root / "home/claude/skills",
            "claude_commands": self.root / "home/claude/commands",
            "codex_current": self.root / "home/agents/skills",
            "codex_legacy": self.root / "home/codex/skills",
            "codex_prompts": self.root / "home/codex/prompts",
            "opencode_skills": self.root / "home/opencode/skills",
            "opencode_commands": self.root / "home/opencode/commands",
        }
        self.catalog_root = self.root / "catalog"
        self.catalog = self.catalog_root / "test-host"
        self.state = self.root / "state/state.json"
        self.safety = self.root / "state/backups"
        self.config = self.root / "ai-kit.json"
        self.config.write_text(
            json.dumps(
                {
                    "version": 1,
                    "host": "test-host",
                    "catalog": str(self.catalog_root),
                    "state_file": str(self.state),
                    "safety_backups": str(self.safety),
                    "tools": {
                        "claude": self.tool_config("claude_skills", "claude_commands"),
                        "codex": {
                            "skills": {
                                "sources": [
                                    {"id": "current", "path": str(self.paths["codex_current"])},
                                    {"id": "legacy", "path": str(self.paths["codex_legacy"])},
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

    def tearDown(self):
        self.temporary.cleanup()

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
        )
        self.assertEqual(
            expected,
            result.returncode,
            "stdout:\n{}\nstderr:\n{}".format(result.stdout, result.stderr),
        )
        return result

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

    def test_backup_preserves_legacy_skill_and_command_exactly(self):
        skill, skill_content = self.add_skill("codex_legacy", "jira-ticket")
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
        self.assertEqual("legacy", manifest["artifacts"][0]["sources"][0]["id"])

    def test_converted_restore_does_not_duplicate_on_next_backup(self):
        self.add_skill("codex_legacy", "jira-ticket")
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
        self.add_skill("codex_legacy", "audit", body="Different skill behavior.")
        self.run_cli("backup", "all")

        conflict = self.run_cli("restore", "claude", "--all-tools", expected=2)
        self.assertIn("divergent catalog entries", conflict.stderr)
        self.assertFalse((self.paths["claude_skills"] / "audit").exists())

        self.run_cli("restore", "claude", "--from", "opencode")
        restored = (self.paths["claude_skills"] / "audit/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Audit command behavior.", restored)

    def test_prune_keeps_artifacts_from_unavailable_legacy_source(self):
        self.add_skill("codex_legacy", "jira-ticket")
        self.run_cli("backup", "codex")
        shutil.rmtree(self.paths["codex_legacy"])
        self.paths["codex_current"].mkdir(parents=True)

        result = self.run_cli("backup", "codex", "--prune")

        self.assertIn("KEEP unavailable source", result.stdout)
        self.assertTrue((self.catalog / "codex/skills/jira-ticket/SKILL.md").is_file())
        manifest = json.loads((self.catalog / "codex/manifest.json").read_text(encoding="utf-8"))
        self.assertEqual("legacy", manifest["artifacts"][0]["sources"][0]["id"])

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
        legacy = self.paths["codex_legacy"] / "jira-ticket"
        legacy.parent.mkdir(parents=True)
        shutil.copytree(self.paths["codex_current"] / "jira-ticket", legacy)
        self.run_cli("backup", "codex")
        shutil.rmtree(self.paths["codex_legacy"])
        current_skill = self.paths["codex_current"] / "jira-ticket/SKILL.md"
        current_skill.write_text(original.replace("Original", "Changed"), encoding="utf-8")

        result = self.run_cli("backup", "codex", expected=2)

        self.assertIn("recorded source(s) legacy are unavailable", result.stderr)
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
        self.add_skill("codex_legacy", "jira-ticket")
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
        self.add_skill("codex_legacy", "shared", body="Codex")
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


if __name__ == "__main__":
    unittest.main()
