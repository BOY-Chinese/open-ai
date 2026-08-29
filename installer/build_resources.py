#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 open-ai 项目资源打包成 installer/resources.zip
==================================================
生成安装器需要的资源包 (内含全部代码 + trae/lib 依赖, 不含 venv/logs/data)。
运行: python build_resources.py
产出: installer/resources.zip
"""
import os
import sys
import zipfile

# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_ZIP = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources.zip')

# 需要打包的顶层文件/目录
INCLUDE = [
    'main.py', 'daemon.py', 'watchdog_boot.py', 'anthropic_api.py',
    'requirements.txt', 'README.md', 'MEMORY.md', '.gitignore',
    'start.bat', 'start_hidden.ps1', 'open-ai-autostart.bat',
    '账号管理.bat',
    'providers', 'scripts', 'trae', 'tests', 'pic',
]
# 额外文件 (来自 installer/ 目录, 打进资源包)
# 注: pic/open-ai.ico 已随 pic/ 目录遍历自动包含, 无需重复
EXTRA_FILES = {
    # 独立卸载程序 (由 build_exe.bat 先生成 installer/uninstall.exe, 再打进资源)
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uninstall.exe'):
        'uninstall.exe',
    # 一键启动器 (由 build_exe.bat 先生成 installer/open-ai-launcher.exe, 再打进资源)
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'open-ai-launcher.exe'):
        'open-ai-launcher.exe',
}
# 排除项 (相对 project root)
EXCLUDE_DIRS = {'__pycache__', '.venv', 'logs', 'data', '.git', 'installer'}
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
        # 追加额外图标文件
        for src, arcname in EXTRA_FILES.items():
            if os.path.exists(src):
                zf.write(src, arcname)
                count += 1
                total_bytes += os.path.getsize(src)
                print(f'  [图标] -> {arcname}')
    size_mb = os.path.getsize(OUT_ZIP) / 1024 / 1024
    print(f'[完成] 打包 {count} 个文件, {total_bytes/1024/1024:.1f} MB -> {size_mb:.1f} MB zip')
    print(f'  输出: {OUT_ZIP}')
    return OUT_ZIP


if __name__ == '__main__':
    build()