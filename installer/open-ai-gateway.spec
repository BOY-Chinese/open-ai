# -*- mode: python ; coding: utf-8 -*-
# open-ai-gateway.exe — OpenAI/Anthropic 兼容网关 + **Broker 宿主**
#
# ★ 这个 exe 承担两个角色 (见 main.py 的 __main__):
#     不带参数                 → 网关本体 (uvicorn, 端口取自 config.json)
#     <路径>\main.py --broker  → Broker (进程监督 / 定时任务 / IPC 服务端)
#   合并的理由: 安装目录里没有 .py 源码, 而 Broker 的启动方式是
#   「品牌化 exe + 脚本路径参数」, 只有把 Broker 也打进一个 exe 才能被这样拉起。
#
# hiddenimports 里逐个点名 app_runtime / daemon / jobmgmt / ipc: 它们是
# 运行期动态 import 的 (daemon.main / app_runtime 内部), PyInstaller 的静态
# 分析看不到, 漏一个就是「装机后才炸」的运行时 ImportError。
a = Analysis(
    ['..\\main.py'],
    pathex=['..', '..\\scripts'],
    binaries=[],
    datas=[],
    hiddenimports=[
        # Broker 侧 (--broker 模式)
        'app_runtime', 'daemon', 'jobmgmt', 'ipc', 'procname',
        # 管理面 / 业务 (admin_api 与 provider 运行期动态 import)
        'admin_api', 'anthropic_api',
        'account_manager', 'api_store', 'credits_api',
        'usage_collector', 'usage_history', 'wb_usage_history',
        'signin_all', 'login_trae', 'login_workbuddy', 'login_workbuddy_intl',
        'playwright', 'playwright.sync_api',
        'uvicorn.logging', 'uvicorn.loops', 'uvicorn.loops.auto',
        'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.http.auto',
        'uvicorn.protocols.http.h11_impl', 'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan',
        'uvicorn.lifespan.on',
    ],
    excludes=['tkinter'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
          name='open-ai-gateway', debug=False, strip=False, upx=False,
          runtime_tmpdir=None, console=False,
          icon=['ico\\open-ai.ico'],
          version='ver_gateway.py')
