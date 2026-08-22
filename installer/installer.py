#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
open-ai 一键安装器
==================
傻瓜式安装: 选目录 -> 自动部署环境 -> 创建桌面快捷方式 -> 询问开机自启。

打包方式:
  1. 先运行 build_resources.py 生成 resources.zip
  2. 用 PyInstaller 打包本文件 (resources.zip 与 config.shell.json 作为数据文件)
  3. 生成单个 exe 即为一键安装包

运行环境: Windows (需 Python 3.10+ 或打包成 exe 后无需 Python)
"""
import json
import os
import shutil
import subprocess
import sys
import threading
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
        # PyInstaller onefile: 资源在 _MEIPASS 临时解压目录
        base = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
        return base
    return os.path.dirname(os.path.abspath(__file__))

RESOURCES_ZIP = os.path.join(_resource_dir(), 'resources.zip')
SHELL_CONFIG = os.path.join(_resource_dir(), 'config.shell.json')

# 下载地址 (Python / Node 官方)
PYTHON_DOWNLOAD = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe'
NODE_DOWNLOAD = 'https://nodejs.org/dist/v20.18.0/node-v20.18.0-x64.msi'


# ================= 工具函数 =================

def log(msg):
    print(f'[installer] {msg}')


def run_cmd(cmd, timeout=600, capture=True):
    """运行命令, 返回 (returncode, output)。"""
    try:
        p = subprocess.run(cmd, capture_output=capture, text=True,
                           timeout=timeout, shell=False)
        out = (p.stdout or '') + (p.stderr or '')
        return p.returncode, out
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
    """查找系统 Node 18+。"""
    rc, out = run_cmd(['node', '--version'], timeout=15)
    if rc == 0:
        ver = out.strip().lstrip('v')
        try:
            parts = tuple(int(x) for x in ver.split('.')[:2])
            if parts >= NODE_MIN:
                return 'node', ver
        except Exception:
            pass
    return None, None


def download_file(url, dest, progress_cb=None):
    """下载文件到 dest, 支持进度回调。"""
    import urllib.request
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get('Content-Length', 0))
        done = 0
        with open(dest, 'wb') as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress_cb and total:
                    progress_cb(done / total)
    return dest


# ================= 安装器主类 =================

class InstallerApp:
    def __init__(self, root):
        self.root = root
        root.title('open-ai 一键安装')
        root.geometry('560x480')
        root.resizable(False, False)

        self.install_dir = tk.StringVar(value=DEFAULT_INSTALL_DIR)
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
        ttk.Label(self.root, text='本地 AI 聚合网关 · 傻瓜式安装', foreground='#666666') \
            .pack()

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
        self.log_text.configure(state='normal')
        self.log_text.insert('end', msg + '\n')
        self.log_text.see('end')
        self.log_text.configure(state='disabled')
        self.root.update_idletasks()

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
            self._log(f'✗ 安装失败: {e}')
            self._set_status('安装失败')
            self.root.after(0, lambda: messagebox.showerror('安装失败', str(e)))
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

        # 2. 生成空壳 config
        self._set_status('生成配置文件...', 15)
        if os.path.exists(SHELL_CONFIG):
            shutil.copy(SHELL_CONFIG, os.path.join(target, 'config.json'))
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

        # 7. 安装 Playwright
        self._set_status('安装 Playwright 浏览器...', 75)
        self._setup_playwright(target)

        # 8. 创建桌面快捷方式
        self._set_status('创建桌面快捷方式...', 88)
        self._create_shortcuts(target)

        # 9. 开机自启
        if self.autostart.get():
            self._set_status('配置开机自启...', 94)
            self._setup_autostart(target)

        # 完成
        self._set_status('安装完成!', 100)
        self._log('')
        self._log('=' * 40)
        self._log('✓ 安装完成!')
        self._log(f'  安装目录: {target}')
        self._log('  桌面快捷方式: 账号管理 / 启动网关')
        self._log('  首次使用请编辑 config.json 填入 api_key 与 device_id')
        self._log('=' * 40)
        self.root.after(0, lambda: messagebox.showinfo(
            '安装完成',
            f'open-ai 安装完成!\n\n安装目录: {target}\n\n'
            f'桌面已创建快捷方式。\n'
            f'首次使用请编辑 {target}\\config.json\n'
            f'填入 api_key 与 device_id (见 README §3)。'))

    def _patch_autostart_bat(self, target):
        """把 open-ai-autostart.bat 里的硬编码路径替换为目标路径。"""
        bat = os.path.join(target, 'open-ai-autostart.bat')
        if not os.path.exists(bat):
            return
        with open(bat, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        # 替换 D:\app\dsh_plugin\open-ai -> target
        content = content.replace(r'D:\app\dsh_plugin\open-ai', target)
        with open(bat, 'w', encoding='utf-8') as f:
            f.write(content)
        self._log(f'✓ 已更新 open-ai-autostart.bat 路径 -> {target}')

    def _install_python(self):
        """下载并静默安装 Python。"""
        self._log('下载 Python 3.12...')
        exe = os.path.join(os.environ.get('TEMP', '.'), 'python-installer.exe')
        download_file(PYTHON_DOWNLOAD, exe, lambda p: self._set_status(f'下载 Python {p*100:.0f}%', 25 + p * 10))
        self._log('安装 Python (静默)...')
        rc, out = run_cmd([exe, '/quiet', 'InstallAllUsers=0',
                           'PrependPath=1', 'Include_pip=1'], timeout=900)
        if rc != 0:
            raise RuntimeError(f'Python 安装失败 (rc={rc})')
        self._log('✓ Python 安装完成')

    def _install_node(self):
        """下载并静默安装 Node.js。"""
        self._log('下载 Node.js 20...')
        msi = os.path.join(os.environ.get('TEMP', '.'), 'node-installer.msi')
        download_file(NODE_DOWNLOAD, msi, lambda p: self._set_status(f'下载 Node.js {p*100:.0f}%', 40 + p * 10))
        self._log('安装 Node.js (静默)...')
        rc, out = run_cmd(['msiexec', '/i', msi, '/qn', '/norestart'], timeout=900)
        if rc != 0:
            raise RuntimeError(f'Node.js 安装失败 (rc={rc})')
        self._log('✓ Node.js 安装完成')

    def _setup_venv(self, target):
        """创建 venv 并安装 requirements。"""
        py_cmd, _ = find_python()
        if not py_cmd:
            raise RuntimeError('未找到 Python')
        venv_py = os.path.join(target, '.venv', 'Scripts', 'python.exe')
        if not os.path.exists(venv_py):
            self._log('创建虚拟环境...')
            rc, out = run_cmd([py_cmd, '-m', 'venv', os.path.join(target, '.venv')], timeout=300)
            if rc != 0:
                raise RuntimeError(f'venv 创建失败: {out[-300:]}')
        self._log('安装依赖 (清华镜像)...')
        rc, out = run_cmd([venv_py, '-m', 'pip', 'install', '-q',
                           '-r', os.path.join(target, 'requirements.txt'),
                           '-i', 'https://pypi.tuna.tsinghua.edu.cn/simple'], timeout=900)
        if rc != 0:
            raise RuntimeError(f'依赖安装失败: {out[-300:]}')
        self._log('✓ 依赖安装完成')

    def _setup_playwright(self, target):
        """安装 Playwright 浏览器。"""
        venv_py = os.path.join(target, '.venv', 'Scripts', 'python.exe')
        self._log('安装 Playwright 浏览器 (可能需要几分钟)...')
        rc, out = run_cmd([venv_py, '-m', 'playwright', 'install'], timeout=1800)
        if rc != 0:
            self._log('⚠ Playwright 浏览器安装失败 (可稍后手动安装)')
        else:
            self._log('✓ Playwright 浏览器安装完成')

    def _create_shortcuts(self, target):
        """创建桌面快捷方式 (账号管理 + 启动网关)。"""
        desktop = os.path.join(os.path.expanduser('~'), 'Desktop')
        if not os.path.isdir(desktop):
            desktop = os.path.join(os.environ.get('USERPROFILE', ''), 'Desktop')
        if not os.path.isdir(desktop):
            self._log('⚠ 未找到桌面目录, 跳过快捷方式')
            return
        venv_py = os.path.join(target, '.venv', 'Scripts', 'pythonw.exe')
        gui = os.path.join(target, 'scripts', 'gui_account_manager.py')
        start_bat = os.path.join(target, 'start.bat')

        # 账号管理快捷方式
        self._make_shortcut(
            os.path.join(desktop, 'open-ai 账号管理.lnk'),
            venv_py, f'"{gui}"', target, 'open-ai 账号管理')
        # 启动网关快捷方式
        self._make_shortcut(
            os.path.join(desktop, 'open-ai 启动网关.lnk'),
            'cmd.exe', f'/c ""{start_bat}""', target, 'open-ai 启动网关')
        self._log('✓ 桌面快捷方式已创建')

    def _make_shortcut(self, lnk_path, target_exe, args, workdir, desc):
        """用 PowerShell WScript.Shell 创建 .lnk 快捷方式。"""
        ps = (
            f"$ws = New-Object -ComObject WScript.Shell; "
            f"$s = $ws.CreateShortcut('{lnk_path}'); "
            f"$s.TargetPath = '{target_exe}'; "
            f"$s.Arguments = '{args}'; "
            f"$s.WorkingDirectory = '{workdir}'; "
            f"$s.Description = '{desc}'; "
            f"$s.Save()"
        )
        rc, out = run_cmd(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                           '-Command', ps], timeout=60)
        if rc != 0:
            self._log(f'⚠ 快捷方式创建失败: {out[-200:]}')

    def _setup_autostart(self, target):
        """开机自启: 复制 open-ai-autostart.bat 到启动文件夹。"""
        startup = os.path.join(os.environ.get('APPDATA', ''),
                               'Microsoft', 'Windows', 'Start Menu', 'Programs', 'Startup')
        src = os.path.join(target, 'open-ai-autostart.bat')
        if os.path.exists(src) and os.path.isdir(startup):
            shutil.copy(src, os.path.join(startup, 'open-ai-autostart.bat'))
            self._log('✓ 已配置开机自启')
        else:
            self._log('⚠ 开机自启配置失败 (启动文件夹不可用)')


def main():
    root = tk.Tk()
    app = InstallerApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()