#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 网页登录 → 应用 token 脚本
====================================
基于抓包逆向的"网页转登录"链路:
  打开 www.workbuddy.cn 登录页(桌面端内嵌) → 用户扫码/密码登录 →
  页面自动调用 POST /console/login/enterprise?state=<uuid>
  (带 x-device-token 与登录 session cookie) → 返回 accessToken/refreshToken

本脚本不关心 x-device-token 如何生成: 用 Playwright 打开登录页让页面自己走完整流程,
直接监听捕获 /console/login/enterprise 的响应即可。

流程:
  1. Playwright 打开 WorkBuddy 登录页 (headful, 用户可见)
  2. 用户手动登录 (扫码/密码)
  3. 监听网络响应, 捕获 /console/login/enterprise 返回的 accessToken/refreshToken
  4. 从 accessToken JWT 解出 userId (sub)
  5. 写入 open-ai/config.json workbuddy.accounts (按 userId 更新/追加)
  6. 写入 open-ai/config.json (签到由内部 signin_all.py 读取)
  7. 自动关闭浏览器

用法:
  python login_workbuddy.py [--timeout 300]      # 等待登录的超时秒数 (默认 300)
  python login_workbuddy.py --browser chromium   # 指定浏览器 (chromium/chrome/msedge/firefox)
  python login_workbuddy.py --reuse-profile      # 复用上次登录会话 (接着没走完的登录)

★ 浏览器方案 (v2.6 起与国际版 login_workbuddy_intl.py 对齐, 更稳定):
  旧版直接 p.chromium.launch(channel='msedge') 拉起系统 Edge —— Playwright
  浏览器自带自动化特征 (navigator.webdriver=true), 走 OAuth/人机挑战时容易被
  卡死 (国际版实测复现过)。现默认 Playwright 自带 Chromium (版本与 playwright
  包锁死, 最稳) + 反自动化检测, 且**每次登录用全新独立 profile**
  (data/pw_profiles/wb/run-<时间戳>/): 干净会话 —— 同一通道可连续添加多个
  账号, 不会被上一次的登录态自动顶号; 上次没走完的登录可用 --reuse-profile
  复用最近会话。
  启动失败自动按 chromium → chrome → msedge → firefox 降级。
"""
import base64
import json
import os
import shutil
import sys
import time
import uuid

# 强制 stdout/stderr 用 utf-8, 避免 GBK 控制台因中文/emoji 崩溃
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# Playwright 由 open-ai/.venv 提供

# ★ 打包态 __file__ 是相对路径, abspath 跟着 CWD 走 —— config 必须钉死安装根。
try:
    import app_paths as _ap
    OPENAI_CFG = _ap.CONFIG_PATH
    # ★ 便携版把 Playwright 浏览器内嵌在 <root>\ms-playwright, 必须指路 ——
    #   否则 Playwright 认为「浏览器未安装」, 启动失败后静默降级到系统 Edge,
    #   而 Edge 恰是本次换 Chromium 要绕开的那个不稳定路径 (见文件头说明)。
    _ap.ensure_playwright_browsers_path()
    # 浏览器 profile 根目录 (data/ 下, 用户数据)。每次登录在其中开一个全新的
    # run-<时间戳> 子目录 —— 干净会话才能在同一通道添加多个账号 (见
    # _pick_profile_dir)。
    PROFILE_ROOT = os.path.join(_ap.DATA_DIR, 'pw_profiles', 'wb')
    # v2.6 早期用过的固定 profile (SSO 登录态常驻会自动顶号), 已废弃, 启动时清理。
    _LEGACY_PROFILE_DIR = os.path.join(_ap.DATA_DIR, 'pw_profile_wb')
except Exception:  # 源码态单独运行的兜底
    HERE = os.path.dirname(os.path.abspath(__file__))
    OPENAI_CFG = os.path.join(HERE, '..', 'config.json')
    PROFILE_ROOT = os.path.join(HERE, '..', 'data', 'pw_profiles', 'wb')
    _LEGACY_PROFILE_DIR = os.path.join(HERE, '..', 'data', 'pw_profile_wb')
LOGIN_URL_TMPL = ('https://www.workbuddy.cn/login/?platform=workbuddy'
                  '&state={state}&version=5.3.12&loginSessionId={lsid}')
ENTERPRISE_MARK = '/console/login/enterprise'

# 启动优先级: 自带 Chromium (版本与 playwright 包锁死, 不受系统浏览器自动更新
# 影响, 最稳) → 系统 Chrome → 系统 Edge (旧版默认) → Firefox。
# 任一档启动失败 (未安装/版本不匹配/profile 被占用) 自动降级到下一档。
BROWSER_FALLBACK = ('chromium', 'chrome', 'msedge', 'firefox')

# 反自动化检测注入: 只隐藏 navigator.webdriver 这一条硬特征。
# ★ 浏览器一律用**原生 UA** (不传 user_agent 覆盖) —— 伪造身份会与
#   client-hints 品牌头自相矛盾, 风控正抓这种不一致; 原生长相最可信。
STEALTH_JS = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
)


def jwt_user_id(token: str) -> str:
    try:
        parts = token.split('.')
        pad = parts[1] + '=' * (-len(parts[1]) % 4)
        p = json.loads(base64.urlsafe_b64decode(pad))
        return str(p.get('sub', ''))
    except Exception:
        return ''


def _pick_profile_dir(reuse=False, firefox=False):
    """挑选本次登录的浏览器 profile 目录 (默认每次**全新**)。

    ★ 为什么不再用固定 profile: 固定 profile 里上一次的登录态 (SSO cookie)
    会让登录页自动顶上一次的账号 —— 同一通道就没法添加第二个账号了。
    现改为每次登录一个全新 run-<时间戳> 目录, 添加任意多个账号互不干扰;
    上次没走完的登录可用 --reuse-profile 复用最近一次会话接着来。

    除「本次 + 最近一次」外的旧会话目录自动清理 (被占用的删不掉则忽略)。
    """
    root = os.path.join(PROFILE_ROOT, 'firefox') if firefox else PROFILE_ROOT
    os.makedirs(root, exist_ok=True)
    runs = sorted(d for d in os.listdir(root)
                  if d.startswith('run-') and os.path.isdir(os.path.join(root, d)))
    if reuse and runs:
        udd = os.path.join(root, runs[-1])
        print(f'[浏览器] 复用上次登录会话: {udd}')
    else:
        udd = os.path.join(root, 'run-' + time.strftime('%Y%m%d-%H%M%S'))
        try:
            os.makedirs(udd)                  # 先占住目录, 并发重跑才不会撞车
        except Exception:                     # 已存在 (同一秒重跑/并发) → 加随机后缀
            udd += '-' + str(uuid.uuid4())[:8]
            os.makedirs(udd, exist_ok=True)
        print(f'[浏览器] 全新登录会话 (干净状态, 便于添加不同账号): {udd}')
    for d in runs[:-1]:                       # 只留最近一次旧会话供 --reuse-profile
        shutil.rmtree(os.path.join(root, d), ignore_errors=True)
    return udd


def launch_browser(p, choice='', reuse_profile=False):
    """启动浏览器 (全新会话 + 反自动化检测), 返回 (context, 实际浏览器名)。

    choice 为空按 BROWSER_FALLBACK 顺序自动尝试; 指定则先试 choice 再降级其余。
    ★ 过 OAuth 人机挑战的关键是反自动化检测 (隐藏 webdriver 等);
      launch_persistent_context 只是承载 profile 目录的方式 —— 目录由
      _pick_profile_dir 决定 (默认全新, --reuse-profile 复用上次)。
    """
    order = ([choice] if choice else []) + \
            [b for b in BROWSER_FALLBACK if b != choice]
    errs = []
    for name in order:
        # Chromium 系与 Firefox 的 profile 格式互不兼容, 分开根目录。
        udd = _pick_profile_dir(reuse_profile, firefox=(name == 'firefox'))
        try:
            if name == 'firefox':
                ctx = p.firefox.launch_persistent_context(
                    udd, headless=False, locale='zh-CN')
            else:
                ctx = p.chromium.launch_persistent_context(
                    udd,
                    headless=False,
                    channel=None if name == 'chromium' else name,
                    locale='zh-CN',
                    no_viewport=True,      # 视口跟随窗口, 不留 1280x720 自动化痕迹
                    args=['--disable-blink-features=AutomationControlled',
                          '--no-first-run', '--no-default-browser-check'],
                    ignore_default_args=['--enable-automation'],
                )
            ctx.add_init_script(STEALTH_JS)
            print(f'[浏览器] 已启动 {name} (profile: {udd})')
            return ctx, name
        except Exception as e:
            errs.append(f'{name} → {type(e).__name__}: {e}')
            print(f'[降级] {name} 启动失败, 尝试下一候选 ...')
    raise RuntimeError(
        '所有候选浏览器都无法启动:\n  ' + '\n  '.join(errs)
        + '\n  提示: 若报 profile 被占用, 先关掉上一次登录残留的浏览器窗口')


def main():
    timeout = 300
    browser_choice = ''
    reuse_profile = False
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == '--timeout' and i + 1 < len(args):
            timeout = int(args[i + 1])
        elif a == '--browser' and i + 1 < len(args):
            browser_choice = args[i + 1].strip().lower()
        elif a in ('--reuse-profile', '--reuse'):
            reuse_profile = True

    # 清理 v2.6 早期的固定 profile (SSO 登录态常驻会自动顶号, 无法加多账号)
    if os.path.isdir(_LEGACY_PROFILE_DIR):
        shutil.rmtree(_LEGACY_PROFILE_DIR, ignore_errors=True)

    from playwright.sync_api import sync_playwright

    state = str(uuid.uuid4())
    lsid = str(uuid.uuid4())
    login_url = LOGIN_URL_TMPL.format(state=state, lsid=lsid)

    print('=' * 60)
    print('  WorkBuddy 网页登录助手')
    print('  即将打开浏览器, 请在网页中完成登录 (扫码/密码)')
    print(f'  等待登录超时: {timeout} 秒')
    print('=' * 60)

    result = {}

    with sync_playwright() as p:
        ctx, browser_name = launch_browser(p, browser_choice, reuse_profile)
        # 诊断: 记录加载失败的请求
        ctx.on('requestfailed', lambda r: print(
            f'[请求失败] {r.url[:120]} → {r.failure}'))

        # 持久化 context 自带一个空白初始页: 直接复用, 免得多一个 about:blank 标签
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_response(resp):
            if ENTERPRISE_MARK in resp.url and resp.status == 200:
                try:
                    j = resp.json()
                except Exception:
                    return
                data = (j or {}).get('data') or {}
                at = data.get('accessToken', '')
                rt = data.get('refreshToken', '')
                if at:
                    result['accessToken'] = at
                    result['refreshToken'] = rt
                    result['userId'] = jwt_user_id(at)

        page.on('response', on_response)
        # 网络不稳时 goto 容易超时, 重试 3 次
        goto_ok = False
        for attempt in range(1, 4):
            try:
                page.goto(login_url, wait_until='domcontentloaded', timeout=60000)
                goto_ok = True
                break
            except Exception as e:
                print(f'[警告] 第 {attempt}/3 次打开登录页失败: {type(e).__name__}: {e}')
                if attempt < 3:
                    print('       网络似乎不稳, 5 秒后重试 ...')
                    time.sleep(5)
        if not goto_ok:
            print('[错误] 三次都打不开登录页, 基本可判定是网络问题。')
            print('       排查: 1) Watt(Steam++)「网络加速」/ Clash 等代理是否在拦截本机流量')
            print('             2) 换网络再试  3) 浏览器能否手工打开 https://www.workbuddy.cn/login')
            ctx.close()
            sys.exit(1)
        print('[等待] 请在弹出的浏览器中登录 WorkBuddy 账号...')
        print('[提示] 登录后页面若弹"请求参数错误(400)"提示, 可忽略——登录实际已成功, 脚本会自动抓取 token')

        closed_by_user = False
        start = time.time()
        while time.time() - start < timeout:
            if result.get('accessToken'):
                break
            try:
                page.wait_for_timeout(1000)  # 关键: 必须用 playwright 的等待, 否则事件不实时投递
            except Exception:
                closed_by_user = True        # 用户手动关闭浏览器 —— 优雅收尾不抛堆栈
                break

        if closed_by_user:
            print('[中止] 浏览器被手动关闭, 本次登录未完成。')
            sys.exit(1)

        if not result.get('accessToken'):
            print('[超时] 未捕获到 /console/login/enterprise 响应, 尝试兜底提取...')
            try:
                ls = page.evaluate(
                    "() => { const o = {}; for (let i=0;i<localStorage.length;i++){"
                    "const k=localStorage.key(i); o[k]=localStorage.getItem(k);} return o; }")
                print('[兜底] localStorage:', json.dumps(ls, ensure_ascii=False)[:2000])
            except Exception as e:
                print('[兜底] localStorage 读取失败:', e)
            try:
                cookies = ctx.cookies()
                print('[兜底] cookies:', json.dumps(
                    [{'name': c.get('name'), 'domain': c.get('domain'),
                      'value': (c.get('value') or '')[:80]} for c in cookies],
                    ensure_ascii=False)[:1500])
            except Exception as e:
                print('[兜底] cookies 读取失败:', e)
            print('[兜底] 当前页面 URL:', page.url)
            ctx.close()
            sys.exit(1)

        at, rt, uid = result['accessToken'], result['refreshToken'], result['userId']
        print(f'[成功] 获取 accessToken: userId={uid} len={len(at)}')
        print(f'[成功] refreshToken: {"有" if rt else "无"} len={len(rt)}')

        # ---------- 写入 open-ai/config.json ----------
        cfg = json.load(open(OPENAI_CFG, encoding='utf-8'))
        wb = cfg['providers'].setdefault('workbuddy', {})
        accounts = wb.setdefault('accounts', [])
        found = False
        for acc in accounts:
            if acc.get('userId') == uid:
                acc['accessToken'] = at
                acc['refreshToken'] = rt
                found = True
                print(f'[更新] workbuddy 账号 {acc.get("userId")} token 已更新')
                break
        if not found:
            accounts.append({'accessToken': at, 'refreshToken': rt, 'userId': uid})
            print(f'[新增] workbuddy 新账号 {uid} 已加入 accounts')
        json.dump(cfg, open(OPENAI_CFG, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
        print('[写入] open-ai/config.json 完成 (签到由内部 signin_all.py 直接读此配置)')

        ctx.close()
        print()
        print(f'登录完成, 浏览器({browser_name})已自动关闭')
        print('   提示: 重启 open-ai 网关后新账号即可参与轮询')


if __name__ == '__main__':
    main()
