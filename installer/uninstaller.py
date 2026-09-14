#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
open-ai 独立卸载程序 (uninstall.exe)
=====================================
简洁可靠的卸载程序:
  1. 显示确认界面 (记录安装位置)
  2. 停止网关/daemon/Node 进程, 清理开机自启/计划任务/桌面快捷方式
  3. 同步删除安装目录 (除自身外), 删除失败如实提示
  4. 自身 (uninstall.exe) 通过 TEMP 副本延迟删除, 不留残留

打包: python -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin ^
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
    """隐藏控制台运行命令, 返回 (returncode, output)。"""
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
                                font=('Consolas', 9), bg='#ffffff', fg='#374151',
                                relief='flat', borderwidth=1)
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
            self._log('等待进程完全退出...')
            self._wait_processes_gone()
            self._log('正在清理开机自启与快捷方式...')
            self._cleanup_shortcuts_and_autostart()
            self._log('正在删除安装目录...')
            residual = self._delete_directory()
            self.pbar.stop()
            if residual:
                self.status.set('未完全卸载')
                self._log('⚠ 有文件删除失败:')
                for r in residual:
                    self._log(f'  - {r}')
                self.root.after(0, lambda: messagebox.showwarning(
                    '未完全卸载',
                    '部分文件删除失败(可能正被其他程序占用)。\n'
                    '请关闭 open-ai 相关窗口后重试，或手动删除剩余目录。'))
                return
            self._log('卸载完成！')
            self.status.set('卸载完成')
            self.root.after(0, self._show_done)
        except Exception as e:
            self.pbar.stop()
            self.status.set('卸载失败')
            self._log(f'错误: {e}')
            self.root.after(0, lambda: messagebox.showerror('卸载失败', str(e)))

    def _show_done(self):
        if self.silent:
            # 静默模式 (GUI 调用): 直接退出, 让延迟删除脚本尽快清掉 uninstall.exe 自身
            self.root.destroy()
            return
        messagebox.showinfo('卸载完成', 'open-ai 已成功卸载！\n本窗口即将关闭。')
        self.root.destroy()

    def _matching_process_names(self):
        """列出仍以安装目录为命令行运行的 python/node/launcher 进程描述。"""
        try:
            esc = self.target.replace('\\', '\\\\').replace("'", "''")
            code, out = run_cmd([
                'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-Command',
                (f"Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
                 f"Where-Object {{ (($_.Name -match 'pythonw?\\.exe' -or $_.Name -match '^node\\.exe$' "
                 f"-or $_.Name -match '^open-ai-launcher\\.exe$') "
                 f"-and $_.CommandLine -match [regex]::Escape('{esc}')) "
                 f"-or $_.Name -match '^open-ai(-[a-z]+)?\\.exe$' }} | "
                 f"ForEach-Object {{ \"$($_.Name)#$($_.ProcessId)\" }}")
            ], timeout=30)
            return [x.strip() for x in (out or '').splitlines() if x.strip()]
        except Exception:
            return []

    def _wait_processes_gone(self, timeout=15):
        """轮询等待安装目录相关进程真正退出 (Stop-Process 后 exe 释放句柄需要时间)。
        这是 .venv 目录删不掉的主因: 命令下发后进程还没退出, 文件仍被占用。"""
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            left = self._matching_process_names()
            if not left:
                time.sleep(1)  # 再缓冲 1s, 等 Windows 释放文件句柄
                return
            time.sleep(0.5)
        # 超时则再强杀一轮并等待
        self._stop_processes()

    def _stop_processes(self):
        """停止匹配安装目录的 python/node 进程, 并释放网关/Node 端口。"""
        try:
            esc = self.target.replace('\\', '\\\\').replace("'", "''")
            run_cmd([
                'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-Command',
                (f"Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
                 f"Where-Object {{ (($_.Name -match 'pythonw?\\.exe' -or $_.Name -match '^node\\.exe$' "
                 f"-or $_.Name -match '^open-ai-launcher\\.exe$') "
                 f"-and $_.CommandLine -match [regex]::Escape('{esc}')) "
                 f"-or $_.Name -match '^open-ai(-[a-z]+)?\\.exe$' }} | "
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
        # 等句柄释放, 避免删除时文件被占用
        import time
        time.sleep(2)

    def _cleanup_shortcuts_and_autostart(self):
        """清理开机自启文件、桌面快捷方式、计划任务。"""
        # 自启文件 + 桌面快捷方式
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
        run_cmd(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                 '-Command', ps], timeout=60)
        # 计划任务兜底: 用 schtasks 命令行再删一次
        for task in ('OpenAI-Watchdog', 'OpenAI-DaemonBoot'):
            run_cmd(['schtasks', '/delete', '/tn', task, '/f'], timeout=30)

    def _delete_directory(self):
        """同步删除安装目录(除自身外), 返回残留文件列表(空=全部删除成功)。

        自身 uninstall.exe 通过 TEMP 副本延迟删除, 不留残留:
        - 先带重试删除其余文件 (瞬时占用最多再等 10s 重试)
        - 仍失败的项写进延迟脚本兜底再删 (等自身完全退出后)
        - 延迟脚本用重试循环等自身退出后再删自身 + 整目录, 直到成功
        """
        if not os.path.isdir(self.target):
            return []
        self_exe = os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__)
        errors = []
        # 1) 同步删除目录内所有项 (跳过自身), 失败项带重试 (进程退出/句柄释放竞态)
        for name in sorted(os.listdir(self.target)):
            p = os.path.join(self.target, name)
            if os.path.normcase(p) == os.path.normcase(self_exe):
                continue
            ok = False
            last_err = ''
            for attempt in range(4):  # 首次 + 3 次重试, 共约 10s
                try:
                    if os.path.isdir(p) and not os.path.islink(p):
                        shutil.rmtree(p, ignore_errors=False)
                    else:
                        os.remove(p)
                    ok = True
                    break
                except Exception as e:
                    last_err = str(e)
                    import time
                    time.sleep(2.5)
            if not ok:
                errors.append(f'{name}: {last_err}')
        # 2) 部署延迟删除脚本: 等自身退出后重试删除自身 + 清理残留/空目录
        try:
            ps_script = os.path.join(os.environ.get('TEMP', '.'),
                                     f'openai_del_{os.getpid()}.ps1')
            # 延迟脚本: 先等 uninstall 进程退出 (最多 30s), 再循环重试删除
            # 失败项/自身/整目录 (每 2s 一轮, 最多 60s), 应对未释放的文件占用
            leftover = [os.path.join(self.target, e.split(':', 1)[0]) for e in errors]
            leftover_lines = '\n'.join(
                f"  Remove-Item -LiteralPath '{p}' -Recurse -Force -ErrorAction SilentlyContinue"
                for p in leftover)
            del_script = (
                f"$self='{self_exe}'\n"
                f"$dir='{self.target}'\n"
                f"$me=$PID\n"
                # 等待 uninstall 进程退出 (自身除外), 最多 30s
                f"for ($i=0; $i -lt 15; $i++) {{\n"
                f"  $p = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
                f"Where-Object {{ $_.Name -match 'uninstall.*\\.exe$' -and $_.ProcessId -ne $me }}\n"
                f"  if (-not $p) {{ break }}\n"
                f"  Start-Sleep -Seconds 2\n"
                f"}}\n"
                # 循环重试删除, 直到目录消失 (最长 60s)
                f"for ($i=0; $i -lt 30; $i++) {{\n"
                f"{leftover_lines}\n"
                f"  if (Test-Path $self) {{ Remove-Item -LiteralPath $self -Force -ErrorAction SilentlyContinue }}\n"
                f"  if (Test-Path $dir) {{ Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue }}\n"
                f"  if (-not (Test-Path $dir)) {{ break }}\n"
                f"  Start-Sleep -Seconds 2\n"
                f"}}\n"
                # 兜底: 清掉可能残留的空目录 (.venv/logs 等)
                f"if (Test-Path $dir) {{\n"
                f"  Get-ChildItem -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue | "
                f"Sort-Object {{ $_.FullName.Length }} -Descending | "
                f"Where-Object {{ $_.PSIsContainer -and -not (Get-ChildItem -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue) }} | "
                f"Remove-Item -Force -ErrorAction SilentlyContinue\n"
                f"  if (-not (Get-ChildItem -LiteralPath $dir -Force -ErrorAction SilentlyContinue)) {{ "
                f"Remove-Item -LiteralPath $dir -Force -ErrorAction SilentlyContinue }}\n"
                f"}}\n"
                f"Remove-Item -LiteralPath '{ps_script}' -Force -ErrorAction SilentlyContinue\n"
            )
            with open(ps_script, 'w', encoding='utf-8') as f:
                f.write(del_script)
            subprocess.Popen([
                'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-File', ps_script
            ], cwd=os.path.join(os.environ.get('WINDIR', 'C:\\Windows'), 'System32'),
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except Exception as e:
            errors.append(f'自身删除脚本: {e}')
        return errors


def main():
    root = tk.Tk()
    app = UninstallerApp(root)
    root.mainloop()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        pass