# -*- coding: utf-8 -*-
"""
open-ai 无窗口后台守护 (由 pythonw.exe 运行, 无任何控制台弹窗)
=============================================================
职责:
  1. 保活自愈: 每 60s 检查网关(8000) / Node后端(18787), 掉线自动拉起
  2. 每日签到: 每天一次执行 scripts/signin_all.py (WorkBuddy/TRAE 签到 + 续期)
  3. 日志: 写入 logs/daemon.log, 状态存 data/

启动方式 (无弹窗):
  start_daemon.vbs 或计划任务用 .venv\\Scripts\\pythonw.exe 运行本脚本
"""
import os
import subprocess
import time
import socket
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE, 'logs')
DATA_DIR = os.path.join(BASE, 'data')
LOG = os.path.join(LOGS_DIR, 'daemon.log')
STATE = os.path.join(DATA_DIR, '.daemon_last_signin')
PIDFILE = os.path.join(DATA_DIR, '.daemon.pid')
PY = os.path.join(BASE, '.venv', 'Scripts', 'python.exe')      # 有控制台(拉起网关用,子进程隐藏)
PYW = os.path.join(BASE, '.venv', 'Scripts', 'pythonw.exe')    # 无控制台(跑签到)
MAIN = os.path.join(BASE, 'main.py')
SIGNIN = os.path.join(BASE, 'scripts', 'signin_all.py')
START_HIDDEN_PS1 = os.path.join(BASE, 'start_hidden.ps1')

CHECK_INTERVAL = 60      # 自愈检查间隔(秒)
SIGNIN_HOUR = 9          # 每日签到小时
TRAE_STATE = os.path.join(DATA_DIR, '.trae_signin_state')        # done / pending
TRAE_RETRY_STATE = os.path.join(DATA_DIR, '.daemon_last_trae_retry')  # 上次 TRAE 补试时间戳
TRAE_RETRY_INTERVAL = 30 * 60   # TRAE 9074 繁忙时, 每 30 分钟补试一次


_SINGLE_MUTEX = 'Global\\open-ai-daemon-mutex'
_single_mutex_handle = None


def _win_pid_alive(pid: int) -> bool:
    """Windows 判断 pid 进程是否存活。"""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        kernel32.CloseHandle(h)
        return ok != 0 and code.value == 259  # STILL_ACTIVE
    except Exception:
        return False


def _lockfile_acquire() -> bool:
    """基于 PID 文件 + 进程存活的单实例锁(主判决, 跨 launcher 双进程可靠)。

    用独占创建(O_CREAT|O_EXCL)保证原子性: 已有 daemon 存活则返回 False。
    持有后把自身 pid 写入; 因双进程(解释器)实际 pid 与 launcher 不同, 以解释器为准。
    """
    global _TOMBSTONE
    try:
        import os as _os
        # 读现有 pid 文件, 若对应进程仍存活则说明已有 daemon
        try:
            with open(PIDFILE, 'r') as f:
                old_pid = f.read().strip()
            if old_pid:
                # 兼容 pythonw.exe(x) 双进程: 校验 pid 存活
                if _win_pid_alive(int(old_pid)):
                    return False
        except Exception:
            pass
        # 写回本次持有的 pid (最后的存活者) —— 幂等覆盖
        try:
            with open(PIDFILE, 'w') as f:
                f.write(str(os.getpid()))
        except Exception:
            pass
        return True
    except Exception:
        return True


def acquire_single_instance():
    """单实例锁: mutex(尽力) + PID 文件存活校验(主判决)。重复启动返回 False。"""
    global _single_mutex_handle
    # 1) mutex 尽力而为 (跨权限受限时静默跳过)
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        if not _single_mutex_handle:
            ERROR_ALREADY_EXISTS = 183
            h = kernel32.CreateMutexW(None, False, _SINGLE_MUTEX)
            if h:
                if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
                    kernel32.CloseHandle(h)
                    # mutex 已存在 -> 已有 daemon
                    return False
                _single_mutex_handle = h
    except Exception:
        _single_mutex_handle = None  # mutex 不可用, 交给 pid 文件判定
    # 2) PID 文件 + 存活主判决: 已有存活 daemon 则拒绝
    try:
        with open(PIDFILE, 'r') as f:
            old_pid = f.read().strip()
        if old_pid and _win_pid_alive(int(old_pid)):
            return False
    except Exception:
        pass
    # 3) 没发现存活 daemon, 允许启动, pid 留在 main() 里写
    return True


def release_single_instance():
    global _single_mutex_handle
    if _single_mutex_handle:
        try:
            import ctypes
            ctypes.windll.kernel32.ReleaseMutex(_single_mutex_handle)
            ctypes.windll.kernel32.CloseHandle(_single_mutex_handle)
        except Exception:
            pass
        _single_mutex_handle = None


def log(msg):
    line = '[%s] %s' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg)
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def port_open(port):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(('127.0.0.1', port))
        s.close()
        return True
    except Exception:
        return False


def create_no_window():
    """确保子进程不创建控制台窗口 (Windows)"""
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    if os.name == 'nt':
        return flags
    return 0


def heal():
    """检查并在掉线时拉起网关/node"""
    gw = port_open(8000)
    node = port_open(18787)
    if gw and node:
        return
    log('[heal] 检测到离线: gateway=%s node=%s, 通过 start_hidden.ps1 拉起' % (gw, node))
    try:
        # 用 powershell -WindowStyle Hidden 拉起 start_hidden.ps1 (隐藏窗口)
        ps = subprocess.Popen(
            ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
             '-WindowStyle', 'Hidden', '-File', START_HIDDEN_PS1],
            creationflags=create_no_window(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ps.wait(timeout=120)
        time.sleep(8)
        log('[heal] 拉起完成: gateway=%s node=%s' % (port_open(8000), port_open(18787)))
    except Exception as e:
        log('[heal] 拉起异常: %s' % e)


def run_signin(args):
    """调用 signin_all.py, 隐藏窗口, 记录日志"""
    try:
        proc = subprocess.Popen(
            args,
            cwd=BASE,
            creationflags=create_no_window(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.wait(timeout=300)
        return proc.returncode
    except Exception as e:
        log('[signin] 签到异常: %s' % e)
        return -1


def get_trae_state():
    try:
        with open(TRAE_STATE, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return ''


def get_trae_retry():
    try:
        with open(TRAE_RETRY_STATE, 'r', encoding='utf-8') as f:
            return float(f.read().strip() or 0)
    except Exception:
        return 0.0


def set_trae_retry(ts):
    try:
        with open(TRAE_RETRY_STATE, 'w', encoding='utf-8') as f:
            f.write(str(ts))
    except Exception:
        pass


def do_signin():
    """每日签到 + TRAE 9074 繁忙时当天持续补试"""
    today = datetime.now().strftime('%Y-%m-%d')
    last = get_state()

    if last != today:
        # 当天完整签到尚未执行 (跨天 / 首次)
        log('[signin] 每日签到开始 (%s)' % today)
        rc = run_signin([PYW, SIGNIN])
        log('[signin] 签到完成 (exit=%s)' % rc)
        set_state(today)
        set_trae_retry(time.time())
        return

    # 完整签到已跑过, 检查 TRAE 是否仍 pending (9074 繁忙)
    if get_trae_state() != 'done':
        now = time.time()
        if now - get_trae_retry() >= TRAE_RETRY_INTERVAL:
            log('[signin] TRAE 仍未签上(9074 繁忙), 补试 (--trae-only)')
            rc = run_signin([PYW, SIGNIN, '--trae-only'])
            log('[signin] TRAE 补试完成 (exit=%s)' % rc)
            set_trae_retry(now)


def get_state():
    try:
        with open(STATE, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return ''


def set_state(day):
    try:
        with open(STATE, 'w', encoding='utf-8') as f:
            f.write(day)
    except Exception:
        pass


def main():
    # 单实例保护: 已有 daemon 在跑则直接退出, 避免重复拉起多进程
    if not acquire_single_instance():
        log('[boot] 检测到已有 open-ai daemon 运行, 本进程退出 (防重复)')
        return
    # 确保日志/状态目录存在
    os.makedirs(LOGS_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    # 写 PID 文件 (供 watchdog_boot 判断存活)
    try:
        with open(PIDFILE, 'w') as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
    log('==== open-ai daemon 启动 (pid=%s) ====' % os.getpid())
    # 启动时先执行一轮 自愈 + 补签
    try:
        heal()
        do_signin()
    except Exception as e:
        log('[loop] 首轮异常: %s' % e)
    try:
        while True:
            try:
                heal()
                do_signin()
            except Exception as e:
                log('[loop] 异常: %s' % e)
            time.sleep(CHECK_INTERVAL)
    finally:
        release_single_instance()


if __name__ == '__main__':
    main()