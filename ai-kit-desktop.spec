# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


root = Path(SPECPATH)
datas = [
    (str(root / "ai_kit/default_config.json"), "ai_kit"),
    (str(root / "ai_kit/templates"), "ai_kit/templates"),
    (str(root / "ai_kit/static"), "ai_kit/static"),
]
hiddenimports = collect_submodules("uvicorn")

a = Analysis(
    [str(root / "desktop_entry.py")],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    name="AI Kit",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    exclude_binaries=True,
)

contents = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="AI Kit",
)

app = BUNDLE(
    contents,
    name="AI Kit.app",
    icon=str(root / "ai-kit.icns"),
    version="0.2.0",
    bundle_identifier="dev.aikit.workbench",
    info_plist={
        "CFBundleDisplayName": "AI Kit",
        "CFBundleVersion": "0.2.0",
        "NSHighResolutionCapable": True,
    },
)
