"""Runtime paths shared by command-line and desktop entry points."""

import os
from pathlib import Path
from typing import Optional


def default_config_path(repository_default: Optional[Path] = None) -> Path:
    override = os.environ.get("AI_KIT_CONFIG")
    if override:
        return Path(override).expanduser().resolve()

    if repository_default is not None and repository_default.is_file():
        return repository_default.resolve()

    working_copy = Path.cwd() / "ai-kit.json"
    if working_copy.is_file():
        return working_copy.resolve()

    return Path(__file__).resolve().with_name("default_config.json")
