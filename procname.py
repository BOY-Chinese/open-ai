# -*- coding: utf-8 -*-
"""
procname.py — open-ai 进程命名注册表 + runtime 构建工厂 (v2.4 架构)
====================================================================
目标: 每个受管进程在任务管理器里都是独立的 open-ai 品牌 exe
      (进程名 + 图标 + 文件描述), 且**单进程** (无 python3.13.exe 子进程污染)。

为什么不能直接复制 venv 的 python.exe:
  venv 启动器 (255KB) 运行时会再 spawn 一个真实解释器子进程 ——
  任务管理器里成对出现, 这正是旧架构"进程乱起八糟"的根源之一。

解释器来源 (find_python_home, 多级回退, Store / python.org 双兼容):
  1) venv 反推 (sys.base_prefix)          — 最通用, 自动跟随宿主解释器
  2) Appx 查询 (Store 注册正常)
  3) WindowsApps / Program Files 目录探测 (注册缺失 / org 版)
  4) py launcher 兜底
  返回的是"真实解释器根" (python.exe / DLLs / Lib / python3xx.dll 齐备),
  无论来自 Store 包目录还是 python.org 安装目录。

Store 版与 org 版的构建差异 (关键):
  Store 版 (WindowsApps) 的 DLL 受 ACL 限制, 外部进程无法加载包内
  DLLs → 需复制 runtime\\DLLs 并在 .pth 里 os.add_dll_directory 注册;
  org 版 (Program Files) 无此限制 → 跳过 DLLs 复制与 .pth 注册。
  两者都必需: 把解释器核心 DLL (python3xx.dll/python3.dll/vcruntime*.dll)
  复制到 runtime\\Scripts (shim exe 的 DLL 搜索路径)。

  布局:
    runtime\\
      pyvenv.cfg                 home = 解释器根 (Store 包目录 / Program Files)
      Lib\\site-packages          junction → ..\\..\\.venv\\Lib\\site-packages
      DLLs\\                     仅 Store: 扩展模块副本 (ACL 绕过)
      Scripts\\
        open-ai-*.exe            品牌化解释器副本 + python3xx.dll 等
"""
import os
import sys
import struct
import ctypes
import shutil
import subprocess

BASE = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DIR = os.path.join(BASE, 'runtime')
SHIM_DIR = os.path.join(RUNTIME_DIR, 'Scripts')
DLLS_DIR = os.path.join(RUNTIME_DIR, 'DLLs')
LIB_DIR = os.path.join(RUNTIME_DIR, 'Lib')
ICON_PATH = os.path.join(BASE, 'pic', 'open-ai.ico')
VENV_SITE = os.path.join(BASE, '.venv', 'Lib', 'site-packages')
PTH_FILE = os.path.join(VENV_SITE, 'openai_runtime.pth')
STAMP_FILE = os.path.join(RUNTIME_DIR, '.build-stamp')
APP_VERSION = '2.4.0'

# 是否打印解释器定位来源 (诊断用; 环境变量 open_ai_verbose_resolve=1 开启)
_VERBOSE_RESOLVE = os.environ.get('open_ai_verbose_resolve') == '1'


class ProcSpec:
    """单个受管进程的展示规范。"""
    def __init__(self, role, exe_name, base_kind, description, version_file,
                 use_icon=True):
        self.role = role
        self.exe_name = exe_name
        self.base_kind = base_kind        # 'python' | 'pythonw' | 'node'
        self.description = description
        self.version_file = version_file
        self.use_icon = use_icon

    @property
    def shim_path(self):
        return os.path.join(SHIM_DIR, self.exe_name)


def _find_node():
    cands = [
        os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'),
                     'nodejs', 'node.exe'),
        os.path.join(os.environ.get('ProgramFiles(x86)',
                                     r'C:\Program Files (x86)'),
                     'nodejs', 'node.exe'),
    ]
    for c in cands:
        if os.path.exists(c):
            return c
    return None


# 描述串 ≤ 30 字符 (版本底板字符串区上限)
SPECS = {
    'gateway': ProcSpec('gateway', 'open-ai-gateway.exe', 'python',
                        'open-ai gateway :8000', 'open-ai-gateway.exe'),
    'trae': ProcSpec('trae', 'open-ai-trae.exe', 'node',
                     'open-ai trae :18787', 'open-ai-trae.exe'),
    # 描述 = 任务管理器"应用"行的显示名称, 用户认的就是这个
    'broker': ProcSpec('broker', 'open-ai-daemon.exe', 'pythonw',
                       'open-ai', 'open-ai-daemon.exe'),
    'task': ProcSpec('task', 'open-ai-task.exe', 'python',
                     'open-ai task (scripts)', 'open-ai-task.exe'),
    'cli': ProcSpec('cli', 'open-ai.exe', 'python',
                    'open-ai control CLI', 'open-ai.exe'),
    # 图形界面 (账号管理): pythonw 系, 自带顶层窗口 → 任务管理器"应用"分组
    'manager': ProcSpec('manager', 'open-ai-manager.exe', 'pythonw',
                        'open-ai 账号管理', 'open-ai-manager.exe'),
}


def get(role):
    return SPECS[role]


# ---------------------------------------------------------------------------
# Store Python 包定位 (WindowsApps ACL 受限, 需 PowerShell)
# ---------------------------------------------------------------------------
def _is_complete_python_home(p):
    """判断目录是否为完整的 Python 解释器根 (构建 shim 所需文件齐备)。
    兼容 Store 版 (Appx) 与 org 版 (Program Files) —— 两者根目录都含
    python.exe / pythonw.exe / DLLs / Lib / python3xx.dll。"""
    if not p or not os.path.isdir(p):
        return False
    if not (os.path.isfile(os.path.join(p, 'python.exe'))
            and os.path.isfile(os.path.join(p, 'pythonw.exe'))):
        return False
    if not (os.path.isdir(os.path.join(p, 'DLLs'))
            and os.path.isdir(os.path.join(p, 'Lib'))):
        return False
    try:
        if not any(f.startswith('python3') and f.endswith('.dll')
                   for f in os.listdir(p)):
            return False
    except Exception:
        return False
    return True


def find_store_pkg_dir():
    """返回 Store Python 包安装目录 (真实解释器所在), 注册正常时优先。
    用通配名 (PythonSoftwareFoundation.Python*) 兼容 3.10~3.14 各版本;
    返回的目录需通过 _is_complete_python_home 校验。"""
    try:
        out = subprocess.check_output(
            ['powershell', '-NoProfile', '-Command',
             "(Get-AppxPackage -Name 'PythonSoftwareFoundation.Python*' "
             "| Sort-Object Version -Descending | Select-Object -First 1).InstallLocation"],
            timeout=30,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        p = out.decode('utf-8', 'ignore').strip()
        if p and _is_complete_python_home(p):
            return p
    except Exception:
        pass
    return None


def _find_python_in_dirs(base_dirs):
    """在给定目录集合里找完整的解释器根 (含 Program Files 的 org 版、
    WindowsApps 里注册缺失的 Store 版)。返回完整目录或 None。"""
    cands = []
    for b in base_dirs:
        if not b or not os.path.isdir(b):
            continue
        try:
            names = os.listdir(b)
        except Exception:
            continue
        for n in names:
            low = n.lower()
            if low.startswith('python3') and n[7:9].isdigit():
                cands.append(os.path.join(b, n))
            elif low.startswith('pythonsoftwarefoundation.python.'):
                cands.append(os.path.join(b, n))
    # 去重 + 校验
    seen = set()
    for c in cands:
        if c in seen:
            continue
        seen.add(c)
        if _is_complete_python_home(c):
            return c
    return None


def _from_venv_base():
    """从当前可用的 venv 解释器反推真实解释器根 (最通用, 兼容 Store/org)。
    用 .venv\\Scripts\\python.exe 的 sys.base_prefix; 若 venv 不可用则回退
    到当前进程解释器的 base_prefix。"""
    venv_py = os.path.join(BASE, '.venv', 'Scripts', 'python.exe')
    if os.path.isfile(venv_py):
        try:
            out = subprocess.check_output(
                [venv_py, '-c', 'import sys; print(sys.base_prefix)'],
                timeout=30,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                stderr=subprocess.DEVNULL)
            p = out.decode('utf-8', 'ignore').strip()
            if p and _is_complete_python_home(p):
                return p
        except Exception:
            pass
    if hasattr(sys, 'base_prefix') and _is_complete_python_home(sys.base_prefix):
        return sys.base_prefix
    return None


def _from_py_launcher():
    """用 py launcher 定位真实解释器 (org 版装了 py launcher 时兜底)。
    按 3.13→3.10 逐个尝试, 兼容各版本; 均失败时回退 'py -3' 最新。"""
    tags = ['-3.13', '-3.12', '-3.11', '-3.10', '-3']
    for tag in tags:
        try:
            out = subprocess.check_output(
                ['py', tag, '-c', 'import sys; print(sys.base_prefix)'],
                timeout=30,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                stderr=subprocess.DEVNULL)
            p = out.decode('utf-8', 'ignore').strip()
            if p and _is_complete_python_home(p):
                return p
        except Exception:
            pass
    return None


def find_python_home():
    """定位真实解释器根目录, 多级回退, 兼容 Store 版 / org 版,
    以及 Store 注册正常 / 注册缺失 各种情况:
      1) venv 反推 (sys.base_prefix)   — 最通用
      2) Appx 查询 (Store 注册正常)
      3) WindowsApps / Program Files 目录探测 (Store 注册缺失 / org 版)
      4) py launcher 兜底
    返回完整解释器目录 (含 DLLs/tcl/python3xx.dll), 找不到返回 None。"""
    order = [
        ('venv', _from_venv_base),
        ('appx', find_store_pkg_dir),
        ('dirs', lambda: _find_python_in_dirs([
            os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'), 'WindowsApps'),
            os.path.join(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'), 'WindowsApps'),
            os.environ.get('LOCALAPPDATA', ''),
            os.environ.get('ProgramFiles', r'C:\Program Files'),
            os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
        ])),
        ('py', _from_py_launcher),
    ]
    for name, fn in order:
        try:
            p = fn()
        except Exception:
            p = None
        if p:
            if _VERBOSE_RESOLVE:
                print('[runtime] 解释器定位[%s]: %s' % (name, p))
            return p
    return None


def _ps_copy(src, dst, recurse=False):
    """Windows 侧复制 (可突破 WindowsApps 的读取限制)。"""
    cmd = "Copy-Item -LiteralPath '%s' -Destination '%s' -Force%s" % (
        src, dst, ' -Recurse' if recurse else '')
    subprocess.check_call(
        ['powershell', '-NoProfile', '-Command', cmd],
        timeout=120,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


# ---------------------------------------------------------------------------
# runtime 布局构建 (幂等; Store/org 双兼容)
# ---------------------------------------------------------------------------
def _ensure_junction():
    """runtime\\Lib\\site-packages → .venv\\Lib\\site-packages"""
    link = os.path.join(LIB_DIR, 'site-packages')
    if os.path.isdir(link):
        # 已存在: 检查指向 (junction 的 os.path.realpath 可解析)
        try:
            target = os.path.realpath(link)
            if target.lower() == os.path.realpath(VENV_SITE).lower():
                return True
        except Exception:
            pass
        shutil.rmtree(link, ignore_errors=True)
    os.makedirs(LIB_DIR, exist_ok=True)
    subprocess.check_call(
        ['cmd', '/c', 'mklink', '/J', link, VENV_SITE],
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return os.path.isdir(link)


def _is_store_home(pkg_dir):
    """解释器根是否来自 WindowsApps Store 包 (ACL 受限 → 需 DLLs 复制 + .pth)。"""
    if not pkg_dir:
        return False
    return ('WindowsApps' in pkg_dir
            or 'PythonSoftwareFoundation' in pkg_dir)


def _core_dll_files(pkg_dir):
    """动态发现解释器核心 DLL 文件名 (python3xx.dll / python3.dll /
    vcruntime140*.dll), 兼容 Store 与 org 各 Python 版本 —— 不再硬编码
    python313.dll (org 版 3.12/3.11 等会 FileNotFoundError)。"""
    out = []
    try:
        names = os.listdir(pkg_dir)
    except Exception:
        return out
    for n in names:
        low = n.lower()
        if (low.startswith('python3') and low.endswith('.dll')) \
                or (low.startswith('vcruntime140') and low.endswith('.dll')):
            out.append(n)
    return sorted(out)


def _copy_file(src, dst):
    """普通文件复制: 优先 shutil (org 版快), ACL 受限时回落 PowerShell。"""
    try:
        shutil.copyfile(src, dst)
        return
    except Exception:
        pass
    _ps_copy(src, dst)


def _ensure_pyvenv_cfg(pkg_dir):
    cfg = os.path.join(RUNTIME_DIR, 'pyvenv.cfg')
    content = 'home = %s\ninclude-system-site-packages = false\n' % pkg_dir
    try:
        with open(cfg, 'r', encoding='utf-8') as f:
            if f.read() == content:
                return
    except Exception:
        pass
    with open(cfg, 'w', encoding='utf-8') as f:
        f.write(content)


def _ensure_pth(store):
    """venv site-packages 里的 .pth: 仅 Store 版注册 runtime\\DLLs
    (WindowsApps ACL 绕过); org 版直接加载 Program Files 的 DLLs,
    若残留旧 .pth 则清除 (指向不存在的目录时 os.path.isdir 为假, 无害)。"""
    if store:
        line = ("import sys, os; _d = r'%s'; "
                "os.path.isdir(_d) and (sys.path.insert(0, _d), "
                "os.add_dll_directory(_d))\n" % DLLS_DIR)
        try:
            with open(PTH_FILE, 'r', encoding='utf-8') as f:
                if f.read() == line:
                    return
        except Exception:
            pass
        with open(PTH_FILE, 'w', encoding='utf-8') as f:
            f.write(line)
    else:
        try:
            if os.path.exists(PTH_FILE):
                os.remove(PTH_FILE)
        except Exception:
            pass


def _shim_stamp_path(spec):
    return spec.shim_path + '.stamp'


def _stamp_payload(spec, pkg_dir):
    """单 shim 的重建判据: 规格字段 + 源文件/图标变更。"""
    parts = [APP_VERSION, spec.exe_name, spec.description, spec.base_kind]
    if spec.base_kind == 'node':
        srcp = _find_node()
    else:
        srcp = os.path.join(pkg_dir or '', spec.base_kind + '.exe')
    try:
        parts.append('%d' % os.path.getmtime(srcp))
    except Exception:
        parts.append('-')
    try:
        parts.append('%d' % os.path.getmtime(ICON_PATH))
    except Exception:
        parts.append('-')
    return '|'.join(parts)


def shim_fresh(spec, pkg_dir):
    """shim 存在且印章匹配 → True。"""
    if not os.path.exists(spec.shim_path):
        return False
    try:
        with open(_shim_stamp_path(spec), 'r', encoding='utf-8') as f:
            return f.read().strip() == _stamp_payload(spec, pkg_dir)
    except Exception:
        return False


_pkg_cache = None


def _pkg_dir_cached():
    global _pkg_cache
    if _pkg_cache is None:
        _pkg_cache = find_python_home()
    return _pkg_cache


def _core_ready():
    """runtime 核心就绪判据。DLLs 目录与 .pth 仅 Store 版需要
    (org 版直接加载 Program Files 的 DLLs, 不复制扩展模块)。"""
    if not (os.path.isfile(os.path.join(RUNTIME_DIR, 'pyvenv.cfg'))
            and os.path.isdir(os.path.join(LIB_DIR, 'site-packages'))):
        return False
    pkg = _pkg_dir_cached()
    if not pkg:                       # 解释器都没找到 → 未就绪
        return False
    if _is_store_home(pkg):
        return bool(os.path.isdir(DLLS_DIR) and os.listdir(DLLS_DIR)
                    and os.path.isfile(PTH_FILE))
    return True


def ensure_runtime(verbose=False):
    """构建/更新 runtime (幂等, 增量): 只重建缺失或变更的 shim,
    运行中被占用的文件跳过 (打印提示), 不影响其他角色。
    Store / org 版双兼容: DLLs 复制与 .pth 仅 Store 需要。"""
    if os.name != 'nt':
        return {}
    os.makedirs(SHIM_DIR, exist_ok=True)
    pkg_dir = None
    # ---- runtime 核心 (junction/pyvenv/pth/DLLs) 只在缺失时构建 ----
    if not _core_ready():
        pkg_dir = _pkg_dir_cached()
        if not pkg_dir:
            raise RuntimeError(
                '未找到可用的 Python 解释器 (Store 版或 python.org 版皆可)。\n'
                '请先安装 Python 3.10+ 或运行 start.bat 完成 venv 初始化。')
        store = _is_store_home(pkg_dir)
        if verbose:
            print('[runtime] 解释器根: %s [%s]'
                  % (pkg_dir, 'store' if store else 'org'))
        _ensure_junction()
        _ensure_pyvenv_cfg(pkg_dir)
        _ensure_pth(store)
        if store:
            if not os.path.isdir(DLLS_DIR) or not os.listdir(DLLS_DIR):
                if verbose:
                    print('[runtime] 复制 DLLs (store) ...')
                if os.path.isdir(DLLS_DIR):
                    shutil.rmtree(DLLS_DIR, ignore_errors=True)
                _ps_copy(os.path.join(pkg_dir, 'DLLs'), DLLS_DIR,
                         recurse=True)
            # Tcl/Tk 数据目录 (GUI 必需; Store 的 WindowsApps 包目录
            # ACL/时序问题 → 复制到 runtime 供 GUI 显式引用)
            tcl_dst = os.path.join(RUNTIME_DIR, 'tcl')
            if not os.path.isdir(tcl_dst):
                try:
                    _ps_copy(os.path.join(pkg_dir, 'tcl'), tcl_dst,
                             recurse=True)
                except Exception as e:
                    if verbose:
                        print('[runtime] tcl 复制失败 (忽略): %s' % e)
        else:
            # org 版: 无需复制扩展 DLLs; tcl 若缺失可交由系统 tkinter
            # 直接加载 (Program Files 无 ACL 问题), 复制失败不阻塞
            tcl_dst = os.path.join(RUNTIME_DIR, 'tcl')
            if not os.path.isdir(tcl_dst):
                src_tcl = os.path.join(pkg_dir, 'tcl')
                if os.path.isdir(src_tcl):
                    try:
                        shutil.copytree(src_tcl, tcl_dst)
                    except Exception as e:
                        if verbose:
                            print('[runtime] tcl 复制失败 (忽略): %s' % e)
        # 核心 DLL (python3xx.dll/python3.dll/vcruntime*.dll) 两种来源都要:
        # shim exe 从原目录复制, 需 DLL 与其同目录 (runtime\Scripts) 才能启动
        for dll in _core_dll_files(pkg_dir):
            dst = os.path.join(SHIM_DIR, dll)
            if not os.path.exists(dst):
                if verbose:
                    print('[runtime] 复制核心 DLL: %s' % dll)
                _copy_file(os.path.join(pkg_dir, dll), dst)
    # ---- 逐 shim 增量构建 ----
    out = {}
    for role, spec in SPECS.items():
        if pkg_dir is None:
            pkg_dir = _pkg_dir_cached()
        if shim_fresh(spec, pkg_dir):
            out[role] = spec.shim_path
            continue
        try:
            _build_shim(spec, pkg_dir, verbose=verbose)
            out[role] = spec.shim_path
        except PermissionError:
            print('[runtime] %s 被占用, 跳过重建 (下次启动自动更新)'
                  % spec.exe_name)
            if os.path.exists(spec.shim_path):
                out[role] = spec.shim_path
        except OSError as e:
            print('[runtime] %s 构建失败: %s' % (spec.exe_name, e))
            if os.path.exists(spec.shim_path):
                out[role] = spec.shim_path
    if verbose:
        print('[runtime] 就绪: %s' % ', '.join(sorted(out)))
    return out


def _build_shim(spec, pkg_dir, verbose=False):
    if spec.base_kind == 'node':
        src = _find_node()
    else:
        src = os.path.join(pkg_dir, spec.base_kind + '.exe')
    if not src or not os.path.exists(src):
        raise RuntimeError('源解释器不存在: %s' % src)
    dst = spec.shim_path
    tmp = dst + '.building'
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
        _copy_file(src, tmp)              # shutil 优先, ACL 受限回落 PowerShell
        if spec.use_icon:
            try:
                _inject_resources(tmp, spec)
            except Exception as e:      # 品牌化失败不阻塞功能
                if verbose:
                    print('[runtime] %s 资源注入失败: %s' % (spec.exe_name, e))
        os.replace(tmp, dst)
        with open(_shim_stamp_path(spec), 'w', encoding='utf-8') as f:
            f.write(_stamp_payload(spec, pkg_dir))
        if verbose:
            print('[runtime] %s -> %s' % (os.path.basename(src), dst))
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
        raise


def shim_status():
    rows = []
    pkg_dir = None
    for role, spec in SPECS.items():
        if pkg_dir is None and os.name == 'nt':
            pkg_dir = _pkg_dir_cached()
        if os.name == 'nt':
            rows.append((role, spec.exe_name, shim_fresh(spec, pkg_dir)))
        else:
            rows.append((role, spec.exe_name, os.path.exists(spec.shim_path)))
    return rows


def shim_for(role):
    """角色 → shim exe 路径 (已就绪时返回; 未构建返回 None)。"""
    p = SPECS[role].shim_path
    return p if os.path.exists(p) else None


def populate(verbose=False):
    """预热 (Broker 启动时调用)。返回 {role: shim_path}。"""
    try:
        return ensure_runtime(verbose=verbose)
    except Exception as e:
        print('[runtime] 构建失败: %s' % e)
        return {}


# ---------------------------------------------------------------------------
# Win32 资源注入 (图标 + 版本信息)
# ---------------------------------------------------------------------------
_k32 = None


def _k32dll():
    global _k32
    if _k32 is None:
        _k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        _k32.BeginUpdateResourceW.restype = ctypes.c_void_p
        _k32.BeginUpdateResourceW.argtypes = [ctypes.c_wchar_p, ctypes.c_int]
        _k32.UpdateResourceW.restype = ctypes.c_int
        _k32.UpdateResourceW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_void_p, ctypes.c_uint16,
                                         ctypes.c_char_p, ctypes.c_uint32]
        _k32.EndUpdateResourceW.restype = ctypes.c_int
        _k32.EndUpdateResourceW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    return _k32


RT_ICON, RT_GROUP_ICON, RT_VERSION = 3, 14, 16
LANG_ID, LANG_NEUTRAL = 0x409, 0x0000
_SVCHOST = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                        'System32', 'svchost.exe')


def _donor_version_block():
    k32 = _k32dll()
    h = k32.LoadLibraryExW(_SVCHOST, 0, 0x2)
    if not h:
        return None
    try:
        hr = k32.FindResourceW(h, 1, RT_VERSION)
        if not hr:
            return None
        return bytearray(ctypes.string_at(
            k32.LockResource(k32.LoadResource(h, hr)),
            k32.SizeofResource(h, hr)))
    finally:
        k32.FreeLibrary(h)


def _patch_version_block(buf, spec):
    pairs = {
        'CompanyName': 'open-ai project',
        'FileDescription': spec.description,
        'FileVersion': APP_VERSION,
        'InternalName': 'open-ai-' + spec.role,
        'LegalCopyright': 'open-ai project',
        'OriginalFilename': spec.version_file,
        'ProductName': 'open-ai',
        'ProductVersion': APP_VERSION,
    }
    raw = bytes(buf)
    for key, val in pairs.items():
        kb = key.encode('utf-16-le')
        idx = raw.find(kb + b'\x00\x00')
        if idx < 0:
            continue
        hdr = idx - 6
        wl, vl, wt = struct.unpack_from('<HHH', buf, hdr)
        v = idx + len(kb) + 2
        v += (-(v - hdr)) % 4
        region = vl * 2
        nb = val.encode('utf-16-le') + b'\x00\x00'
        if len(nb) > region:
            nb = nb[:region - 2] + b'\x00\x00'
        buf[v:v + region] = nb + b'\x00' * (region - len(nb))
    return bytes(buf)


def _inject_resources(exe_path, spec):
    """图标+版本注入: 原位替换原生图标组 (ID 1), 保证 Explorer/任务管理器取到。

    要点 (实测教训):
      - 源 exe (python/node) 的原生图标组固定在 整数ID=1、lang=0x0000;
        任务管理器按"最低 ID 的组"取默认图标 → 必须替换 ID 1 的组,
        而不是新增字符串名的组 (会被无视)。
      - 组条目 GRPICONENTRY 是 14 字节: bWidth/bHeight/bColorCount/bReserved
        各 1 字节 + wPlanes/wBitCount 各 2 字节 + dwBytesInRes 4 字节 + nID 2 字节。
      - 帧资源在同语言槽位 (0x0000) 原位替换; 原生帧多则删, 少则补新 ID。
    """
    k32 = _k32dll()
    if not os.path.exists(ICON_PATH):
        return False
    ico = open(ICON_PATH, 'rb').read()
    n = struct.unpack('<H', ico[4:6])[0]
    mine = [struct.unpack('<BBBBHHII', ico[6 + i * 16:22 + i * 16])
            for i in range(n)]
    mine_data = [ico[e[7]:e[7] + e[6]] for e in mine]

    h = k32.BeginUpdateResourceW(exe_path, False)
    if not h:
        return False
    ok = True
    try:
        # ---- 1) 读原生图标组 (ID=1), 学习语言与帧 ID 布局 ----
        k32.FindResourceExW.restype = ctypes.c_void_p
        k32.FindResourceExW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_void_p, ctypes.c_uint16]
        k32.SizeofResource.restype = ctypes.c_uint32
        k32.SizeofResource.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k32.LockResource.restype = ctypes.c_void_p
        k32.LoadResource.restype = ctypes.c_void_p
        orig_lang = 0x0000
        hr = k32.FindResourceExW(h, RT_GROUP_ICON, 1, orig_lang)
        if not hr:
            orig_lang = 0x0409
            hr = k32.FindResourceExW(h, RT_GROUP_ICON, 1, orig_lang)
        orig_frames = []          # [(frame_id, size)]
        if hr:
            size = k32.SizeofResource(h, hr)
            buf = ctypes.string_at(k32.LockResource(k32.LoadResource(h, hr)),
                                   size)
            _, _, cnt = struct.unpack_from('<HHH', buf, 0)
            for i in range(cnt):
                row = struct.unpack_from('<HBBHHIH', buf, 6 + i * 14)
                orig_frames.append((row[6], row[5]))   # (nID, dwBytesInRes)
        else:
            orig_frames = [(i + 1, 0) for i in range(n)]

        # ---- 2) 新组目录 (14 字节条目), 复用原帧 ID 槽位, 替换原生组 ----
        grp = struct.pack('<HHH', 0, 1, n)
        for i in range(n):
            w, hh, sz = mine[i][0], mine[i][1], mine[i][6]
            fid = orig_frames[i][0] if i < len(orig_frames) else 200 + i
            grp += struct.pack('<BBBBHHIH', w % 256, hh % 256, 0, 0,
                               1, 32, sz, fid)
        ok &= bool(k32.UpdateResourceW(h, RT_GROUP_ICON, 1, orig_lang,
                                       grp, len(grp)))
        # ---- 3) 帧数据原位替换 ----
        for i in range(n):
            fid = orig_frames[i][0] if i < len(orig_frames) else 200 + i
            ok &= bool(k32.UpdateResourceW(h, RT_ICON, fid, orig_lang,
                                           mine_data[i], len(mine_data[i])))
        # ---- 4) 原生帧多 → 删除多余槽位 ----
        for fid, _sz in orig_frames[n:]:
            ok &= bool(k32.UpdateResourceW(h, RT_ICON, fid, orig_lang,
                                           None, 0))
        # ---- 5) 版本块 (双语言槽位) ----
        try:
            donor = _donor_version_block()
            if donor:
                vblock = _patch_version_block(donor, spec)
                for lang in (LANG_ID, LANG_NEUTRAL):
                    ok &= bool(k32.UpdateResourceW(h, RT_VERSION, 1, lang,
                                                   vblock, len(vblock)))
        except Exception:
            pass
    finally:
        ok &= bool(k32.EndUpdateResourceW(h, False))
    return ok


if __name__ == '__main__':
    for role, exe, ok in shim_status():
        print('%-8s %-24s %s' % (role, exe, 'ready' if ok else 'MISSING'))
    print('ensuring runtime...')
    for role, p in populate(verbose=True).items():
        print('  %-8s %s' % (role, p))
