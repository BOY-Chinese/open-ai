#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
open-ai 独立卸载程序 (uninstall.exe)
=====================================
像正常软件一样的卸载程序, 带简单 UI:
  1. 显示确认界面
  2. 停止网关/daemon/Node 进程, 清理开机自启/计划任务/桌面快捷方式
  3. 彻底删除安装目录 (复制自身到 TEMP 后从外部删除)
  4. 显示「卸载完成」
  5. 删除自身 (uninstall.exe 随安装目录一起删除, 不留残留)

打包: python -m PyInstaller --noconfirm --clean --onefile --windowed ^
        --name "uninstall" --icon "ico/open-ai.ico" uninstaller.py
产出: dist/uninstall.exe
"""
import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

APP_NAME = 'open-ai'


def run_cmd(cmd, timeout=120, capture=True):
    try:
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        p = subprocess.run(cmd, capture_output=capture, text=True,
                           timeout=timeout, shell=False, creationflags=flags)
        return p.returncode, (p.stdout or '') + (p.stderr or '')
    except Exception as e:
        return -1, str(e)


def get_install_dir():
    """uninstall.exe 所在目录 = 安装目录。"""
    return os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False)
                                           else __file__))


class UninstallerApp:
    def __init__(self, root):
        self.root = root
        self.target = get_install_dir()
        self.silent = '--silent' in sys.argv
        root.title('卸载 open-ai')
        root.geometry('440x300')
        root.resizable(False, False)
        # 图标
        try:
            ico = os.path.join(self.target, 'pic', 'open-ai.ico')
            if os.path.exists(ico):
                root.iconbitmap(ico)
        except Exception:
            pass

        # 标题
        ttk.Label(root, text='open-ai 卸载程序', font=('Microsoft YaHei UI', 14, 'bold')) \
            .pack(pady=(18, 6))
        ttk.Label(root, text=f'安装位置: {self.target}', foreground='#666666',
                  wraplength=400).pack(pady=(0, 12))

        # 状态区
        self.status = tk.StringVar(value='就绪')
        ttk.Label(root, textvariable=self.status, foreground='#333333').pack(pady=6)

        # 进度条
        self.pbar = ttk.Progressbar(root, mode='indeterminate')
        self.pbar.pack(fill='x', padx=30, pady=6)

        # 日志区
        self.log_text = tk.Text(root, height=6, state='disabled',
                                font=('Consolas', 9), bg='#f5f5f5')
        self.log_text.pack(fill='both', expand=True, padx=20, pady=8)

        # 按钮
        btn_frame = ttk.Frame(root)
        btn_frame.pack(pady=6)
        self.btn_uninstall = ttk.Button(btn_frame, text='卸载', command=self._start)
        self.btn_uninstall.pack(side='left', padx=6)
        ttk.Button(btn_frame, text='取消', command=root.destroy).pack(side='left', padx=6)

        if self.silent:
            # GUI 已确认, 自动开始
            self.root.after(300, self._start)

    def _log(self, msg):
        self.log_text.configure(state='normal')
        self.log_text.insert('end', msg + '\n')
        self.log_text.see('end')
        self.log_text.configure(state='disabled')
        self.root.update_idletasks()

    def _start(self):
        if not self.silent:
            if not messagebox.askyesno(
                    '确认卸载',
                    f'确定要卸载 open-ai 吗？\n\n安装位置: {self.target}\n\n'
                    '将删除：\n· 全部插件文件\n· 配置、账号与 API 密钥\n'
                    '· 开机自启、计划任务、桌面快捷方式\n\n此操作不可恢复！'):
                return
        self.btn_uninstall.configure(state='disabled')
        self.pbar.start(12)
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            self._log('正在停止进程...')
            self._stop_processes()
            self._log('正在清理开机自启与快捷方式...')
            self._cleanup_shortcuts_and_autostart()
            self._log('正在删除安装目录...')
            ok = self._delete_directory()
            self.pbar.stop()
            self._log('卸载完成！')
            self.status.set('卸载完成')
            self.root.after(0, self._show_done)
        except Exception as e:
            self.pbar.stop()
            self.status.set('卸载失败')
            self._log(f'错误: {e}')
            self.root.after(0, lambda: messagebox.showerror('卸载失败', str(e)))

    def _show_done(self):
        messagebox.showinfo('卸载完成', 'open-ai 已成功卸载！\n本窗口即将关闭。')
        self.root.destroy()

    def _stop_processes(self):
        try:
            esc = self.target.replace('\\', '\\\\').replace("'", "''")
            run_cmd([
                'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-Command',
                (f"Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
                 f"Where-Object {{ ($_.Name -match 'pythonw?\\.exe' -or $_.Name -match '^node\\.exe$') "
                 f"-and $_.CommandLine -match [regex]::Escape('{esc}') }} | "
                 f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}")
            ], timeout=60)
        except Exception:
            pass
        for port in (8000, 18787):
            run_cmd([
                'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-Command',
                (f"Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction SilentlyContinue "
                 f"| ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }}")
            ], timeout=30)

    def _cleanup_shortcuts_and_autostart(self):
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

    def _delete_directory(self):
        """删除整个目录 + 删除自身。复制删除脚本到 TEMP, 从 System32 分离执行。"""
        try:
            # 生成删除脚本: 等本进程退出后删除目录 + 删除 TEMP 副本
            ps_script = os.path.join(os.environ.get('TEMP', '.'),
                                     f'openai_del_{os.getpid()}.ps1')
            del_script = (
                f"Start-Sleep -Seconds 2\n"
                f"if (Test-Path '{self.target}') {{ Remove-Item -LiteralPath '{self.target}' -Recurse -Force -ErrorAction SilentlyContinue }}\n"
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
    root = tk.Tk()
    app = UninstallerApp(root)
    root.mainloop()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        pass