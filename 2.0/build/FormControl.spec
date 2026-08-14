# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:/Users/王嘉琦/Desktop/表格整理/2.0/server/server.py'],
    pathex=[],
    binaries=[],
    datas=[('C:/Users/王嘉琦/Desktop/表格整理/2.0/login.html', '.'), ('C:/Users/王嘉琦/Desktop/表格整理/2.0/admin.html', '.'), ('C:/Users/王嘉琦/Desktop/表格整理/2.0/表格分类汇总.html', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='FormControl',
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
)
