#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成 open-ai-trae.exe (node 的品牌化副本) → installer/exes/open-ai-trae.exe
========================================================================
Trae 后端 (trae/server.js) 需要 Node 运行时; exe 打包方案下 broker 以
open-ai-trae.exe <安装根>\\trae\\server.js 拉起, 故必须提供一个品牌化的 node 副本。

本脚本:
  1. 定位本机 node.exe (Program Files\\nodejs\\node.exe)
  2. 复制为 exes\\open-ai-trae.exe
  3. 复用 procname 的 Win32 资源注入 (图标 + 版本信息), 失败则仅提示不阻塞

运行: python build_trae_shim.py
产出: exes/open-ai-trae.exe (供 build_exe.bat 打包进安装器)
"""
import os
import shutil
import sys

# 把项目根加到 sys.path, 以复用 procname 的 _find_node / _inject_resources
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import procname  # noqa: E402

OUT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'exes')
TARGET = os.path.join(OUT_ROOT, 'open-ai-trae.exe')


def main():
    node = procname._find_node()
    if not node or not os.path.isfile(node):
        print('[trae-shim] 未找到 node.exe, 跳过 — Trae 通道将禁用 (WorkBuddy 不受影响)')
        return 0
    os.makedirs(OUT_ROOT, exist_ok=True)
    # 功能核心: node 副本即可运行 server.js; 品牌化失败不阻塞
    fd = None
    try:
        shutil.copyfile(node, TARGET)
    except Exception as e:
        print('[trae-shim] 复制 node.exe 失败: %s' % e)
        return 1
    try:
        procname._inject_resources(TARGET, procname.SPECS['trae'])
        print('[trae-shim] 已生成 %s (来自 %s, 已注入资源)' % (TARGET, node))
    except Exception as e:
        print('[trae-shim] %s 已生成, 资源注入失败(不影响功能): %s' % (os.path.basename(TARGET), e))
    return 0


if __name__ == '__main__':
    sys.exit(main())
