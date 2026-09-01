#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
open-ai 安装程序 (open-ai-installer.exe) —— v2.5 新架构版
==========================================================
一键安装 open-ai 本地聚合网关 (Broker 托管进程树 + 系统托盘 GUI):

  1. 解压 resources.zip 到安装目录
  2. 保留用户已有 config.json (覆盖安装时备份), 否则生成空壳模板
  3. 检测/自动安装 Python 3.10+ 与 Node.js
  4. 创建 venv + 安装 requirements + Playwright 浏览器
  5. 构建品牌化运行时 (procname.py → runtime/Scripts/open-ai-*.exe)
  6. 创建桌面快捷方式 (指向 open-ai-launcher.exe) + 可选开机自启

打包: python -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin ^
        --name "open-ai-installer" --icon "ico/open-ai.ico" ^
        --add-data "resources.zip;." --add-data "config.shell.json;." ^
        --add-data "ico/open-ai.ico;ico" installer.py
产出: dist/open-ai-installer.exe
"""
import os
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import zipfile
from tkinter import ttk, messagebox

# ============================== 常量 ==============================
DEFAULT_INSTALL_DIR = r'C:\open-ai'
PYTHON_MIN = (3, 10)
PYINSTALLER_DIR = os.path.dirname(os.path.abspath(__file__))

_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def _resource_dir():
    """PyInstaller onefile 运行时资源解压目录 (打包时 --add-data 注入)。"""
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return PYINSTALLER_DIR


RESOURCES_ZIP = os.path.join(_resource_dir(), 'resources.zip')
SHELL_CONFIG = os.path.join(_resource_dir(), 'config.shell.json')


def run_cmd(cmd, timeout=60, capture=True):
    try:
        p = subprocess.run(cmd, capture_output=capture, text=True,
                           timeout=timeout, shell=False, creationflags=_NO_WINDOW)
        return p.returncode, (p.stdout or '') + (p.stderr or '')
    except Exception as e:
        return -1, str(e)


def find_python():
    """查找系统 Python 3.10+。"""
    candidates = ['python', 'py']
    for c in candidates:
        rc, out = run_cmd([c, '--version'], timeout=15)
        if rc == 0:
            ver = out.strip().split()[-1]
            try:
                parts = tuple(int(x) for x in ver.split('.')[:2])
                if parts >= PYTHON_MIN:
                    return c, ver
            except Exception:
                pass
    return None, None


def find_node():
    rc, out = run_cmd(['node', '--version'], timeout=15)
    if rc == 0:
        return 'node', (out.strip() or '?')
    return None, None


# ============================== 安装器 UI ==============================
class InstallerApp:
    def __init__(self, root):
        self.root = root
        root.title('open-ai 一键安装')
        root.geometry('560x380')
        root.resizable(False, False)
        try:
            ico = os.path.join(_resource_dir(), 'ico', 'open-ai.ico')
            if os.path.exists(ico):
                root.iconbitmap(default=ico)
        except Exception:
            pass

        ttk.Label(root, text='open-ai 本地聚合网关 一键安装',
                  font=('Microsoft YaHei UI', 13, 'bold')).pack(pady=(16, 4))
        ttk.Label(root, text='v2.5 (Broker 进程树 + 系统托盘)',
                  foreground='#888888').pack()

        # 安装目录
        dir_frame = ttk.Frame(root)
        dir_frame.pack(fill='x', padx=20, pady=(14, 4))
        ttk.Label(dir_frame, text='安装目录:').pack(side='left')
        self.install_dir = tk.StringVar(value=DEFAULT_INSTALL_DIR)
        ent = ttk.Entry(dir_frame, textvariable=self.install_dir, width=40)
        ent.pack(side='left', fill='x', expand=True, padx=6)
        ttk.Button(dir_frame, text='浏览...', command=self._browse).pack(side='left')

        # 开机自启
        self.autostart = tk.BooleanVar(value=False)
        ttk.Checkbutton(root, text='开机自动运行 (网关 + 守护进程)',
                        variable=self.autostart).pack(anchor='w', padx=24, pady=4)

        # 状态
        self.status = tk.StringVar(value='准备就绪')
        ttk.Label(root, textvariable=self.status,
                  foreground='#666666').pack(pady=(8, 2))
        self.pbar = ttk.Progressbar(root, length=480, mode='determinate')
        self.pbar.pack(pady=6)

        # 日志
        self.log = tk.Text(root, height=8, state='disabled',
                           font=('Consolas', 9))
        self.log.pack(fill='both', expand=True, padx=20, pady=(6, 12))

        # 按钮
        self.btn_install = ttk.Button(root, text='开始安装',
                                      command=self._start_install)
        self.btn_install.pack(pady=(0, 14))

    def _browse(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(title='选择安装目录',
                                    initialdir=self.install_dir.get())
        if d:
            self.install_dir.set(os.path.abspath(d))

    def _log(self, msg):
        self.log.configure(state='normal')
        self.log.insert('end', msg + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')
        self.root.update_idletasks()

    def _set_status(self, msg, pct=None):
        self.status.set(msg)
        if pct is not None:
            self.pbar['value'] = pct
        self.root.update_idletasks()

    # ---------------- 安装流程 ----------------
    def _start_install(self):
        if not os.path.exists(RESOURCES_ZIP):
            messagebox.showerror('错误',
                                 f'未找到资源包: {RESOURCES_ZIP}\n'
                                 '请先运行 build_exe.bat 构建安装器。')
            return
        self.btn_install.configure(state='disabled', text='安装中...')
        threading.Thread(target=self._do_install, daemon=True).start()

    def _do_install(self):
        target = self.install_dir.get().strip()
        if not target:
            raise ValueError('请选择安装目录')
        target = os.path.abspath(target)
        try:
            # 1. 解压资源
            self._set_status('正在解压资源...', 5)
            self._log(f'安装到: {target}')
            os.makedirs(target, exist_ok=True)
            with zipfile.ZipFile(RESOURCES_ZIP) as zf:
                zf.extractall(target)
            self._log(f'✓ 资源解压完成')

            # 2. 生成配置文件 (保留已有 config, 不覆盖!)
            self._set_status('生成配置文件...', 12)
            cfg_path = os.path.join(target, 'config.json')
            if os.path.exists(cfg_path):
                stamp = time.strftime('%Y%m%d_%H%M%S')
                bak = os.path.join(target, f'config.json.bak-{stamp}')
                try:
                    shutil.copy2(cfg_path, bak)
                    self._log(f'✓ 保留已有 config.json (备份: {os.path.basename(bak)})')
                except Exception as e:
                    self._log(f'⚠ config.json 备份失败: {e}')
            elif os.path.exists(SHELL_CONFIG):
                shutil.copy(SHELL_CONFIG, cfg_path)
                self._log('✓ 已生成 config.json (空壳, 需自行填 api_key/device_id)')

            # 3. 处理 open-ai-autostart.bat 硬编码路径
            self._patch_autostart_bat(target)

            # 4. 检测/安装 Python
            self._set_status('检测 Python...', 25)
            py_cmd, py_ver = find_python()
            if py_cmd:
                self._log(f'✓ 检测到 Python {py_ver}')
            else:
                self._log('未检测到 Python, 正在自动下载安装...')
                self._install_python()

            # 5. 检测/安装 Node
            self._set_status('检测 Node.js...', 38)
            node_cmd, node_ver = find_node()
            if node_cmd:
                self._log(f'✓ 检测到 Node.js {node_ver}')
            else:
                self._log('未检测到 Node.js, 正在自动下载安装...')
                self._install_node()

            # 6. 创建 venv + 装依赖
            self._set_status('创建虚拟环境并安装依赖...', 50)
            self._setup_venv(target)

            # 7. 构建品牌化运行时 (procname.py → runtime/Scripts)
            self._set_status('构建品牌化运行时...', 70)
            self._setup_runtime(target)

            # 8. 安装 Playwright
            self._set_status('安装 Playwright 浏览器...', 80)
            self._setup_playwright(target)

            # 9. 创建桌面快捷方式
            self._set_status('创建桌面快捷方式...', 92)
            self._create_shortcuts(target)

            # 10. 开机自启
            if self.autostart.get():
                self._set_status('配置开机自启...', 96)
                self._setup_autostart(target)

            self._set_status('安装完成!', 100)
            self._log('')
            self._log('=' * 40)
            self._log('✓ 安装完成!')
            self._log(f'  安装目录: {target}')
            self._log('  桌面快捷方式: open-ai')
            self._log('  首次使用请编辑 config.json 填入 api_key 与 device_id')
            self._log('=' * 40)
            self.root.after(0, lambda: messagebox.showinfo(
                '安装完成',
                f'open-ai 安装完成!\n\n安装目录: {target}\n\n'
                f'桌面已创建快捷方式「open-ai」。\n'
                f'首次使用请编辑 {target}\\config.json\n'
                f'填入 api_key 与 device_id (见 README §3)。'))
        except Exception as e:
            self._log(f'✗ 安装失败: {e}')
            self._set_status('安装失败')
            self.root.after(0, lambda: messagebox.showerror('安装失败', str(e)))
        finally:
            self.root.after(0, lambda: self.btn_install.configure(
                state='normal', text='开始安装'))

    # ---------------- 辅助函数 ----------------
    def _patch_autostart_bat(self, target):
        """把 open-ai-autostart.bat 里的旧路径替换为实际安装目录。"""
        bat = os.path.join(target, 'open-ai-autostart.bat')
        if not os.path.exists(bat):
            return
        try:
            with open(bat, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            # 替换旧源码路径 (仅当其中出现 D:\app\dsh_plugin\open-ai 等)
            import re
            content = re.sub(r'(?i)[a-z]:\\[^\s"]*?open-ai',
                             target.replace('\\', '\\\\'), content)
            with open(bat, 'w', encoding='utf-8') as f:
                f.write(content)
            self._log('✓ 自启脚本路径已适配安装目录')
        except Exception as e:
            self._log(f'⚠ 自启脚本路径适配失败: {e}')

    def _install_python(self):
        raise RuntimeError('未检测到 Python, 请先手动安装 Python 3.10+ 后重试')

    def _install_node(self):
        raise RuntimeError('未检测到 Node.js, 请先手动安装 Node.js 18+ 后重试')

    def _setup_venv(self, target):
        """创建 venv 并安装 requirements。"""
        py_cmd, _ = find_python()
        if not py_cmd:
            raise RuntimeError('未找到 Python')
        venv_py = os.path.join(target, '.venv', 'Scripts', 'python.exe')
        if not os.path.exists(venv_py):
            self._log('创建虚拟环境...')
            rc, out = run_cmd([py_cmd, '-m', 'venv',
                               os.path.join(target, '.venv')], timeout=300)
            if rc != 0:
                raise RuntimeError(f'venv 创建失败: {out[-300:]}')
        self._log('安装依赖 (清华镜像)...')
        rc, out = run_cmd([venv_py, '-m', 'pip', 'install', '-q',
                           '-r', os.path.join(target, 'requirements.txt'),
                           '-i', 'https://pypi.tuna.tsinghua.edu.cn/simple'],
                          timeout=900)
        if rc != 0:
            raise RuntimeError(f'依赖安装失败: {out[-300:]}')
        self._log('✓ 依赖安装完成')

    def _setup_runtime(self, target):
        """构建品牌化运行时 (procname.py): runtime/Scripts/open-ai-*.exe。"""
        venv_py = os.path.join(target, '.venv', 'Scripts', 'python.exe')
        procname = os.path.join(target, 'procname.py')
        if os.path.exists(venv_py) and os.path.exists(procname):
            self._log('构建品牌化进程 (procname.py)...')
            rc, out = run_cmd([venv_py, procname], timeout=600)
            if rc != 0:
                self._log(f'⚠ 运行时构建失败 (可稍后运行 start.bat): {out[-300:]}')
            else:
                self._log('✓ 品牌化运行时就绪 (runtime/Scripts)')

    def _setup_playwright(self, target):
        """安装 Playwright 浏览器 (已装过则跳过)。"""
        venv_py = os.path.join(target, '.venv', 'Scripts', 'python.exe')
        if os.path.exists(venv_py):
            rc, out = run_cmd([venv_py, '-c',
                               'from playwright.sync_api import sync_playwright; '
                               'import sys; '
                               'p = sync_playwright().start(); '
                               'b = p.chromium; sys.exit(0)'], timeout=60)
            if rc == 0:
                self._log('✓ Playwright 已就绪, 跳过重装')
                return
        self._log('安装 Playwright 浏览器 (可能需要几分钟)...')
        rc, out = run_cmd([venv_py, '-m', 'playwright', 'install'], timeout=1800)
        if rc != 0:
            self._log('⚠ Playwright 浏览器安装失败 (可稍后手动安装)')
        else:
            self._log('✓ Playwright 浏览器安装完成')

    def _find_desktop(self):
        ps = ("[Environment]::GetFolderPath('Desktop')")
        rc, out = run_cmd(['powershell.exe', '-NoProfile',
                           '-ExecutionPolicy', 'Bypass', '-Command', ps],
                          timeout=30)
        if rc == 0:
            path = (out or '').strip().splitlines()
            if path and path[0] and os.path.isdir(path[0]):
                return path[0]
        profile = os.environ.get('USERPROFILE', os.path.expanduser('~'))
        candidates = [
            os.path.join(profile, 'Desktop'),
            os.path.join(profile, 'OneDrive', 'Desktop'),
            os.path.join(profile, 'OneDrive', '桌面'),
            os.path.join(profile, '桌面'),
        ]
        for d in candidates:
            if os.path.isdir(d):
                return d
        return None

    def _create_shortcuts(self, target):
        """创建 'open-ai' 桌面快捷方式 (指向 open-ai-launcher.exe)。"""
        desktop = self._find_desktop()
        if not desktop:
            self._log('⚠ 未找到桌面目录, 跳过快捷方式')
            return
        icon_ico = os.path.join(target, 'pic', 'open-ai.ico')
        if not os.path.exists(icon_ico):
            icon_ico = os.path.join(target, 'pic', 'software_logo.png')
        launcher_exe = os.path.join(target, 'open-ai-launcher.exe')
        if os.path.isfile(launcher_exe):
            launcher_target, workdir = launcher_exe, target
        else:
            launcher_bat = os.path.join(target, 'open-ai 启动.bat')
            self._create_launcher_bat(target, launcher_bat)
            launcher_target, workdir = launcher_bat, os.path.dirname(launcher_bat)
        self._make_shortcut(
            os.path.join(desktop, 'open-ai.lnk'),
            launcher_target, '', workdir,
            'open-ai 本地AI聚合网关', icon_ico)
        self._log('✓ 已创建桌面快捷方式: open-ai')

    def _create_launcher_bat(self, target, launcher_bat):
        """兜底启动器 (资源包无 launcher exe 时): Bootstrap 启动 + 打开 GUI。"""
        bat_content = (
            '@echo off\r\n'
            'rem open-ai 一键启动 (v2.5: Broker 进程树 + 托盘 GUI)\r\n'
            'if not "%1"=="hidden" (\r\n'
            '    start "" /min cmd /c "%~f0" hidden\r\n'
            '    exit /b\r\n'
            ')\r\n'
            'cd /d "%~dp0"\r\n'
            'if exist "%~dp0runtime\\Scripts\\open-ai-daemon.exe" (\r\n'
            '    start "" /b "%~dp0runtime\\Scripts\\open-ai-daemon.exe" '
            '"%~dp0bootstrap.py" start\r\n'
            ') else (\r\n'
            '    start "" /b "%~dp0.venv\\Scripts\\python.exe" '
            '"%~dp0bootstrap.py" start\r\n'
            ')\r\n'
            'timeout /t 2 /nobreak >nul\r\n'
            'if exist "%~dp0runtime\\Scripts\\open-ai-manager.exe" (\r\n'
            '    start "" "%~dp0runtime\\Scripts\\open-ai-manager.exe" '
            '"%~dp0scripts\\gui_account_manager.py"\r\n'
            ') else (\r\n'
            '    start "" "%~dp0.venv\\Scripts\\pythonw.exe" '
            '"%~dp0scripts\\gui_account_manager.py"\r\n'
            ')\r\n'
            'exit /b\r\n'
        )
        with open(launcher_bat, 'w', encoding='gbk', newline='\r\n') as f:
            f.write(bat_content)
        self._log(f'✓ 已生成启动器: {os.path.basename(launcher_bat)}')

    def _make_shortcut(self, lnk_path, target_exe, args, workdir, desc, icon_path=''):
        icon_set = (f"$s.IconLocation = '{icon_path}'; "
                    if icon_path and os.path.exists(icon_path) else '')
        ps = (
            f"$ws = New-Object -ComObject WScript.Shell; "
            f"$s = $ws.CreateShortcut('{lnk_path}'); "
            f"$s.TargetPath = '{target_exe}'; "
            f"$s.Arguments = '{args}'; "
            f"$s.WorkingDirectory = '{workdir}'; "
            f"$s.Description = '{desc}'; "
            f"{icon_set}"
            f"$s.Save()"
        )
        rc, out = run_cmd(['powershell.exe', '-NoProfile',
                           '-ExecutionPolicy', 'Bypass', '-Command', ps],
                          timeout=60)
        if rc != 0:
            self._log(f'⚠ 快捷方式创建失败: {out[-200:]}')

    def _setup_autostart(self, target):
        startup = os.path.join(os.environ.get('APPDATA', ''),
                               'Microsoft', 'Windows', 'Start Menu',
                               'Programs', 'Startup')
        src = os.path.join(target, 'open-ai-autostart.bat')
        if os.path.exists(src) and os.path.isdir(startup):
            shutil.copy(src, os.path.join(startup, 'open-ai-autostart.bat'))
            self._log('✓ 已配置开机自启')
        else:
            self._log('⚠ 开机自启配置失败 (启动文件夹不可用)')


def main():
    root = tk.Tk()
    InstallerApp(root)
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
