# -*- coding: utf-8 -*-
"""
task_main — open-ai-task.exe 的统一入口 (PyInstaller 打包)
==========================================================
Broker (app_runtime.TaskScheduler) 会以「脚本路径 + 参数」的旧风格调用:

    open-ai-task.exe <脚本路径.py> [参数...]
    open-ai-task.exe <安装根>\\scripts\\signin_all.py --wb-only
    open-ai-task.exe <安装根>\\scripts\\usage_collector.py --collect
    open-ai-task.exe D:\\...\\login_trae.py
    open-ai-task.exe D:\\...\\login_workbuddy.py --timeout 120

本入口只按**脚本文件名**路由到内置模块, 不理会路径 —— 因为安装目录里
并没有 `scripts\\*.py` 源码 (exe 打包方案下源码不随安装包分发)。
参数原样透传给对应模块的 main()。

也直接支持子命令风格: signin / collect / login-trae / login-workbuddy。
"""
import os
import sys


def _root():
    """安装根目录 (日志/GUI 面板都在这里)。frozen: exe 在安装根; 脚本态: 上两级。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    try:
        import app_paths as _ap
        return _ap.ROOT
    except Exception:  # 源码态兜底: task_main.py 在 scripts/ 下, 根是上两级
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _route(script, rest):
    """按脚本文件名路由到内置模块; 返回 True 表示已处理。"""
    name = os.path.basename(script).lower()
    rest = list(rest)
    root = _root()

    if name in ('signin_all.py', 'signin'):
        sys.argv = ['signin_all.py'] + rest
        import signin_all
        return signin_all.main()

    if name in ('usage_collector.py', 'collect'):
        sys.argv = ['usage_collector.py'] + rest
        import usage_collector
        return usage_collector.main()

    if name in ('login_trae.py', 'login-trae'):
        sys.path.insert(0, root)          # 让脚本能读到安装根的 config.json
        import login_trae
        return login_trae.main()

    if name in ('login_workbuddy.py', 'login-workbuddy'):
        sys.path.insert(0, root)
        import login_workbuddy
        return login_workbuddy.main()

    if name in ('login_workbuddy_intl.py', 'login-workbuddy-intl'):
        sys.path.insert(0, root)
        import login_workbuddy_intl
        return login_workbuddy_intl.main()

    if name in ('usage_history.py', 'wusage'):
        sys.argv = ['usage_history.py'] + rest
        import usage_history
        return usage_history.main()

    # WorkBuddy 国际版网页端每日活跃 (2026-09-18 五期实验判决后新增):
    # 国际版无签到渠道, 改发一条 /console/chat/completions 对话拿每日 +30。
    # 平时由 signin_all 内部直接 import 调用; 这里保留独立入口, 便于
    # 排查 / 手工补发一条 (打包态同样按文件名路由)。
    if name in ('wb_web_daily.py', 'wb-web-daily', 'wbweb'):
        sys.argv = ['wb_web_daily.py'] + rest
        import wb_web_daily
        return wb_web_daily.main()

    return None


def main():
    argv = sys.argv[1:]
    if not argv:
        sys.stderr.write(
            '用法: open-ai-task.exe <脚本名|子命令> [参数...]\n'
            '  脚本名: signin_all.py / usage_collector.py / login_trae.py /\n'
            '          login_workbuddy.py / login_workbuddy_intl.py / wb_web_daily.py\n'
            '  子命令: signin / collect / login-trae / login-workbuddy / login-workbuddy-intl\n')
        return 2

    script, rest = argv[0], argv[1:]
    rc = _route(script, rest)
    if rc is None:
        sys.stderr.write('[错误] 无法识别的任务: %s\n' % script)
        return 2
    return rc or 0


if __name__ == '__main__':
    sys.exit(main())
