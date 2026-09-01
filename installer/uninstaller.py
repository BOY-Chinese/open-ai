#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
open-ai 卸载程序 (uninstall.exe) —— v2.5 新架构版
==================================================
职责:
  1. 停止全部 open-ai 进程 (Broker/gateway/trae/品牌化 shim/Node)
  2. 清理开机自启 / 计划任务 / 桌面快捷方式
  3. 删除安装目录 (含 config.json 等全部数据, 不可恢复)

打包: python -m PyInstaller --noconfirm --clean --onefile --windowed ^
        --name "uninstall" --icon "ico/open-ai.ico" uninstaller.py
产出: dist/uninstall.exe
"""
import ctypes
import os
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def run_cmd(cmd, timeout=60, capture=True):
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


def _pid_alive(pid):
    try:
        k = ctypes.WinDLL('kernel32')
        k.OpenProcess.restype = ctypes.c_void_p
        k.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        h = k.OpenProcess(0x1000, False, int(pid))
        if h:
            k.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


class UninstallerApp:
    def __init__(self, root, target):
        self.root = root
        self.target = target
        root.title('卸载 open-ai')
        root.geometry('520x300')
        root.resizable(False, False)
        try:
            root.iconbitmap(default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                 'ico', 'open-ai.ico'))
        except Exception:
            pass

        ttk.Label(root, text='正在卸载 open-ai...',
                  font=('Microsoft YaHei UI', 12, 'bold')).pack(pady=(16, 4))
        self.status = tk.StringVar(value='准备中...')
        ttk.Label(root, textvariable=self.status,
                  foreground='#666666').pack(pady=(0, 8))
        self.pbar = ttk.Progressbar(root, length=420, mode='determinate')
        self.pbar.pack(pady=8)
        self.log = tk.Text(root, height=8, state='disabled',
                           font=('Consolas', 9))
        self.log.pack(fill='both', expand=True, padx=16, pady=(0, 16))

        threading.Thread(target=self._worker, daemon=True).start()

    def _log(self, msg):
        self.log.configure(state='normal')
        self.log.insert('end', msg + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def _set_status(self, msg, pct=None):
        self.status.set(msg)
        if pct is not None:
            self.pbar['value'] = pct
        self.root.update_idletasks()

    def _worker(self):
        ok = False
        try:
            self._set_status('停止全部 open-ai 进程...', 10)
            self._stop_processes()
            self._set_status('清理自启 / 计划任务 / 快捷方式...', 30)
            self._cleanup_shortcuts_and_autostart()
            self._set_status('删除安装目录...', 60)
            ok = self._delete_directory()
        except Exception as e:
            self._log(f'卸载异常: {e}')
        finally:
            self.root.after(0, lambda: self._finish(ok))

    def _stop_processes(self):
        """停止安装目录相关的全部 open-ai 进程 (品牌化 shim + python/node)。"""
        try:
            esc = self.target.replace('\\', '\\\\').replace("'", "''")
            run_cmd([
                'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-Command',
                (f"Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
                 f"Where-Object {{ $_.CommandLine -match [regex]::Escape('{esc}') "
                 f"-and ($_.Name -match '^(open-ai|python|pythonw|node)\\.exe$' "
                 f"-or $_.Name -match '^open-ai-') }} | "
                 f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}")
            ], timeout=60)
        except Exception:
            pass
        # Job Object 兜底: 终止 open-ai 的 root/leaf jobs (整树清零)
        try:
            root = self.target
            if root not in sys.path:
                sys.path.insert(0, root)
            import jobmgmt
            for name in ('open-ai.gateway', 'open-ai.trae', 'open-ai.task',
                         'open-ai.tree'):
                try:
                    j = jobmgmt.open_job(name)
                    if j:
                        j.terminate()
                        j.close()
                except Exception:
                    pass
        except Exception:
            pass
        # 端口兜底
        for port in (8000, 18787):
            run_cmd([
                'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-Command',
                (f"Get-NetTCPConnection -State Listen -LocalPort {port} "
                 f"-ErrorAction SilentlyContinue "
                 f"| ForEach-Object {{ Stop-Process -Id $_.OwningProcess "
                 f"-Force -ErrorAction SilentlyContinue }}")
            ], timeout=30)
        time.sleep(2)

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
            "  if ($t) { Unregister-ScheduledTask -TaskName $task -Confirm:$false "
            "  -ErrorAction SilentlyContinue } }"
        )
        run_cmd(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                 '-Command', ps], timeout=60)
        for task in ('OpenAI-Watchdog', 'OpenAI-DaemonBoot'):
            run_cmd(['schtasks', '/delete', '/tn', task, '/f'], timeout=30)

    def _delete_directory(self):
        """删除安装目录, 返回成功与否。"""
        try:
            if os.path.exists(self.target):
                shutil.rmtree(self.target, ignore_errors=False)
            return not os.path.exists(self.target)
        except Exception:
            return False

    def _finish(self, ok):
        if ok:
            self._log('✓ 卸载完成')
            messagebox.showinfo('卸载完成',
                                'open-ai 已彻底卸载。\n\n'
                                '（本卸载程序正在退出...）')
        else:
            self._log('✗ 部分文件被占用, 卸载未完全成功')
            messagebox.showerror(
                '卸载未完全',
                '部分文件被占用 (可能仍有进程在运行)。\n'
                '请关闭相关程序后重试, 或手动删除安装目录。')
        self.root.destroy()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--silent':
        # 静默卸载 (GUI 设置页调用): 不弹确认, 直接停进程+删目录
        root = os.path.dirname(os.path.abspath(sys.executable))
        app = UninstallerApp.__new__(UninstallerApp)
        try:
            app._stop_processes()
            app._cleanup_shortcuts_and_autostart()
            app._delete_directory()
        except Exception:
            pass
        return 0
    target = os.path.dirname(os.path.abspath(sys.executable))
    root = tk.Tk()
    UninstallerApp(root, target)
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
