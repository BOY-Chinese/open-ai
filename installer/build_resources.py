#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 open-ai 项目资源打包成 installer/resources.zip
==================================================
生成安装器需要的资源包 (不含 venv/logs/data/配置)。

exe 打包方案下, 全部 Python 代码已内嵌进各品牌 exe, 资源包只带
"运行必需 + 用户可见" 的文件:

    trae/        — Node 侧 (server.js + lib), 唯一非 Python 的真实运行时
    pic/         — 托盘/快捷方式图标 (procname 与安装器都按这个路径找)
    desktop/     — 桌面端 Tauri exe (由 build_exe.bat 先生成到桌面副本再收进来)
    uninstall.exe— 独立卸载程序 (设置页「一键卸载」与桌面端 Rust 都调用它)
    version.py   — 设置页经 importlib 读它显示版本号 / 一键更新比对
    start.bat 等 — 手动启停入口
    README/requirements — 用户文档与依赖说明

**绝不打包**: config.json (含真实 token/密钥)、logs/、data/、.venv/、tests/、
MEMORY.md (含设备指纹线索)、任何 .py 源码、任何 .bak 配置备份。

运行: python build_resources.py
产出: installer/resources.zip
"""
import os
import sys
import zipfile

# 项目根目录 (本文件在 <root>/installer/ 下)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_ZIP = os.path.join(HERE, 'resources.zip')

# 需要打包的顶层文件/目录 (相对项目根)
INCLUDE = [
    'version.py',
    'requirements.txt',
    'README.md',
    'start.bat',
    'open-ai-autostart.bat',
    'trae',
    'pic',
    'desktop',          # build_exe.bat 会先把 Tauri release 产物同步到这里
]

# 额外文件: {源绝对路径: zip 内路径}
EXTRA_FILES = {
    os.path.join(HERE, 'uninstall.exe'): 'uninstall.exe',
}

# 排除项 (相对项目根的目录名 / 扩展名 / 文件名)
#
# ★ 这里是**目录名**匹配, 不是路径匹配 —— 所以不能简单地放 'node_modules' 进去:
#   trae/lib/node_modules 里装的是 Trae 客户端网络栈的真实依赖
#   (@aha-kit/net-win32-x64-msvc 的原生 .node), 那是**运行时必需**的,
#   砍掉它 Trae 通道直接起不来。本项目已踩过一次: 资源包里少了这两个文件,
#   装出来 server.js 报「未找到网络栈依赖」。
#   故用 EXCLUDE_DIR_PATHS 做精确路径排除, 只砍前端那份 node_modules。
EXCLUDE_DIRS = {'__pycache__', '.venv', 'logs', 'data', '.git', 'installer',
                'tests', 'runtime', 'toolchain'}
EXCLUDE_DIR_PATHS = {'desktop-ui/node_modules', 'desktop-ui/node_modules_store'}
EXCLUDE_EXTS = {'.pyc', '.log'}
EXCLUDE_FILES = {
    'config.json',      # ★ 真实配置 (token / api_key / device_id), 安装器另行生成空壳
    'MEMORY.md',        # 内部排障记忆, 不随安装包分发
    '.gitignore',
}

# 必须存在、否则安装包是残废的条目
# 少了任何一项, 装出来的东西就是残废的 —— 必须中止, 不能只打印警告。
# ★ uninstall.exe 在这里是**强制**的: 设置页「一键卸载」与桌面端 Rust 的
#   uninstall_app 都调它, 缺了用户就没有卸载入口。它由 build_exe.bat 第 2 步
#   生成在 installer/ 根下 —— 手工清理该目录时会把它一起删掉, 所以这里必须拦。
REQUIRED = [
    'uninstall.exe',
    'trae/server.js',
    'trae/lib/sscronet.dll',
    'trae/lib/@aha-kit/net/wrapper.js',
    'trae/lib/@aha-kit/net-win32-x64-msvc/index.win32-x64-msvc.node',
    'pic/open-ai.ico',
    'desktop/open-ai-desktop.exe',
]


def should_include(relpath):
    rel = relpath.replace('\\', '/')
    for bad in EXCLUDE_DIR_PATHS:
        if rel == bad or rel.startswith(bad + '/'):
            return False
    parts = rel.split('/')
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


def build():
    count = 0
    total_bytes = 0
    names = []
    if os.path.exists(OUT_ZIP):
        os.remove(OUT_ZIP)

    with zipfile.ZipFile(OUT_ZIP, 'w', zipfile.ZIP_DEFLATED) as zf:
        for include in INCLUDE:
            src = os.path.join(PROJECT_ROOT, include)
            if not os.path.exists(src):
                print('[跳过] 不存在: %s' % include)
                continue
            if os.path.isfile(src):
                if should_include(include):
                    zf.write(src, include)
                    names.append(include)
                    count += 1
                    total_bytes += os.path.getsize(src)
            else:
                for root, dirs, files in os.walk(src):
                    # 目录级剪枝: 与 should_include 用同一套判定 (含精确路径排除),
                    # 否则 trae/lib/node_modules 会在遍历阶段就被砍掉。
                    rel_root = os.path.relpath(root, PROJECT_ROOT).replace('\\', '/')
                    if rel_root == '.':
                        rel_root = ''
                    dirs[:] = [
                        d for d in dirs
                        if should_include((rel_root + '/' + d).lstrip('/') + '/x')
                    ]
                    for f in files:
                        full = os.path.join(root, f)
                        rel = os.path.relpath(full, PROJECT_ROOT).replace('\\', '/')
                        if should_include(rel):
                            zf.write(full, rel)
                            names.append(rel)
                            count += 1
                            total_bytes += os.path.getsize(full)

        for src, arcname in EXTRA_FILES.items():
            if os.path.exists(src):
                zf.write(src, arcname)
                names.append(arcname)
                count += 1
                total_bytes += os.path.getsize(src)
                print('  [附加] -> %s' % arcname)
            else:
                print('  [警告] 缺少 %s (安装器将没有卸载程序)' % arcname)

    # ── 自检: 缺件就失败, 不能产出一个"装完才发现少东西"的包 ──
    have = set(names)
    missing = [r for r in REQUIRED if r not in have]
    if missing:
        print('[错误] 资源包缺少必需项: %s' % ', '.join(missing))
        print('       桌面端: 先跑 desktop-ui/tools/build-tauri-release.ps1')
        print('       卸载器: 先跑 build_exe.bat 的 uninstall 步骤')
        return None

    size_mb = os.path.getsize(OUT_ZIP) / 1024 / 1024
    print('[完成] 打包 %d 个文件, %.1f MB -> %.1f MB zip'
          % (count, total_bytes / 1024 / 1024, size_mb))
    print('  输出: %s' % OUT_ZIP)
    return OUT_ZIP


if __name__ == '__main__':
    sys.exit(0 if build() else 1)
