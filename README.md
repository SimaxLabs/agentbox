# AI Kit

AI Kit keeps exact backups of personal skills, commands, and legacy prompts used by AI coding tools. Backups are separated by hostname so multiple teammates can safely share one repository.

The repository is the durable backup. Tool directories are working copies.

## Requirements

- Python 3.8 or newer
- No third-party packages

## Quick Start

Preview and create an initial backup:

```bash
./ai-kit backup all --dry-run
./ai-kit backup all
git diff
```

Restore this host's Codex artifacts to Codex as skills:

```bash
./ai-kit restore codex --dry-run
./ai-kit restore codex
```

Restore only artifacts originally backed up from one tool:

```bash
./ai-kit restore claude --from opencode
```

Restore this host's artifacts from every tool to Claude Code:

```bash
./ai-kit restore claude --all-tools
```

Restore the matching tool's artifacts from every backed-up hostname:

```bash
./ai-kit restore opencode --all-hosts
```

Restore exact artifacts without converting commands or legacy prompts:

```bash
./ai-kit restore opencode --as-backed-up
```

Check whether working copies differ from their exact backups:

```bash
./ai-kit status
./ai-kit status codex
```

## Backup Behavior

`backup <tool>` discovers skills and commands in every configured current and legacy source. It stores their bytes, supporting files, relative source locations, and file modes under the detected hostname:

```text
catalog/
├── alice-macbook/
│   ├── claude/
│   │   ├── skills/
│   │   ├── commands/
│   │   └── manifest.json
│   ├── codex/
│   └── opencode/
└── bob-workstation/
```

Backup is additive by default. Use `--prune` to remove catalog artifacts that no longer exist in the selected tool's source directories.

```bash
./ai-kit backup codex
./ai-kit backup all --prune
```

The catalog preserves commands and legacy prompts exactly. Conversion does not happen during backup.

The hostname comes from `socket.gethostname()`. Override it without changing shared configuration:

```bash
AI_KIT_HOST=alice-work ./ai-kit backup all
./ai-kit --host alice-work backup all
```

## Restore Modes

### Portable Skills

Portable restoration is the default:

```bash
./ai-kit restore claude
```

By default, it reads only the matching tool under the selected hostname. Therefore `restore claude` reads `catalog/<hostname>/claude` and does not import Codex, OpenCode, or another teammate's entries.

Selection options:

- `--from opencode` selects one different source tool.
- `--all-tools` selects every source tool for the selected hostname.
- `--host alice-work` selects one hostname instead of the detected hostname. This global option goes before `restore`.
- `--all-hosts` selects every hostname in the catalog.
- `--all-tools --all-hosts` explicitly restores the complete shared catalog to the target tool.

- Existing skills are copied exactly, including scripts and references.
- Commands and legacy prompts become `<name>/SKILL.md` packages.
- Conversion keeps `name` and `description`, removes tool-specific command frontmatter, and preserves the prompt body.
- A short instruction explains that `$ARGUMENTS`, positional arguments, and named placeholders refer to the current user request.
- Identical outputs with the same skill name are installed once.
- Divergent outputs with the same name stop the restore before it writes anything. Select an origin with `--from <tool>`.

### Exact Original

Use `--as-backed-up` to prevent conversion:

```bash
./ai-kit restore codex --as-backed-up
```

This mode only reads the target tool's catalog for the selected hostname. It restores each artifact to the current or legacy source location recorded during backup. The content and kind remain unchanged. `--all-hosts` is supported and stops if host backups disagree about content targeting the same location.

`--from` and `--all-tools` cannot be combined with `--as-backed-up` because exact restoration already uses the target tool's recorded origins.

## Duplicate Prevention

Portable restores write a deployment receipt to `~/.local/state/ai-kit/state.json`. The receipt records the installed hash and its catalog origins.

On the next backup:

- An unchanged restored copy is skipped instead of being backed up as a duplicate.
- A restored copy edited inside the tool directory is reported as a conflict before the catalog changes.
- `--include-derived` explicitly backs up such an edited copy as an independent artifact.
- If deployment state is unavailable, identical portable content is still deduplicated during backup and restore.

Backing up an edited derived copy can intentionally create two divergent entries with the same name. Resolve that later with `restore <target> --from <origin>` or reconcile the catalog contents.

## Safety

- `--dry-run` previews backup and restore operations.
- Divergent catalog entries stop portable restoration before target files are changed.
- Existing target artifacts are copied to `~/.local/state/ai-kit/backups/<timestamp>/` before replacement.
- New payloads are staged before they replace an existing catalog or tool artifact.
- Symlinked target artifacts are not replaced unless `--force` is supplied.
- Symlinked source artifacts and catalog paths are rejected instead of being followed outside configured roots.
- A missing current or legacy source keeps its catalog entries and recorded locations, including with `--prune`.
- A changed artifact shared with an unavailable source is not allowed to overwrite that source's only known backup.
- Catalog pruning only happens with `backup --prune`.
- Exact-restore manifest paths are validated to prevent traversal outside configured roots.

## Default Locations

| Tool | Skills | Commands or prompts |
| --- | --- | --- |
| Codex | `~/.agents/skills`, plus legacy backup from `~/.codex/skills` | `~/.codex/prompts` (legacy) |
| Claude Code | `~/.claude/skills` | `~/.claude/commands` (legacy) |
| OpenCode | `~/.config/opencode/skills` | `~/.config/opencode/commands` |

The paths and additional tools are configured in `ai-kit.json`. Every source has a stable ID so exact restoration can map a catalog entry back to the correct current or legacy location without storing machine-specific absolute paths in Git.

## Commands

```text
./ai-kit [--host hostname] backup <all|tool> [--dry-run] [--prune] [--include-derived]
./ai-kit [--host hostname] restore <all|tool> [--dry-run] [--from tool | --all-tools] [--all-hosts] [--as-backed-up] [--force]
./ai-kit [--host hostname] status [all|tool]
```

When using a different configuration file, put the global option before the command:

```bash
./ai-kit --config /path/to/ai-kit.json backup all
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```
