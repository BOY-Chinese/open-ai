#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TRAE 网页登录 → 自动抓 cookie → 换 token 脚本
=============================================
流程:
  1. Playwright 打开 TRAE 登录页 (headful, 用户可见)
  2. 用户手动登录 (扫码/密码)
  3. 自动检测登录成功 (X-Cloudide-Session cookie 出现)
  4. 抓取全部 cookie → GetUserToken 换完整 token
  5. 自动写入 open-ai/config.json providers.trae.accounts (新账号追加) + cookie 字段
  6. 同步到自动签到 config.json (trae_accounts)
  7. 自动关闭浏览器

用法:
  python login_trae.py [--timeout 300]           # 等待登录的超时秒数 (默认 300)
  python login_trae.py --browser chromium        # 指定浏览器 (chromium/chrome/msedge/firefox)
  python login_trae.py --reuse-profile           # 复用上次登录会话 (接着没走完的登录)

★ 浏览器方案 (v2.6 起与国际版 login_workbuddy_intl.py 对齐, 更稳定):
  旧版直接 p.chromium.launch(channel='msedge') 拉起系统 Edge —— Playwright
  浏览器自带自动化特征 (navigator.webdriver=true), 走 OAuth/人机挑战时容易被
  卡死 (国际版实测复现过)。现默认 Playwright 自带 Chromium (版本与 playwright
  包锁死, 最稳) + 反自动化检测, 且**每次登录用全新独立 profile**
  (data/pw_profiles/trae/run-<时间戳>/): 干净会话 —— 同一通道可连续添加多个
  账号, 不会被上一次的登录态自动顶号; 上次没走完的登录可用 --reuse-profile
  复用最近会话。
  启动失败自动按 chromium → chrome → msedge → firefox 降级。

  ⚠️ 人机验证报「检测到您的网络环境较差 [5014]」≠ 真断网: 那是字节风控对
  本机「设备指纹 + 出口 IP」的综合打分拒绝。v2.6.2 已改为**原生 UA**(不再
  伪造 Edge 身份, 避免与 client-hints 品牌头自相矛盾) + 最小伪装来降嫌疑;
  仍被拒时: ① 改用扫码登录绕开短信验证 ② 换手机热点 ③ 等 30-60 分钟让
  风控降权再试。
"""
import base64
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
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
    PROFILE_ROOT = os.path.join(_ap.DATA_DIR, 'pw_profiles', 'trae')
    # v2.6 早期用过的固定 profile (SSO 登录态常驻会自动顶号), 已废弃, 启动时清理。
    _LEGACY_PROFILE_DIR = os.path.join(_ap.DATA_DIR, 'pw_profile_trae')
except Exception:  # 源码态单独运行 (python scripts/login_trae.py) 的兜底
    HERE = os.path.dirname(os.path.abspath(__file__))
    OPENAI_CFG = os.path.join(HERE, '..', 'config.json')
    PROFILE_ROOT = os.path.join(HERE, '..', 'data', 'pw_profiles', 'trae')
    _LEGACY_PROFILE_DIR = os.path.join(HERE, '..', 'data', 'pw_profile_trae')
LOGIN_URL = 'https://www.trae.cn/login'
GET_TOKEN_URL = 'https://api.trae.cn/cloudide/api/v3/common/GetUserToken'
# 仅作无浏览器 HTTP 调用 (GetUserToken) 的兜底 UA; 浏览器一律用**原生 UA** ——
# 让自带 Chromium 谎报 Edg 身份会与 client-hints 品牌头 (真实是 Chromium)
# 自相矛盾, 字节风控正抓这种不一致 (滑块验证报 [5014] 的嫌疑点之一)。
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0'

# 启动优先级: 自带 Chromium (版本与 playwright 包锁死, 不受系统浏览器自动更新
# 影响, 最稳) → 系统 Chrome → 系统 Edge (旧版默认) → Firefox。
# 任一档启动失败 (未安装/版本不匹配/profile 被占用) 自动降级到下一档。
BROWSER_FALLBACK = ('chromium', 'chrome', 'msedge', 'firefox')

# 反自动化检测注入: 只隐藏 navigator.webdriver 这一条硬特征。
# ★ 刻意最小化: 半吊子伪造 (补 window.chrome/语言列表) 在真实浏览器上多为
#   no-op, 在降级浏览器上反而制造新的不一致 —— 原生长相就是最好的伪装。
STEALTH_JS = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
)


def get_device_id():
    """从 open-ai/config.json providers.trae 读取设备 ID"""
    try:
        cfg = json.load(open(OPENAI_CFG, encoding='utf-8'))
        trae = (cfg.get('providers') or {}).get('trae') or {}
        return (trae.get('device_id') or (trae.get('headers') or {}).get('x-device-id') or '')
    except Exception:
        return ''


def get_token_by_cookie(cookie, ua=''):
    req = urllib.request.Request(GET_TOKEN_URL, data=b'{}', method='POST', headers={
        'Cookie': cookie, 'User-Agent': ua or UA, 'Content-Type': 'application/json',
        'Origin': 'https://www.trae.cn', 'Referer': 'https://www.trae.cn/',
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            j = json.loads(r.read().decode('utf-8', 'replace'))
            tok = j.get('Result', {}).get('Token', '')
            if tok:
                parts = tok.split('.')
                pad = parts[1] + '=' * (-len(parts[1]) % 4)
                p = json.loads(base64.urlsafe_b64decode(pad))
                uid = p.get('data', {}).get('id', '?')
                return tok, uid
            return None, None
    except Exception as e:
        print(f'[错误] GetUserToken 失败: {e}')
        return None, None


def verify_token(token):
    did = get_device_id()
    req = urllib.request.Request(
        'https://api.trae.cn/trae/api/v2/pay/ide_user_ent_usage',
        data=b'{"require_usage": true, "req_source": 1}', method='POST',
        headers={'Authorization': f'Cloud-IDE-JWT {token}', 'x-device-id': did,
                 'Content-Type': 'application/json', 'User-Agent': 'TRAE SOLO CN/2.3.70844'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


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

    print('=' * 60)
    print('  TRAE 网页登录助手')
    print('  即将打开浏览器, 请在网页中完成登录 (扫码/密码)')
    print(f'  等待登录超时: {timeout} 秒')
    print('=' * 60)

    with sync_playwright() as p:
        ctx, browser_name = launch_browser(p, browser_choice, reuse_profile)
        # 诊断: 记录加载失败的请求 —— 风控/验证码端点被拒时日志里有据可查
        ctx.on('requestfailed', lambda r: print(
            f'[请求失败] {r.url[:120]} → {r.failure}'))

        # 持久化 context 自带一个空白初始页: 直接复用, 免得多一个 about:blank 标签
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        # 网络不稳时 goto 容易超时 (国际版日志实测 TCP 连接就耗过 19s), 重试 3 次
        goto_ok = False
        for attempt in range(1, 4):
            try:
                page.goto(LOGIN_URL, wait_until='domcontentloaded', timeout=60000)
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
            print('             2) 换手机热点再试 (字节风控对 IP 信誉敏感)')
            print('             3) 浏览器能否手工打开 https://www.trae.cn/login')
            ctx.close()
            sys.exit(1)
        print('[等待] 请在弹出的浏览器中登录 TRAE 账号...')

        # 轮询检测登录成功: X-Cloudide-Session cookie 出现
        cookie_str = None
        closed_by_user = False
        start = time.time()
        while time.time() - start < timeout:
            try:
                time.sleep(2)
                cookies = ctx.cookies()
            except Exception:
                # 用户手动关闭浏览器 —— 优雅收尾, 不抛 TargetClosedError 堆栈
                closed_by_user = True
                break
            has_session = any(c['name'] == 'X-Cloudide-Session' and c.get('value') for c in cookies)
            if has_session:
                parts = []
                for c in cookies:
                    if c.get('value'):
                        parts.append(f"{c['name']}={c['value']}")
                cookie_str = '; '.join(parts)
                break
            # 也检查 sessionid (部分登录态)
            has_sid = any(c['name'] in ('sessionid', 'sid_guard') and c.get('value') for c in cookies)
            if has_sid and cookie_str is None:
                cookie_str = '; '.join(f"{c['name']}={c['value']}" for c in cookies if c.get('value'))

        if closed_by_user:
            print('[中止] 浏览器被手动关闭, 本次登录未完成 (未抓到登录态)。')
            sys.exit(1)

        if not cookie_str:
            print('[超时] 未检测到登录成功 (X-Cloudide-Session 未出现)')
            ctx.close()
            sys.exit(1)

        m = re.search(r'X-Cloudide-Session=([^;]+)', cookie_str)
        print(f'[成功] 检测到登录态! X-Cloudide-Session: {m.group(1)[:30]}...' if m else '[成功] 检测到登录态!')
        print('[步骤] 抓取 cookie 完成, 正在换取 token...')

        # 换 token 的 UA 用浏览器**实际发送**的 UA (与产生 cookie 的会话保持一致)
        try:
            browser_ua = page.evaluate('navigator.userAgent')
        except Exception:
            browser_ua = ''
        tok, uid = get_token_by_cookie(cookie_str, browser_ua)
        if not tok:
            print('[失败] 换 token 失败, cookie 可能不完整')
            ctx.close()
            sys.exit(1)
        print(f'[成功] 获取 token: uid={uid} len={len(tok)}')

        ok = verify_token(tok)
        print(f'[验证] token 有效性: {"有效" if ok else "无效"}')

        if ok:
            # 写入 open-ai/config.json providers.trae.accounts
            cfg = json.load(open(OPENAI_CFG, encoding='utf-8'))
            trae = cfg.setdefault('providers', {}).setdefault('trae', {})
            found = False
            for acc in trae.get('accounts', []):
                if acc.get('uid') == uid:
                    acc['token'] = tok
                    acc['cookie'] = cookie_str
                    found = True
                    print(f'[更新] 账号 {acc.get("name", uid)} token+cookie 已更新')
                    break
            if not found:
                trae.setdefault('accounts', []).append({
                    'name': f'网页登录账号({uid})',
                    'uid': uid,
                    'token': tok,
                    'cookie': cookie_str,
                })
                print(f'[新增] 新账号 {uid} 已加入 accounts')
            # 原子写入: 先写临时文件再替换, 避免并发覆盖/损坏
            with open(OPENAI_CFG, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            print('[写入] open-ai/config.json providers.trae.accounts 完成')

        ctx.close()
        print()
        print(f'登录完成, 浏览器({browser_name})已自动关闭')
        print('   提示: 重启 open-ai (start.bat) 后新账号即可参与轮询; cookie 13 天内 token 可自动续期')


if __name__ == '__main__':
    main()
