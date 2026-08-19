"""Runtime paths shared by command-line and browser entry points."""

import os
from pathlib import Path
import sys


def user_config_path() -> Path:
    if sys.platform == "win32":  # pragma: no cover - exercised on Windows.
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if base:
            candidate = Path(base).expanduser()
            if candidate.is_absolute():
                return candidate / "AI Kit/config.json"
        return Path.home() / "AppData/Roaming/AI Kit/config.json"
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        candidate = Path(xdg_config_home).expanduser()
        if candidate.is_absolute():
            return candidate / "ai-kit/config.json"
    return Path.home() / ".config/ai-kit/config.json"


def default_config_path(repository_default: Path | None = None) -> Path:
    override = os.environ.get("AI_KIT_CONFIG")
    if override:
        return Path(override).expanduser().resolve()

    if repository_default is not None and repository_default.is_file():
        return repository_default.resolve()

    working_copy = Path.cwd() / "ai-kit.json"
    if working_copy.is_file():
        return working_copy.resolve()

    return user_config_path()
