# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for dwarf3_to_stellarium (one-folder, cross-platform).
# The bridge uses plain Flask + requests + websocket-client only (no cv2,
# no flask-socketio), so the dependency footprint is small.
a = Analysis(
    ["server.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    excludes=["tkinter", "matplotlib", "PyQt5", "PySide2", "cv2", "numpy"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="dwarf3_to_stellarium",
    console=True,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name="dwarf3_to_stellarium",
)
