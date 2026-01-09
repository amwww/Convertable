#!/usr/bin/env python3
# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

block_cipher = None

# PyInstaller does not reliably define __file__ when executing the spec.
# Prefer PyInstaller-provided globals when available.
_specpath = globals().get("SPECPATH") or globals().get("specpath")
project_dir = Path(_specpath).resolve() if _specpath else Path.cwd().resolve()
entry = str(project_dir / "main.py")

icon_path = project_dir / "assets" / "icon.icns"
if not icon_path.exists():
    raise FileNotFoundError(f"Missing app icon: {icon_path}")

# Bundle data files (so window icon loads from inside the .app)
icon_png_path = project_dir / "assets" / "icon.png"
if not icon_png_path.exists():
    raise FileNotFoundError(f"Missing window icon PNG: {icon_png_path}")

datas = [(str(icon_png_path), "assets")]

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
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_path),
)

app = BUNDLE(
    exe,
    name=f"{app_name}.app",
    icon=str(icon_path),
    bundle_identifier=bundle_id,
    info_plist=info_plist,
)
