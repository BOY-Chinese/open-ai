#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
open-ai 一键启动器 (open-ai-launcher.exe) —— v2.4 新架构版
==========================================================
双击后自动完成"一键启动", 全程不弹任何终端/控制台窗口:
  1. 首次运行自动构建品牌化运行时 (runtime/, procname.py)
  2. 启动后台服务 (bootstrap.py start, 幂等)
  3. 打开账号管理图形界面 (open-ai-manager.exe / pythonw.exe 无窗口)

exe 放在安装目录根下, 双击即用。自身所在目录即安装目录, 因此兼容
本机项目目录 与 朋友机器的 C:\\open-ai 安装目录, 无需改任何路径。

打包: python -m PyInstaller --noconfirm --clean --onefile --windowed ^
        --name "open-ai-launcher" --icon "ico/open-ai.ico" launcher.py
产出: dist/open-ai-launcher.exe
"""
import os
import subprocess
import sys

# 无窗口运行标志
_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def run_cmd(cmd, timeout=60, capture=True):
    """隐藏控制台运行命令, 返回 (returncode, output)。

    输出解码失败 (VM 控制台编码 GBK/UTF-8 不匹配) 时回落 bytes,
    防止 UnicodeDecodeError 中断启动流程。
    """
    try:
        p = subprocess.run(cmd, capture_output=capture, text=True,
                           timeout=timeout, shell=False, creationflags=_NO_WINDOW)
        return p.returncode, (p.stdout or '') + (p.stderr or '')
    except UnicodeDecodeError:
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                               shell=False, creationflags=_NO_WINDOW)
            out = (p.stdout or b'') + (p.stderr or b'')
            return p.returncode, out.decode('utf-8', errors='replace')
        except Exception as e:
            return -1, str(e)
    except Exception as e:
        return -1, str(e)


def install_dir():
    """open-ai-launcher.exe 所在目录 = 安装根目录。"""
    if getattr(sys, 'frozen', False):
        # PyInstaller onefile 解压到临时目录, 用 exe 真实所在路径
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def start_services(root):
    """用 Broker 托管模式启动后台服务 (bootstrap.py start, 幂等)。"""
    shim = os.path.join(root, 'runtime', 'Scripts', 'open-ai-daemon.exe')
    py = os.path.join(root, '.venv', 'Scripts', 'python.exe')
    bootstrap = os.path.join(root, 'bootstrap.py')
    if os.path.isfile(shim):
        return run_cmd([shim, bootstrap, 'start'], timeout=300)
    if os.path.isfile(py):
        return run_cmd([py, bootstrap, 'start'], timeout=300)
    return False, '未找到 runtime shim 或 .venv (请先运行 start.bat 完成安装)'


def open_account_manager(root):
    """打开管理界面。

    v3.0：优先拉起**桌面端**（desktop/open-ai-desktop.exe，Tauri + React）；
    资源包未带桌面端时回退到品牌化 Python GUI（open-ai-manager.exe / pythonw），
    保证旧包与「仅后端」部署仍可用。
    """
    desktop = os.path.join(root, 'desktop', 'open-ai-desktop.exe')
    if os.path.isfile(desktop):
        try:
            subprocess.Popen([desktop], cwd=os.path.dirname(desktop),
                             creationflags=_NO_WINDOW)
            return True, ''
        except Exception as e:
            # 桌面端拉起失败不应阻断流程：继续尝试 Python GUI
            try:
                logs = os.path.join(root, 'logs')
                os.makedirs(logs, exist_ok=True)
                with open(os.path.join(logs, 'launcher.err.log'), 'a',
                          encoding='utf-8') as f:
                    f.write('桌面端启动失败, 回退 Python GUI: %s\n' % e)
            except Exception:
                pass

    manager = os.path.join(root, 'runtime', 'Scripts', 'open-ai-manager.exe')
    pyw = os.path.join(root, '.venv', 'Scripts', 'pythonw.exe')
    gui = os.path.join(root, 'scripts', 'gui_account_manager.py')
    if not os.path.isfile(gui):
        return False, '未找到 desktop/ 桌面端与 scripts/gui_account_manager.py'
    # 显式 Tcl/Tk 数据目录 (runtime 内副本), 根治 Store 版 init.tcl 探测失败
    env = dict(os.environ)
    tcl = os.path.join(root, 'runtime', 'tcl', 'tcl8.6')
    tk = os.path.join(root, 'runtime', 'tcl', 'tk8.6')
    if os.path.isfile(os.path.join(tcl, 'init.tcl')):
        env['TCL_LIBRARY'] = tcl
    if os.path.isdir(tk):
        env['TK_LIBRARY'] = tk
    try:
        if os.path.isfile(manager):
            exe, args = manager, [gui]
        elif os.path.isfile(pyw):
            exe, args = pyw, [gui]
        else:
            return False, '未找到 open-ai-manager.exe / pythonw.exe'
        subprocess.Popen([exe] + args, cwd=root, env=env,
                         creationflags=_NO_WINDOW)
        return True, ''
    except Exception as e:
        return False, str(e)


def main():
    root = install_dir()

    # 1. 首次运行确保 runtime 构建 (品牌化 shim)
    py = os.path.join(root, '.venv', 'Scripts', 'python.exe')
    if not os.path.isfile(os.path.join(root, 'runtime', 'Scripts',
                                       'open-ai-daemon.exe')) and os.path.isfile(py):
        run_cmd([py, os.path.join(root, 'procname.py')], timeout=300)

    # 2. 后台启动服务 (Broker, 幂等)
    ok, out = start_services(root)
    if not ok:
        try:
            logs = os.path.join(root, 'logs')
            os.makedirs(logs, exist_ok=True)
            with open(os.path.join(logs, 'launcher.err.log'), 'a', encoding='utf-8') as f:
                f.write('服务启动失败: %s\n' % out[-500:])
        except Exception:
            pass

    # 3. 打开账号管理窗口 (托盘常驻)
    open_account_manager(root)

    return 0


if __name__ == '__main__':
    sys.exit(main())
