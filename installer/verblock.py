# -*- coding: utf-8 -*-
"""构建期版本块生成器: build_exe.bat 先跑本脚本生成各 *_ver.py, 供 spec 引用。

用法: python verblock.py [通道]  —— 通道缺省 dev, 决定安装器的
InternalName / OriginalFilename (open-ai-installer-<通道>.exe)。
"""
import os
import sys
from version_info import get_version_info

CHANNEL = (sys.argv[1].strip() if len(sys.argv) > 1 and sys.argv[1].strip()
           else 'dev')

TARGETS = {
    'ver_daemon.py':   ('open-ai 后台服务', 'open-ai-daemon', 'open-ai-daemon.exe'),
    'ver_gateway.py':  ('open-ai gateway :8000', 'open-ai-gateway', 'open-ai-gateway.exe'),
    'ver_task.py':     ('open-ai task (scripts)', 'open-ai-task', 'open-ai-task.exe'),
    'ver_cli.py':      ('open-ai control CLI', 'open-ai', 'open-ai.exe'),
    'ver_desktop.py':  ('open-ai desktop', 'open-ai-desktop', 'open-ai-desktop.exe'),
    'ver_uninstall.py': ('open-ai 卸载程序', 'uninstall', 'uninstall.exe'),
    'ver_installer.py': ('open-ai 安装程序 (%s)' % CHANNEL,
                         'open-ai-installer-%s' % CHANNEL,
                         'open-ai-installer-%s.exe' % CHANNEL),
}
for fn, (desc, internal, orig) in TARGETS.items():
    vi = get_version_info(desc, internal, orig)
    with open(fn, 'w', encoding='utf-8') as f:
        f.write(str(vi))
    print('生成', fn)
