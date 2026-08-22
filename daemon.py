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
    while True:
        try:
            heal()
            do_signin()
        except Exception as e:
            log('[loop] 异常: %s' % e)
        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    main()