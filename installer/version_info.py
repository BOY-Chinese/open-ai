# -*- coding: utf-8 -*-
"""统一版本资源 (品牌 exe 与安装器共用)。发布新版本时只需改根目录 version.py 一处。

Windows 版本资源 (FileVersion / ProductVersion) 的取值来自根目录 version.py 的
APP_VERSION (如 'v3.0-dev'), 自动提取纯数字段 (3.0.0) 作为版本资源 ——
项目根版本号保持单源, 不再另存一份硬编码数字串。
"""
import os
import re
import sys

# 单源: 从根目录 version.py 读取 APP_VERSION; 读取失败兜底默认值。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
try:
    from version import APP_VERSION
except Exception:
    APP_VERSION = '3.0.0'

# 纯数字版本号 (major.minor.patch): 'v3.0-dev' -> '3.0.0'。
_VER_NUMS = re.findall(r'\d+', APP_VERSION or '')
FILEVERSION = '.'.join((_VER_NUMS + ['0', '0', '0'])[:3])

from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo,
                                                 StringTable, StringStruct,
                                                 VarFileInfo, VarStruct,
                                                 VSVersionInfo)


def get_version_info(desc, internal_name, orig_name):
    v = FILEVERSION.split('.')
    vers = tuple(int(x) for x in (v + ['0', '0', '0'])[:4])
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=vers, prodvers=vers, mask=0x3f, flags=0x0,
                          OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
        kids=[
            StringFileInfo([
                StringTable('040904b0', [
                    StringStruct('CompanyName', 'open-ai project'),
                    StringStruct('FileDescription', desc),
                    StringStruct('FileVersion', FILEVERSION),
                    StringStruct('InternalName', internal_name),
                    StringStruct('LegalCopyright', 'open-ai project'),
                    StringStruct('OriginalFilename', orig_name),
                    StringStruct('ProductName', 'open-ai'),
                    StringStruct('ProductVersion', FILEVERSION)]),
            ]),
            VarFileInfo([VarStruct('Translation', [0, 1200])]),
        ])
