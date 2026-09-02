#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
open-ai 一键安装器 (open-ai-installer.exe) —— 对齐 2.3 版 GUI + v2.5 新架构
===========================================================================
傻瓜式安装: 选目录 -> 自动部署环境 -> 构建品牌化运行时 -> 创建桌面快捷方式
-> 询问开机自启。

GUI/卸载交互对齐 D:\\MyCode\\项目副本\\open-ai-github-副本-2.3:
  - 管理员提醒 / 安装目录 / 安装选项 / 进度条 / 日志区 / 开始安装+退出按钮
  - 支持 --dir <路径> 预填安装目录 (GUI「一键更新」传入)

打包方式:
  1. 先运行 build_resources.py 生成 resources.zip
  2. 用 PyInstaller 打包本文件 (resources.zip 与 config.shell.json 作为数据文件)

运行环境: Windows (需 Python 3.10+ 或打包成 exe 后无需 Python)
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import zipfile
from tkinter import filedialog, messagebox, ttk

# ================= 常量 =================
APP_NAME = 'open-ai'
DEFAULT_INSTALL_DIR = r'C:\open-ai'
PYTHON_MIN = (3, 10)
NODE_MIN = (18, 0)

# 资源定位: 打包后 resources.zip 与 config.shell.json 与 exe 同目录
def _resource_dir():
    if getattr(sys, 'frozen', False):
        base = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
        return base
    return os.path.dirname(os.path.abspath(__file__))

RESOURCES_ZIP = os.path.join(_resource_dir(), 'resources.zip')
SHELL_CONFIG = os.path.join(_resource_dir(), 'config.shell.json')
# 图标: 打包后位于 _MEIPASS/ico/open-ai.ico; 开发时在 installer/ico/open-ai.ico
ICON_ICO = os.path.join(_resource_dir(), 'ico', 'open-ai.ico')
if not os.path.exists(ICON_ICO):
    ICON_ICO = os.path.join(_resource_dir(), 'open-ai.ico')

PYTHON_DOWNLOAD = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe'
NODE_DOWNLOAD = 'https://nodejs.org/dist/v20.18.0/node-v20.18.0-x64.msi'

_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


# ================= 工具函数 =================

def run_cmd(cmd, timeout=600, capture=True, encoding=None):
    """运行命令, 返回 (returncode, output)。隐藏控制台窗口。

    输出解码失败 (控制台编码 GBK/UTF-8 不匹配) 时回落 bytes, 防中断。
    """
    flags = _NO_WINDOW
    try:
        kw = dict(capture_output=capture, text=True, timeout=timeout,
                  shell=False, creationflags=flags)
        if encoding:
            kw['encoding'] = encoding
            kw['errors'] = 'replace'
        p = subprocess.run(cmd, **kw)
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


def download_file(url, dest, progress_cb=None):
    """下载文件 (进度回调可选), 用于自动安装 Python/Node。"""
    import urllib.request
    req = urllib.request.Request(url, headers={'User-Agent': 'open-ai-installer'})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, 'wb') as f:
        total = int(resp.headers.get('Content-Length', 0))
        done = 0
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress_cb and total:
                progress_cb(done / total)


# ================= 安装器 UI (对齐 2.3) =================
class InstallerApp:
    def __init__(self, root):
        self.root = root
        root.title('open-ai 一键安装')
        root.geometry('560x500')
        root.resizable(False, False)

        # 设置窗口图标
        try:
            if os.path.exists(ICON_ICO):
                root.iconbitmap(ICON_ICO)
        except Exception:
            pass

        self.install_dir = tk.StringVar(value=DEFAULT_INSTALL_DIR)
        # 支持 --dir <路径> 预填安装目录 (一键更新时由 GUI 传入当前安装目录)
        if '--dir' in sys.argv:
            try:
                d = sys.argv[sys.argv.index('--dir') + 1]
                if d and os.path.isdir(d):
                    self.install_dir.set(os.path.abspath(d))
            except Exception:
                pass
        self.autostart = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value='准备就绪')
        self.progress = tk.DoubleVar(value=0)

        self._build_ui()
        self._check_resources()

    def _build_ui(self):
        pad = {'padx': 16, 'pady': 8}
        # 标题
        ttk.Label(self.root, text='open-ai 一键安装', font=('Microsoft YaHei UI', 16, 'bold')) \
            .pack(pady=(20, 4))
        # 红色高亮提醒: 需以管理员身份运行
        ttk.Label(self.root,
                  text='注意：本安装程序需要以管理员身份运行',
                  foreground='#d32f2f', font=('Microsoft YaHei UI', 11, 'bold')) \
            .pack(pady=(0, 6))

        # 安装目录
        dir_frame = ttk.LabelFrame(self.root, text='安装目录')
        dir_frame.pack(fill='x', **pad)
        ttk.Entry(dir_frame, textvariable=self.install_dir, width=40).pack(side='left', padx=8, pady=6)
        ttk.Button(dir_frame, text='浏览...', command=self._browse_dir).pack(side='left', padx=4)

        # 选项
        opt_frame = ttk.LabelFrame(self.root, text='安装选项')
        opt_frame.pack(fill='x', **pad)
        ttk.Checkbutton(opt_frame, text='创建桌面快捷方式', variable=tk.BooleanVar(value=True),
                        state='disabled').pack(anchor='w', padx=8, pady=2)
        ttk.Checkbutton(opt_frame, text='开机自动运行 (登录 Windows 后自动启动网关)',
                        variable=self.autostart).pack(anchor='w', padx=8, pady=2)

        # 状态
        status_frame = ttk.Frame(self.root)
        status_frame.pack(fill='x', **pad)
        ttk.Label(status_frame, textvariable=self.status, foreground='#333333').pack(anchor='w')
        self.pbar = ttk.Progressbar(status_frame, variable=self.progress, maximum=100)
        self.pbar.pack(fill='x', pady=4)

        # 日志区
        self.log_text = tk.Text(self.root, height=8, state='disabled',
                                font=('Consolas', 9), bg='#f5f5f5')
        self.log_text.pack(fill='both', expand=True, **pad)

        # 按钮
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill='x', **pad)
        self.btn_install = ttk.Button(btn_frame, text='开始安装', command=self._start_install)
        self.btn_install.pack(side='right', padx=4)
        ttk.Button(btn_frame, text='退出', command=self.root.destroy).pack(side='right', padx=4)

    def _check_resources(self):
        if not os.path.exists(RESOURCES_ZIP):
            self._log('⚠ 未找到 resources.zip, 请先运行 build_resources.py')
            self.status.set('资源缺失')
            self.btn_install.configure(state='disabled')
        else:
            self._log(f'✓ 资源包就绪: {os.path.basename(RESOURCES_ZIP)}')

    def _log(self, msg):
        try:
            self.log_text.configure(state='normal')
            self.log_text.insert('end', msg + '\n')
            self.log_text.see('end')
            self.log_text.configure(state='disabled')
            self.root.update_idletasks()
        except Exception:
            pass

    def _browse_dir(self):
        d = filedialog.askdirectory(initialdir='C:\\', title='选择安装目录')
        if d:
            self.install_dir.set(d)

    def _set_status(self, msg, pct=None):
        self.status.set(msg)
        if pct is not None:
            self.progress.set(pct)
        self.root.update_idletasks()

    # ================= 安装流程 =================
    def _start_install(self):
        self.btn_install.configure(state='disabled')
        threading.Thread(target=self._install_worker, daemon=True).start()

    def _install_worker(self):
        try:
            self._do_install()
        except Exception as e:
            # 容错: 异常消息里若含无法显示的特殊字符, 先清洗再展示
            try:
                safe = str(e).encode('utf-8', errors='replace').decode('utf-8', errors='replace')
            except Exception:
                safe = str(e)
            self._log(f'✗ 安装失败: {safe}')
            self._set_status('安装失败')
            self.root.after(0, lambda: messagebox.showerror('安装失败', safe))
            self.root.after(0, lambda: self.btn_install.configure(state='normal'))

    def _do_install(self):
        target = self.install_dir.get().strip()
        if not target:
            raise ValueError('请选择安装目录')
        target = os.path.abspath(target)

        # 1. 解压资源
        self._set_status('正在解压资源...', 5)
        self._log(f'安装到: {target}')
        os.makedirs(target, exist_ok=True)
        with zipfile.ZipFile(RESOURCES_ZIP) as zf:
            zf.extractall(target)
        self._log(f'✓ 资源解压完成 ({len(os.listdir(target))} 项)')

        # 2. 生成配置文件 (保留已有 config, 不覆盖!)
        self._set_status('生成配置文件...', 15)
        cfg_path = os.path.join(target, 'config.json')
        if os.path.exists(cfg_path):
            stamp = time.strftime('%Y%m%d_%H%M%S')
            bak = os.path.join(target, f'config.json.bak-{stamp}')
            try:
                shutil.copy2(cfg_path, bak)
                self._log(f'✓ 检测到已有 config.json, 保留原配置 (备份: {os.path.basename(bak)})')
            except Exception as e:
                self._log(f'⚠ config.json 备份失败 (继续保留原文件): {e}')
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
        self._set_status('检测 Node.js...', 40)
        node_cmd, node_ver = find_node()
        if node_cmd:
            self._log(f'✓ 检测到 Node.js {node_ver}')
        else:
            self._log('未检测到 Node.js, 正在自动下载安装...')
            self._install_node()

        # 6. 创建 venv + 装依赖
        self._set_status('创建虚拟环境并安装依赖...', 55)
        self._setup_venv(target)

        # 7. 构建品牌化运行时 (procname.py → runtime/Scripts)
        self._set_status('构建品牌化运行时...', 68)
        self._setup_runtime(target)

        # 8. 安装 Playwright
        self._set_status('安装 Playwright 浏览器...', 80)
        self._setup_playwright(target)

        # 9. 创建桌面快捷方式
        self._set_status('创建桌面快捷方式...', 90)
        self._create_shortcuts(target)

        # 10. 开机自启
        if self.autostart.get():
            self._set_status('配置开机自启...', 96)
            self._setup_autostart(target)

        # 完成
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
            f'桌面已创建快捷方式。\n'
            f'首次使用请编辑 {target}\\config.json\n'
            f'填入 api_key 与 device_id (见 README §3)。'))

    # ================= 辅助 (编码全容错) =================
    def _patch_autostart_bat(self, target):
        """把 open-ai-autostart.bat 里的硬编码路径替换为目标路径。

        字节级替换 (不依赖文件编码), 兼容 GBK/UTF-8 任意编码的 bat,
        根治 "'gbk' codec can't encode character '\\ufffd'"。
        """
        bat = os.path.join(target, 'open-ai-autostart.bat')
        if not os.path.exists(bat):
            return
        try:
            with open(bat, 'rb') as f:
                raw = f.read()
            import re
            # 函数式替换, 避免 re.sub 把替换串里的 '\' 当转义
            new_raw = re.sub(
                rb'(?i)[a-z]:\\(?:[^\r\n"]*?\\)*[^\\\r\n"]*open-ai',
                lambda m: target.encode('utf-8', errors='surrogateescape'),
                raw)
            if new_raw != raw:
                with open(bat, 'wb') as f:
                    f.write(new_raw)
            self._log(f'✓ 已更新 open-ai-autostart.bat 路径 -> {target}')
        except Exception as e:
            self._log(f'⚠ 自启脚本路径适配失败: {e}')

    def _install_python(self):
        """自动下载安装 Python (Python.org 官方)。"""
        dest = os.path.join(os.environ.get('TEMP', '.'), 'python-installer.exe')
        self._log(f'下载 Python...')
        download_file(PYTHON_DOWNLOAD, dest)
        self._log('安装 Python (静默)...')
        rc, out = run_cmd([dest, '/quiet', 'InstallAllUsers=1',
                           'PrependPath=1'], timeout=1800)
        if rc != 0:
            raise RuntimeError(f'Python 安装失败: {out[-200:]}')
        self._log('✓ Python 安装完成')

    def _install_node(self):
        """自动下载安装 Node.js (官方 MSI)。"""
        dest = os.path.join(os.environ.get('TEMP', '.'), 'node-installer.msi')
        self._log('下载 Node.js...')
        download_file(NODE_DOWNLOAD, dest)
        self._log('安装 Node.js (静默)...')
        rc, out = run_cmd(['msiexec', '/i', dest, '/quiet',
                           '/norestart'], timeout=1800)
        if rc != 0:
            raise RuntimeError(f'Node.js 安装失败: {out[-200:]}')
        self._log('✓ Node.js 安装完成')

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
        """返回真实桌面目录 (支持 OneDrive 重定向 / 中文系统)。"""
        rc, out = run_cmd(['powershell.exe', '-NoProfile',
                           '-ExecutionPolicy', 'Bypass',
                           '-Command', "[Environment]::GetFolderPath('Desktop')"],
                          timeout=30)
        if rc == 0:
            path = (out or '').strip().splitlines()
            if path and path[0] and os.path.isdir(path[0]):
                return path[0]
        profile = os.environ.get('USERPROFILE', os.path.expanduser('~'))
        for d in (os.path.join(profile, 'Desktop'),
                  os.path.join(profile, 'OneDrive', 'Desktop'),
                  os.path.join(profile, 'OneDrive', '桌面'),
                  os.path.join(profile, '桌面')):
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
            'rem open-ai launcher (fallback)\r\n'
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
        with open(launcher_bat, 'wb') as f:
            f.write(bat_content.encode('gbk', errors='replace'))
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
        # 用隐藏 VBS 调 start_hidden.ps1 (WindowStyle=0): 开机无控制台窗口、无报错。
        # 不用 open-ai-autostart.bat 直接进启动文件夹 (bat 会闪现 cmd 控制台)。
        self._log('配置开机自启 (隐藏启动, 无控制台窗口)...')
        if os.path.isdir(startup):
            ps1 = os.path.join(target, 'start_hidden.ps1')
            vbs = ('Set sh = CreateObject("WScript.Shell")\r\n'
                   'sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass '
                   '-WindowStyle Hidden -File ""' + ps1 + '""", 0, False\r\n')
            try:
                with open(os.path.join(startup, 'open-ai-autostart.vbs'),
                          'w', encoding='gbk', newline='') as f:
                    f.write(vbs)
                self._log('✓ 已配置开机自启')
            except Exception as e:
                self._log(f'⚠ 开机自启配置失败: {e}')
        else:
            self._log('⚠ 开机自启配置失败 (启动文件夹不可用)')


def main():
    root = tk.Tk()
    InstallerApp(root)
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
