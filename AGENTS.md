# Repository Guidance

## Setup And Commands

- The project requires Python 3.14+. Core operations remain dependency-free, while standard installation includes the browser UI dependencies.
- Install the editable project with `pip install -e .`.
- Global CLI options must precede the action, for example `./agentbox.py --config /tmp/agentbox.json backup all`.
- Run all dependency-free tests with `python3 -m unittest discover -s tests -v`; web tests are skipped when their extras are absent.
- Run the complete suite after `pip install -e '.[test]'` with the same unittest command.
- Run one CLI test with `python3 -m unittest tests.test_agentbox.AgentBoxTest.test_dry_run_does_not_write -v`.
- Run one web test with `python3 -m unittest tests.test_web.AgentBoxWebTest.test_operation_posts_require_csrf_token -v`.

## Architecture

- `agentbox.py` is the executable source launcher; keep its executable bit. Installed entry points are declared in `pyproject.toml`.
- `agentbox/core.py` owns all backup, restore, status, path-validation, atomic-write, and operation-lock behavior. CLI and UI code must call `run_operation` rather than reimplement filesystem operations.
- Core operations report typed `OperationEvent` values. Preserve this boundary instead of adding presentation-specific `print` calls to core logic.
- `agentbox/cli.py` lazily imports the browser UI so the base CLI remains dependency-free. Do not introduce FastAPI or uvicorn imports into the core import path.
- `agentbox/web.py` serves the loopback-only browser UI.
- The source launcher defaults to the adjacent repository `agentbox.json`; installed entry points prefer `AGENTBOX_CONFIG`, then a current-directory config, then the platform user configuration.

## Safety Invariants

- UI writes require a dry-run preview, short-lived single-use token, matching filesystem signature, CSRF validation, and the shared operation lock. Keep preview and execution routed through the same `OperationRequest` and core preflight.
- The web server is intentionally loopback-only and uses trusted-host validation. Do not broaden bind addresses or add generic command/filesystem endpoints.
- Active UI jobs are serialized and non-daemon; server shutdown waits for them.
- Preserve all-or-nothing preflight for multi-tool restores and the existing symlink, traversal, unavailable-source, safety-copy, and deployment-receipt checks.
- Use temporary test configs and roots as the existing tests do. Never point write-capable tests at the repository's real `agentbox.json` or user tool directories.

## UI And Packaging

- HTMX is vendored at `agentbox/static/vendor/htmx.min.js`; the UI must remain usable offline and its CSP must not gain a remote script source.
- Provider cards and individual provider selectors show only providers detected from their known filesystem markers. This is presentation filtering only; configured core operations retain their existing semantics.
- Provider logos are local SVG assets under `agentbox/static/`. Do not replace them with remote images, icon services, or other network dependencies.
- The current-version update result uses an HTMX out-of-band swap into `#footer-version`; actionable update, stale, disabled, and error notices remain in the top awareness area.
- `logo.png` is the source artwork; `agentbox/static/logo.png` is the optimized web copy. Regenerate it when the source changes.
- New templates or static assets must be included in `pyproject.toml` package data when existing directory globs do not cover them.
- For packaging changes, verify `python3 -m pip wheel . --no-deps --wheel-dir <temporary-directory>` in addition to the tests.
