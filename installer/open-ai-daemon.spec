# -*- mode: python ; coding: utf-8 -*-
# open-ai-daemon.exe — Broker 独立入口 (进程监督 / Job 进程树 / IPC / 定时任务)
#
# 与 open-ai-gateway.exe 的 --broker 模式是**同一个 Broker 的两条启动路径**:
#   * 本 exe            —— 开机自启 / 计划任务 / bootstrap.py start 拉起的默认路径
#   * gateway.exe 宿主  —— 供「网关自愈」使用: 网关若被单独拉起, 它自己兜底起 Broker,
#                          从而不需要再理解「安装根里没有 daemon.py」这件事
# 两者都有单实例保护 (mutex + 状态文件), 同时只可能活一个。
a = Analysis(
    ['..\\daemon.py'],
    pathex=['..', '..\\scripts'],
    binaries=[],
    datas=[],
    hiddenimports=[
        'app_runtime', 'jobmgmt', 'ipc', 'procname',
        'account_manager', 'api_store', 'credits_api',
    ],
    excludes=['tkinter', 'playwright'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
          name='open-ai-daemon', debug=False, strip=False, upx=False,
          runtime_tmpdir=None, console=False,
          icon=['ico\\open-ai.ico'],
          version='ver_daemon.py')
