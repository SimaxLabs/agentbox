# Repository Guidance

## Setup And Commands

- The core CLI requires Python 3.14+ and has no dependencies: `./ai-kit ...`.
- Install optional surfaces explicitly: `pip install -e '.[ui]'`, `pip install -e '.[desktop]'`, or `pip install -e '.[build]'`.
- Global CLI options must precede the action, for example `./ai-kit --config /tmp/ai-kit.json backup all`.
- Run all dependency-free tests with `python3 -m unittest discover -s tests -v`; web tests are skipped when their extras are absent.
- Run the complete suite after `pip install -e '.[test]'` with the same unittest command.
- Run one CLI test with `python3 -m unittest tests.test_ai_kit.AiKitTest.test_dry_run_does_not_write -v`.
- Run one web test with `python3 -m unittest tests.test_web.AiKitWebTest.test_operation_posts_require_csrf_token -v`.

## Architecture

- `ai-kit` is an executable compatibility wrapper; keep its executable bit. Installed entry points are declared in `pyproject.toml`.
- `ai_kit/core.py` owns all backup, restore, status, path-validation, atomic-write, and operation-lock behavior. CLI and UI code must call `run_operation` rather than reimplement filesystem operations.
- Core operations report typed `OperationEvent` values. Preserve this boundary instead of adding presentation-specific `print` calls to core logic.
- `ai_kit/cli.py` lazily imports UI modules so the base CLI remains dependency-free. Do not introduce FastAPI, uvicorn, or pywebview imports into the core import path.
- `ai_kit/web.py` serves both the browser UI and the pywebview shell. `ai_kit/desktop.py` only manages the native window and loopback uvicorn lifecycle.
- The source launcher defaults to the adjacent repository `ai-kit.json`; installed or packaged entry points prefer `AI_KIT_CONFIG`, then a current-directory config, then `ai_kit/default_config.json`.

## Safety Invariants

- UI writes require a dry-run preview, short-lived single-use token, matching filesystem signature, CSRF validation, and the shared operation lock. Keep preview and execution routed through the same `OperationRequest` and core preflight.
- The web server is intentionally loopback-only and uses trusted-host validation. Do not broaden bind addresses or add generic command/filesystem endpoints.
- Active UI jobs are serialized and non-daemon; desktop shutdown waits for them. Do not allow window closure to interrupt an atomic replacement.
- Preserve all-or-nothing preflight for multi-tool restores and the existing symlink, traversal, unavailable-source, safety-copy, and deployment-receipt checks.
- Use temporary test configs and roots as the existing tests do. Never point write-capable tests at the repository's real `ai-kit.json` or user tool directories.

## UI And Packaging

- HTMX is vendored at `ai_kit/static/vendor/htmx.min.js`; the UI must remain usable offline and its CSP must not gain a remote script source.
- New templates or static assets must be included in both `pyproject.toml` package data and `ai-kit-desktop.spec` PyInstaller data when existing directory globs do not cover them.
- Desktop version metadata is duplicated in `pyproject.toml` and `ai-kit-desktop.spec`; update both together.
- Build the native artifact with `pyinstaller --noconfirm ai-kit-desktop.spec`; macOS output is `dist/AI Kit.app`, which is ignored.
- For packaging changes, verify both `python3 -m pip wheel . --no-deps --wheel-dir <temporary-directory>` and the PyInstaller build in addition to the tests.
