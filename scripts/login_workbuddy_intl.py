#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 国际版 (www.workbuddy.ai) 网页登录 → 应用 token 脚本
==============================================================
基于国内版 login_workbuddy.py 复制改造, 并按国际版真实登录链路加固。

国际版登录链路 (抓页面 JS 逆向确认, 与国内版不同):
  1. 打开 /login/?platform=workbuddy&state=<uuid>  —— React SPA
  2. SPA 在 **iframe** 里走 Keycloak OIDC:
     /auth/realms/copilot/protocol/openid-connect/auth
       ?client_id=console&response_type=code
       &redirect_uri=<origin>/login/select?...&product=workbuddy
  3. 登录成功后 iframe 回到 /login/select, SPA 拉账号列表
  4. SPA 主页面 POST /console/login/enterprise → 返回 accessToken/refreshToken
     (Keycloak 签发: typ=Bearer 为访问令牌, typ=Offline 为刷新令牌)

因此本脚本的抓取策略是**多路兜底** (旧版只监听主页面单一 URL, 一旦链路有偏差就抓不到):
  A. context 级响应监听 (覆盖 iframe / 新开窗口, 不只是主页面)
  B. 任意响应体里出现 accessToken/access_token/refreshToken/refresh_token 即抓
  C. 等待期间每 5s 从 localStorage / sessionStorage / cookies 里扫 JWT 兜底,
     并按 JWT 的 typ 判定访问/刷新令牌 —— 这样即使用户随后关掉浏览器也不丢 token
  D. 写入前用真实接口 (/v2/enterprises/personal/models) 验证 token 是否可用

另外: 浏览器被手动关闭不再抛 TargetClosedError 堆栈, 而是走兜底提取并给出明确结论。
全过程网络活动写入 logs/login_workbuddy_intl.log, 便于事后定位。

用法:
  python login_workbuddy_intl.py [--timeout 300]   # 等待登录的超时秒数 (默认 300)
  python login_workbuddy_intl.py --debug           # 打印全部网络响应 (排障用)
  python login_workbuddy_intl.py --keep-open       # 失败时保留浏览器窗口供人工检查
  python login_workbuddy_intl.py --url <登录页URL>  # 覆盖登录页 URL (链路变更时用)
"""
import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import uuid

# 强制 stdout/stderr 用 utf-8, 避免 GBK 控制台因中文/emoji 崩溃
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# Playwright 由 open-ai/.venv 提供

# ★ 打包态 __file__ 是相对路径, abspath 跟着 CWD 走 —— 钉死安装根。
try:
    import app_paths as _ap
    OPENAI_CFG = _ap.CONFIG_PATH
    LOG_PATH = os.path.join(_ap.LOGS_DIR, 'login_workbuddy_intl.log')
except Exception:  # 源码态单独运行的兜底
    HERE = os.path.dirname(os.path.abspath(__file__))
    OPENAI_CFG = os.path.join(HERE, '..', 'config.json')
    LOG_PATH = os.path.join(HERE, '..', 'logs', 'login_workbuddy_intl.log')

INTL_HOST = 'www.workbuddy.ai'
INTL_DOMAIN = 'www.workbuddy.ai'
INTL_PRODUCT = 'workbuddy-ai'

LOGIN_URL_TMPL = ('https://www.workbuddy.ai/login/?platform=workbuddy'
                  '&state={state}&version=5.3.12&loginSessionId={lsid}')
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0')

# 会返回 token 的端点: 命中这些路径的响应里带的 token 优先采用 (已实测确认)。
#   /console/login/enterprise      —— 真正的换 token 接口 (POST → accessToken/refreshToken)
#   /v2/plugin/auth/token/refresh  —— token 刷新接口 (返回的 token 同样可用)
# ⚠️ /v2/plugin/auth/token (不带 /refresh) 是**登录状态轮询**接口,
#    GET 返回 {"code":11217,"msg":"login ing..."}, 不返回 token, 别当成换 token 端点。
# 抓取本身不依赖这份名单 (见下面 harvest_obj 的通用抓取), 它只用于来源打分排序。
TOKEN_ENDPOINTS = ('/console/login/enterprise', '/v2/plugin/auth/token/refresh')

# 响应体里出现这些键名 (小写比较) 就当作候选 token 字段
ACCESS_KEYS = {'accesstoken', 'access_token', 'token', 'webtoken', 'bearer',
               'authorization'}
REFRESH_KEYS = {'refreshtoken', 'refresh_token'}
HARVEST_INTERVAL = 5      # 每 N 秒做一次存储兜底扫描
STATUS_EVERY = 15         # 每 N 秒打印一次当前页面 URL (让用户看到进度)


# ==================== 日志 ====================

def log(msg):
    line = f'[{time.strftime("%H:%M:%S")}] {msg}'
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def log_diag(msg):
    """只写文件不打印 (网络流水等噪音)。"""
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(f'[{time.strftime("%H:%M:%S")}] {msg}\n')
    except Exception:
        pass


# ==================== JWT 工具 ====================

def _b64d(seg):
    seg += '=' * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg)


def jwt_payload(token):
    try:
        return json.loads(_b64d(token.split('.')[1]))
    except Exception:
        return {}


def is_jwt(v):
    """粗判是否 JWT: 三段、够长、首段是 eyJ (base64 的 '{')。"""
    return (isinstance(v, str) and 80 < len(v) < 4096
            and v.count('.') == 2 and v.startswith('eyJ'))


def classify_jwt(token):
    """按 payload 判定 'access' / 'refresh' / None。"""
    p = jwt_payload(token)
    typ = str(p.get('typ') or '').lower()
    if typ in ('offline', 'refresh', 'refresh_token'):
        return 'refresh'
    if typ == 'bearer':
        return 'access'
    # 无 typ 时: 有 azp/aud/scope 的按访问令牌处理
    if p.get('azp') or p.get('aud') or p.get('scope'):
        return 'access'
    return None


def jwt_user_id(token):
    """账号 userId: 国际版是 Keycloak 的 sub。"""
    return str(jwt_payload(token).get('sub') or '')


def jwt_display_name(token):
    """账号显示名: preferred_username / email (国际版无昵称字段)。"""
    p = jwt_payload(token)
    return str(p.get('preferred_username') or p.get('email') or p.get('name') or '')


def jwt_iss(token):
    return str(jwt_payload(token).get('iss') or '')


# ==================== token 收集 ====================

def _authoritative_mark(path):
    """该路径是否权威 token 端点? 返回命中的标记 (否则 '')。"""
    for mark in TOKEN_ENDPOINTS:
        if mark in path:
            return mark
    return ''


def _src_base_score(src):
    """来源可信度基准分: token 端点 > 其它网络响应 > 本地存储。"""
    if src.startswith('net:'):
        if _authoritative_mark(src[4:]):
            return 100
        return 50
    if src.startswith('storage:'):
        return 10
    return 5


class TokenBag:
    """多路兜底收集 token。

    按**来源**成对保存 (同一来源的 access/refresh 一起用, 避免把 A 的访问令牌
    和 B 的刷新令牌拼在一起), 取分最高且确实含访问令牌的来源作为最终结果。
    iss 指向目标域名的来源额外加分, 防止误选其它站点的 JWT。
    """

    def __init__(self, prefer_host=INTL_HOST):
        self.prefer_host = prefer_host
        self.sources = {}

    def offer(self, kind, token, src):
        if not kind or not is_jwt(token):
            return False
        tok = token.strip()
        if tok.lower().startswith('bearer '):
            tok = tok[7:].strip()
        e = self.sources.setdefault(src, {})
        if kind in e:
            return False
        e[kind] = tok
        e.setdefault('iss', jwt_iss(tok))
        return True

    def _score(self, src):
        e = self.sources.get(src) or {}
        if not e.get('access'):
            return -1                     # 没有访问令牌的来源不能作主来源
        score = _src_base_score(src)
        if self.prefer_host and self.prefer_host in (e.get('iss') or ''):
            score += 20
        return score

    def best(self):
        if not self.sources:
            return None
        src = max(self.sources, key=self._score)
        if self._score(src) < 0:
            return None
        e = self.sources[src]
        return src, e.get('access', ''), e.get('refresh', '')

    @property
    def access(self):
        b = self.best()
        return b[1] if b else ''

    @property
    def refresh(self):
        b = self.best()
        return b[2] if b else ''

    def complete(self):
        return bool(self.access)

    def summary(self):
        if not self.sources:
            return '(未捕获任何 token)'
        cands = ', '.join(
            f'{s}[a={len(self.sources[s].get("access") or "")},'
            f'r={len(self.sources[s].get("refresh") or "")}]'
            for s in sorted(self.sources, key=self._score, reverse=True))
        b = self.best()
        chosen = f'✓ 选用 {b[0]}' if b else '✗ 无可用访问令牌'
        return f'{chosen}  |  候选: {cands}'


def harvest_obj(obj, bag, src, depth=0):
    """递归扫任意 JSON 结构里的 JWT 值。"""
    if depth > 10 or obj is None:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if isinstance(v, str) and is_jwt(v):
                kind = classify_jwt(v)
                if kind is None:
                    if kl in ACCESS_KEYS:
                        kind = 'access'
                    elif kl in REFRESH_KEYS:
                        kind = 'refresh'
                if kind:
                    bag.offer(kind, v, src)
            else:
                harvest_obj(v, bag, src, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:200]:
            harvest_obj(v, bag, src, depth + 1)


def harvest_storage(page, ctx, bag, host_hint=INTL_HOST):
    """从 localStorage / sessionStorage / cookies 里兜底扫 JWT。

    优先取 iss 指向目标域名的令牌 (避免误抓其它站点的 JWT)。
    """
    found = []
    try:
        store = page.evaluate(
            "() => { const o = {};"
            " for (const s of ['localStorage','sessionStorage']) {"
            "   try { const st = window[s];"
            "     for (let i=0;i<st.length;i++){ const k=st.key(i);"
            "       o[s+':'+k] = st.getItem(k); } } catch(e){} }"
            " return o; }")
        for k, v in (store or {}).items():
            if is_jwt(v):
                found.append((k, v))
    except Exception:
        pass  # 页面跳转中 execution context 被销毁, 属正常, 下轮再扫
    try:
        for c in ctx.cookies():
            v = urllib.parse.unquote(c.get('value') or '')
            if is_jwt(v):
                found.append((f'cookie:{c.get("name")}', v))
    except Exception:
        pass

    if not found:
        return False
    # iss 命中目标域名的排前面
    found.sort(key=lambda kv: 0 if host_hint in jwt_iss(kv[1]) else 1)
    hit = False
    for key, v in found:
        kind = classify_jwt(v)
        if kind is None:
            continue
        if bag.offer(kind, v, f'storage:{key}'):
            hit = True
            log(f'  [存储兜底] {key} → {kind} (iss={jwt_iss(v)[:48]})')
    return hit


# ==================== 校验与写入 ====================

def validate_token(access_token, user_id, domain=INTL_DOMAIN, product=INTL_PRODUCT):
    """用真实只读接口验证 token。

    返回 (verdict, detail):
      'ok'      —— 明确可用 (HTTP 200)
      'reject'  —— 明确不可用 (401/403, 令牌本身无效, 不应写入)
      'unknown' —— 无法判定 (网络异常/超时, 写入但提示)
    """
    url = f'https://{domain}/v2/enterprises/personal/models'
    req = urllib.request.Request(url, method='GET', headers={
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {access_token}',
        'X-User-Id': user_id,
        'X-Domain': domain,
        'X-Product': product,
        'X-Product-Code': product,
        'User-Agent': 'WorkBuddy/5.3.12 WorkBuddy/5.3.12 CLI/2.115.0',
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode('utf-8', 'replace')
            code = resp.status
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return 'reject', f'HTTP {e.code} (令牌被拒)'
        return 'unknown', f'HTTP {e.code}'
    except Exception as e:
        return 'unknown', f'{type(e).__name__}: {e}'
    if code in (401, 403):
        return 'reject', f'HTTP {code} (令牌被拒)'
    if code != 200:
        return 'unknown', f'HTTP {code}'
    try:
        d = json.loads(body)
        models = ((d.get('data') or {}).get('models')) or []
        return 'ok', f'{len(models)} 个模型可见'
    except Exception:
        return 'ok', '响应非 JSON 但 HTTP 200'


def write_account(access_token, refresh_token, user_id, name):
    cfg = json.load(open(OPENAI_CFG, encoding='utf-8'))
    wbai = cfg.setdefault('providers', {}).setdefault('workbuddy_intl', {})
    wbai.setdefault('domain', INTL_DOMAIN)
    wbai.setdefault('product', INTL_PRODUCT)
    accounts = wbai.setdefault('accounts', [])
    found = False
    for acc in accounts:
        if acc.get('userId') == user_id:
            acc.update({'accessToken': access_token, 'refreshToken': refresh_token,
                        'name': name, 'enabled': True})
            found = True
            log(f'[更新] workbuddy_intl 账号 {user_id} token 已更新')
            break
    if not found:
        accounts.append({'accessToken': access_token, 'refreshToken': refresh_token,
                         'userId': user_id, 'name': name, 'enabled': True})
        log(f'[新增] workbuddy_intl 新账号 {user_id} 已加入 accounts')
    json.dump(cfg, open(OPENAI_CFG, 'w', encoding='utf-8'),
              ensure_ascii=False, indent=2)
    log('[写入] open-ai/config.json 完成 (providers.workbuddy_intl)')


# ==================== 主流程 ====================

def parse_args(argv):
    opts = {'timeout': 300, 'debug': False, 'keep_open': False, 'url': ''}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == '--timeout' and i + 1 < len(argv):
            opts['timeout'] = int(argv[i + 1]); i += 2; continue
        if a == '--debug':
            opts['debug'] = True; i += 1; continue
        if a == '--keep-open':
            opts['keep_open'] = True; i += 1; continue
        if a == '--url' and i + 1 < len(argv):
            opts['url'] = argv[i + 1]; i += 2; continue
        i += 1
    return opts


def main():
    opts = parse_args(sys.argv[1:])
    timeout = opts['timeout']

    from playwright.sync_api import sync_playwright

    state = str(uuid.uuid4())
    lsid = str(uuid.uuid4())
    login_url = opts['url'] or LOGIN_URL_TMPL.format(state=state, lsid=lsid)

    log('=' * 60)
    log('  WorkBuddy 国际版 (www.workbuddy.ai) 网页登录助手')
    log(f'  等待登录超时: {timeout} 秒   (排障日志: {LOG_PATH})')
    log('=' * 60)

    bag = TokenBag()
    resp_seen = 0
    token_ep_hits = []          # 命中的权威 token 端点 (含状态码)

    def on_response(resp):
        nonlocal resp_seen
        resp_seen += 1
        url = resp.url
        try:
            status = resp.status
        except Exception:
            return
        try:
            path = urllib.parse.urlsplit(url).path or url
        except Exception:
            path = url
        mark = _authoritative_mark(path)
        if mark:
            token_ep_hits.append(f'{mark} HTTP {status}')
            log(f'  [命中] {mark} HTTP {status}')
        log_diag(f'RESP {status:>3} {url[:180]}')
        if opts['debug'] and 'workbuddy.ai' in url:
            print(f'    ← {status} {url[:150]}', flush=True)
        # 按资源类型过滤静态资源, 其余一律尝试按 JSON 解析 —— 不依赖 content-type,
        # 因为部分接口的 content-type 不规范, 用 content-type 判断会漏抓 token。
        try:
            rtype = resp.request.resource_type
        except Exception:
            rtype = ''
        if rtype in ('image', 'media', 'font', 'stylesheet', 'script'):
            return
        try:
            body = resp.json()
        except Exception:
            return
        # 来源标识用 URL 路径 (含 token 端点完整特征, 便于可信度打分)
        try:
            src = 'net:' + (urllib.parse.urlsplit(url).path or url)
        except Exception:
            src = f'net:{url[:80]}'
        before = bag.access
        harvest_obj(body, bag, src)
        if bag.access and bag.access != before:
            log(f'  [网络捕获] accessToken 来自 {src}')

    browser = None
    ctx = None
    page = None
    closed_by_user = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel='msedge')
        ctx = browser.new_context(user_agent=UA)
        # A. context 级监听 —— 覆盖 iframe / 弹窗, 不只主页面
        ctx.on('response', on_response)

        def on_page(newp):
            log(f'  [新窗口] {newp.url[:100]}')
            newp.on('response', on_response)
        ctx.on('page', on_page)

        page = ctx.new_page()
        # 网络不稳时 (实测见过 TCP 连接就耗 19s) goto 很容易超时, 故重试 3 次。
        goto_ok = False
        for attempt in range(1, 4):
            try:
                page.goto(login_url, wait_until='domcontentloaded', timeout=60000)
                goto_ok = True
                break
            except Exception as e:
                log(f'[警告] 第 {attempt}/3 次打开登录页失败: {type(e).__name__}: {e}')
                if attempt < 3:
                    log('       网络似乎不稳, 5 秒后重试 ...')
                    time.sleep(5)
        if not goto_ok:
            log('[错误] 三次都打不开登录页, 基本可判定是网络问题。')
            log('       排查: 1) Steam++/Watt「网络加速」是否在拦截 (可在其界面关掉再试)')
            log('             2) 系统代理/加速器是否异常')
            log('             3) 浏览器能否手工打开 https://www.workbuddy.ai/login/')
            try:
                browser.close()
            except Exception:
                pass
            return 1

        log('[等待] 请在弹出的浏览器中完成登录 (扫码/密码)')
        log('[提示] 登录后页面若弹"请求参数错误(400)"提示, 可忽略——脚本会自动抓取 token')
        log('[提示] 若页面停在账号选择/注册补全步骤, 请继续点完, 否则拿不到 token')

        start = time.time()
        last_harvest = 0.0
        last_status = 0.0
        last_url = ''

        while time.time() - start < timeout:
            if bag.complete():
                break
            try:
                page.wait_for_timeout(1000)   # 必须用 playwright 等待, 否则事件不实时投递
            except Exception as e:
                # 用户手动关闭了浏览器 —— 不再抛堆栈, 走兜底
                closed_by_user = True
                log(f'[提示] 浏览器已被关闭 ({type(e).__name__}), 停止等待并尝试兜底提取')
                break

            elapsed = time.time() - start
            # C. 存储兜底: 定期扫 localStorage/sessionStorage/cookies
            if elapsed - last_harvest >= HARVEST_INTERVAL:
                last_harvest = elapsed
                try:
                    harvest_storage(page, ctx, bag)
                except Exception:
                    pass
            # 让用户看到进度
            if elapsed - last_status >= STATUS_EVERY:
                last_status = elapsed
                try:
                    cur = page.url
                    if cur != last_url:
                        last_url = cur
                        log(f'  [页面] {cur[:110]}')
                except Exception:
                    pass

        # 结束前再兜底扫一次 (此时浏览器若还在)
        if not bag.complete() and not closed_by_user:
            try:
                harvest_storage(page, ctx, bag)
            except Exception:
                pass

        log(f'[统计] 共收到 {resp_seen} 个响应')
        log(f'[统计] token 端点命中: '
            + ('; '.join(token_ep_hits) if token_ep_hits else '无'))
        log(f'[统计] token: {bag.summary()}')

        if not bag.complete():
            log('[失败] 未捕获到 accessToken。按下面顺序自查:')
            log(f'  1) 浏览器是否被提前关闭? {"是" if closed_by_user else "否"};'
                f' 登录流程是否走完整 (含账号选择/注册补全)?')
            log(f'  2) token 端点({" 或 ".join(TOKEN_ENDPOINTS)})是否命中? —— 若"无",'
                f' 说明登录没走到最后一步, 登录成功后不要立刻关浏览器')
            log('  3) 详细网络流水见上面提到的日志文件, 可发给鲸继续分析')
            if opts['keep_open']:
                log('[保留] --keep-open: 浏览器保持打开, 请人工检查; 按回车退出')
                try:
                    input()
                except Exception:
                    pass
            try:
                browser.close()
            except Exception:
                pass
            return 1

        # ---------- 写入 config.json (先验证) ----------
        access, refresh = bag.access, bag.refresh
        uid = jwt_user_id(access)
        uname = jwt_display_name(access)
        log(f'[成功] 获取 accessToken: userId={uid or "(JWT无sub)"} len={len(access)}')
        log(f'[成功] refreshToken: {"有" if refresh else "无"} len={len(refresh)}'
            + ('' if refresh else ' (缺刷新令牌, 过期后需重新登录)'))

        log('[校验] 正在用真实接口验证 token ...')
        verdict, detail = validate_token(access, uid)
        if verdict == 'ok':
            log(f'[校验] ✓ token 可用 ({detail})')
        elif verdict == 'reject':
            log(f'[校验] ✗ token 被上游拒绝: {detail}')
        else:
            log(f'[校验] ? 无法判定 ({detail}), 仍写入配置')

        name = f'国际版账号({uname})' if uname else '国际版账号'
        if verdict == 'reject':
            log('[失败] 捕获到的令牌无效, 已放弃写入 —— 避免写入不可用账号。')
            log('       请重跑本脚本; 若反复如此, 把上面日志文件发给鲸继续分析')
            if opts['keep_open']:
                log('[保留] --keep-open: 浏览器保持打开供人工检查; 按回车退出')
                try:
                    input()
                except Exception:
                    pass
            try:
                browser.close()
            except Exception:
                pass
            return 1

        write_account(access, refresh, uid, name)

        try:
            browser.close()
        except Exception:
            pass

    log('登录完成, 浏览器已自动关闭')
    log('   提示: 重启 open-ai 网关 (或点 GUI「⑤ 重新连接」) 后新账号即可参与轮询')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        log('[中断] 用户取消')
        sys.exit(130)
    except Exception:
        import traceback
        log('[异常] 未预期的错误:')
        try:
            os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
            with open(LOG_PATH, 'a', encoding='utf-8') as f:
                traceback.print_exc(file=f)
        except Exception:
            pass
        traceback.print_exc()
        sys.exit(1)
