#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_desktop_bundle.py — 桌面端 exe 内嵌的是哪一版前端 (构建门禁)
==================================================================
Tauri 的资源表以**明文**存 asset 文件名, 而前端产物名是内容哈希, 所以
「exe 里出现的 index-<hash>.js」唯一确定它打包时用的是哪一份 desktop-ui/dist。

为什么需要这道门禁:
  桌面端 exe 是**单独一步**产出的 (cargo build), 很容易装出「安装包里的 exe 是
  上一次的」。而上一版前端可能还带着清洗前的演示数据 —— 真实网关 api_key 会被
  Vite 原样打进前端产物, 随安装包公开。这一层光检查源文件是查不出来的。
  本项目实测踩过: desktop-ui/dist/assets/ 里同时留着新旧两份 index-*.js,
  旧的那份被一起嵌进了 exe。

用法:
    python check_desktop_bundle.py [exe 路径 ...]
    (缺省检查 ../desktop/open-ai-desktop.exe)

退出码: 0 = 可用版本; 1 = 禁用版本 / 版本无法判定
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_EXE = os.path.join(HERE, '..', 'desktop', 'open-ai-desktop.exe')

# 清洗**前**的 dist (含真实网关 api_key 与真实账号名) —— 见到就中止构建
DIRTY_BUNDLES = ['index-DFsuRhLZ.js']
# 清洗后的各版本: 3q5GVwtK 去过密钥; BusDnuvG 再去掉发布仓库归属
# 每次改前端都会产生新哈希 —— 构建出新 exe 后要把对应哈希登记进来。
# Bu3xmmhl: v3.0-portable 首版前端 (Loomy 通道 + Auto路由连) —— ★已作废:
#   其 mock 数据带了主机同款 Loomy 账号/模型缓存, 已被 DH4ONbl3 替换。
# DH4ONbl3: v3.0-portable 修正版前端 (mock 清除全部 Loomy 种子数据;
#   dist 已过真值/形态扫描, sk- 演示占位沿用旧版风格)。
# BRHJC03a: v3.0-portable Loomy 桌面通道登录版前端 (sendLoomyDesktopCode /
#   loomyDesktopLogin 接入; mock 账号仍为「示例账号(…)/随机数」命名,
#   sk- 仅为 DEMO-KEY-NOT-REAL 占位, 已过真值/形态扫描 —— 2026-09-14)。
# udsI5bB_: Auto路由连 Loomy 前缀显示修复版前端 (channelOfRouteModel 对齐
#   后端 route_provider 的 lm- 前缀判定; 仅改通道显示逻辑, mock/演示数据
#   与 BRHJC03a 完全一致, 无新增数据 —— 2026-09-15)。
CLEAN_BUNDLES = ['index-DcXtEmiT.js', 'index-BusDnuvG.js', 'index-3q5GVwtK.js',
                 'index-DH4ONbl3.js', 'index-BRHJC03a.js', 'index-udsI5bB_.js',
                 # 3fBWhgE_: dev-v3.1 / portable-v3.1 前端 —— 修复三处用户实测 bug
                 #   (账号页刷新补签 / 积分看板刷新拉新流水 / 版本号显示 dev-v3.1)。
                 #   新增 POST /accounts/signin/refresh 与 /credits/refresh 两个端点调用;
                 #   mock 演示数据仍为占位值, 已过真值扫描 (13 项真实凭据 0 泄漏)。
                 'index-3fBWhgE_.js',
                 # DU-XzvPp: 2026-09-16 修复「密钥轮换后界面卡在启动门 / 拿不到
                 #   后端数据」后的前端 —— lib/gateway.ts 探活遇 401 重读 config.json
                 #   再试、AppLayout 离线每 20 秒自愈。仅改这两处逻辑, mock / 演示
                 #   数据一字未动; 已按本仓 sanitize_known_values.txt (18 条真值)
                 #   扫描产物: 0 命中 (唯一 sk- 命中是既有的 DEMO-KEY-NOT-REAL 占位)。
                 'index-DU-XzvPp.js',
                 # j58bi9IW: 2026-09-18 WorkBuddy 国际版「网页端拿积分」并入后的前端
                 #   —— 账号页国际版未拿到积分时的 tooltip 由「暂无签到渠道，无每日
                 #   签到积分」改为「积分由后台自动入账，无法通过刷新主动获取」;
                 #   刷新提示剔除 WorkBuddy_IE (避免延迟入账被误报成「未签上」);
                 #   lib/backend.ts 演示态注释同步。仅改提示文案与一处过滤,
                 #   mock / 演示数据一字未动; 已按本仓 sanitize_known_values.txt
                 #   (14 条真值) 扫描产物: 0 命中 (唯一 sk- 命中是既有的
                 #   DEMO-KEY-NOT-REAL 占位)。
                 'index-j58bi9IW.js',
                 # BsQeg5Rl: 2026-09-19 **v3.2 全量前端**（dev 仓）—— 本次同步
                 #   了改动 B/C/D/E 的全部前端:
                 #     B 国际版文案改「后台自动入账」+ 刷新剔除 WorkBuddy_IE;
                 #     C AutoRouterPage 整页重做(多链/右键菜单/模型简报三态);
                 #     D 倍率三态(未知显示 `--` 而非 0.00) + modelCache 兼容旧结构;
                 #     E 一键更新对话框(下载进度/确认安装) + 删除硬编码仓库地址
                 #       (改由后端 /v1/admin/version 下发 repo)。
                 #   由 `node node_modules/vite/bin/vite.js build` 产出(2493 模块),
                 #   同批 CSS 为 index-fW-_RaXL.css。
                 'index-BsQeg5Rl.js',
                 # Cmfx0B8k: 2026-09-19 **移除本机定制「启动dsh」后的前端**
                 #   (`dev` 仓) —— master 指出上一版误把仅本机使用的
                 #   「启动dsh」按钮一并同步进了发布版。本次删除该功能:
                 #     AccountsPage.tsx 移除按钮与 onLaunchDsh/dshBusy 及 Rocket 图标;
                 #     httpBackend/backend 移除 launchDsh 实现(含 mock);
                 #     oplogBackend 移除 launchDsh 埋点; domain.ts 移除 DshLaunchResult;
                 #     后端 admin_api.py 本就不含 /dsh/launch(同步时已剔除该定制段),
                 #     故删前端后该端点在前端已无任何引用。
                 #   产物 830.35 kB(较 BsQeg5Rl 的 831.83 kB 略小), CSS 仍 index-fW-_RaXL.css。
                 'index-Cmfx0B8k.js']

# 可选加强: 本机若放了「已知真实值」清单 (不入库, 见 sanitize_check.py),
# 顺带在 exe 里搜一遍。前端资源是压缩存放的, 明文搜不到 bundle 里的密钥;
# 但 Rust 侧常量与 PE 版本元数据是明文, 值得一起过一道。
LOCAL_VALUES_FILE = os.path.join(HERE, '..', 'sanitize_known_values.txt')
ASSET_NAME_RE = re.compile(rb'index-[A-Za-z0-9_\-]{8}\.js')


def load_known_values():
    if not os.path.exists(LOCAL_VALUES_FILE):
        return []
    out = []
    with io.open(LOCAL_VALUES_FILE, encoding='utf-8', errors='replace') as f:
        for line in f:
            v = line.strip()
            if v and not v.startswith('#') and len(v) >= 12:
                out.append(v)
    return out


def probe(path, known_values):
    with open(path, 'rb') as f:
        data = f.read()
    names = set(m.group(0).decode() for m in ASSET_NAME_RE.finditer(data))
    dirty = sorted(n for n in names if n in DIRTY_BUNDLES)
    clean = sorted(n for n in names if n in CLEAN_BUNDLES)
    leaked = [v for v in known_values if v.encode() in data]
    return sorted(names), dirty, clean, leaked


def main():
    paths = sys.argv[1:] or [DEFAULT_EXE]
    known = load_known_values()
    bad = 0
    for p in paths:
        try:
            names, dirty, clean, leaked = probe(p, known)
        except OSError as e:
            print('[FAIL] %s: %s' % (p, e))
            bad = 1
            continue

        if dirty or leaked:
            bad = 1
            print('[FAIL] %s' % p)
            if dirty:
                print('        内嵌的是清洗**前**的前端: %s' % ', '.join(dirty))
            if leaked:
                print('        明文里含本机已知真值 %d 处: %s'
                      % (len(leaked), ', '.join(v[:10] + '…' for v in leaked[:3])))
            print('        重建: cd desktop-ui && npm run build && '
                  'powershell -File tools\\build-tauri-release.ps1')
            continue

        if clean:
            print('[ OK ] %s  (内嵌 %s; 真值扫描%s)'
                  % (p, ', '.join(clean), '已加载清单' if known else '无清单可跑'))
        elif names:
            # 认得出 bundle 名但不在已知名单里 —— 多半是前端刚改过还没登记。
            # 不能默认放行, 否则这道门就白装了。
            bad = 1
            print('[FAIL] %s: 内嵌 %s, 不在已知名单里\n'
                  '        确认它是清洗后的版本后, 把文件名加进 CLEAN_BUNDLES'
                  % (p, ', '.join(names)))
        else:
            bad = 1
            print('[FAIL] %s: 找不到任何 asset 文件名, 无法判定内嵌版本' % p)
    return bad


if __name__ == '__main__':
    sys.exit(main())
