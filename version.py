# -*- coding: utf-8 -*-
"""open-ai 版本号定义。

发布新版本时同步修改此文件并打同名 tag (如 v2.4-dev)。
GUI「设置」页显示该版本号, 「一键更新」据此与 GitHub 最新 release 比较决定是否更新。
"""

APP_VERSION = 'v2.4-dev'

# 更新通道标识 (UPDATE_CHANNEL): 当前构建版本的更新通道, 决定「一键更新」在
# GitHub Release Assets 中精确匹配文件名含该关键词的安装包。dev 通道匹配
# open-ai-installer-dev.exe; portable 便携版 (独立会话维护, APP_VERSION 形如
# 'portable-v2.4') 走 portable 通道, 与本文件互不关联、各自独立发版。
UPDATE_CHANNEL = 'dev'
