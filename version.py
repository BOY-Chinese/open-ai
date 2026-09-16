# -*- coding: utf-8 -*-
"""open-ai 版本号定义。

发布新版本时同步修改此文件并打同名 tag (如 v3.0-dev)。
GUI「设置」页显示该版本号, 「一键更新」据此与 GitHub 最新 release 比较决定是否更新。
"""

APP_VERSION = 'dev-v3.1'

# 更新通道标识 (UPDATE_CHANNEL): 当前构建版本的更新通道, 决定「一键更新」在
# GitHub Release Assets 中精确匹配文件名含该关键词的安装包。dev 通道匹配
# open-ai-installer-dev.exe; portable 便携版 (独立会话维护, APP_VERSION 形如
# 'portable-v3.0') 走 portable 通道, 与本文件互不关联、各自独立发版。
UPDATE_CHANNEL = 'dev'

# 发布仓库 (owner/repo) —— 「检查更新」与「一键更新」查它的 latest release。
# 保留为占位值是有意的: 仓库由发布者决定, 不该把某个人的 GitHub 账号写死进源码。
# 部署时改这一处, 或设环境变量 OPEN_AI_UPDATE_REPO=<owner>/<repo> 覆盖。
UPDATE_REPO = 'owner/open-ai'
