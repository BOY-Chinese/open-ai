#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 open-ai 项目资源打包成 installer/resources.zip —— v2.4 新架构版
====================================================================
生成安装器需要的资源包 (内含全部代码 + trae/lib 依赖, 不含 venv/runtime/
logs/data/config.json)。
运行: python build_resources.py
产出: installer/resources.zip
"""
import os
import sys
import zipfile

# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_ZIP = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources.zip')

# 需要打包的顶层文件/目录 (v3.0: Broker + 托盘 + procname 品牌化运行时 + 桌面端)
INCLUDE = [
    'main.py', 'daemon.py', 'app_runtime.py', 'ipc.py', 'jobmgmt.py',
    'procname.py', 'bootstrap.py', 'launcher_main.py', 'watchdog_boot.py',
    'anthropic_api.py', 'admin_api.py', 'version.py', 'launcher_version.txt',
    'requirements.txt', 'README.md', 'MEMORY.md', '.gitignore',
    'start.bat', 'start_hidden.ps1', 'open-ai-autostart.bat',
    'providers', 'scripts', 'trae', 'tests', 'pic',
]
# v3.0 移除项：'账号管理.bat'（旧 tkinter GUI 入口）与 scripts/ 下的
# gui_account_manager.py、tray_icon.py 已整条删除，界面只有 desktop/ 桌面端。
# 额外目录 (来自 installer/dist/, PyInstaller onedir 产物):
#   键 = 源目录; 值 = zip 内基准路径 ('' = 装到安装根)。
# onedir 布局: uninstall.exe + uninstall_internal/、open-ai-launcher.exe +
# launcher_internal/, exe 位于安装根, 依赖目录随资源包一起解压到安装根。
# (onefile 打包退出时 bootloader 清理 %TEMP%\_MEI 失败会弹
#  "Failed to remove temporary directory" 警告框, onedir 无临时目录, 根治。)
EXTRA_DIRS = {
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dist', 'uninstall'):
        '',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dist', 'open-ai-launcher'):
        '',
}

# ── v3.0：桌面端（Tauri 2 + React）资源 ──
# 只打包 exe：前端资源已由 tauri-build 在编译期**内嵌进 exe**
#   （Cargo.toml 启用 custom-protocol + tauri.conf.json 的 frontendDist），
#   exe 运行时不读任何外部目录，故不再附加 desktop/resources/。
# 安装后位于 <安装根>/desktop/open-ai-desktop.exe。
# 注：node_modules / src-tauri/target 等构建中间产物一律不带。
DESKTOP_SRC = os.path.join(PROJECT_ROOT, 'desktop-ui')
DESKTOP_EXE_CANDIDATES = [
    # 优先 release，缺失则回落 debug（便于快速验证）
    os.path.join(DESKTOP_SRC, 'src-tauri', 'target', 'release', 'open-ai-desktop.exe'),
    os.path.join(DESKTOP_SRC, 'src-tauri', 'target', 'debug', 'open-ai-desktop.exe'),
]

# 排除项 (相对 project root)
EXCLUDE_DIRS = {'__pycache__', '.venv', 'runtime', 'logs', 'data', '.git', 'installer'}
EXCLUDE_EXTS = {'.pyc', '.log'}
EXCLUDE_FILES = {'config.json'}  # 安装器会生成空壳 config, 不打真实配置


def should_include(relpath: str) -> bool:
    parts = relpath.replace('\\', '/').split('/')
    for p in parts[:-1]:
        if p in EXCLUDE_DIRS:
            return False
    basename = parts[-1]
    if basename in EXCLUDE_FILES:
        return False
    for ext in EXCLUDE_EXTS:
        if basename.endswith(ext):
            return False
    return True


def build_desktop(zf, count: int, total_bytes: int):
    """打包桌面端（Tauri exe）到 zip 内的 desktop/ 下。

    前端资源已内嵌进 exe，无需附带 dist/。
    缺失时只告警不中断 —— 保证「仅后端」安装包仍可构建，
    便于 python 侧单独发版。
    """
    exe = next((p for p in DESKTOP_EXE_CANDIDATES if os.path.isfile(p)), None)
    if exe is None:
        print('[警告] 未找到 open-ai-desktop.exe，跳过桌面端（请先构建 desktop-ui）')
        return count, total_bytes

    arc = 'desktop/open-ai-desktop.exe'
    zf.write(exe, arc)
    count += 1
    total_bytes += os.path.getsize(exe)
    print(f'  [附加] 桌面端 exe -> {arc} '
          f'({os.path.getsize(exe)/1024/1024:.1f} MB, {os.path.basename(os.path.dirname(os.path.dirname(exe)))} 构建)')
    return count, total_bytes


def build():
    count = 0
    total_bytes = 0
    if os.path.exists(OUT_ZIP):
        os.remove(OUT_ZIP)
    with zipfile.ZipFile(OUT_ZIP, 'w', zipfile.ZIP_DEFLATED) as zf:
        for include in INCLUDE:
            src = os.path.join(PROJECT_ROOT, include)
            if not os.path.exists(src):
                print(f'[跳过] 不存在: {include}')
                continue
            if os.path.isfile(src):
                if should_include(include):
                    zf.write(src, include)
                    count += 1
                    total_bytes += os.path.getsize(src)
            else:
                for root, dirs, files in os.walk(src):
                    for f in files:
                        full = os.path.join(root, f)
                        rel = os.path.relpath(full, PROJECT_ROOT).replace('\\', '/')
                        if should_include(rel):
                            zf.write(full, rel)
                            count += 1
                            total_bytes += os.path.getsize(full)
        # 追加额外目录 (onedir exe + 依赖目录, 递归打包)
        for src_dir, base in EXTRA_DIRS.items():
            if not os.path.isdir(src_dir):
                print(f'[错误] 缺少 onedir 产物目录: {src_dir}')
                print('       请先运行 build_exe.bat 生成 PyInstaller 产物。')
                sys.exit(1)
            if not any(should_include(os.path.relpath(os.path.join(r, f), src_dir).replace('\\', '/'))
                       for r, _, fs in os.walk(src_dir) for f in fs):
                print(f'[警告] {src_dir} 为空, 未打包任何内容')
                continue
            packed = 0
            for root, dirs, files in os.walk(src_dir):
                for f in files:
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, src_dir).replace('\\', '/')
                    arcname = f'{base}/{rel}'.lstrip('/') if base else rel
                    if should_include(arcname):
                        zf.write(full, arcname)
                        count += 1
                        total_bytes += os.path.getsize(full)
                        packed += 1
            print(f'  [附加] {os.path.basename(src_dir)}/ -> {base or "(安装根)"} ({packed} 个文件)')
        # 追加桌面端（Tauri exe + 前端资源）
        count, total_bytes = build_desktop(zf, count, total_bytes)
    size_mb = os.path.getsize(OUT_ZIP) / 1024 / 1024
    print(f'[完成] 打包 {count} 个文件, {total_bytes/1024/1024:.1f} MB -> {size_mb:.1f} MB zip')
    print(f'  输出: {OUT_ZIP}')
    return OUT_ZIP


if __name__ == '__main__':
    build()
