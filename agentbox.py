#!/usr/bin/env python3
"""AgentBox command-line entry point."""

import sys
from pathlib import Path


def main() -> None:
    if sys.version_info < (3, 14):
        current = ".".join(str(part) for part in sys.version_info[:3])
        print(
            f"error: AgentBox requires Python 3.14 or newer; found {current}",
            file=sys.stderr,
        )
        raise SystemExit(2)

    from agentbox.cli import entrypoint

    entrypoint(Path(__file__).resolve().with_name("agentbox.json"))


if __name__ == "__main__":
    main()
