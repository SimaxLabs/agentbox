"""Command-line interface for AgentBox."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .core import AgentBoxError, OperationRequest, managed_provider_ids, run_operation
from .paths import default_config_path
from .update import check_for_updates, current_build, install_update, prepare_update_plan


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

    status = subparsers.add_parser("status", help="compare tool artifacts with their exact backups")
    status.add_argument("tool", nargs="?", default="all", choices=tool_choices)

    storage = subparsers.add_parser(
        "storage", help="persist local, managed Git, or dual storage"
    )
    storage.add_argument("--local", dest="storage_local", help="local catalog directory")
    storage.add_argument("--git", dest="storage_git", help="managed Git repository URL")
    storage.add_argument("--dry-run", action="store_true")

    subparsers.add_parser("update", help="check for and install the latest AgentBox release")

    ui = subparsers.add_parser("ui", help="open the local browser interface")
    ui.add_argument("--bind", default="127.0.0.1", choices=["127.0.0.1", "localhost"])
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-open", action="store_true", help="do not open a browser automatically")
    return parser.parse_args(arguments)


def announce_available_update() -> None:
    status = check_for_updates()
    if status.warning:
        print("warning: {}".format(status.warning), file=sys.stderr)
    if status.stale:
        print("warning: update status is from the last successful check", file=sys.stderr)
    if status.update_available:
        guidance = (
            "run '{}'".format(status.install_command)
            if status.install_command
            else "update through {}".format(status.install_channel)
            if status.install_channel
            else "run 'agentbox update'"
        )
        print(
            "update available: AgentBox {} is available (commit {}); {}".format(
                status.latest_label, status.latest_commit_label, guidance
            ),
            file=sys.stderr,
        )


def run_update() -> int:
    status = check_for_updates(force=True)
    if status.warning:
        print("warning: {}".format(status.warning), file=sys.stderr)
    if status.error:
        raise AgentBoxError(status.error)
    if status.version_relation == "older":
        print(
            "Latest AgentBox release {} is older than this installation {}; "
            "semantic downgrade was refused.".format(
                status.latest_label, status.current_label
            )
        )
        return 0
    if status.update_available is False:
        print(
            "AgentBox is up to date at {} (commit {}).".format(
                status.current_label, status.current_commit_label
            )
        )
        return 0
    if status.install_channel:
        print(
            "Latest AgentBox release: {} (commit {}).".format(
                status.latest_label, status.latest_commit_label
            )
        )
        if status.install_command:
            print(
                "This installation is managed by {}. Run '{}' to update it.".format(
                    status.install_channel, status.install_command
                )
            )
        else:
            print(
                "This installation is managed by {}. Update it through that installation channel; "
                "direct replacement is disabled.".format(status.install_channel)
            )
        return 0
    if status.update_available and status.relation in {"behind", "diverged", "identical"}:
        print(
            "AgentBox {} is available, but its commit is {} relative to this build; "
            "automatic update was refused.".format(status.latest_label, status.relation)
        )
        return 0
    if not status.can_self_update:
        if status.latest_version:
            print(
                "Latest AgentBox release: {} (commit {}).".format(
                    status.latest_label, status.latest_commit_label
                )
            )
        print(
            "Automatic updates are available only for standalone releases. "
            "Update the source checkout or Python installation using the same method "
            "that installed it."
        )
        return 0
    plan = prepare_update_plan()
    print(
        "Updating AgentBox from v{} (commit {}) to v{} (commit {})...".format(
            plan.current_version,
            plan.current_commit[:12],
            plan.latest_version,
            plan.latest_commit[:12],
        )
    )
    result = install_update(plan)
    print(result.message)
    return 0


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

    if args.action == "update":
        return run_update()

    if args.action != "ui":
        announce_available_update()

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
