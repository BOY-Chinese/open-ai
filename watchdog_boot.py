# -*- coding: utf-8 -*-
"""
watchdog_boot.py — 计划任务保活引导 (v2.4)
============================================
由计划任务 OpenAI-DaemonBoot 周期性调用 (每 5 分钟), pythonw 无窗口运行。
职责: 检查 Broker 是否存活, 不在则通过 bootstrap.py 重新拉起。

v2.4 变化: Broker 自身有 root Job KILL_ON_JOB_CLOSE + 指数退避重启,
本脚本退化为"最后防线" — 只负责 Broker 进程整体消失的场景
(例如用户在任务管理器里整组结束后想自愈, 或Broker 崩溃后自动复活)。
"""
import os
import sys
import subprocess
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE, 'logs')
LOG = os.path.join(LOGS_DIR, 'daemon_boot.log')
PYW = os.path.join(BASE, '.venv', 'Scripts', 'pythonw.exe')
PY = os.path.join(BASE, '.venv', 'Scripts', 'python.exe')
BOOTSTRAP = os.path.join(BASE, 'bootstrap.py')
PIDFILE = os.path.join(BASE, 'data', '.daemon.pid')
RUNTIME_STATE = os.path.join(BASE, 'data', 'runtime_state.json')
# GUI 托盘「彻底退出」/ bootstrap stop 时写的抑制标记 (时间戳):
# 抑制窗口内不复活 Broker, 避免用户刚退出服务又被计划任务拉起。
SUPPRESS_FLAG = os.path.join(BASE, 'data', '.gui_exit_suppress')
SUPPRESS_SECONDS = 600


def log(msg):
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write('[%s] %s\n' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                   msg))
    except Exception:
        pass


def suppressed():
    """最近 10 分钟内有用户主动退出标记 → 不拉起 (防退出后立刻复活)。"""
    try:
        with open(SUPPRESS_FLAG, 'r', encoding='utf-8') as f:
            ts = float(f.read().strip() or 0)
        return (datetime.now().timestamp() - ts) < SUPPRESS_SECONDS
    except Exception:
        return False


def pid_alive(pid):
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                                 int(pid))
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        kernel32.CloseHandle(h)
        return ok != 0 and code.value == 259   # STILL_ACTIVE
    except Exception:
        return False


def broker_alive():
    """Broker 存活 = 状态文件里的 pid 仍活着。"""
    try:
        with open(RUNTIME_STATE, 'r', encoding='utf-8') as f:
            import json
            st = json.load(f)
        pid = st.get('broker_pid')
        return bool(pid and pid_alive(pid))
    except Exception:
        pass
    # 回退: 旧 pid 文件 (兼容升级中间态)
    try:
        with open(PIDFILE, 'r') as f:
            pid = f.read().strip()
        return bool(pid and pid_alive(pid))
    except Exception:
        return False


def start_broker():
    """通过 bootstrap.py 拉起 Broker (它自带单实例保护, 幂等)。"""
    exe = PYW if os.path.exists(PYW) else PY
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        subprocess.Popen([exe, BOOTSTRAP, 'start'], cwd=BASE,
                         creationflags=flags,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)
        log('[boot] 已通过 bootstrap.py start 拉起 Broker')
    except Exception as e:
        log('[boot] 拉起失败: %s' % e)


if __name__ == '__main__':
    if broker_alive():
        pass  # Broker 在跑, 无事可做
    elif suppressed():
        # 用户刚通过托盘「退出」/ bootstrap stop 主动关闭 → 尊重退出意图
        log('[boot] 近期有主动退出标记, 抑制自动拉起 (%d 分钟内)' %
            (SUPPRESS_SECONDS // 60))
    else:
        log('[boot] 检测到 Broker 不在运行, 启动它')
        start_broker()
