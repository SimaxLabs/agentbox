# AgentBox

<p align="center">
  <img src="logo.png" alt="AgentBox logo" width="150">
</p>

AgentBox is a local-first catalog for the configuration that makes AI coding agents useful: skills, commands, prompts, supporting files, modes, and source lineage.

It gives Claude Code, Codex, and OpenCode one deliberate backup and restore workflow without flattening their original formats. Backups can live in a local folder, a Git repository you manage, or both.

## Why AgentBox

### Exact on the way in

AgentBox preserves artifact bytes, supporting files, POSIX executable modes when the filesystem exposes them, relative source locations, and stable source IDs. Backup does not convert commands or rewrite skills.

### Portable on the way out

Restore exact originals to their recorded locations, or turn compatible commands and prompts into portable skill packages for another supported provider. Divergent artifacts stop before any target is changed.

### Every browser write is reviewed

The browser UI requires a dry-run preview before execution. Confirmation uses a short-lived, single-use token, repeats preflight under the operation lock, and rejects the operation if the filesystem or reviewed plan changed.

### Storage without a hosted service

- Local folder, including a cloud-synced folder or NAS mount
- Managed Git repository using your existing SSH or credential helper
- Dual local and Git copies with strict divergence checks

AgentBox does not provide a hosted cloud, account system, analytics, or telemetry.

AgentBox cannot verify remote visibility. If your catalog contains sensitive material, confirm that the Git repository is private before pushing.

### Designed for multiple machines

Catalog entries are separated by host namespace. AgentBox runs on macOS, Linux, and Windows and uses platform-appropriate configuration, data, and state directories.

### Provider-first visibility

First-run onboarding detects known providers and lets you enable resources explicitly. The workbench shows provider health, source availability, catalog artifacts, origins, storage, and live operation logs.

## Supported Providers

Typed backup and restore support:

| Provider | Managed resources |
| --- | --- |
| Claude Code | Skills and commands |
| Codex | Skills and prompts |
| OpenCode | Skills and commands |

Detection-only visibility is included for Cursor, Windsurf, Gemini CLI, GitHub Copilot, Continue, Goose, and Kiro. AgentBox shows these providers without applying unsafe generic file-copy behavior. Typed support will be added provider by provider.

## Safety Model

AgentBox treats configuration movement as a filesystem operation, not a convenience copy.

- Multi-provider operations complete all preflight checks before writing.
- Existing restore targets receive timestamped safety copies.
- Symlinked sources, catalogs, and unsafe targets are rejected.
- `--force` can explicitly permit replacement of symlinked restore targets from the CLI or reviewed UI operation.
- Catalog traversal and overlapping destinations are rejected.
- Missing sources cannot silently prune their only known backup.
- Git pushes are never forced.
- Rejected pushes roll back managed changes.
- Ambiguous push outcomes preserve recoverable state for the next operation.
- Dual storage stops when its copies disagree or Git is unavailable.
- The web server binds only to loopback and serves all assets locally.

## Requirements

- Standalone release: no Python installation required
- Source or Python package installation: Python 3.14 or newer
- Git only when managed Git storage is enabled

## GitHub Releases

Every successful push to `main` replaces one rolling `latest` GitHub Release. Publication occurs only after every native build passes its platform smoke test:

| Archive | Platform |
| --- | --- |
| `agentbox-latest-macos-arm64.tar.gz` | Apple Silicon macOS |
| `agentbox-latest-linux-x86_64.tar.gz` | x86-64 Linux |
| `agentbox-latest-linux-arm64.tar.gz` | ARM64 Linux |
| `agentbox-latest-windows-x86_64.zip` | 64-bit Windows |

Download and extract the archive for your machine, then put `agentbox` or `agentbox.exe` somewhere on `PATH`. The standalone executable includes the CLI, browser UI, templates, and static assets:

```bash
agentbox --help
agentbox ui
```

The release also includes `SHA256SUMS.txt`. Every native archive contains `SOURCE_COMMIT`, the exact AgentBox source tree in `agentbox-source.zip`, the resolved `DEPENDENCIES.txt`, `SOURCE_MANIFEST.json`, a hashed native-library inventory in `NATIVE_COMPONENTS.json`, all discovered license notices, and `BUILD_ENVIRONMENT.txt`. Source manifests include durable upstream locations and available SHA-256 digests. Unknown native dependencies stop the release instead of shipping without provenance.

Linux binaries require glibc 2.35 or newer, matching Ubuntu 22.04 and newer. Other distributions can use the Python installation when their libc is older.

The current macOS and Windows builds are unsigned. Those operating systems may display an unidentified-developer warning until signing certificates are configured.

## Install from Source

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Windows PowerShell:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
```

Installed commands:

```text
agentbox
agentbox-ui
```

The executable source launcher is `./agentbox.py`.

## First Run

```bash
agentbox ui
```

When no configuration is discovered, AgentBox opens onboarding and:

1. Detects supported and recognized providers.
2. Lets you choose managed resources.
3. Lets you choose local, Git, or dual storage.
4. Shows a dry-run of the configuration change.
5. Saves only after explicit confirmation.

The repository includes `agentbox.json`, so launching from its checkout uses that development configuration. Run the installed command from another directory to exercise first-run onboarding.

The browser UI is available at `http://127.0.0.1:8765` by default.

```bash
agentbox ui --port 9000
```

## Core Workflow

Preview and create a backup:

```bash
agentbox backup all --dry-run
agentbox backup all
```

Check drift:

```bash
agentbox status
agentbox status codex
```

Restore matching portable resources:

```bash
agentbox restore claude --dry-run
agentbox restore claude
```

Restore from another provider:

```bash
agentbox restore claude --from opencode
```

Restore exact originals to recorded locations:

```bash
agentbox restore opencode --as-backed-up
```

Restore across all catalog hosts:

```bash
agentbox restore opencode --all-hosts
```

## Storage

Configure a local catalog:

```bash
agentbox storage --local ~/Backups/agentbox-catalog
```

Configure managed Git storage:

```bash
agentbox storage --git git@github.com:example/private-agentbox-catalog.git
```

Keep both copies:

```bash
agentbox storage \
  --local ~/Backups/agentbox-catalog \
  --git git@github.com:example/private-agentbox-catalog.git
```

Add `--dry-run` to preview a storage configuration change.

Default local catalog locations:

- Windows: `%LOCALAPPDATA%\AgentBox\catalog`
- Linux: `${XDG_DATA_HOME:-~/.local/share}/agentbox/catalog`
- macOS: `${XDG_DATA_HOME:-~/.local/share}/agentbox/catalog`

Managed Git checkouts live under the corresponding platform data directory in `AgentBox/repositories` or `agentbox/repositories`.

## Configuration

Configuration lookup order:

1. `AGENTBOX_CONFIG`
2. The repository launcher’s adjacent `agentbox.json`
3. `agentbox.json` in the current directory
4. The platform user configuration

Platform user configuration locations:

- Windows: `%APPDATA%\AgentBox\config.json`
- Linux and macOS: `${XDG_CONFIG_HOME:-~/.config}/agentbox/config.json`

Override the host namespace with `AGENTBOX_HOST` or the global `--host` option.

```bash
AGENTBOX_HOST=workstation agentbox backup all
agentbox --host workstation backup all
```

Global options must precede the action:

```bash
agentbox --config /path/to/agentbox.json backup all
```

## Architecture

- `agentbox/core.py` owns backup, restore, status, provider compilation, storage transactions, path validation, atomic writes, and operation locks.
- CLI and browser operations share the same typed `OperationRequest` and `OperationEvent` boundary.
- Provider definitions are immutable package data in `agentbox/providers.json`.
- The browser UI is loopback-only, server-rendered, HTMX-enhanced, and usable offline.
- Core backup and restore operations remain independent of web dependencies.

## AI Development Disclosure

This software is developed with strong assistance from GPT 5.5, GPT 5.6, and Claude Fable, with humans leading the ideas, testing, and debugging. We say this openly because it shaped how the project was built.

If you are not comfortable using AI-developed code, this software is not for you.

AI assistance does not replace verification: AgentBox uses isolated filesystem tests, local bare Git repositories, browser security tests, package builds, dry-run invariants, and explicit human review of behavior and failures.

## Tests

Run the dependency-free suite:

```bash
python3 -m unittest discover -s tests -v
```

Run the complete suite:

```bash
pip install -e '.[test]'
python3 -m unittest discover -s tests -v
```

Build and smoke-test a standalone executable:

```bash
pip install -e '.[release]'
python3 -m PyInstaller --noconfirm --clean agentbox.spec
python3 scripts/smoke_standalone.py dist/agentbox
```

## Rolling Releases

Push release-ready source to `main`. No version bump or manual tag is required.

The `Release` workflow tests the source, builds all four native archives, executes each native binary, generates checksums, and publishes a commit-addressed release marked as GitHub's Latest. Only after the new release is public does it remove older rolling releases. Pull requests and manual workflow runs build and validate artifacts without replacing the public release.

No manual version bump or release tag is needed. Internal release tags use the full `agentbox-latest-<commit>` identifier and are managed by the workflow.

## License

AgentBox is licensed under the GNU General Public License v3.0 only (`GPL-3.0-only`). See `LICENSE`.

Verify packaging:

```bash
python3 -m pip wheel . --no-deps --wheel-dir /tmp/agentbox-wheel
```
