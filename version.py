# -*- coding: utf-8 -*-
"""open-ai 版本号定义。

发布新版本时同步修改此文件并打同名 tag (如 v3.0-dev)。
GUI「设置」页显示该版本号, 「一键更新」据此与 GitHub 最新 release 比较决定是否更新。
"""

# v3.3 (2026-10) 追加: Trae 视觉(图片输入)通道 —— 见《Trae视觉通道实现文档》
#   * trae/server.js convertMessages(): image_url / image 块原样保留 (原先被拍平成
#     {type,text} 丢掉图片数据, 上游回 "the param is invalid", 被误判成
#     「Trae 不支持图片」); developer 角色 → system
#   * image_util.py (新增): 本地路径 / http(s) URL / 残缺 data URI → base64
#     data URI + 魔数校验 (上游只认 data:<mime>;base64,<b64>)
#   * main.py: 请求入口 _normalize_body() 统一归一化 (developer→system 对所有
#     provider 生效, 含 /v1/messages); 阻塞 IO 走 asyncio.to_thread 不冻结事件循环
APP_VERSION = 'dev-v3.3'

# 更新通道标识 (UPDATE_CHANNEL): 当前构建版本的更新通道, 决定「一键更新」在
# GitHub Release Assets 中精确匹配文件名含该关键词的安装包。dev 通道匹配
# open-ai-installer-dev.exe; portable 便携版 (独立会话维护, APP_VERSION 形如
# 'portable-v3.0') 走 portable 通道, 与本文件互不关联、各自独立发版。
UPDATE_CHANNEL = 'dev'

# 发布仓库 (owner/repo) —— 「检查更新」与「一键更新」查它的 latest release。
# ★ 必须填真实发布仓库, 不能留占位值: 本字段是「一键更新」查询 release 的
#   唯一来源 (admin_api._fetch_latest_release 拼
#   https://api.github.com/repos/<UPDATE_REPO>/releases/latest)。
#   留成 'owner/open-ai' 的话, 装机用户点「一键更新」永远查不到任何 release,
#   界面只会提示「检查更新失败」—— 整条更新链路对最终用户直接失效。
#   （一键更新是面向**用户**的功能, 不是本机自用功能。）
# 仍可用环境变量 OPEN_AI_UPDATE_REPO=<owner>/<repo> 临时覆盖, 但那是调试手段,
# 正式发布必须写在本文件里。
UPDATE_REPO = 'BOY-Chinese/open-ai'
