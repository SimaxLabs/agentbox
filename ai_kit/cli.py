"""Command-line interface for AI Kit."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .core import AiKitError, OperationRequest, managed_provider_ids, run_operation
from .paths import default_config_path


def parse_args(
    tool_names: Sequence[str],
    default_config: Path,
    arguments: Sequence[str] | None = None,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Back up and restore skills and commands across AI coding tools."
    )
    parser.add_argument(
        "--config",
        default=str(default_config),
        help="configuration file (default: discovered ai-kit.json)",
    )
    parser.add_argument(
        "--host",
        help="catalog hostname namespace (default: AI_KIT_HOST or detected hostname)",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    tool_choices = ["all", *tool_names]

    backup = subparsers.add_parser("backup", help="copy exact tool artifacts into the catalog")
    backup.add_argument("tool", choices=tool_choices)
    backup.add_argument("--dry-run", action="store_true")
    backup.add_argument("--prune", action="store_true", help="remove catalog entries absent at the source")
    backup.add_argument(
        "--include-derived",
        action="store_true",
        help="back up locally modified artifacts previously restored by ai-kit",
    )

    restore = subparsers.add_parser("restore", help="restore catalog artifacts")
    restore.add_argument("tool", choices=tool_choices)
    restore.add_argument("--dry-run", action="store_true")
    restore.add_argument("--force", action="store_true", help="allow replacement of symlinked artifacts")
    tool_selection = restore.add_mutually_exclusive_group()
    tool_selection.add_argument(
        "--from",
        dest="source_tool",
        choices=list(tool_names),
        help="restore portable skills from only one tool's catalog",
    )
    tool_selection.add_argument(
        "--all-tools",
        action="store_true",
        help="restore portable skills backed up from every tool",
    )
    restore.add_argument(
        "--all-hosts",
        action="store_true",
        help="restore from every hostname namespace instead of the selected host",
    )
    restore.add_argument(
        "--as-backed-up",
        action="store_true",
        help="restore exact original artifacts to their recorded locations",
    )

    status = subparsers.add_parser("status", help="compare tool artifacts with their exact backups")
    status.add_argument("tool", nargs="?", default="all", choices=tool_choices)

    storage = subparsers.add_parser(
        "storage", help="persist local, managed Git, or dual storage"
    )
    storage.add_argument("--local", dest="storage_local", help="local catalog directory")
    storage.add_argument("--git", dest="storage_git", help="managed Git repository URL")
    storage.add_argument("--dry-run", action="store_true")

    ui = subparsers.add_parser("ui", help="open the local browser interface")
    ui.add_argument("--bind", default="127.0.0.1", choices=["127.0.0.1", "localhost"])
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-open", action="store_true", help="do not open a browser automatically")
    return parser.parse_args(arguments)


def main(
    arguments: Sequence[str] | None = None,
    repository_config: Path | None = None,
) -> int:
    default_config = default_config_path(repository_config)
    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument("--config", default=str(default_config))
    bootstrap.add_argument("--host")
    bootstrap_args, _ = bootstrap.parse_known_args(arguments)
    config_path = Path(bootstrap_args.config).expanduser().resolve()
    args = parse_args(managed_provider_ids(), default_config, arguments)

    if args.action == "ui":
        from .web import run_browser

        return run_browser(
            config_path,
            host_override=args.host,
            bind=args.bind,
            port=args.port,
            open_browser=not args.no_open,
        )

    request = OperationRequest(
        action=args.action,
        tool=getattr(args, "tool", "all"),
        host=args.host,
        dry_run=getattr(args, "dry_run", False),
        prune=getattr(args, "prune", False),
        include_derived=getattr(args, "include_derived", False),
        source_tool=getattr(args, "source_tool", None),
        all_tools=getattr(args, "all_tools", False),
        all_hosts=getattr(args, "all_hosts", False),
        as_backed_up=getattr(args, "as_backed_up", False),
        force=getattr(args, "force", False),
        storage_local=getattr(args, "storage_local", None),
        storage_git=getattr(args, "storage_git", None),
    )
    run_operation(config_path, request)
    return 0


def entrypoint(repository_config: Path | None = None) -> None:
    try:
        raise SystemExit(main(repository_config=repository_config))
    except AiKitError as exc:
        print("error: {}".format(exc), file=sys.stderr)
        raise SystemExit(2)
    except ImportError as exc:
        print(
            "error: UI dependencies are unavailable: {}. "
            "Install or reinstall ai-kit.".format(exc),
            file=sys.stderr,
        )
        raise SystemExit(2)
