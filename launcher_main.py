# -*- coding: utf-8 -*-
"""
open-ai-launcher 主程序 (PyInstaller onefile 打包, v3.0)
=========================================================
双击 = 一键启动 open-ai:
  1. 首次运行自动构建品牌化运行时 (runtime/)
  2. 启动后台服务 (幂等, 已在跑直接返回)
  3. 打开桌面端界面 (desktop/open-ai-desktop.exe, Tauri + React)

启动器自身立即退出; 常驻主体是 open-ai-daemon.exe (服务) 与
open-ai-desktop.exe (界面)。

v3.0 变更: 旧的 Python tkinter 界面 (scripts/gui_account_manager.py +
scripts/tray_icon.py + open-ai-manager.exe shim + 账号管理.bat) 已**整条删除**。
界面只有一个入口 —— 桌面端。因此这里不再有任何「回落到 Python GUI」的分支:
桌面端缺失时直接弹窗报错, 而不是悄悄打开一个已经被删掉的旧界面。
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


def _find_desktop():
    """定位桌面端可执行文件。

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


def _spawn(argv, wait=False):
    flags = CREATE_NO_WINDOW if wait else (DETACHED_PROCESS | CREATE_NO_WINDOW)
    p = subprocess.Popen(argv, cwd=ROOT, creationflags=flags,
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

    # 1) 品牌化运行时 (首次运行构建): 以 broker shim 是否就位为判据
    broker = os.path.join(ROOT, 'runtime', 'Scripts', 'open-ai-daemon.exe')
    if not os.path.exists(broker):
        py = os.path.join(ROOT, '.venv', 'Scripts', 'python.exe')
        if not os.path.exists(py):
            _msg('未找到运行环境。\n\n请先在软件目录运行一次 start.bat 完成安装。')
            return 1
        _spawn([py, os.path.join(ROOT, 'procname.py')], wait=True)
        if not os.path.exists(broker):
            _msg('运行时构建失败, 请重试或运行 start.bat。')
            return 1

    # 2) 后台服务 (幂等, Broker 自带单实例锁)
    _spawn([broker, os.path.join(ROOT, 'bootstrap.py'), 'start'])

    # 3) 界面: 桌面端是唯一入口
    desktop = _find_desktop()
    if not desktop:
        _msg('未找到桌面端 desktop\\open-ai-desktop.exe。\n\n'
             '后台服务已启动，但界面无法打开。\n'
             '请重新安装，或联系开发者。')
        return 1
    _spawn([desktop])
    return 0


if __name__ == '__main__':
    sys.exit(main())
