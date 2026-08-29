#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
open-ai 一键启动器 (open-ai-launcher.exe)
==========================================
双击后自动完成"一键启动", 全程不弹任何终端/控制台窗口:
  1. 后台启动网关 (Node 后端 18787 + Python 网关 8000) —— 复用 start_hidden.ps1, 窗口隐藏
  2. 打开账号管理图形界面 (gui_account_manager.py, 用 pythonw.exe 无窗口运行)

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
    """隐藏控制台运行命令, 返回 (returncode, output)。"""
    try:
        p = subprocess.run(cmd, capture_output=capture, text=True,
                           timeout=timeout, shell=False, creationflags=_NO_WINDOW)
        return p.returncode, (p.stdout or '') + (p.stderr or '')
    except Exception as e:
        return -1, str(e)


def install_dir():
    """open-ai-launcher.exe 所在目录 = 安装根目录。"""
    if getattr(sys, 'frozen', False):
        # PyInstaller onefile 解压到临时目录, 用 exe 真实所在路径
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def start_gateway_hidden(root):
    """用 PowerShell 隐藏窗口执行 start_hidden.ps1, 后台启动网关 + Node 后端。"""
    ps1 = os.path.join(root, 'start_hidden.ps1')
    if not os.path.isfile(ps1):
        return False, '未找到 start_hidden.ps1'
    # 隐藏窗口 + 隐藏 PowerShell 控制台
    rc, out = run_cmd(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                       '-WindowStyle', 'Hidden', '-File', ps1], timeout=300)
    return rc == 0, out


def open_account_manager(root):
    """用 pythonw.exe 打开账号管理 GUI (无控制台窗口)。"""
    pyw = os.path.join(root, '.venv', 'Scripts', 'pythonw.exe')
    if not os.path.isfile(pyw):
        pyw = 'pythonw.exe'  # 兜底: 用系统 pythonw
    gui = os.path.join(root, 'scripts', 'gui_account_manager.py')
    if not os.path.isfile(gui):
        return False, '未找到 scripts/gui_account_manager.py'
    try:
        subprocess.Popen([pyw, gui], cwd=os.path.join(root, 'scripts'),
                         creationflags=_NO_WINDOW)
        return True, ''
    except Exception as e:
        return False, str(e)


def main():
    root = install_dir()

    # 1. 后台启动网关 (隐藏窗口)
    ok, out = start_gateway_hidden(root)
    if not ok:
        # 网关启动失败: 写错误日志, 但仍尝试打开账号管理
        try:
            logs = os.path.join(root, 'logs')
            os.makedirs(logs, exist_ok=True)
            with open(os.path.join(logs, 'launcher.err.log'), 'a', encoding='utf-8') as f:
                f.write('网关启动失败: %s\n' % out[-500:])
        except Exception:
            pass

    # 2. 打开账号管理窗口
    open_account_manager(root)

    return 0


if __name__ == '__main__':
    sys.exit(main())
