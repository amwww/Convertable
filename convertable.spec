# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

block_cipher = None

project_dir = Path(__file__).resolve().parent
entry = str(project_dir / "main.py")

# Best-effort icon: PyInstaller on macOS expects .icns
icon_path = None
for candidate in [project_dir / "assets" / "icon.icns", project_dir / "icon.icns"]:
    if candidate.exists():
        icon_path = str(candidate)
        break

# Bundle optional data files (so window icon loads from inside the .app)
datas = []
for src, dest in [
    (project_dir / "icon.png", "."),
    (project_dir / "assets" / "icon.png", "assets"),
]:
    if src.exists():
        datas.append((str(src), dest))

app_name = "Convertable"
bundle_id = "com.amwww.convertable"

info_plist = {
    "CFBundleName": app_name,
    "CFBundleDisplayName": app_name,
    "CFBundleIdentifier": bundle_id,
    "NSHighResolutionCapable": True,
}


a = Analysis(
    [entry],
    pathex=[str(project_dir)],
    binaries=[],
    datas=datas,
    hiddenimports=["tkinterdnd2"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=app_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path,
)

app = BUNDLE(
    exe,
    name=f"{app_name}.app",
    icon=icon_path,
    bundle_identifier=bundle_id,
    info_plist=info_plist,
)
