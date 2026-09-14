# -*- mode: python ; coding: utf-8 -*-
# open-ai.exe — 控制 CLI (bootstrap.py 的品牌化入口)
#
# 源码形态下这个位置是 runtime\Scripts\open-ai.exe (procname 复制解释器生成的
# shim)。打包形态没有 runtime\, 但**桌面端与计划任务仍按这个名字找它**:
#   desktop\open-ai-desktop.exe 的托盘「退出」→ stop_all_backends → open-ai.exe stop
#   开机自启 / 手动启停                            → open-ai.exe start|status|doctor
# 少了它, 托盘「退出」会静默不生效 (后端留在后台), 桌面端也会判定 can_start=false。
# 入口是 bootstrap.py, 它自己就是 `python bootstrap.py <子命令>` 的 CLI。
a = Analysis(
    ['..\\bootstrap.py'],
    pathex=['..', '..\\scripts'],
    binaries=[],
    datas=[],
    hiddenimports=[
        'app_paths', 'procname', 'jobmgmt', 'ipc', 'app_runtime', 'daemon',
    ],
    excludes=['tkinter', 'playwright'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
          name='open-ai', debug=False, strip=False, upx=False,
          runtime_tmpdir=None, console=True,
          icon=['ico\\open-ai.ico'],
          version='ver_cli.py')
