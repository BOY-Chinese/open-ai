#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
open-ai 独立卸载程序 (uninstall.exe)
=====================================
像正常软件一样提供独立的卸载 exe。由安装器放入安装目录, 或由账号管理GUI调用。
功能:
  1. 停止网关/daemon/Node 进程
  2. 移除开机自启、计划任务、桌面快捷方式
  3. 彻底删除整个安装目录 (通过复制自身到 TEMP 后从外部删除)

打包: python -m PyInstaller --noconfirm --clean --onefile --windowed ^
        --name "uninstall" --icon "ico/open-ai.ico" uninstaller.py
产出: dist/uninstall.exe
"""
import os
import shutil
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox

APP_NAME = 'open-ai'


def run_cmd(cmd, timeout=120, capture=True):
    try:
        p = subprocess.run(cmd, capture_output=capture, text=True,
                           timeout=timeout, shell=False)
        return p.returncode, (p.stdout or '') + (p.stderr or '')
    except Exception as e:
        return -1, str(e)


def get_install_dir():
    """uninstall.exe 所在目录 = 安装目录。"""
    return os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False)
                                           else __file__))


def stop_processes(target_dir):
    """停止目标目录下的网关/daemon/Node 进程, 及占用端口进程。"""
    try:
        # 杀目标目录下的 python/node
        esc = target_dir.replace('\\', '\\\\').replace("'", "''")
        rc, out = run_cmd([
            'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-Command',
            (f"Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
             f"Where-Object {{ ($_.Name -match 'pythonw?\\.exe' -or $_.Name -match '^node\\.exe$') "
             f"-and $_.CommandLine -match [regex]::Escape('{esc}') }} | "
             f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}")
        ], timeout=60)
    except Exception:
        pass
    # 停占用端口
    for port in (8000, 18787):
        run_cmd([
            'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-Command',
            (f"Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction SilentlyContinue "
             f"| ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }}")
        ], timeout=30)


def cleanup_shortcuts_and_autostart():
    """移除桌面快捷方式与开机自启。"""
    ps = (
        "$startup = [Environment]::GetFolderPath('Startup'); "
        "$autostart = Join-Path $startup 'open-ai-autostart.bat'; "
        "if (Test-Path $autostart) { Remove-Item $autostart -Force }; "
        "$desktop = [Environment]::GetFolderPath('Desktop'); "
        "foreach ($lnk in @('open-ai.lnk','open-ai 账号管理.lnk','open-ai 启动网关.lnk')) { "
        "  $p = Join-Path $desktop $lnk; if (Test-Path $p) { Remove-Item $p -Force } }; "
        "foreach ($task in @('OpenAI-Watchdog','OpenAI-DaemonBoot')) { "
        "  $t = Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue; "
        "  if ($t) { Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue } }"
    )
    run_cmd(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps], timeout=60)


def delete_directory(target_dir):
    """删除整个目录。复制自身到 TEMP, 用分离进程从 System32 删除, 可删自身所在目录。"""
    try:
        # 复制 uninstall.exe 到 TEMP
        temp_exe = os.path.join(os.environ.get('TEMP', '.'),
                                f'openai_uninstall_{os.getpid()}.exe')
        if getattr(sys, 'frozen', False):
            shutil.copy(sys.executable, temp_exe)
        # 生成删除脚本并启动分离进程
        ps_script = os.path.join(os.environ.get('TEMP', '.'),
                                 f'openai_del_{os.getpid()}.ps1')
        # 先等本进程退出再删, 避免 self-occupation
        del_script = (
            f"Start-Sleep -Seconds 2\n"
            f"if (Test-Path '{target_dir}') {{ Remove-Item -LiteralPath '{target_dir}' -Recurse -Force -ErrorAction SilentlyContinue }}\n"
            f"Start-Sleep -Seconds 1\n"
            f"if (Test-Path '{temp_exe}') {{ Remove-Item -LiteralPath '{temp_exe}' -Force -ErrorAction SilentlyContinue }}\n"
            f"Remove-Item -LiteralPath '{ps_script}' -Force -ErrorAction SilentlyContinue\n"
        )
        with open(ps_script, 'w', encoding='utf-8') as f:
            f.write(del_script)
        subprocess.Popen([
            'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', ps_script
        ], cwd=os.path.join(os.environ.get('WINDIR', 'C:\\Windows'), 'System32'),
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return True
    except Exception:
        return False


def main():
    target = get_install_dir()
    # 支持 --silent: 由 GUI 启动时已确认过, 不再二次询问
    silent = '--silent' in sys.argv
    if not silent:
        # 确认卸载
        if not messagebox.askyesno(
                '卸载 open-ai',
                f'确定要卸载 open-ai 吗？\n\n安装位置: {target}\n\n'
                '将删除：\n· 全部插件文件\n· 配置、账号与 API 密钥\n'
                '· 开机自启、计划任务、桌面快捷方式\n\n此操作不可恢复！'):
            return

    # 执行卸载
    stop_processes(target)
    cleanup_shortcuts_and_autostart()
    ok = delete_directory(target)

    if not silent:
        if ok:
            messagebox.showinfo('卸载完成',
                                'open-ai 已卸载，正在删除安装目录...\n'
                                '本窗口即将关闭。')
        else:
            messagebox.showerror('卸载失败', '删除目录失败，请手动删除安装目录。')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        pass