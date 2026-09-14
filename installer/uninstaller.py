#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
open-ai 独立卸载程序 (uninstall.exe) —— 参考 2.3 版可靠卸载逻辑
==============================================================
简洁可靠的卸载程序 (对齐 D:\\MyCode\\项目副本\\open-ai-github-副本-2.3 的
卸载实现, 并适配 v2.4 新架构的品牌化进程):

  1. 显示确认界面 (记录安装位置)
  2. 停止网关/daemon/Node/品牌化 shim 进程, 清理开机自启/计划任务/桌面快捷方式
  3. 轮询等待进程真正退出 (防 .venv 目录被占用删不掉)
  4. 同步删除安装目录 (除自身及其依赖目录外), 删除失败如实提示
  5. 自身 (uninstall.exe) 通过 TEMP 副本延迟删除, 不留残留

打包 (onedir: 无 _MEI 临时目录 → 根治退出时
"Failed to remove temporary directory" 弹窗; 且启动更快; 不带 --uac-admin, GUI「一键卸载」以普通进程启动即免 UAC):
  python -m PyInstaller --noconfirm --clean --noupx --windowed ^
        --contents-directory "uninstall_internal" ^
        --name "uninstall" --icon "ico/open-ai.ico" uninstaller.py
产出: dist/uninstall/  (uninstall.exe + uninstall_internal/)
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
    """隐藏控制台运行命令, 返回 (returncode, output)。

    输出解码失败 (控制台编码 GBK/UTF-8 不匹配) 时回落 bytes, 防中断。
    """
    try:
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        p = subprocess.run(cmd, capture_output=capture, text=True,
                           timeout=timeout, shell=False, creationflags=flags)
        return p.returncode, (p.stdout or '') + (p.stderr or '')
    except UnicodeDecodeError:
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                               shell=False, creationflags=flags)
            out = (p.stdout or b'') + (p.stderr or b'')
            return p.returncode, out.decode('utf-8', errors='replace')
        except Exception as e:
            return -1, str(e)
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
        try:
            self.log_text.configure(state='normal')
            self.log_text.insert('end', msg + '\n')
            self.log_text.see('end')
            self.log_text.configure(state='disabled')
            self.root.update_idletasks()
        except Exception:
            pass

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

    # ---------------- 进程停止 (对齐 2.3, 适配品牌化 shim) ----------------
    def _process_match_ps(self, target, action):
        """构造进程匹配 PowerShell 命令。

        用 -like 通配符 (而非 -match + [regex]::Escape): 实测 -match 与
        Escaped 路径组合在 PowerShell 里对品牌化进程 (open-ai-daemon.exe
        等) 会匹配失败, -like '*目标*' 稳定命中。
        action: 查询(输出 Name#PID) 或 停止(Stop-Process)。
        """
        t = target.replace("'", "''")
        where = (
            f"($_.Name -like 'open-ai*' -or $_.Name -like 'python*' "
            f"-or $_.Name -eq 'node.exe') -and "
            f"$_.CommandLine -like '*{t}*'"
        )
        if action == 'list':
            body = f"ForEach-Object {{ \"$($_.Name)#$($_.ProcessId)\" }}"
        else:
            body = ("ForEach-Object { Stop-Process -Id $_.ProcessId "
                    "-Force -ErrorAction SilentlyContinue }")
        return (
            f"Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
            f"Where-Object {{ {where} }} | {body}"
        )

    def _matching_process_names(self):
        """列出仍以安装目录为命令行运行的 python/node/launcher/品牌化 shim 进程。"""
        try:
            code, out = run_cmd([
                'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-Command', self._process_match_ps(self.target, 'list')
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
        """停止匹配安装目录的 python/node/品牌化 shim 进程, 并释放端口。"""
        try:
            run_cmd([
                'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-Command', self._process_match_ps(self.target, 'stop')
            ], timeout=60)
        except Exception:
            pass
        # Job Object 兜底 (新架构: 品牌化进程可能不匹配 CommandLine 时)
        try:
            if self.target not in sys.path:
                sys.path.insert(0, self.target)
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

    # ---------------- 清理 (对齐 2.3) ----------------
    def _cleanup_shortcuts_and_autostart(self):
        """清理开机自启文件、桌面快捷方式、计划任务。"""
        # 自启文件 + 桌面快捷方式
        ps = (
            "$startup = [Environment]::GetFolderPath('Startup'); "
            "$autostart = Join-Path $startup 'open-ai-autostart.bat'; "
            "if (Test-Path $autostart) { Remove-Item $autostart -Force }; "
            "$autostart2 = Join-Path $startup 'open-ai-autostart.vbs'; "
            "if (Test-Path $autostart2) { Remove-Item $autostart2 -Force }; "
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

    # ---------------- 删除目录 (对齐 2.3 延迟删除) ----------------
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
        # onedir 打包 (PyInstaller ≥6): 自身依赖目录 (uninstall_internal/) 与 exe
        # 同级, 运行期间其内 DLL/python DLL 被本进程映射占用, 同步删除必失败。
        # 同步阶段跳过它, 交给延迟脚本在自身完全退出后清理 (整目录递归删除已覆盖)。
        self_internal = ''
        if getattr(sys, 'frozen', False):
            meipass = getattr(sys, '_MEIPASS', '')
            if meipass and os.path.isdir(meipass):
                # onefile: _MEIPASS 在 %TEMP%\_MEIxxxx (不在安装目录内, 无需跳过)
                # onedir : _MEIPASS = <安装目录>\uninstall_internal
                if os.path.dirname(os.path.normpath(meipass)) == \
                        os.path.dirname(os.path.normpath(self_exe)):
                    self_internal = os.path.normpath(meipass)
        errors = []
        # 1) 同步删除目录内所有项 (跳过自身与自身依赖目录), 失败项带重试
        for name in sorted(os.listdir(self.target)):
            p = os.path.join(self.target, name)
            if os.path.normcase(p) == os.path.normcase(self_exe):
                continue
            if self_internal and os.path.normcase(p) == os.path.normcase(self_internal):
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
            leftover = [os.path.join(self.target, e.split(':', 1)[0]) for e in errors]
            if self_internal:
                leftover.append(self_internal)
            leftover_lines = '\n'.join(
                f"  Remove-Item -LiteralPath '{p}' -Recurse -Force -ErrorAction SilentlyContinue"
                for p in leftover)
            del_script = (
                f"$self='{self_exe}'\n"
                f"$dir='{self.target}'\n"
                f"$me=$PID\n"
                f"for ($i=0; $i -lt 15; $i++) {{\n"
                f"  $p = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
                f"Where-Object {{ $_.Name -match 'uninstall.*\\.exe$' -and $_.ProcessId -ne $me }}\n"
                f"  if (-not $p) {{ break }}\n"
                f"  Start-Sleep -Seconds 2\n"
                f"}}\n"
                f"for ($i=0; $i -lt 30; $i++) {{\n"
                f"{leftover_lines}\n"
                f"  if (Test-Path $self) {{ Remove-Item -LiteralPath $self -Force -ErrorAction SilentlyContinue }}\n"
                f"  if (Test-Path $dir) {{ Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue }}\n"
                f"  if (-not (Test-Path $dir)) {{ break }}\n"
                f"  Start-Sleep -Seconds 2\n"
                f"}}\n"
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
    UninstallerApp(root)
    root.mainloop()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        pass
