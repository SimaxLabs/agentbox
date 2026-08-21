# -*- mode: python ; coding: utf-8 -*-

import json
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


build_info = Path("build/agentbox-build-info/build.json")
build_info.parent.mkdir(parents=True, exist_ok=True)
build_info.write_text(
    json.dumps(
        {
            "version": os.environ.get("AGENTBOX_BUILD_VERSION", ""),
            "commit": os.environ.get("AGENTBOX_BUILD_COMMIT", ""),
            "repository": os.environ.get(
                "AGENTBOX_BUILD_REPOSITORY", "SimaxLabs/AgentBox"
            ),
        },
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)

analysis = Analysis(
    ["scripts/agentbox_standalone.py"],
    pathex=[],
    binaries=[],
    datas=[*collect_data_files("agentbox"), (str(build_info), "agentbox")],
    hiddenimports=["agentbox.web", *collect_submodules("uvicorn")],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
python_archive = PYZ(analysis.pure)

executable = EXE(
    python_archive,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="agentbox",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
