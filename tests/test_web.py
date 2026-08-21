import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentbox.core import OperationRequest, list_catalog_revisions, run_operation

try:
    import httpx

    from agentbox.web import create_app
    from agentbox.update import UpdateStatus
except ModuleNotFoundError:
    httpx = None
    create_app = None


@unittest.skipIf(httpx is None, "web test dependencies are not installed")
class AgentBoxWebTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        environment = patch.dict(
            os.environ,
            {
                "HOME": str(self.root / "home"),
                "USERPROFILE": str(self.root / "home"),
                "APPDATA": str(self.root / "appdata"),
                "LOCALAPPDATA": str(self.root / "local-appdata"),
                "XDG_CONFIG_HOME": str(self.root / "home/.config"),
                "XDG_DATA_HOME": str(self.root / "data"),
                "XDG_STATE_HOME": str(self.root / "state-root"),
                "AGENTBOX_CONFIG": "",
                "AGENTBOX_HOST": "",
                "AGENTBOX_NO_UPDATE_CHECK": "1",
                "AGENTBOX_INSTALL_CHANNEL": "",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.skills = self.root / "home/.config/opencode/skills"
        self.commands = self.root / "home/.config/opencode/commands"
        self.catalog = self.root / "catalog"
        self.config = self.root / "agentbox.json"
        self.config.write_text(
            json.dumps(
                {
                    "version": 2,
                    "host": "test-host",
                    "storage": {"local": str(self.catalog)},
                    "state_file": str(self.root / "state/state.json"),
                    "safety_backups": str(self.root / "state/backups"),
                    "providers": {
                        "opencode": {
                            "enabled": True,
                            "resources": {"skills": True, "commands": True},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.app = create_app(self.config)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://testserver"
        )
        self.addAsyncCleanup(self.client.aclose)
        self.csrf = self.app.state.runtime.csrf_token

    def add_command(self):
        self.commands.mkdir(parents=True)
        command = self.commands / "audit.md"
        command.write_text("---\ndescription: Audit code\n---\n\nAudit $ARGUMENTS\n", encoding="utf-8")
        return command

    async def preview_backup(self):
        return await self.client.post(
            "/operations/preview",
            data={
                "csrf_token": self.csrf,
                "action": "backup",
                "host": "test-host",
                "tool": "opencode",
            },
        )

    async def test_dashboard_renders_local_status_and_security_headers(self):
        self.add_command()

        response = await self.client.get("/")

        self.assertEqual(200, response.status_code)
        self.assertIn("AgentBox Workbench", response.text)
        self.assertIn("UNBACKED opencode commands/audit.md", response.text)
        self.assertIn("default-src 'self'", response.headers["content-security-policy"])
        self.assertEqual("no-store", response.headers["cache-control"])

    async def test_brand_assets_are_served_locally(self):
        page = await self.client.get("/")
        logo = await self.client.get("/static/logo.png")
        manifest = await self.client.get("/static/site.webmanifest")
        script = await self.client.get("/static/app.js")

        self.assertIn('href="/static/logo.png"', page.text)
        self.assertEqual(200, logo.status_code)
        self.assertEqual("image/png", logo.headers["content-type"])
        self.assertTrue(logo.content.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(200, manifest.status_code)
        self.assertEqual("/static/logo.png", manifest.json()["icons"][0]["src"])
        self.assertIn("htmx:beforeSwap", script.text)
        self.assertIn("event.detail.shouldSwap = true", script.text)

    async def test_preview_does_not_write_and_confirmed_job_streams_events(self):
        self.add_command()

        preview = await self.preview_backup()

        self.assertEqual(200, preview.status_code)
        self.assertIn("BACKUP opencode commands/audit.md", preview.text)
        self.assertFalse(self.catalog.exists())
        token = re.search(r'name="preview_token" value="([^"]+)"', preview.text).group(1)

        execute = await self.client.post(
            "/operations/execute",
            data={"csrf_token": self.csrf, "preview_token": token},
        )
        self.assertEqual(200, execute.status_code)
        job_id = re.search(r'data-job-id="([^"]+)"', execute.text).group(1)
        events = await self.client.get(
            "/operations/{}/events".format(job_id), params={"token": self.csrf}
        )

        self.assertEqual(200, events.status_code)
        self.assertIn('"kind": "backup"', events.text)
        self.assertIn('"kind": "complete"', events.text)
        self.assertTrue((self.catalog / "test-host/opencode/commands/audit.md").is_file())

        reused = await self.client.post(
            "/operations/execute",
            data={"csrf_token": self.csrf, "preview_token": token},
        )
        self.assertEqual(409, reused.status_code)
        self.assertIn("already used", reused.text)

    async def test_operation_posts_require_csrf_token(self):
        response = await self.client.post(
            "/operations/preview",
            data={"action": "backup", "host": "test-host", "tool": "opencode"},
        )

        self.assertEqual(403, response.status_code)
        self.assertFalse(self.catalog.exists())

    async def test_catalog_history_is_visible_and_uses_reviewed_restore(self):
        command = self.add_command()
        first_content = command.read_text(encoding="utf-8")
        run_operation(
            self.config,
            OperationRequest("backup", tool="opencode", host="test-host"),
        )
        first_revision = list_catalog_revisions(self.config)[0]
        command.write_text(first_content + "Second version\n", encoding="utf-8")
        run_operation(
            self.config,
            OperationRequest("backup", tool="opencode", host="test-host"),
        )

        page = await self.client.get("/")
        detail = await self.client.get(
            "/catalog/revisions/{}".format(first_revision.revision_id),
            params={"host": "test-host"},
        )

        self.assertIn("Revision history", page.text)
        self.assertIn(first_revision.revision_id, page.text)
        self.assertIn("audit", detail.text)

        preview = await self.client.post(
            "/operations/preview",
            data={
                "csrf_token": self.csrf,
                "action": "restore",
                "host": "test-host",
                "tool": "opencode",
                "restore_mode": "exact",
                "source_mode": "matching",
                "catalog_revision": first_revision.revision_id,
            },
        )
        self.assertEqual(200, preview.status_code)
        self.assertIn("USING CATALOG REVISION", preview.text)
        self.assertIn("current catalog will not be rewound", preview.text)
        self.assertIn("Second version", command.read_text(encoding="utf-8"))

        token = re.search(r'name="preview_token" value="([^"]+)"', preview.text).group(1)
        execute = await self.client.post(
            "/operations/execute",
            data={"csrf_token": self.csrf, "preview_token": token},
        )
        job_id = re.search(r'data-job-id="([^"]+)"', execute.text).group(1)
        events = await self.client.get(
            "/operations/{}/events".format(job_id), params={"token": self.csrf}
        )

        self.assertIn('"kind": "complete"', events.text)
        self.assertEqual(first_content, command.read_text(encoding="utf-8"))
        current_catalog = self.catalog / "test-host/opencode/commands/audit.md"
        self.assertIn("Second version", current_catalog.read_text(encoding="utf-8"))

    async def test_update_status_links_standalone_builds_to_github_release(self):
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
        with patch("agentbox.web.check_for_updates", return_value=status):
            response = await self.client.get("/updates/status")

        self.assertEqual(200, response.status_code)
        self.assertIn("Update available", response.text)
        self.assertIn("v1.2.0", response.text)
        self.assertIn("bbbbbbbbbbbb", response.text)
        self.assertIn("Manual download", response.text)
        self.assertIn("Open GitHub release", response.text)
        self.assertIn(release_url, response.text)
        self.assertNotIn("Review update", response.text)
        self.assertNotIn("/updates/preview", response.text)

    async def test_update_status_renders_managed_commands_without_review_form(self):
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
            with self.subTest(channel=channel), patch(
                "agentbox.web.check_for_updates", return_value=status
            ):
                response = await self.client.get("/updates/status")

            self.assertEqual(200, response.status_code)
            self.assertIn(command, response.text)
            self.assertNotIn("Review update", response.text)
            self.assertNotIn('action="/updates/preview"', response.text)

    async def test_update_install_routes_do_not_exist(self):
        for route in ("/updates/preview", "/updates/execute"):
            with self.subTest(route=route):
                response = await self.client.post(
                    route, data={"csrf_token": self.csrf}
                )
                self.assertEqual(404, response.status_code)

    async def test_confirmation_stops_if_filesystem_changed_after_preview(self):
        command = self.add_command()
        preview = await self.preview_backup()
        token = re.search(r'name="preview_token" value="([^"]+)"', preview.text).group(1)
        command.write_text("Changed after preview\n", encoding="utf-8")

        execute = await self.client.post(
            "/operations/execute",
            data={"csrf_token": self.csrf, "preview_token": token},
        )
        job_id = re.search(r'data-job-id="([^"]+)"', execute.text).group(1)
        events = await self.client.get(
            "/operations/{}/events".format(job_id), params={"token": self.csrf}
        )

        self.assertIn('"kind": "error"', events.text)
        self.assertIn("filesystem changed", events.text)
        self.assertFalse(self.catalog.exists())

    async def test_storage_selection_is_previewed_and_persisted(self):
        selected = self.root / "persistent-catalog"
        preview = await self.client.post(
            "/operations/preview",
            data={
                "csrf_token": self.csrf,
                "action": "storage",
                "local_enabled": "on",
                "storage_local": str(selected),
            },
        )

        self.assertEqual(200, preview.status_code)
        self.assertIn("UPDATE STORAGE CONFIGURATION", preview.text)
        self.assertEqual(
            {"local": str(self.catalog)},
            json.loads(self.config.read_text(encoding="utf-8"))["storage"],
        )
        token = re.search(r'name="preview_token" value="([^"]+)"', preview.text).group(1)

        execute = await self.client.post(
            "/operations/execute",
            data={"csrf_token": self.csrf, "preview_token": token},
        )
        self.assertIn('data-reload-page="true"', execute.text)
        job_id = re.search(r'data-job-id="([^"]+)"', execute.text).group(1)
        events = await self.client.get(
            "/operations/{}/events".format(job_id), params={"token": self.csrf}
        )

        self.assertIn('"kind": "complete"', events.text)
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual({"local": str(selected)}, saved["storage"])
        self.assertNotIn("catalog", saved)

    async def test_first_run_onboarding_is_previewed_and_persisted(self):
        self.config.unlink()
        app = create_app(self.config)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        csrf = app.state.runtime.csrf_token

        page = await client.get("/")

        self.assertEqual(200, page.status_code)
        self.assertIn("Map your AI", page.text)
        self.assertIn("Managed providers", page.text)
        self.assertFalse(self.config.exists())

        preview = await client.post(
            "/operations/preview",
            data={
                "csrf_token": csrf,
                "action": "providers",
                "provider_resource": ["opencode.skills", "opencode.commands"],
                "local_enabled": "on",
                "storage_local": str(self.catalog),
            },
        )

        self.assertEqual(200, preview.status_code)
        self.assertIn("PROVIDER OpenCode", preview.text)
        self.assertFalse(self.config.exists())
        token = re.search(r'name="preview_token" value="([^"]+)"', preview.text).group(1)
        execute = await client.post(
            "/operations/execute",
            data={"csrf_token": csrf, "preview_token": token},
        )
        self.assertIn('data-reload-page="true"', execute.text)
        job_id = re.search(r'data-job-id="([^"]+)"', execute.text).group(1)
        events = await client.get(
            "/operations/{}/events".format(job_id), params={"token": csrf}
        )

        self.assertIn('"kind": "complete"', events.text)
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(2, saved["version"])
        self.assertTrue(saved["providers"]["opencode"]["resources"]["skills"])
        self.assertFalse(saved["providers"]["claude"]["enabled"])
        self.assertEqual(0o600, self.config.stat().st_mode & 0o777)

        dashboard = await client.get("/")
        self.assertIn("Provider overview", dashboard.text)

    async def test_onboarding_stops_if_config_parent_is_redirected(self):
        config = self.root / "missing-settings/config.json"
        app = create_app(config)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        csrf = app.state.runtime.csrf_token
        preview = await client.post(
            "/operations/preview",
            data={
                "csrf_token": csrf,
                "action": "providers",
                "provider_resource": ["opencode.skills"],
                "local_enabled": "on",
                "storage_local": str(self.catalog),
            },
        )
        token = re.search(r'name="preview_token" value="([^"]+)"', preview.text).group(1)
        redirected = self.root / "redirected-settings"
        redirected.mkdir()
        config.parent.symlink_to(redirected, target_is_directory=True)

        execute = await client.post(
            "/operations/execute",
            data={"csrf_token": csrf, "preview_token": token},
        )
        job_id = re.search(r'data-job-id="([^"]+)"', execute.text).group(1)
        events = await client.get(
            "/operations/{}/events".format(job_id), params={"token": csrf}
        )

        self.assertIn('"kind": "error"', events.text)
        self.assertIn("filesystem changed", events.text)
        self.assertFalse((redirected / "config.json").exists())


if __name__ == "__main__":
    unittest.main()
