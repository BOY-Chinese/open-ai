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

# 需要打包的顶层文件/目录 (v3.0: 后端源码 + 前端源码 + 桌面端; 开源版**装的是
# 源码**, 用户装完可直接查看/修改 .py 与前端 —— 与 portable 用户版的冻结 exe
# 方案相反)
INCLUDE = [
    'main.py', 'daemon.py', 'app_runtime.py', 'ipc.py', 'jobmgmt.py',
    'procname.py', 'bootstrap.py', 'watchdog_boot.py',
    'anthropic_api.py', 'admin_api.py', 'app_paths.py', 'auto_router.py',
    'version.py',
    'requirements.txt', 'README.md', 'MEMORY.md', '.gitignore',
    'start.bat', 'start_hidden.ps1', 'open-ai-autostart.bat',
    'providers', 'scripts', 'trae', 'pic',
    'desktop-ui',  # ★ 前端源码整包: 没有它用户改不了界面 (改完用包内
                   #   tools/build-tauri-release.ps1 重编桌面端)。
                   #   tests 不随安装包分发 —— 那是开发机跑 pytest 的本机
                   #   测试, 只留在 git 仓库, 装出来的应用运行时不碰它。
]
# v2.4 → v3.0 变化: launcher_main.py / launcher_version.txt (v2.4 启动器体系)
# 已被桌面端整体替代, 不再分发; app_paths.py / auto_router.py 为 v3.0 新增后端模块。
# v3.0 移除项：'账号管理.bat'（旧 tkinter GUI 入口）与 scripts/ 下的
# gui_account_manager.py、tray_icon.py 已整条删除，界面只有 desktop/ 桌面端。
# 额外目录 (来自 installer/dist/, PyInstaller onedir 产物):
#   键 = 源目录; 值 = zip 内基准路径 ('' = 装到安装根)。
# onedir 布局: uninstall.exe + uninstall_internal/, exe 位于安装根。
# (onefile 打包退出时 bootloader 清理 %TEMP%\_MEI 失败会弹
#  "Failed to remove temporary directory" 警告框, onedir 无临时目录, 根治。)
EXTRA_DIRS = {
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dist', 'uninstall'):
        '',
}

# ── v3.0：桌面端（Tauri 2 + React）资源 ──
# 只打包 exe：前端资源已由 tauri-build 在编译期**内嵌进 exe**
#   （Cargo.toml 启用 custom-protocol + tauri.conf.json 的 frontendDist），
#   exe 运行时不读任何外部目录，故不再附加 desktop/resources/。
# 安装后位于 <安装根>/desktop/open-ai-desktop.exe。
# ★ 优先取仓库根 desktop/ 的免构建产物 —— build_exe.bat 的第 0 步门禁
#   (check_desktop_bundle.py) 校验的正是这一份, 两者必须同源;
#   缺失再回落 src-tauri 本机构建产物。
# 注：node_modules / src-tauri/target 等构建中间产物一律不带。
DESKTOP_SRC = os.path.join(PROJECT_ROOT, 'desktop-ui')
DESKTOP_EXE_CANDIDATES = [
    # 优先: 仓库根免构建产物 (与 check_desktop_bundle 门禁同源)
    os.path.join(PROJECT_ROOT, 'desktop', 'open-ai-desktop.exe'),
    # 回落: 本机构建产物
    os.path.join(DESKTOP_SRC, 'src-tauri', 'target', 'release', 'open-ai-desktop.exe'),
    os.path.join(DESKTOP_SRC, 'src-tauri', 'target', 'debug', 'open-ai-desktop.exe'),
]

# 排除项 (相对 project root)
EXCLUDE_DIRS = {'__pycache__', '.venv', 'runtime', 'logs', 'data', '.git', 'installer'}
EXCLUDE_EXTS = {'.pyc', '.log'}
EXCLUDE_FILES = {'config.json'}  # 安装器会生成空壳 config, 不打真实配置

# ★ 精确路径排除 (前缀匹配, 只砍前端构建产物, 不伤及无辜):
# 不能把 'node_modules' / 'dist' / 'target' 直接丢进 EXCLUDE_DIRS —— 那是**目录名**
# 全局匹配, 会把 trae/lib/node_modules (Trae 通道运行时必需的 @aha-kit 原生
# 依赖) 一起砍掉, 装出来 server.js 报「未找到网络栈依赖」。本项目已踩过一次。
EXCLUDE_DIR_PATHS = {
    'desktop-ui/node_modules',
    'desktop-ui/node_modules_store',
    'desktop-ui/node_modules_tools',
    'desktop-ui/dist',
    'desktop-ui/src-tauri/target',
}
EXCLUDE_FILE_PATHS = {
    'desktop-ui/package-lock.json',  # npm 产物, 用户 npm install 自生成
}

# ── 防空壳包: 资源包里必须存在的关键条目 (build() 末尾强制校验) ──
# 体积闸门只能看总量, 看不出「该装的没装进去」。源码版安装包的灵魂是
# 「装完就是可读可改的源码」—— 后端入口、v3.0 新模块、loomy 注册、任务脚本、
# Trae 网络栈、桌面端、卸载器, 少一样装出来就是残废, 直接中止构建。
REQUIRED_ENTRIES = [
    'main.py', 'bootstrap.py', 'version.py',
    'app_paths.py', 'auto_router.py',
    'providers/__init__.py', 'providers/loomy.py',
    'scripts/task_main.py', 'scripts/loomy_client.py',
    'trae/server.js', 'trae/lib/sscronet.dll',
    'desktop/open-ai-desktop.exe',
    'uninstall.exe',
    # 前端源码 (用户改界面的本钱): 包管理定义 / Tauri 配置 / 相对路径
    # cargo 配置 / 重编脚本 / 前端入口, 缺一个前端就重建不起来
    'desktop-ui/package.json',
    'desktop-ui/src/main.tsx',
    'desktop-ui/src-tauri/tauri.conf.json',
    'desktop-ui/src-tauri/.cargo/config.toml',
    'desktop-ui/tools/build-tauri-release.ps1',
]


def should_include(relpath: str) -> bool:
    rel = relpath.replace('\\', '/')
    parts = rel.split('/')
    for p in parts[:-1]:
        if p in EXCLUDE_DIRS:
            return False
    # ★ 精确路径排除 (前缀匹配): 只砍前端构建产物, 不伤 trae/lib/node_modules
    for d in EXCLUDE_DIR_PATHS:
        if rel == d or rel.startswith(d + '/'):
            return False
    if rel in EXCLUDE_FILE_PATHS:
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
        # 防空壳包: 关键条目强制校验
        names = set(zf.namelist())
        missing = [e for e in REQUIRED_ENTRIES if e not in names]
        if missing:
            print('[错误] 资源包缺少关键条目: %s' % ', '.join(missing))
            print('       源码不完整或 INCLUDE 配置错误, 拒绝产出安装包。')
            sys.exit(1)
        print(f'  [校验] 关键条目 {len(REQUIRED_ENTRIES)} 项齐全 (源码分装完整)')
    size_mb = os.path.getsize(OUT_ZIP) / 1024 / 1024
    print(f'[完成] 打包 {count} 个文件, {total_bytes/1024/1024:.1f} MB -> {size_mb:.1f} MB zip')
    print(f'  输出: {OUT_ZIP}')
    return OUT_ZIP


if __name__ == '__main__':
    build()
