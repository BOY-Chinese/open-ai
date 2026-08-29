# -*- coding: utf-8 -*-
"""
open-ai 无窗口守护保活引导 (Watchdog Boot)
==========================================
由计划任务 OpenAI-Watchdog 周期性调用 (每 5 分钟), 用 pythonw 运行, 无任何弹窗。
职责:
  1. 若后台守护 daemon.py 未在运行, 用 pythonw 拉起它 (无窗口)
  2. 立即退出

配合 daemon.py (常驻) + 计划任务, 实现: daemon 崩溃后由本脚本自动重启, 全程无控制台弹窗。
"""
import os
import subprocess
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE, 'logs')
DATA_DIR = os.path.join(BASE, 'data')
LOG = os.path.join(LOGS_DIR, 'daemon_boot.log')
PYW = os.path.join(BASE, '.venv', 'Scripts', 'pythonw.exe')
DAEMON = os.path.join(BASE, 'daemon.py')
PIDFILE = os.path.join(DATA_DIR, '.daemon.pid')


_SINGLE_MUTEX = 'Global\\open-ai-daemon-mutex'


def daemon_single_mutex_taken():
    """返回 True 表示已有一个 daemon 持有互斥体(即在运行), 用于 watchdog 判定。"""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        ERROR_ALREADY_EXISTS = 183
        h = kernel32.CreateMutexW(None, False, _SINGLE_MUTEX)
        if h:
            err = kernel32.GetLastError()
            taken = (err == ERROR_ALREADY_EXISTS)
            kernel32.CloseHandle(h)
            return taken
    except Exception:
        pass
    return False


def log(msg):
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write('[%s] %s\n' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg))
    except Exception:
        pass


def pid_alive(pid):
    """Windows: 判断 pid 进程是否存活"""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False  # 进程不存在或无权限
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        kernel32.CloseHandle(h)
        # STILL_ACTIVE = 259
        return ok != 0 and code.value == 259
    except Exception:
        return True  # 无法判断时保守认为存活


def daemon_running():
    # 仅依据 PID 文件判断; 若文件被删或进程消失则视为未运行, 交由 start_daemon 重新拉起
    try:
        with open(PIDFILE, 'r') as f:
            pid = f.read().strip()
        if pid and pid_alive(pid):
            return True
    except Exception:
        pass
    return False


def start_daemon():
    try:
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        proc = subprocess.Popen(
            [PYW, DAEMON],
            cwd=BASE,
            creationflags=flags,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # daemon 会自己刷新 PID 文件, 这里等待片刻不阻塞调用方
        log('[boot] 已用 pythonw 拉起 daemon (pid=%s)' % proc.pid)
    except Exception as e:
        log('[boot] 拉起失败: %s' % e)


if __name__ == '__main__':
    # 结合 pid 文件 + 互斥体双保险: 任一判定已在运行则不重复拉起
    if daemon_running() or daemon_single_mutex_taken():
        pass  # 已在运行
    else:
        log('[boot] 检测到 daemon 未运行, 启动它')
        start_daemon()