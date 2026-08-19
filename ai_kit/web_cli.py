"""Standalone command for the browser UI."""

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from .core import AiKitError
from .paths import default_config_path


def main(arguments: Optional[Sequence[str]] = None) -> int:
    from .web import run_browser

    parser = argparse.ArgumentParser(description="Open the AI Kit browser interface.")
    parser.add_argument("--config", default=str(default_config_path()))
    parser.add_argument("--host")
    parser.add_argument("--bind", default="127.0.0.1", choices=["127.0.0.1", "localhost"])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(arguments)
    return run_browser(
        Path(args.config).expanduser().resolve(),
        host_override=args.host,
        bind=args.bind,
        port=args.port,
        open_browser=not args.no_open,
    )


def entrypoint() -> None:
    try:
        raise SystemExit(main())
    except AiKitError as exc:
        print("error: {}".format(exc), file=sys.stderr)
        raise SystemExit(2)
    except ImportError as exc:
        print(
            "error: UI dependencies are unavailable: {}. "
            "Install ai-kit with the ui extra.".format(exc),
            file=sys.stderr,
        )
        raise SystemExit(2)
