"""Native pywebview shell for the local AI Kit web application."""

import argparse
import socket
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from .core import AiKitError
from .paths import default_config_path


def run_desktop(
    config_path: Path,
    host_override: str | None = None,
    debug: bool = False,
) -> int:
    import uvicorn
    import webview

    from .web import create_app

    app = create_app(config_path, host_override)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    server_thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        name="ai-kit-web",
        daemon=True,
    )
    server_thread.start()
    deadline = time.monotonic() + 8
    while not server.started and server_thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.025)
    if not server.started:
        server.should_exit = True
        listener.close()
        raise AiKitError("The local desktop server did not start")

    webview.create_window(
        "AI Kit Workbench",
        "http://127.0.0.1:{}/".format(port),
        width=1360,
        height=900,
        min_size=(860, 620),
        background_color="#f4f0e6",
        text_select=True,
    )
    try:
        webview.start(debug=debug, private_mode=True)
    finally:
        app.state.runtime.wait_for_jobs()
        server.should_exit = True
        server_thread.join(timeout=5)
        listener.close()
    return 0


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open AI Kit in a native desktop window.")
    parser.add_argument("--config", default=str(default_config_path()))
    parser.add_argument("--host")
    parser.add_argument("--debug", action="store_true")
    args, unknown = parser.parse_known_args(arguments)
    invalid = [argument for argument in unknown if not argument.startswith("-psn_")]
    if invalid:
        parser.error("unrecognized arguments: {}".format(" ".join(invalid)))
    return run_desktop(
        Path(args.config).expanduser().resolve(),
        host_override=args.host,
        debug=args.debug,
    )


def entrypoint() -> None:
    try:
        raise SystemExit(main())
    except AiKitError as exc:
        print("error: {}".format(exc), file=sys.stderr)
        raise SystemExit(2)
    except ImportError as exc:
        print(
            "error: desktop dependencies are unavailable: {}. "
            "Install ai-kit with the desktop extra.".format(exc),
            file=sys.stderr,
        )
        raise SystemExit(2)
