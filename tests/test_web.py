import json
import re
import tempfile
import unittest
from pathlib import Path

try:
    import httpx

    from ai_kit.web import create_app
except ModuleNotFoundError:
    httpx = None
    create_app = None


@unittest.skipIf(httpx is None, "web test dependencies are not installed")
class AiKitWebTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.skills = self.root / "home/opencode/skills"
        self.commands = self.root / "home/opencode/commands"
        self.catalog = self.root / "catalog"
        self.config = self.root / "ai-kit.json"
        self.config.write_text(
            json.dumps(
                {
                    "version": 1,
                    "host": "test-host",
                    "catalog": str(self.catalog),
                    "state_file": str(self.root / "state/state.json"),
                    "safety_backups": str(self.root / "state/backups"),
                    "tools": {
                        "opencode": {
                            "skills": {
                                "sources": [{"id": "skills", "path": str(self.skills)}],
                                "target": str(self.skills),
                            },
                            "commands": {
                                "sources": [{"id": "commands", "path": str(self.commands)}],
                                "target": str(self.commands),
                            },
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
        self.csrf = self.app.state.runtime.csrf_token

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temporary.cleanup()

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
        self.assertIn("AI Kit Workbench", response.text)
        self.assertIn("UNBACKED opencode commands/audit.md", response.text)
        self.assertIn("default-src 'self'", response.headers["content-security-policy"])
        self.assertEqual("no-store", response.headers["cache-control"])

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


if __name__ == "__main__":
    unittest.main()
