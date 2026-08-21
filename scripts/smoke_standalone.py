"""Smoke-test a frozen AgentBox executable and its bundled browser UI."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def read_url(url: str) -> bytes:
    with urlopen(url, timeout=2) as response:
        if response.status != 200:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        return response.read()


def kill_windows_process_tree(process: subprocess.Popen[str]) -> None:
    subprocess.run(
        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
        check=False,
        capture_output=True,
    )


def stop_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":  # pragma: no cover - exercised by the Windows release job.
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        except OSError:
            kill_windows_process_tree(process)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            kill_windows_process_tree(process)
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.kill()
        process.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-commit", required=True)
    args = parser.parse_args()
    executable = args.executable.resolve()

    version = subprocess.run(
        [str(executable), "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if (
        "AgentBox v{}".format(args.expected_version) not in version.stdout
        or args.expected_commit not in version.stdout
    ):
        raise RuntimeError(
            "Frozen build identity did not contain expected version {} and commit {}: {}".format(
                args.expected_version, args.expected_commit, version.stdout.strip()
            )
        )

    with tempfile.TemporaryDirectory(prefix="agentbox-release-smoke-") as temporary:
        root = Path(temporary)
        port = available_port()
        config = root / "config.json"
        repository = root / "catalog.git"
        git = shutil.which("git")
        if git is None:
            raise RuntimeError("Git is required for the standalone release smoke test")
        subprocess.run(
            [git, "init", "--bare", "--initial-branch=main", str(repository)],
            check=True,
            capture_output=True,
            timeout=60,
        )
        commands = root / "config-home/opencode/commands"
        commands.mkdir(parents=True)
        (commands / "release-smoke.md").write_text("Release smoke test\n", encoding="utf-8")
        config.write_text(
            json.dumps(
                {
                    "version": 2,
                    "host": "release-smoke",
                    "storage": {"git": repository.as_uri()},
                    "state_file": str(root / "state/state.json"),
                    "safety_backups": str(root / "state/backups"),
                    "providers": {
                        "opencode": {
                            "enabled": True,
                            "resources": {"skills": False, "commands": True},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment.update(
            {
                "AGENTBOX_CONFIG": str(config),
                "AGENTBOX_HOST": "",
                "AGENTBOX_NO_UPDATE_CHECK": "1",
                "HOME": str(root),
                "USERPROFILE": str(root),
                "APPDATA": str(root / "appdata"),
                "LOCALAPPDATA": str(root / "local-appdata"),
                "XDG_CONFIG_HOME": str(root / "config-home"),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_STATE_HOME": str(root / "state"),
            }
        )
        preview = subprocess.run(
            [str(executable), "backup", "opencode", "--dry-run"],
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            timeout=60,
        )
        if preview.returncode != 0 or "GIT READY" not in preview.stdout:
            raise RuntimeError(
                "Frozen Git smoke test failed:\n{}\n{}".format(
                    preview.stdout, preview.stderr
                )
            )

        update_result = subprocess.run(
            [str(executable), "update"],
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            timeout=60,
        )
        if update_result.returncode == 0 or "invalid choice" not in update_result.stderr:
            raise RuntimeError(
                "Frozen executable unexpectedly exposes an update command:\n{}\n{}".format(
                    update_result.stdout, update_result.stderr
                )
            )

        log_path = root / "server.log"
        log = log_path.open("w", encoding="utf-8")
        process = subprocess.Popen(
            [
                str(executable),
                "ui",
                "--bind",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-open",
            ],
            cwd=root,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=os.name != "nt",
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
        )
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    log.flush()
                    output = log_path.read_text(encoding="utf-8")
                    raise RuntimeError(
                        f"AgentBox exited before serving the UI ({process.returncode}):\n{output}"
                    )
                try:
                    page = read_url(f"http://127.0.0.1:{port}/")
                    script = read_url(f"http://127.0.0.1:{port}/static/app.js")
                    styles = read_url(f"http://127.0.0.1:{port}/static/app.css")
                    htmx = read_url(f"http://127.0.0.1:{port}/static/vendor/htmx.min.js")
                    manifest = read_url(f"http://127.0.0.1:{port}/static/site.webmanifest")
                    logo = read_url(f"http://127.0.0.1:{port}/static/logo.png")
                    update_status = read_url(f"http://127.0.0.1:{port}/updates/status")
                except (OSError, RuntimeError, URLError):
                    time.sleep(0.25)
                    continue
                if (
                    b"AgentBox" not in page
                    or not script
                    or not styles
                    or not htmx
                    or b"AgentBox" not in manifest
                    or not logo.startswith(b"\x89PNG")
                    or b"Update checks disabled" not in update_status
                ):
                    raise RuntimeError("Bundled UI assets did not contain the expected content")
                csrf = re.search(rb'name="csrf_token" value="([^"]+)"', page)
                if csrf is None:
                    raise RuntimeError("Bundled dashboard did not contain a CSRF token")
                request = Request(
                    f"http://127.0.0.1:{port}/operations/preview",
                    data=urlencode(
                        {
                            "csrf_token": csrf.group(1).decode(),
                            "action": "backup",
                            "host": "release-smoke",
                            "tool": "opencode",
                        }
                    ).encode(),
                    method="POST",
                )
                with urlopen(request, timeout=30) as response:
                    form_preview = response.read()
                if b"Dry run complete" not in form_preview:
                    raise RuntimeError("Bundled form parsing or preview template failed")
                return 0
            raise RuntimeError("Timed out waiting for the bundled AgentBox UI")
        finally:
            stop_process_tree(process)
            log.close()


if __name__ == "__main__":
    raise SystemExit(main())
