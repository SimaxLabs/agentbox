"""Command-line interface for AgentBox."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .core import (
    AgentBoxError,
    OperationRequest,
    inspect_catalog_revision,
    list_catalog_revisions,
    managed_provider_ids,
    run_operation,
)
from .paths import default_config_path
from .update import check_for_updates, current_build


class BuildVersionAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        repository, version, commit = current_build()
        print(
            "AgentBox v{} (commit {}, {})".format(
                version or "unknown", commit or "unknown", repository
            )
        )
        parser.exit()


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
        help="configuration file (default: discovered agentbox.json)",
    )
    parser.add_argument(
        "--host",
        help="catalog hostname namespace (default: AGENTBOX_HOST or detected hostname)",
    )
    parser.add_argument(
        "--version",
        action=BuildVersionAction,
        nargs=0,
        help="show the version and source provenance, then exit",
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
        help="back up locally modified artifacts previously restored by agentbox",
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
    restore.add_argument(
        "--revision",
        dest="catalog_revision",
        metavar="REVISION",
        help="restore from an immutable local catalog revision",
    )

    status = subparsers.add_parser("status", help="compare tool artifacts with their exact backups")
    status.add_argument("tool", nargs="?", default="all", choices=tool_choices)

    history = subparsers.add_parser("history", help="list or inspect local catalog revisions")
    history.add_argument("catalog_revision", nargs="?", metavar="REVISION")

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


def announce_available_update() -> None:
    status = check_for_updates()
    if status.stale:
        print("warning: update status is from the last successful check", file=sys.stderr)
    if status.update_available:
        if status.install_command:
            guidance = "run '{}'".format(status.install_command)
        elif status.install_channel:
            guidance = "update through {}".format(status.install_channel)
        elif status.standalone and status.release_url:
            guidance = "download it manually from {}".format(status.release_url)
        elif status.release_url:
            guidance = "update the installation manually; release: {}".format(
                status.release_url
            )
        else:
            guidance = "update the installation manually"
        print(
            "update available: AgentBox {} is available (commit {}); {}".format(
                status.latest_label, status.latest_commit_label, guidance
            ),
            file=sys.stderr,
        )


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

    if args.action == "history":
        if args.catalog_revision:
            detail = inspect_catalog_revision(
                config_path, args.catalog_revision, args.host
            )
            revision = detail.summary
            print(
                "REVISION {} {} host={} artifacts={} changes={}".format(
                    revision.revision_id,
                    revision.created_at,
                    revision.host,
                    revision.artifact_count,
                    revision.changes,
                )
            )
            for artifact in detail.artifacts:
                print(
                    "CATALOG {host} {tool} {kind} {path}".format(**artifact)
                )
        else:
            revisions = list_catalog_revisions(config_path, args.host)
            if not revisions:
                print("No local catalog revisions.")
            for revision in revisions:
                print(
                    "REVISION {} {} host={} artifacts={} changes={}".format(
                        revision.revision_id,
                        revision.created_at,
                        revision.host,
                        revision.artifact_count,
                        revision.changes,
                    )
                )
        announce_available_update()
        return 0

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
        catalog_revision=getattr(args, "catalog_revision", None),
    )
    run_operation(config_path, request)
    announce_available_update()
    return 0


def entrypoint(repository_config: Path | None = None) -> None:
    try:
        raise SystemExit(main(repository_config=repository_config))
    except AgentBoxError as exc:
        print("error: {}".format(exc), file=sys.stderr)
        raise SystemExit(2)
    except ImportError as exc:
        print(
            "error: UI dependencies are unavailable: {}. "
            "Install or reinstall AgentBox.".format(exc),
            file=sys.stderr,
        )
        raise SystemExit(2)
