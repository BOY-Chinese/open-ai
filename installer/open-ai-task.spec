# -*- mode: python ; coding: utf-8 -*-
# open-ai-task.exe — 短命任务载体: 签到 / 流水采集 / 登录助手
#
# 入口是 scripts/task_main.py: 它按「脚本文件名」路由到内置模块,
# 忽略参数里的路径 —— 安装目录没有 scripts\*.py 源码, 只有本 exe。
# 参数风格保持与 Broker 一致: open-ai-task.exe <路径>\signin_all.py --wb-only
a = Analysis(
    ['..\\scripts\\task_main.py'],
    pathex=['..', '..\\scripts'],
    binaries=[],
    datas=[],
    hiddenimports=[
        'signin_all', 'usage_collector', 'login_trae', 'login_workbuddy',
        'login_workbuddy_intl', 'usage_history', 'wb_usage_history',
        'account_manager', 'api_store', 'credits_api',
        'playwright', 'playwright.sync_api',
    ],
    excludes=['tkinter'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
          name='open-ai-task', debug=False, strip=False, upx=False,
          runtime_tmpdir=None, console=False,
          icon=['ico\\open-ai.ico'],
          version='ver_task.py')
