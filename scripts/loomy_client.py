#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Loomy (讯飞 Loomy 办公助手) 客户端
=================================
从 Loomy 桌面端逆向得来 (app.asar.unpacked/electron):
  - 登录: 讯飞账号服务 https://account.xfinfr.com  (HMAC-SHA1 ak/sk 签名)
      发验证码 POST /login/phone/sendMsgCode
      短信登录 POST /login/phone/checkCode       -> 返回 session + userid
      用户信息 POST /userinfo/query/baseInfo
  - 积分: https://loomyad.xunfei.cn  (请求头 token: <session>)
      查积分 GET  /api/v1/points/records
      V2     GET  /api/v2/points/records
      每日首次登录领取 POST /api/v1/points/first-login   <-- 每日登录积分
      激活/邀请码  GET  /api/v1/points/activation
      兑换码 POST /api/v1/points/redemption-codes/redeem

用法:
  python loomy_client.py send-code 13800138000        # 发送短信验证码 (桌面通道)
  python loomy_client.py login 13800138000 <code> <msgid>  # 短信登录并存 session
  python loomy_client.py userinfo                     # 查询用户信息
  python loomy_client.py points                       # 查询积分余额
  python loomy_client.py points-all                   # 逐账号查询积分 (多账号排查)
  python loomy_client.py ledger                       # 逐账号积分台账 (消耗归属)
  python loomy_client.py first-login                  # 领取每日登录积分
  python loomy_client.py daily                        # 积分 + 每日登录领取

鉴权整改 (重要): 早期版本把反编译得到的 iModel apiKey 当共享凭据硬编码, 导致
所有人的积分都记在同一个陌生账号上。现已彻底移除 —— 每个账号用**自己的登录态**
(session / web cookie) 调上游, 积分各记各的。详见 LOOMY_鉴权整改文档。

Web 端登录 (https://loomy.xunfei.cn, cookie 会话, 推荐):
  python loomy_client.py web-send-code 13800138000    # Web 端发送验证码
  python loomy_client.py web-login 13800138000 <code> <msgid>  # Web 登录, cookie 写入 config
  python loomy_client.py web-me                       # 校验 Web 会话 (GET /web/api/auth/me)
  python loomy_client.py web-models                   # Web 端模型目录 (GET /web/api/models)
  python loomy_client.py web-points                   # Web 端团队积分余额
"""
import base64
import hashlib
import hmac
import http.cookiejar
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from email.utils import formatdate
from urllib.parse import quote

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# ★ 同 credits_api: 打包态 __file__ 相对路径会跟着 CWD 走, 必须钉死安装根。
try:
    import app_paths as _ap
    OPENAI_CFG = _ap.CONFIG_PATH
except Exception:  # 源码态单独运行的兜底
    HERE = os.path.dirname(os.path.abspath(__file__))
    OPENAI_CFG = os.path.join(HERE, '..', 'config.json')

# 逆向自 Loomy resources/.env.prod (生产环境)
XFYUN_BASE = 'https://account.xfinfr.com'
XFYUN_AK = '2thryby66wxi53sk'
XFYUN_SK = 'zsak6eadrbawz683wf5r3m2snrwj868r'
XFYUN_APPID = 'GM3LOOMY'
POINTS_BASE = 'https://loomyad.xunfei.cn'
UA = 'Loomy|Desktop|Electron|macOS'

# Web 端 (https://loomy.xunfei.cn/web/) — 逆向自 Web SPA bundle index-p6xFTG72.js:
#   认证 = 手机号短信验证码, 无 CSRF/签名/风控指纹;
#   凭据 = 纯 Cookie 会话 (前端 fetch 固定 credentials: "same-origin", 无 token 头);
#   API 前缀 = /web/api/* (SPA 相对 /web base path)。
WEB_BASE = 'https://loomy.xunfei.cn'
WEB_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
          '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')

# ---------------- 讯飞账号签名 (HMAC-SHA1) ----------------

def _rfc3986(s):
    return quote(str(s), safe='-_.~')

def _escaped_path(path):
    clean = path if path.startswith('/') else '/' + path
    if len(clean) > 1 and clean.endswith('/'):
        clean = clean[:-1]
    return '/'.join(_rfc3986(seg) if seg else '' for seg in clean.split('/'))

def _content_md5(body_str):
    if not body_str:
        return ''
    return base64.b64encode(hashlib.md5(body_str.encode('utf-8')).digest()).decode()

def _sign_headers(method, path, body_str, content_type='application/json'):
    date = formatdate(usegmt=True)
    nonce = str(uuid.uuid4())
    md5 = _content_md5(body_str)
    parts = [method.upper(), _escaped_path(path), '', md5, content_type,
             date, nonce, '', '']
    sts = '\n'.join(parts)
    sig = base64.b64encode(hmac.new(XFYUN_SK.encode(), sts.encode('utf-8'),
                                    hashlib.sha1).digest()).decode()
    headers = {
        'Authorization': f'account {XFYUN_AK}:{sig}',
        'Date': date,
        'Nonce': nonce,
        'Content-Type': content_type,
    }
    if md5:
        headers['Content-MD5'] = md5
    return headers

def _http(method, url, headers=None, body=None, timeout=25):
    data = body.encode('utf-8') if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=ssl.create_default_context()) as r:
            return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')
    except Exception as e:
        return 0, f'{type(e).__name__}: {e}'

def _xfyun_post(path, body):
    body_str = json.dumps(body, ensure_ascii=False) if body is not None else ''
    headers = _sign_headers('POST', path, body_str)
    return _http('POST', XFYUN_BASE + path, headers, body_str)

def _base_body(extra=None):
    b = {'appid': XFYUN_APPID, 'modelid': 'web', 'version': '1.0.0',
         'devid': 'web', 'ua': UA, 'traceid': uuid.uuid4().hex}
    if extra:
        b.update(extra)
    return b

# ---------------- 登录 ----------------

def send_sms_code(phone):
    """发送短信验证码, 返回 (success, msgid/err)。"""
    body = {'base': _base_body(),
            'param': {'ccode': '86', 'phone': phone, 'expire': 300}}
    code, raw = _xfyun_post('/login/phone/sendMsgCode', body)
    try:
        d = json.loads(raw)
    except Exception:
        return False, f'HTTP {code}: {raw[:200]}'
    if d.get('code') == '000000':
        return True, (d.get('data') or {}).get('msgid', '')
    return False, d.get('desc') or d.get('code') or raw[:200]

def login_by_sms(phone, mcode, msgid):
    """短信验证码登录, 返回 (success, {session, userid} / err)。"""
    body = {'base': _base_body(),
            'param': {'ccode': '86', 'phone': phone, 'mcode': mcode,
                      'msgid': msgid, 'expire': 14 * 24 * 3600}}
    code, raw = _xfyun_post('/login/phone/checkCode', body)
    try:
        d = json.loads(raw)
    except Exception:
        return False, f'HTTP {code}: {raw[:200]}'
    if d.get('code') == '000000':
        data = d.get('data') or {}
        return True, {'session': data.get('session', ''),
                      'userid': data.get('userid', '')}
    return False, d.get('desc') or d.get('code') or raw[:200]

def get_user_info(session):
    body = {'base': _base_body(), 'param': {'session': session}}
    code, raw = _xfyun_post('/userinfo/query/baseInfo', body)
    try:
        return True, json.loads(raw)
    except Exception:
        return False, f'HTTP {code}: {raw[:200]}'

def logout(session):
    return _xfyun_post('/login/account/logout',
                       {'base': {'appId': XFYUN_APPID}, 'param': {'session': session}})

# ---------------- 积分 (token 头鉴权) ----------------

def _points_request(method, path, session, query=None, body=None):
    url = POINTS_BASE + path
    if query:
        qs = '&'.join(f'{k}={quote(str(v))}' for k, v in query.items()
                      if v is not None and v != '')
        if qs:
            url += '?' + qs
    headers = {'token': session, 'Accept': 'application/json'}
    data = None
    if body is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(body, ensure_ascii=False)
    return _http(method, url, headers, data)

def query_points(session):
    """查询积分余额/汇总。"""
    code, raw = _points_request('GET', '/api/v1/points/records', session,
                                {'pageNo': 1, 'pageSize': 20, 'recordType': 'all'})
    try:
        return True, json.loads(raw)
    except Exception:
        return False, f'HTTP {code}: {raw[:300]}'

def query_points_v2(session, direction='debit', granularity='chat'):
    code, raw = _points_request('GET', '/api/v2/points/records', session,
                                {'pageNo': 1, 'pageSize': 20,
                                 'direction': direction, 'granularity': granularity})
    try:
        return True, json.loads(raw)
    except Exception:
        return False, f'HTTP {code}: {raw[:300]}'

def first_login_reward(session, invite_code='', device_id=''):
    """领取每日首次登录积分 (Loomy 每日登录奖励)。"""
    body = {}
    if invite_code:
        body['inviteCode'] = invite_code
    if device_id:
        body['deviceId'] = device_id
    code, raw = _points_request('POST', '/api/v1/points/first-login', session,
                                body=body)
    try:
        return True, json.loads(raw)
    except Exception:
        return False, f'HTTP {code}: {raw[:300]}'

def check_activation(session):
    code, raw = _points_request('GET', '/api/v1/points/activation', session)
    try:
        return True, json.loads(raw)
    except Exception:
        return False, f'HTTP {code}: {raw[:300]}'


def account_token(acc):
    """取账号的可用 Bearer 凭据 (桌面 session / Web cookie 二选一)。

    鉴权整改后不再有任何共享 apiKey: 每个账号用自己的登录态调上游,
    积分各记各的。
    """
    return str((acc or {}).get('session')
               or ((acc or {}).get('cookies') or {}).get('loomy_web_session')
               or '').strip()


def points_ledger(session, page_size=100, max_pages=5, record_type='all'):
    """官方积分台账 (逐条 debit/credit 流水)。返回 (items, err)。

    item 字段 (实测): ledgerId / modelName / direction('debit'|'credit') /
    description / createdAt(unix秒) / pointsActual / dailyCycleDate('YYYY-MM-DD')。

    ⚠️ 鉴权整改: session 参数现在是**必填的本账号凭据**, 不再有共享 apiKey 兜底。
    采集器 (usage_collector) 按账号逐个调用, 把各账号自己的消耗流水写进库。
    """
    token = str(session or '').strip()
    if not token:
        return [], '缺少账号凭据 (请先登录 Loomy 账号)'
    items, page = [], 1
    while page <= max_pages:
        code, raw = _points_request('GET', '/api/v1/points/records', token,
                                    {'pageNo': page, 'pageSize': page_size,
                                     'recordType': record_type})
        try:
            d = json.loads(raw)
        except Exception:
            return items, f'HTTP {code}: {raw[:200]}'
        if d.get('code') != '000000':
            return items, d.get('desc') or d.get('code') or raw[:200]
        data = d.get('data') or {}
        batch = data.get('list') or []
        items.extend(batch)
        try:
            total = int(data.get('total') or 0)
        except (TypeError, ValueError):
            total = 0
        if not batch or len(items) >= total:
            break
        page += 1
    return items, None


def ledger_all_accounts(page_size=100, max_pages=5, record_type='all'):
    """逐账号抓取积分台账。返回 ([(acc, items)], [err...])。

    鉴权整改的核心收益: 每条流水都归属于**产生它的那个账号**, 不再是
    全库混在一个共享池里。usage_collector 用 account.userid 当 uid 入库,
    看板上就能按账号区分。

    只遍历有 `session` 的桌面账号 —— Web 账号的 cookie 仅能访问 /web/api/*,
    打 /api/v1/points/records 会回 100002 (实测); 而 Web 账号在整改后也
    无法鉴权对话, 天然没有消耗流水, 跳过即免报错噪音。
    """
    results, errors = [], []
    for acc in load_accounts():
        if not acc.get('enabled', True):
            continue
        token = str(acc.get('session') or '').strip()
        if not token:
            continue  # Web(cookie) 账号: 无对话能力, 无台账可采
        items, err = points_ledger(token, page_size=page_size,
                                   max_pages=max_pages, record_type=record_type)
        if err and not items:
            errors.append(f"{acc.get('phone') or acc.get('userid')}: {err}")
        results.append((acc, items))
    return results, errors

# ---------------- Web 端登录 (loomy.xunfei.cn, cookie 会话) ----------------
# 全流程只需手机号 + 短信验证码, 无需浏览器。凭据为 Cookie 头字符串,
# 登录成功后整体写入 config providers.loomy.accounts[].cookies。

def web_fingerprint(seed=''):
    """生成 Web 端 deviceId (逆向自 bundle 的 Dw(): 'web_fp_' + FNV-1a 32bit)。

    同一 seed 恒定输出 —— 用手机号当 seed, 保证同一账号在网关侧 deviceId 稳定,
    不触发服务端「设备频繁变化」类风控。
    """
    data = seed or uuid.uuid4().hex
    h = 2166136261
    for ch in data:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return 'web_fp_' + format(h, '08x')


def _web_request(method, path, cookies=None, body=None, timeout=25):
    """请求 Web 端 API。cookies: dict; body: dict (自动 JSON)。
    返回 (status, resp_dict_or_text, set_cookie_headers)。"""
    url = WEB_BASE + path
    headers = {
        'User-Agent': WEB_UA,
        'Accept': 'application/json',
        'Origin': WEB_BASE,
        'Referer': WEB_BASE + '/web/login',
    }
    if cookies:
        headers['Cookie'] = '; '.join(f'{k}={v}' for k, v in cookies.items())
    data = None
    if body is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=ssl.create_default_context()) as r:
            raw = r.read().decode('utf-8', 'replace')
            return r.status, raw, r.headers.get_all('Set-Cookie') or []
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', 'replace')
        return e.code, raw, e.headers.get_all('Set-Cookie') or []
    except Exception as e:
        return 0, f'{type(e).__name__}: {e}', []


def _web_json(status, raw):
    try:
        return json.loads(raw)
    except Exception:
        return {'success': False, 'code': f'HTTP_{status}', 'error': raw[:200]}


def parse_set_cookies(set_cookie_headers):
    """把多条 Set-Cookie 头解析成 {name: value} (取每个 cookie 最后一次出现的值)。"""
    out = {}
    for line in set_cookie_headers or []:
        first = (line or '').split(';', 1)[0].strip()
        if '=' in first:
            k, v = first.split('=', 1)
            out[k.strip()] = v.strip()
    return out


def web_send_sms_code(phone):
    """Web 端发送短信验证码。返回 (success, {messageId} / err)。"""
    status, raw, _ = _web_request('POST', '/web/api/auth/sms-code',
                                  body={'phone': phone})
    d = _web_json(status, raw)
    if d.get('success'):
        return True, {'messageId': d.get('messageId') or d.get('data', {}).get('messageId') or ''}
    return False, d.get('error') or d.get('code') or raw[:200]


def web_login(phone, code, message_id, device_id=None):
    """Web 端短信登录。返回 (success, {cookies, deviceId, me} / err)。

    成功后 cookies 是完整的会话 Cookie (dict), /web/api/* 全部可用。
    """
    if not device_id:
        device_id = web_fingerprint(phone)
    body = {'phone': phone, 'code': str(code), 'messageId': message_id,
            'channel': '', 'visitedId': '', 'deviceId': device_id}
    status, raw, set_cookies = _web_request('POST', '/web/api/auth/login', body=body)
    d = _web_json(status, raw)
    if not d.get('success'):
        return False, d.get('error') or d.get('code') or raw[:200]
    cookies = parse_set_cookies(set_cookies)
    if not cookies:
        return False, f'登录响应未下发任何 Set-Cookie (HTTP {status})'
    # 立即校验会话有效性
    ok, me = web_me(cookies)
    return True, {'cookies': cookies, 'deviceId': device_id,
                  'me': me if ok else None}


def web_request_json(method, path, cookies, body=None):
    status, raw, _ = _web_request(method, path, cookies=cookies, body=body)
    d = _web_json(status, raw)
    if status == 200 and d.get('success', True):
        return True, d
    return False, d.get('error') or d.get('code') or raw[:200]


def web_me(cookies):
    """校验 Web 会话。GET /web/api/auth/me → 401 即会话失效。"""
    return web_request_json('GET', '/web/api/auth/me', cookies)


def web_models(cookies):
    """Web 端模型目录 (含倍率/上下文长度等)。"""
    return web_request_json('GET', '/web/api/models', cookies)


def web_team_points(cookies):
    """Web 端团队积分余额。"""
    return web_request_json('GET', '/web/api/team/points/balance', cookies)


# ---------------- config 读写 ----------------

def _load_cfg():
    with open(OPENAI_CFG, encoding='utf-8') as f:
        return json.load(f)

def _save_cfg(cfg):
    with open(OPENAI_CFG, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

def save_session(userid, session, phone=''):
    """写入/更新 Loomy 账号 (多账号列表 providers.loomy.accounts)。"""
    cfg = _load_cfg()
    loomy = cfg['providers'].setdefault('loomy', {})
    accounts = loomy.setdefault('accounts', [])
    # 兼容旧单账号字段
    legacy = loomy.pop('account', None)
    if legacy and legacy.get('session') and not any(
            a.get('userid') == legacy.get('userid') for a in accounts):
        accounts.append(legacy)
    acct = {'userid': userid, 'session': session, 'phone': phone,
            'name': f'Loomy({phone})' if phone else f'Loomy({str(userid)[:8]})',
            'enabled': True}
    found = False
    for a in accounts:
        if a.get('userid') == userid:
            a.update(acct)
            acct = a
            found = True
            break
    if not found:
        accounts.append(acct)
    _save_cfg(cfg)
    return acct

def load_accounts():
    """返回 Loomy 全部账号列表 (兼容旧单账号字段)。"""
    cfg = _load_cfg()
    loomy = (cfg.get('providers') or {}).get('loomy') or {}
    accounts = list(loomy.get('accounts') or [])
    legacy = loomy.get('account')
    if legacy and legacy.get('session') and not any(
            a.get('userid') == legacy.get('userid') for a in accounts):
        accounts.append(legacy)
    return accounts

def remove_account(userid):
    """删除指定 Loomy 账号。"""
    cfg = _load_cfg()
    loomy = (cfg.get('providers') or {}).get('loomy') or {}
    accounts = [a for a in (loomy.get('accounts') or [])
                if a.get('userid') != userid]
    loomy['accounts'] = accounts
    if (loomy.get('account') or {}).get('userid') == userid:
        loomy.pop('account', None)
    _save_cfg(cfg)
    return True

def load_session():
    """返回第一个可用账号 (兼容旧调用)。"""
    accounts = load_accounts()
    for a in accounts:
        if a.get('enabled', True) and a.get('session'):
            return a
    return accounts[0] if accounts else {}


# ---------------- Web 会话存取 (providers.loomy.accounts[].cookies) ----------------

def save_web_session(phone, cookies, device_id='', me=None):
    """写入/更新 Web 端 cookie 账号 (providers.loomy.accounts)。

    与桌面通道账号共存于同一列表: 桌面账号有 session 字段, Web 账号有
    cookies 字段, 按 userid 区分 (web:<phone>)。
    """
    userid = f'web:{phone}'
    cfg = _load_cfg()
    loomy = cfg['providers'].setdefault('loomy', {})
    accounts = loomy.setdefault('accounts', [])
    acct = {'userid': userid, 'phone': phone, 'kind': 'web',
            'cookies': cookies, 'deviceId': device_id,
            'name': f'Loomy Web({phone})', 'enabled': True}
    if isinstance(me, dict):
        uid = (me.get('data') or {}).get('userid') or (me.get('data') or {}).get('id')
        if uid:
            acct['webUserId'] = str(uid)
        nick = (me.get('data') or {}).get('nickname')
        if nick:
            acct['name'] = f'Loomy Web({nick})'
    found = False
    for a in accounts:
        if a.get('userid') == userid:
            a.update(acct)
            found = True
            break
    if not found:
        accounts.append(acct)
    _save_cfg(cfg)
    return acct


def load_web_accounts():
    """返回全部 Web 端 cookie 账号。"""
    return [a for a in load_accounts()
            if a.get('kind') == 'web' and a.get('cookies')]


def load_web_cookies():
    """返回第一个可用 Web 账号的 cookies (dict); 无则 None。"""
    for a in load_web_accounts():
        if a.get('enabled', True):
            return a.get('cookies') or {}
    return None


# ---------------- 每日登录领取的本地凭证缓存 ----------------
# Loomy 不进流水库 (usage_collector 不采集), 「今天领没领到」的本地唯一凭证
# 就是这份按天覆盖的缓存。写点: signin_all.loomy_daily_one 每次领取后;
# 读点: admin_api /accounts/signin (账号管理页「每日签到」列)。

try:
    DATA_DIR = _ap.DATA_DIR  # _ap 已在上方 import app_paths 时就位
except Exception:
    DATA_DIR = os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', 'data'))
SIGNIN_STATE_FILE = os.path.join(DATA_DIR, 'loomy_signin_state.json')


def _today():
    import datetime
    return datetime.date.today().isoformat()


def record_daily_result(userid, ok, data=None, error=None):
    """记录某账号当日首次登录领取结果。userid 必须与 config 里的 userid 一致
    (admin_api 按 `Loomy:<userid>` 对表)。同一天多次调用取最后一次。"""
    state = {'day': _today(), 'accounts': {}}
    try:
        with open(SIGNIN_STATE_FILE, encoding='utf-8') as f:
            old = json.load(f)
        if old.get('day') == state['day']:
            state['accounts'] = old.get('accounts') or {}
    except Exception:
        pass
    entry = {'claimed': bool(ok), 'ts': int(time.time())}
    if isinstance(data, dict):
        entry['alreadyProcessed'] = bool(data.get('alreadyProcessed'))
        for k in ('currentBalance', 'dailyBalance', 'dailyQuota'):
            if data.get(k) is not None:
                entry[k] = data.get(k)
    if error:
        entry['error'] = str(error)[:200]
    state['accounts'][str(userid)] = entry
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(SIGNIN_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # 缓存是辅助凭证, 写失败不影响领取主流程
    return entry


def load_daily_state(day=None):
    """读当日领取缓存 {userid: entry}; 非当天/无文件返回空 dict。"""
    try:
        with open(SIGNIN_STATE_FILE, encoding='utf-8') as f:
            state = json.load(f)
    except Exception:
        return {}
    if not isinstance(state, dict) or state.get('day') != (day or _today()):
        return {}
    return state.get('accounts') or {}

# ---------------- CLI ----------------

def _print(title, obj):
    print(f'--- {title} ---')
    print(json.dumps(obj, ensure_ascii=False, indent=2) if isinstance(obj, (dict, list))
          else obj)

def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd = args[0]

    if cmd == 'send-code':
        ok, r = send_sms_code(args[1])
        _print('发送验证码', {'success': ok, 'msgid/err': r})

    elif cmd == 'login':
        phone, mcode, msgid = args[1], args[2], args[3]
        ok, r = login_by_sms(phone, mcode, msgid)
        if not ok:
            _print('登录失败', r)
            return
        acct = save_session(r['userid'], r['session'], phone)
        _print('登录成功 (已写入 config providers.loomy.accounts)',
               {'userid': acct['userid'], 'session_len': len(acct['session'])})

    elif cmd == 'userinfo':
        acc = load_session()
        ok, r = get_user_info(account_token(acc))
        _print('用户信息', r)

    elif cmd == 'points':
        acc = load_session()
        if not account_token(acc):
            _print('积分', 'config 中无可用 Loomy 账号, 请先 login / web-login')
            return
        ok, r = query_points(account_token(acc))
        _print('积分', r)

    elif cmd == 'points-all':
        # 鉴权整改后的排查命令: 逐账号看各自余额, 确认积分归属正确
        accs = [a for a in load_accounts() if a.get('enabled', True)]
        if not accs:
            _print('各账号积分', 'config 中无可用 Loomy 账号')
            return
        for a in accs:
            tok = account_token(a)
            label = a.get('phone') or a.get('userid')
            if not tok:
                _print(f'{label}', '无凭据, 跳过')
                continue
            ok, r = query_points(tok)
            d = (r or {}).get('data') or {} if isinstance(r, dict) else {}
            # 桌面台账接口: balance=永久钱包, dailyBalance=今日额度,
            # availableBalance=总可用 (字段名与 Web 端 points-summary 不同, 踩过)
            _print(f"{label} ({a.get('userid')})",
                   {'ok': ok, 'permanent': d.get('balance'),
                    'daily': d.get('dailyBalance'),
                    'available': d.get('availableBalance')} if ok else r)

    elif cmd == 'ledger':
        # 逐账号台账 (鉴权整改后积分按账号归属)
        results, errors = ledger_all_accounts()
        if not results:
            _print('积分台账', 'config 中无可用 Loomy 账号')
            return
        for acc, items in results:
            label = acc.get('phone') or acc.get('userid')
            debits = [i for i in items if i.get('direction') == 'debit']
            total = sum(float(i.get('pointsActual') or 0) for i in debits)
            _print(f'{label} ({acc.get("userid")})',
                   {'entries': len(items), 'debits': len(debits),
                    'consumed': total,
                    'recent': [i.get('modelName') for i in debits[:5]]})
        if errors:
            _print('错误', errors)

    elif cmd == 'first-login':
        acc = load_session()
        ok, r = first_login_reward(account_token(acc))
        _print('每日登录领取', r)

    elif cmd == 'daily':
        acc = load_session()
        session = account_token(acc)
        ok, r = query_points(session)
        _print('积分', r)
        ok2, r2 = first_login_reward(session)
        _print('每日登录领取', r2)

    # ── Web 端 (loomy.xunfei.cn, cookie 会话) ──
    elif cmd == 'web-send-code':
        ok, r = web_send_sms_code(args[1])
        _print('Web 发送验证码', {'success': ok, 'messageId/err': r})

    elif cmd == 'web-login':
        phone, code, msgid = args[1], args[2], args[3]
        ok, r = web_login(phone, code, msgid)
        if not ok:
            _print('Web 登录失败', r)
            return
        acct = save_web_session(phone, r['cookies'], r.get('deviceId', ''), r.get('me'))
        _print('Web 登录成功 (cookie 已写入 config providers.loomy.accounts)',
               {'userid': acct['userid'], 'cookie_names': sorted(r['cookies'].keys()),
                'deviceId': r.get('deviceId', '')})

    elif cmd == 'web-me':
        cookies = load_web_cookies()
        if not cookies:
            _print('Web 会话', 'config 中无 Web 账号, 请先 web-login')
            return
        ok, r = web_me(cookies)
        _print('Web 会话校验', {'valid': ok, 'me': r})

    elif cmd == 'web-models':
        cookies = load_web_cookies()
        if not cookies:
            _print('Web 模型目录', 'config 中无 Web 账号, 请先 web-login')
            return
        ok, r = web_models(cookies)
        _print('Web 模型目录', r)

    elif cmd == 'web-points':
        cookies = load_web_cookies()
        if not cookies:
            _print('Web 积分', 'config 中无 Web 账号, 请先 web-login')
            return
        ok, r = web_team_points(cookies)
        _print('Web 团队积分', r)

    else:
        print(__doc__)

if __name__ == '__main__':
    main()
