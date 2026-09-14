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
        # PyInstaller onefile: 资源在 _MEIPASS 临时解压目录
        base = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
        return base
    return os.path.dirname(os.path.abspath(__file__))

RESOURCES_ZIP = os.path.join(_resource_dir(), 'resources.zip')
SHELL_CONFIG = os.path.join(_resource_dir(), 'config.shell.json')
# exe 打包方案: 品牌 exe 由 build_exe.bat 先生成在 installer/dist/,
# 构建安装器时随 --add-data 打进资源目录; 安装时直接复制到安装根。
DIST_DIR = os.path.join(_resource_dir(), 'exes')
# dev 通道的品牌 exe:
#   daemon  — Broker 独立入口 (开机自启 / 计划任务 / start.bat 走这条)
#   gateway — 网关, 同时是 Broker 的另一个宿主 (main.py --broker)
#   task    — 短命任务 (签到 / 采集 / 登录助手)
#   open-ai — 控制 CLI (start|stop|restart|status|doctor); 桌面端与计划任务都调它
#   trae    — node 品牌化副本 (Trae 通道的网络栈)
#
# ★ 这里既没有 open-ai-manager.exe 也没有 open-ai-launcher.exe —— 两者随旧
#   tkinter GUI 一起废弃。v3.0 的界面是 Tauri 桌面端 desktop\open-ai-desktop.exe,
#   它随 resources.zip 解压落位 (见 build_resources.py), 并且**自己就是启动器**:
#   打开即拉起后端 + 驻留托盘, 不需要单独的 launcher.exe。
#   清单里多写一个不存在的 exe = 安装时直接报「缺少程序文件」。
BRAND_EXES = ('open-ai-daemon.exe', 'open-ai-gateway.exe', 'open-ai-task.exe',
              'open-ai.exe', 'open-ai-trae.exe')
# 可选品牌 exe: open-ai-trae.exe 是 node 的品牌化副本 (Trae 后端), 依赖存在;
# 缺失时仅 Trae 通道禁用, 不应阻断整体安装 (WorkBuddy 不受影响)。
OPTIONAL_EXES = {'open-ai-trae.exe'}

# 历史 bat 里可能残留的旧安装根前缀 (供 _patch_autostart_bat 替换成真实安装路径)。
# 只在**文本真的含有**这些串时才替换, 不匹配就原样跳过 —— 新生成的 bat 用
# %~dp0 自解析, 根本不需要改写。
# ★ 这里刻意不写任何开发机的私有目录: 那是发布者的本机布局, 写进来等于泄漏。
LEGACY_ROOT_HINTS = (
    r'C:\open-ai',
    r'D:\open-ai',
    r'C:\Program Files\open-ai',
)
# 图标: 打包后位于 _MEIPASS/ico/open-ai.ico; 开发时在 installer/ico/open-ai.ico
ICON_ICO = os.path.join(_resource_dir(), 'ico', 'open-ai.ico')
if not os.path.exists(ICON_ICO):
    ICON_ICO = os.path.join(_resource_dir(), 'open-ai.ico')

# 下载地址 (Python / Node 官方)
PYTHON_DOWNLOAD = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe'
NODE_DOWNLOAD = 'https://nodejs.org/dist/v20.18.0/node-v20.18.0-x64.msi'

# pip 镜像源回退链: 单一镜像故障/劫持/缓存污染时自动切换 (末位为官方源)
PIP_INDEXES = [
    'https://pypi.tuna.tsinghua.edu.cn/simple',      # 清华 (主)
    'https://mirrors.aliyun.com/pypi/simple',        # 阿里云
    'https://mirrors.cloud.tencent.com/pypi/simple', # 腾讯云
    'https://pypi.org/simple',                       # PyPI 官方 (兜底)
]
# Playwright 浏览器下载镜像 (npm 官方 CDN 在部分网络不可达)
PLAYWRIGHT_DOWNLOAD_HOSTS = [
    'https://npmmirror.com/mirrors/playwright',      # 淘宝 npmmirror
    'https://registry.npmmirror.com/-/binary/playwright',
    '',                                              # 官方默认 (兜底)
]


# ================= 工具函数 =================

def log(msg):
    print(f'[installer] {msg}')


def run_cmd(cmd, timeout=600, capture=True):
    """运行命令, 返回 (returncode, output)。隐藏控制台窗口。"""
    try:
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        p = subprocess.run(cmd, capture_output=capture, text=True,
                           timeout=timeout, shell=False, creationflags=flags)
        out = (p.stdout or '') + (p.stderr or '')
        return p.returncode, out
    except Exception as e:
        return -1, str(e)


def verify_venv_python(venv_py):
    """验证 venv 的 python.exe 真的可执行 (存在且能跑通)。

    背景: CPython 3.13/3.14 的 venv 在 Windows 上复制 venvlauncher.exe 失败时
    只 logger.warning 不报错, `python -m venv` 仍返回 0, 但 Scripts 下没有
    python.exe -> 后续 subprocess 直接 FileNotFoundError [WinError 2]。
    """
    if not os.path.isfile(venv_py):
        return False, '不存在'
    rc, out = run_cmd([venv_py, '-c', 'import sys; print(sys.version)'],
                      timeout=60)
    if rc != 0:
        return False, f'无法执行 (rc={rc}): {out[-200:]}'
    return True, out.strip().splitlines()[0] if out.strip() else ''


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


def check_python_arch(py_cmd):
    """检测 Python 是否为 x86_64 (AMD64)。py_cmd 可为 'python' 或 ['py','-3.12']。

    背景: playwright 等只发布 win_amd64 wheel; 若用户装的是 ARM64/32 位
    Python, pip 会出现 'from versions: none' 的静默解析失败。
    返回 ('x64', arch_str) 或 ('bad', arch_str)。
    """
    cmd = list(py_cmd) if isinstance(py_cmd, (list, tuple)) else [py_cmd]
    rc, out = run_cmd(cmd + ['-c',
                       'import platform, struct; '
                       'print(platform.machine(), struct.calcsize("P")*8)'],
                      timeout=30)
    if rc != 0:
        return 'bad', 'unknown'
    parts = (out or '').split()
    machine = parts[0].upper() if parts else 'UNKNOWN'
    bits = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    if machine in ('AMD64', 'X86_64', 'X64') and bits == 64:
        return 'x64', f'{machine} {bits}bit'
    return 'bad', f'{machine} {bits}bit'


def find_corrected_python():
    """架构纠正后重新查找可用的 64 位 Python, 返回命令前缀 (list)。

    优先尝试 `py -3.12` (新装解释器在 PyManager/launcher 体系下可能不是
    `python` 别名的默认指向), 再回退逐个检测 py/python/python3。
    """
    for prefix in (['py', '-3.12'], ['py'], ['python'], ['python3']):
        rc, out = run_cmd(prefix + ['--version'], timeout=15)
        if rc != 0:
            continue
        ver = (out or '').strip().split()[-1]
        try:
            parts = tuple(int(x) for x in ver.split('.')[:2])
        except Exception:
            continue
        if parts < PYTHON_MIN:
            continue
        arch, _ = check_python_arch(prefix)
        if arch == 'x64':
            return prefix
    return None


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

        # 1b. 放置品牌化 exe (内嵌 Python, 用户机器无需安装 Python)
        self._set_status('放置程序文件...', 8)
        missing_exe = []
        for name in BRAND_EXES:
            src_exe = os.path.join(DIST_DIR, name)
            if os.path.isfile(src_exe):
                shutil.copy2(src_exe, os.path.join(target, name))
                self._log(f'  ✓ {name}')
            elif name in OPTIONAL_EXES:
                self._log(f'  ⚠ {name} 缺失 (Trae 通道将禁用, WorkBuddy 不受影响)')
            else:
                missing_exe.append(name)
        if missing_exe:
            raise RuntimeError('安装包缺少程序文件: %s' % ', '.join(missing_exe))
        self._log('✓ 全部程序文件就绪 (内嵌运行环境, 无需安装 Python)')

        # 2. 生成配置文件 (关键: 保留用户已有 config, 不覆盖!)
        self._set_status('生成配置文件...', 15)
        cfg_path = os.path.join(target, 'config.json')
        if os.path.exists(cfg_path):
            # 覆盖安装 (一键更新): 已有配置是用户真实数据 (token/api_key/device_id),
            # 绝不能用空壳覆盖; 另存时间戳备份以防新版本字段结构升级需对照迁移
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

        # 5. 检测/安装 Node (trae 通道需要; exe 方案下唯一的外部运行时)
        self._set_status('检测 Node.js...', 40)
        node_cmd, node_ver = find_node()
        if node_cmd:
            self._log(f'✓ 检测到 Node.js {node_ver}')
        else:
            self._log('⚠ 未检测到 Node.js — trae 通道将禁用 (WorkBuddy 网关不受影响)')

        # 6/7. (exe 打包方案) 依赖与 Playwright 均已内嵌到各品牌 exe,
        #      无需 venv / pip 下载 / playwright install
        self._log('✓ 运行环境由程序内置 (无 venv / 无 pip 依赖下载)')

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
        """把 open-ai-autostart.bat 里的硬编码路径替换为目标路径。
        兼容 GBK / UTF-8 两种编码的 bat (exe 方案的新 bat 为 GBK, 历史 bat 为 GBK,
        第三方编辑器可能存成 UTF-8); 无需替换时原样跳过, 绝不重写文件。"""
        bat = os.path.join(target, 'open-ai-autostart.bat')
        if not os.path.exists(bat):
            return
        raw = open(bat, 'rb').read()
        content = None
        for enc in ('gbk', 'utf-8'):
            try:
                content = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if content is None:
            self._log('⚠ open-ai-autostart.bat 编码无法识别, 跳过路径修正')
            return
        # 历史版本的 bat 里可能残留旧安装根 (形如 <盘>:\...\open-ai)。
        # 这里不再写死开发机的私有目录, 改为按「配置过的旧前缀」替换。
        changed = False
        for needle in LEGACY_ROOT_HINTS:
            if needle in content:
                content = content.replace(needle, target)
                changed = True
        if not changed:
            return                      # 无硬编码路径, 无需改写
        # 以可编码的字节写回 (GBK 优先; 仍失败则 UTF-8, 保证安装不中断)
        try:
            data = content.encode('gbk')
        except UnicodeEncodeError:
            data = content.encode('utf-8')
        if b'\r\n' not in data:            # 已是 CRLF 则不动, 防止 CRCRLF
            data = data.replace(b'\n', b'\r\n')
        open(bat, 'wb').write(data)
        self._log(f'✓ 已更新 open-ai-autostart.bat 路径 -> {target}')

    def _install_python(self, reason=''):
        """下载并静默安装 Python (64 位 3.12.10, 官方安装器)。"""
        self._log(f'下载 Python 3.12{(" (%s)" % reason) if reason else ""}...')
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

    def _repair_venv_python(self, target, py_cmd):
        """手动修复 venv: 把基础解释器的启动器补齐为 Scripts 下的 python(.w).exe。

        CPython 3.13+ 的 Windows venv 依赖 Lib/venv/scripts/nt/venvlauncher.exe,
        venv 复制环节被静默跳过时这里手动补齐 (等价 pymanager #133 官方修复)。
        """
        venv_dir = os.path.join(target, '.venv')
        scripts = os.path.join(venv_dir, 'Scripts')
        os.makedirs(scripts, exist_ok=True)
        prefix = list(py_cmd) if isinstance(py_cmd, (list, tuple)) else [py_cmd]
        rc, out = run_cmd(
            [*prefix, '-c',
             'import sys, os, sysconfig;'
             'base = getattr(sys, "_base_executable", "") or sys.executable;'
             'print(base);'
             'print(os.path.join(sysconfig.get_paths()["stdlib"],'
             '"venv", "scripts", "nt"))'],
            timeout=30)
        if rc != 0:
            return False
        lines = [x.strip() for x in (out or '').splitlines() if x.strip()]
        if len(lines) < 2:
            return False
        base_exe, launcher_dir = lines[0], lines[1]
        venv_py = os.path.join(scripts, 'python.exe')
        venv_pyw = os.path.join(scripts, 'pythonw.exe')
        candidates = {
            venv_py: [os.path.join(launcher_dir, 'venvlauncher.exe'), base_exe],
            venv_pyw: [os.path.join(launcher_dir, 'venvwlauncher.exe'), base_exe],
        }
        for dst, srcs in candidates.items():
            if os.path.isfile(dst):
                continue
            for src in srcs:
                try:
                    if os.path.isfile(src):
                        shutil.copy2(src, dst)
                        break
                except Exception:
                    pass
        return os.path.isfile(venv_py)

    def _setup_venv(self, target):
        """创建 venv 并安装 requirements。

        关键防御: CPython 3.13/3.14 的 venv 在 Windows 上若 venvlauncher.exe
        复制失败 (杀软拦截/新版官方安装器的运行时目录布局变化), 命令仍返回 0
        但 Scripts 目录下 python.exe 不会生成, 后续调用它装依赖就会报
        "[WinError 2] 系统找不到指定的文件"。因此创建后必须校验可执行性:
        校验失败 -> 清除重建 -> 手动修复 -> 最后才回退无 venv 直装。
        """
        py_cmd = getattr(self, 'python_prefix', None) or find_python()[0]
        if not py_cmd:
            raise RuntimeError('未找到 Python')
        prefix = list(py_cmd) if isinstance(py_cmd, (list, tuple)) else [py_cmd]
        venv_dir = os.path.join(target, '.venv')
        venv_py = os.path.join(venv_dir, 'Scripts', 'python.exe')
        req = os.path.join(target, 'requirements.txt')

        ok, info = verify_venv_python(venv_py) if os.path.exists(venv_py) else (False, '不存在')
        if not ok:
            self._log(f'⚠ 检测到损坏/不完整的 venv ({info}), 正在重建...')
            shutil.rmtree(venv_dir, ignore_errors=True)
        if not os.path.exists(venv_py):
            self._log('创建虚拟环境...')
            rc, out = run_cmd([*prefix, '-m', 'venv', venv_dir], timeout=300)
            if rc != 0:
                raise RuntimeError(f'venv 创建失败: {out[-300:]}')
            ok, info = verify_venv_python(venv_py)
            if not ok:
                # 3.13/3.14 静默失败高发: 命令返回 0 但没有 python.exe。
                self._log(f'⚠ venv 创建后不可用 ({info}), 清除重建...')
                shutil.rmtree(venv_dir, ignore_errors=True)
                rc, out = run_cmd([*prefix, '-m', 'venv', '--clear', venv_dir],
                                  timeout=300)
                if rc != 0:
                    raise RuntimeError(f'venv 创建失败 (重试): {out[-300:]}')
                ok, info = verify_venv_python(venv_py)
                if not ok and self._repair_venv_python(target, prefix):
                    self._log('⚠ 已手动补齐 venv 解释器, 重新校验...')
                    ok, info = verify_venv_python(venv_py)
                if not ok:
                    self._log(f'⚠ venv 仍不可用 ({info}), 回退为直接用系统 Python 安装依赖')
                    self._log('  (稍后可通过 start.bat 首次运行时重建 venv)')
                    self._pip_install(prefix, req, target)
                    self.final_python = prefix
                    self._log('✓ 依赖安装完成 (无 venv 模式)')
                    return
        self._log('安装依赖 (多镜像源自动回退)...')
        self._pip_install(venv_py, req, target)
        self.final_python = venv_py
        self._log('✓ 依赖安装完成')

    def _pip_install(self, python_cmd, requirements, target):
        """用指定 Python 执行 pip 安装。

        - 统一 --no-cache-dir: 本地 HTTP 缓存被污染会持续报 from versions: none
        - 多镜像源回退: 清华 -> 阿里云 -> 腾讯云 -> PyPI 官方
        - 失败时完整输出写入日志文件
        """
        prefix = list(python_cmd) if isinstance(python_cmd, (list, tuple)) \
            else [python_cmd]
        last = ''
        for i, index in enumerate(PIP_INDEXES):
            rc, out = run_cmd([*prefix, '-m', 'pip', 'install', '-q',
                               '--no-cache-dir',
                               '-r', requirements,
                               '-i', index,
                               '--timeout', '30'], timeout=900)
            if rc == 0:
                if i:
                    self._log(f'✓ 第 {i + 1} 源 ({index}) 安装成功')
                return
            last = out or ''
            short = ' '.join(last.split())[-160:]
            self._log(f'⚠ 第 {i + 1} 源失败 ({index}): {short}')
            if 'No matching distribution' not in last and \
                    'versions: none' not in last and rc == 1:
                # 非镜像源问题 (如依赖冲突/网络中断), 换源意义不大
                break
        try:
            log_path = os.path.join(target, 'pip-install-error.log')
            with open(log_path, 'w', encoding='utf-8', errors='replace') as f:
                f.write(last or '(无输出)')
            hint = f'完整输出已保存: {log_path}'
        except Exception:
            hint = f'输出末尾: {last[-300:]}'
        raise RuntimeError(f'依赖安装失败 (rc={rc}); {hint}; {last[-300:]}')

    def _setup_playwright(self, target):
        """安装 Playwright 浏览器 (已装过则跳过, 避免更新时无谓重装)。"""
        # 跟随 _setup_venv 的结论: playwright 包装在哪个解释器里, 就用哪个跑 CLI
        # (venv 成功 -> final_python=venv_py; 兜底直装 -> final_python=系统前缀)
        venv_py = os.path.join(target, '.venv', 'Scripts', 'python.exe')
        py_cmd = getattr(self, 'final_python', None) or venv_py
        if isinstance(py_cmd, (list, tuple)):
            prefix = list(py_cmd)          # 系统 Python 前缀 (已在架构校验中确认)
        else:
            ok, _info = verify_venv_python(py_cmd)
            if not ok:
                sys_py = find_python()[0]
                py_cmd = sys_py if sys_py else py_cmd
            prefix = [py_cmd]
        if not prefix or not prefix[0]:
            self._log('⚠ 未找到可用 Python, 跳过 Playwright 浏览器安装')
            return
        rc, _o = run_cmd([*prefix, '-c',
                          'from playwright.sync_api import sync_playwright; '
                          'import sys; '
                          'p = sync_playwright().start(); '
                          'b = p.chromium; sys.exit(0)'], timeout=60)
        if rc == 0:
            self._log('✓ Playwright 已就绪, 跳过重装')
            return
        self._log('安装 Playwright 浏览器 (可能需要几分钟)...')
        installed = False
        for i, host in enumerate(PLAYWRIGHT_DOWNLOAD_HOSTS):
            env = os.environ.copy()
            if host:
                env['PLAYWRIGHT_DOWNLOAD_HOST'] = host
                self._log(f'  下载镜像 ({i + 1}/{len(PLAYWRIGHT_DOWNLOAD_HOSTS)}): {host}')
            rc, out = self._run_cmd_env(prefix + ['-m', 'playwright', 'install'],
                                        env, timeout=1800)
            if rc == 0:
                installed = True
                break
            short = ' '.join((out or '').split())[-120:]
            self._log(f'⚠ 下载镜像 {i + 1} 失败: {short}')
        if installed:
            self._log('✓ Playwright 浏览器安装完成')
        else:
            self._log('⚠ Playwright 浏览器安装失败 (可稍后手动安装, 不影响网关启动)')

    def _run_cmd_env(self, cmd, env, timeout=600):
        """带自定义环境变量运行命令 (供 PLAYWRIGHT_DOWNLOAD_HOST 切换使用)。"""
        try:
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout, shell=False,
                               creationflags=flags, env=env)
            return p.returncode, (p.stdout or '') + (p.stderr or '')
        except Exception as e:
            return -1, str(e)

    def _find_desktop(self):
        """返回真实桌面目录 (支持 OneDrive 重定向 / 中文系统 / 自定义位置)。

        优先用 PowerShell 读取系统已知文件夹(桌面)的真实路径——OneDrive 重定向、
        自定义位置时此值依然准确; 失败时再退回常见路径兜底。
        """
        # 1) 用 PowerShell 读取系统真实的桌面路径 (最稳, 兼容 OneDrive 重定向)
        ps = ("[Environment]::GetFolderPath('Desktop')")
        rc, out = run_cmd(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                           '-Command', ps], timeout=30)
        if rc == 0:
            path = (out or '').strip().splitlines()
            if path and path[0] and os.path.isdir(path[0]):
                return path[0]

        # 2) 兜底: 常见桌面路径 (含 OneDrive / 中文"桌面")
        profile = os.environ.get('USERPROFILE', os.path.expanduser('~'))
        candidates = [
            os.path.join(profile, 'Desktop'),
            os.path.join(profile, 'OneDrive', 'Desktop'),
            os.path.join(profile, 'OneDrive', '桌面'),
            os.path.join(profile, '桌面'),
            os.path.join(profile, 'OneDrive', 'OneDrive', 'Desktop'),
        ]
        for d in candidates:
            if os.path.isdir(d):
                return d
        return None

    def _create_shortcuts(self, target):
        """创建单个 'open-ai' 桌面快捷方式 (先启动网关, 再打开账号管理)。"""
        desktop = self._find_desktop()
        if not desktop:
            self._log('⚠ 未找到桌面目录, 跳过快捷方式')
            return

        icon = os.path.join(target, 'pic', 'software_logo.png')  # 快捷方式图标(用PNG转的ico更好)
        icon_ico = os.path.join(target, 'pic', 'open-ai.ico')
        if not os.path.exists(icon_ico):
            icon_ico = icon

        # 快捷方式指向 Tauri 桌面端 —— 它是唯一的界面入口, 且自带「拉起后端」:
        # 打开时 start_backend 幂等地把 Broker/网关/Trae/定时任务整棵进程树带起来,
        # 关窗口则收进系统托盘。旧的 open-ai-launcher.exe 与 tkinter 界面一并废弃。
        app_exe = os.path.join(target, 'desktop', 'open-ai-desktop.exe')
        if not os.path.isfile(app_exe):
            self._log('⚠ 未找到 desktop\\open-ai-desktop.exe, 跳过桌面快捷方式')
            self._log('  (资源包不完整: 需先 build-tauri-release.ps1 再 build_resources.py)')
            return

        self._make_shortcut(
            os.path.join(desktop, 'open-ai.lnk'),
            app_exe, '', target,
            'open-ai 账号管理 (本地 AI 聚合网关)', icon_ico)
        self._log('✓ 已创建桌面快捷方式: open-ai')

    def _create_launcher_bat(self, target, launcher_bat):
        """生成启动器: 先启动网关(后台), 再打开账号管理GUI。自动隐藏自身控制台。"""
        bat_content = (
            '@echo off\r\n'
            'rem open-ai 一键启动: 先启网关(后台) 再开账号管理\r\n'
            'rem 隐藏本控制台窗口\r\n'
            'if not "%1"=="hidden" (\r\n'
            '    start "" /min cmd /c "%~f0" hidden\r\n'
            '    exit /b\r\n'
            ')\r\n'
            'cd /d "%~dp0"\r\n'
            'start "" powershell.exe -NoProfile -ExecutionPolicy Bypass '
            '-WindowStyle Hidden -File "%~dp0start_hidden.ps1"\r\n'
            'timeout /t 3 /nobreak >nul\r\n'
            'start "" "%~dp0.venv\\Scripts\\pythonw.exe" "%~dp0scripts\\gui_account_manager.py"\r\n'
            'exit /b\r\n'
        )
        with open(launcher_bat, 'w', encoding='gbk', newline='\r\n') as f:
            f.write(bat_content)
        self._log(f'✓ 已生成启动器: {os.path.basename(launcher_bat)}')

    def _make_shortcut(self, lnk_path, target_exe, args, workdir, desc, icon_path=''):
        """用 PowerShell WScript.Shell 创建 .lnk 快捷方式, 可选自定义图标。"""
        icon_set = f"$s.IconLocation = '{icon_path}'; " if icon_path and os.path.exists(icon_path) else ''
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
            # 自启拉「桌面端 --minimized」而不是裸 daemon:
            #   托盘图标由桌面端创建 —— 只拉 daemon 会得到「后端在跑, 但托盘里
            #   没有图标」, 用户 consequently 找不到退出入口 (v3.0 实测反馈)。
            #   --minimized 让窗口不弹出, 只留托盘, 与「开机静默常驻」的预期一致。
            bat = ('@echo off\r\n'
                   'rem open-ai autostart (dev): tray app minimized + backend up\r\n'
                   'cd /d "%s"\r\n'
                   'if not exist "desktop\\open-ai-desktop.exe" (\r\n'
                   '    echo [ERROR] desktop\\open-ai-desktop.exe not found. Reinstall open-ai.\r\n'
                   '    exit /b 1\r\n'
                   ')\r\n'
                   'start "" "desktop\\open-ai-desktop.exe" --minimized\r\n' % target)
            with open(os.path.join(startup, 'open-ai-autostart.bat'), 'w', encoding='gbk', newline='') as f:
                f.write(bat)
            self._log('✓ 已配置开机自启')
        else:
            self._log('⚠ 开机自启配置失败 (启动文件夹不可用)')


def main():
    root = tk.Tk()
    app = InstallerApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()