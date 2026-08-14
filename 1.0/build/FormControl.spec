# -*- mode: python ; coding: utf-8 -*-
# 质量文控平台 · 本地数据服务打包脚本
# 入口为 server/server.py（自带 __main__，独立运行 http.server + sqlite3）。
# HTML 由服务在运行时从安装目录磁盘读取并托管，不内嵌进 exe，因此界面改动只需更新 HTML 文件 + 重编译安装包。
# 历史版本曾引入 pywebview，当前 server.py 已改用 webbrowser 打开系统浏览器，故移除 webview 依赖以减小体积。

a = Analysis(
    ['C:\\Users\\王嘉琦\\Desktop\\表格整理\\server\\server.py'],
    pathex=['C:\\Users\\王嘉琦\\Desktop\\表格整理\\server'],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['webview', 'PyQt5', 'PyQt6', 'tkinter'],
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
