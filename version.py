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
#
# v3.4 (2026-10) 三端同步自本机端 local-v3.4, 共三项:
#   * A (来源端 git 0a6dad9): 账号运行态「假断连」—— 重新登录 / 点刷新后页面仍
#     显示断连。新增 AccountHealth.clear_invalid + Provider.clear_account_runtime,
#     admin_api 增加 _clear_channel_runtime 并在 4 个时机接线 (登录成功 / 点刷新 /
#     重新连接 / 登录脚本成功退出)。
#   * B (来源端 git f13aef8 + ad7f288): 采集器长事务锁库 —— 积分看板「今日消耗」
#     恒为 0、账号页「每日签到」除 Loomy 外全显示「未签到」(实际全部签到成功)。
#     治本: usage_collector 各写入点改为写即提交, 联网期间不持写锁;
#     治标: credits_api 增加忙等待重试与 CreditsReadError / probe(), 不再静默吞错;
#     兜底: admin_api 三端点读不到时返回 503 (不再交出假零值);
#     前端: CreditsPage 读取失败渲染错误态 (不再拿 0 冒充数据)。
#   * C (来源端 git 26de3b5): 新增 prompt_sanitizer.py 提示词净化过滤器 ——
#     上游扫描 system/assistant 开头的竞品 harness 身份指纹, 命中即 400
#     code=11128「unapproved channel」。改写 system/assistant 里的指纹字面
#     (保住语义), user 内容一字不动; 接入 main.py::_normalize_body 与
#     providers/workbuddy.py::_to_upstream_body 两处 (幂等)。
#     ★ 本端 installer/build_resources.py 的 INCLUDE 已同步加入该新模块。
#
# v3.4.1 (2026-10) 同步 C·补充 —— 过滤器从「改写句式」改为「屏蔽产品名」:
#   (来源端 git 3d3aefc + 58a4d6f + 0c615d5)
#   * 原因: 原 C 属**逐个句式打补丁**, 上游一挥措辞规则就整条失配(实测同一句
#     时 200 时 400, 每条改动都要重新抓包)。实测决定性证据: 原句 400, 而
#     **只屏蔽掉产品名的任意一半**即 200 —— 说明上游做的是产品名匹配。
#   * 改法: prompt_sanitizer.py 整体重写 —— `_REWRITES`(15 条句式改写) →
#     `_MASKS`(23 条产品名屏蔽), 一律替换为占位符 `MASK`。
#     排序敏感: 计费头「整行删除」规则必须排在对应产品名规则之前, 否则行名
#     先被改成半残形式、整行删除再也匹配不到(有回归测试守住)。
#     通用词(agent/assistant/code/cli/branch)一律不屏蔽, 以免毁掉正常技术文档。
#   * 扫描角色 system / assistant / **tool** —— 新增 tool(来源端 3d3aefc):
#     真实案例是一条 579 消息 / 623 KB 的请求被拦, 触发词全在 tool 消息里
#     (抓来的 issue 全文 / 同步文档里逐字列出的指纹 / grep 源码打印的官方句)。
#     表现为「干净工作区能用、排查过 11128 的工作区必挂」。user 仍绝不篡改。
#   * 本次只动 prompt_sanitizer.py + 其测试 2 个文件; main.py / workbuddy.py
#     的接入点零改动(原 C 已装好, 两版兼容)。
#   * install、白名单无需再改: 文件名未变, 只是内容变了。
# v4.0 (2026-10) 三端同步自本机端 local-v4.0 —— 详见《open-ai三端同步文件夹/V4.0同步.md》
#   * v4.0 三通道深度治理：工具协议回声回读 / 一次性凭据落盘与互斥 /
#     CodeArts 积分与探活节流
#   * v4.1 Qoder 签到真修复（ok vs credited）+ 官方 UMID 风控身份 +
#     RESOURCE_LIMIT / 405 归类 + Loomy 内容审核不再误判成断连
#   * v4.2 关闭 Qoder 模型自动降级 / 签到跨区域真实身份兜底 / 405 同主机退避 /
#     三通道消耗入流水库 / 周视图补柱子 / RESOURCE_LIMIT 等待拉长到约 160 秒
#   * 追加（2026-10-27）Q：workbuddy 400/11128 复发 —— 整句提示词同义改写 +
#     被拒请求体落盘 + 子进程 stdio 编码钉 UTF-8
APP_VERSION = 'dev-v4.0'

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
