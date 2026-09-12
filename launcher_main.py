# -*- coding: utf-8 -*-
"""
open-ai-launcher 主程序 (PyInstaller onefile 打包, v2.4)
=========================================================
双击 = 一键启动 open-ai:
  1. 首次运行自动构建品牌化运行时 (runtime/)
  2. 启动后台服务 (幂等, 已在跑直接返回)
  3. 打开管理界面 (显式 Tcl/Tk 数据目录, 根治 init.tcl 探测失败)
启动器自身立即退出; 常驻主体是 open-ai-daemon.exe (服务) 与
open-ai-manager.exe (界面)。
"""
import os
import sys
import ctypes
import subprocess

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008

ROOT = os.path.dirname(os.path.abspath(sys.executable))


def _msg(text):
    try:
        ctypes.windll.user32.MessageBoxW(None, text, 'open-ai', 0x40)
    except Exception:
        pass


def _gui_env():
    """GUI 子进程环境: 显式 Tcl/Tk 数据目录 (runtime 内副本)。
    根治偶发 'Can't find a usable init.tcl'。"""
    env = dict(os.environ)
    tcl = os.path.join(ROOT, 'runtime', 'tcl', 'tcl8.6')
    tk = os.path.join(ROOT, 'runtime', 'tcl', 'tk8.6')
    if os.path.isfile(os.path.join(tcl, 'init.tcl')):
        env['TCL_LIBRARY'] = tcl
    if os.path.isdir(tk):
        env['TK_LIBRARY'] = tk
    return env


def _find_desktop():
    """定位 Tauri 桌面端可执行文件。

    两个候选覆盖两种布局，运行时不必区分「本机项目目录」与「安装目录」：
      - desktop/open-ai-desktop.exe                    —— 安装目录 / 本机构建同步副本
      - desktop-ui/src-tauri/target/release/...        —— 本机 cargo 构建产物
    """
    for rel in (os.path.join('desktop', 'open-ai-desktop.exe'),
                os.path.join('desktop-ui', 'src-tauri', 'target', 'release',
                             'open-ai-desktop.exe')):
        p = os.path.join(ROOT, rel)
        if os.path.isfile(p):
            return p
    return None


def _spawn(argv, wait=False, env=None):
    flags = CREATE_NO_WINDOW if wait else (DETACHED_PROCESS | CREATE_NO_WINDOW)
    p = subprocess.Popen(argv, cwd=ROOT, creationflags=flags, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)
    if wait:
        try:
            p.wait(timeout=300)
        except Exception:
            pass
    return p


def main():
    if os.name != 'nt':
        return 1
    # 1) 品牌化运行时 (首次运行构建)
    manager = os.path.join(ROOT, 'runtime', 'Scripts', 'open-ai-manager.exe')
    if not os.path.exists(manager):
        py = os.path.join(ROOT, '.venv', 'Scripts', 'python.exe')
        if not os.path.exists(py):
            _msg('未找到运行环境。\n\n请先在软件目录运行一次 start.bat 完成安装。')
            return 1
        _spawn([py, os.path.join(ROOT, 'procname.py')], wait=True)
        if not os.path.exists(manager):
            _msg('运行时构建失败, 请重试或运行 start.bat。')
            return 1
    # 2) 后台服务 (幂等)
    broker = os.path.join(ROOT, 'runtime', 'Scripts', 'open-ai-daemon.exe')
    if os.path.exists(broker):
        _spawn([broker, os.path.join(ROOT, 'bootstrap.py'), 'start'])
    # 3) 界面：v3.0 起优先桌面端 (Tauri)，找不到才回落 Python tkinter GUI
    #
    # 为什么必须放在最前面：v3.0 的界面已整体迁移到 desktop-ui (Tauri + React)，
    # Python GUI 只是兼容兜底。此前这里写死拉起 gui_account_manager.py，
    # 导致「双击快捷方式还是旧界面」(本机实测反馈)。
    desktop = _find_desktop()
    if desktop:
        _spawn([desktop], env=None)
        return 0
    # 4) 兼容兜底：Python GUI (显式 Tcl/Tk 环境)
    _spawn([manager, os.path.join(ROOT, 'scripts', 'gui_account_manager.py')],
           env=_gui_env())
    return 0


if __name__ == '__main__':
    sys.exit(main())
