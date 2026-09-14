#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
open-ai 内部签到脚本 (不依赖任何外部脚本/目录)
============================================
合并原 auto_renew.py + checkin_all.py 的功能, 数据源全部来自 open-ai 自身 config:
  * TRAE token 自动续期   : config.json providers.trae (accounts[].cookie -> GetUserToken)
  * WorkBuddy 每日签到    : config.json providers.workbuddy (copilot.tencent.com)
  * WB 国际版每日签到     : config.json providers.workbuddy_intl (www.workbuddy.ai)
  * TRAE 每日签到         : config.json providers.trae (api.trae.cn)

用法:
  python scripts/signin_all.py          # 续期 + 双平台签到
  python scripts/signin_all.py --no-renew   # 只签到, 不续期
  python scripts/signin_all.py --force      # 强制续期 (忽略剩余时间)
"""
import base64
import json
import os
import random
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
OPENAI_CFG = os.path.join(HERE, '..', 'config.json')
LOGS_DIR = os.path.join(HERE, '..', 'logs')
DATA_DIR = os.path.join(HERE, '..', 'data')
SIGNIN_LOG = os.path.join(LOGS_DIR, 'signin.log')
# TRAE 签到状态: done=今日已签上 / pending=今日仍未签上(9074 繁忙), 供 daemon 补试
TRAE_SIGNIN_STATE = os.path.join(DATA_DIR, '.trae_signin_state')
# open-ai 自包含: TRAE 配置统一在 open-ai/config.json providers.trae 段

GET_TOKEN_URL = 'https://api.trae.cn/cloudide/api/v3/common/GetUserToken'
TRAE_STATUS_PATH = '/trae/api/v2/ug/checkin_credits/status'
TRAE_CLAIM_PATH = '/trae/api/v2/ug/checkin_credits/claim'
RENEW_THRESHOLD = 6 * 3600  # token 剩余 < 6 小时续期

UA_WB = 'WorkBuddy/5.3.12 WorkBuddy/5.3.12 CLI/2.115.0'
UA_TR = 'TRAE SOLO CN/2.3.70844'
UA_WEB = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
          '(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0')


def get_device_id():
    """从 open-ai/config.json providers.trae 读取设备 ID"""
    try:
        cfg = json.load(open(OPENAI_CFG, encoding='utf-8'))
        trae = (cfg.get('providers') or {}).get('trae') or {}
        return (trae.get('device_id') or (trae.get('headers') or {}).get('x-device-id') or '')
    except Exception:
        return ''


def ts():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def log(tag, msg):
    line = f'[{ts()}] [{tag}] {msg}'
    print(line)
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        with open(SIGNIN_LOG, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def post_json(url, headers, body=b'{}', timeout=15):
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=ssl.create_default_context()) as resp:
            return resp.status, resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')
    except Exception as e:
        return 0, str(e)


def load_json(path):
    return json.load(open(path, encoding='utf-8'))


def save_json(path, obj):
    json.dump(obj, open(path, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)


# ================= TRAE token 续期 =================

def decode_payload(token):
    try:
        parts = token.split('.')
        pad = parts[1] + '=' * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(pad))
    except Exception:
        return {}


def token_exp(token):
    return decode_payload(token).get('exp', 0) if token else 0


def get_token_by_cookie(cookie):
    req = urllib.request.Request(GET_TOKEN_URL, data=b'{}', method='POST', headers={
        'Cookie': cookie, 'User-Agent': UA_WEB, 'Content-Type': 'application/json',
        'Origin': 'https://www.trae.cn', 'Referer': 'https://www.trae.cn/',
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            j = json.loads(r.read().decode('utf-8', 'replace'))
            tok = j.get('Result', {}).get('Token', '')
            if tok:
                return tok, decode_payload(tok).get('data', {}).get('id', '?')
            return None, None
    except Exception:
        return None, None


def verify_token(token, device_id):
    code, _ = post_json('https://api.trae.cn/trae/api/v2/pay/ide_user_ent_usage',
                        {'Authorization': f'Cloud-IDE-JWT {token}',
                         'x-device-id': device_id, 'Content-Type': 'application/json',
                         'User-Agent': UA_TR},
                        b'{"require_usage": true, "req_source": 1}')
    return code == 200


def ensure_account_device_ids(trae, openai_cfg):
    """为没有 device_id 的 TRAE 账号自动分配一个七位随机数 device_id (如 8888888)。

    TRAE 按 x-device-id 数值判定"设备", 同值 device 只能给一个账号签到。
    因此每个账号都应配一个独立的简单数字 device_id。这里对缺失的自动生成并写回 config。
    返回 True 表示有改动(已写回 config)。
    """
    accounts = trae.get('accounts') or []
    if not accounts:
        return False
    # 收集已占用的 device_id (全局 + 各账号), 避免撞号
    used = set()
    g = trae.get('device_id') or (trae.get('headers') or {}).get('x-device-id') or ''
    if g:
        used.add(str(g))
    for a in accounts:
        if a.get('device_id'):
            used.add(str(a['device_id']))
    changed = False
    for a in accounts:
        if a.get('device_id'):
            continue  # 已有独立 device_id, 不覆盖
        # 生成不与现有冲突的七位随机数
        for _ in range(50):
            cand = str(random.randint(1000000, 9999999))
            if cand not in used:
                break
        else:
            cand = str(random.randint(1000000, 9999999))
        a['device_id'] = cand
        used.add(cand)
        log('device_id', f'账号{a.get("uid", "?")} 未配置 device_id, 已自动分配 {cand}')
        changed = True
    if changed:
        openai_cfg['providers']['trae'] = trae
        save_json(OPENAI_CFG, openai_cfg)
        log('device_id', '已保存自动分配的 device_id 到 config.json')
    return changed


def renew_trae_tokens(cfg, force=False, device_id=''):
    accounts = cfg.get('accounts', [])
    now = int(time.time())
    changed = False
    for acc in accounts:
        uid = acc.get('uid', '?')
        cookie = acc.get('cookie', '')
        exp = token_exp(acc.get('token', ''))
        remain = exp - now if exp else 0
        if not cookie:
            log('续期', f'账号{uid} 无 cookie, 跳过')
            continue
        if not force and exp and remain > RENEW_THRESHOLD:
            log('续期', f'账号{uid} token 仍有效 (剩余 {remain // 3600}h)')
            continue
        new_tok, new_uid = get_token_by_cookie(cookie)
        if not new_tok:
            log('续期', f'账号{uid} GetUserToken 失败, 跳过')
            continue
        if str(new_uid) != str(uid):
            log('续期', f'账号{uid} cookie 换到 uid={new_uid} 不匹配, 跳过')
            continue
        acc_did = acc.get('device_id') or device_id  # 账号可自带独立 device_id (多账号不同设备)
        if not verify_token(new_tok, acc_did):
            log('续期', f'账号{uid} 新 token 验证失败, 跳过')
            continue
        acc['token'] = new_tok
        changed = True
        log('续期', f'账号{uid} 已续期 (新 token 剩余 {(token_exp(new_tok) - now) // 3600}h)')
    return changed


# ================= WorkBuddy 签到 =================



def today_str():
    return time.strftime('%Y-%m-%d')


def _wb_headers(acc, domain, product):
    """WorkBuddy 系请求头: 国际版额外带 X-Product-Code。"""
    headers = {
        'Accept': 'application/json', 'Content-Type': 'application/json',
        'Authorization': f"Bearer {acc.get('accessToken', '')}",
        'X-User-Id': acc.get('userId', ''),
        'X-Domain': domain, 'X-Product': product,
        # ⚠️ 不能带 X-Enterprise-Id: 会切到企业视角, 个人资源包被隐藏
        'User-Agent': UA_WB,
    }
    if str(domain).endswith('.ai'):
        headers['X-Product-Code'] = product
    return headers


def _wb_post_result(base, path, headers):
    """POST 并返回 (ok, result_dict, http_code)。

    ⚠️ 只有「HTTP 200 + JSON 解析成功」才 ok=True。
    传输失败 (HTTP 0, 如超时) / 非 200 非 JSON / 解析失败一律 ok=False ——
    修复旧版"兜底 {'code': 0} 把超时记成签到成功"的假成功 bug
    (实测 2026-09-12 16:22/16:28 两条假成功, 耗时均为 15s 超时整)。
    """
    code, raw = post_json(base + path, headers)
    if code == 0:
        return False, {'_error': f'传输失败: {raw[:120]}'}, 0
    try:
        d = json.loads(raw)
    except Exception:
        return False, {'_error': f'响应非 JSON: {raw[:120]}'}, code
    if not isinstance(d, dict):
        return False, {'_error': f'响应结构异常: {str(d)[:120]}'}, code
    return True, d, code


def wb_checkin_one(acc, domain, product,
                   base='https://copilot.tencent.com', tag='WB签到'):
    """WorkBuddy 系签到 (国内 host=copilot.tencent.com, 国际版 host=www.workbuddy.ai)。

    2026-09-13 按"深扒国际版签到"实测结论重写 (详见 MEMORY.md):
    1. 状态接口切换到官方在用的 checkin-activity-status:
       旧 checkin-status 是废弃接口, 返回死数据 (国内账号实测 active=false/streak=0,
       同一时刻新接口 active=true/streak=13/total=1300), 官方 web/IDE 均只用新接口。
    2. active=false 时跳过领取: 官方 UI 在 active=false 时连签到入口都不渲染,
       此时 daily-checkin 必返回 10001"签到活动未开启或已过期"。
       国际版 (workbuddy.ai) 目前无签到渠道, 账号恒为该形态 —— 每天只打 1 次状态
       探测, 不再每 10 秒白打领取请求。
    3. 10001 是终态 (官方错误映射: 1001=已领 1002=无资格 1003=活动结束,
       其余未知), 不再"补领"—— 补领 100% 失败, 只是刷日志。
    4. 仅 HTTP 200 + JSON 解析成功 + code==0 才算签到成功。
    """
    uid = acc.get('userId', '?')
    if acc.get('enabled') is False:
        log(tag, f'账号{uid} enabled=false, 跳过')
        return
    headers = _wb_headers(acc, domain, product)

    # 1) 活动状态 (官方真实接口)
    ok, d, code = _wb_post_result(base, '/billing/meter/checkin-activity-status', headers)
    if not ok:
        # 状态查询失败不瞎领: 宁可等 daemon 下轮补试 (30 分钟), 也不盲打领取
        log(tag, f'账号{uid} 活动状态查询失败 HTTP {code} ({d.get("_error", "")})')
        return
    st = d.get('data') or {}
    if st.get('today_checked_in'):
        log(tag, f'账号{uid} 今日已签到 (streak={st.get("streak_days", 0)})')
        return
    if not st.get('active'):
        # 活动对该账号未开启/未参与 (终态): 打 daily-checkin 必 10001, 直接跳过
        log(tag, f'账号{uid} 签到活动未参与 (active=false), 今日跳过领取')
        return

    # 2) 领取 (仅 active=true 且今日未签时才会走到这里)
    ok, d, code = _wb_post_result(base, '/billing/meter/daily-checkin', headers)
    if not ok:
        log(tag, f'账号{uid} 领取请求失败 HTTP {code} ({d.get("_error", "")})')
        return
    c = d.get('code')
    if c == 0:
        credit = (d.get('data') or {}).get('credit', '')
        streak = (d.get('data') or {}).get('streak_days', '')
        log(tag, f'账号{uid} 签到成功! credit={credit} streak={streak}')
    elif c == 10001:
        # 服务端终态拒绝 (已领过/活动未开启): 不补领
        log(tag, f'账号{uid} 服务端判定不可领取 (code=10001), 视为终态')
    elif c == 400:
        log(tag, f'账号{uid} 今日已签到(HTTP 400/重复领取)')
    else:
        log(tag, f'账号{uid} 签到失败 code={c} '
                 f'message={d.get("message") or d.get("msg") or ""}')


# 国际版 host (与国内版接口路径完全一致)
WB_INTL_BASE = 'https://www.workbuddy.ai'


def wb_intl_checkin_one(acc, domain, product):
    """WorkBuddy 国际版签到 (www.workbuddy.ai)。"""
    wb_checkin_one(acc, domain, product, base=WB_INTL_BASE, tag='WB国际签到')


# ================= Loomy 每日登录积分 =================
# Loomy (讯飞) 每日首次登录领取: POST /api/v1/points/first-login (头 token: <session>)
# Web 账号 (kind=web, 凭据 cookies) 无显式领取端点 —— 积分随当日登录由服务端
# 自动发放, 故「签到」语义 = 会话有效 + 拉到 points-summary。
# 两条路径的结果都写入 data/loomy_signin_state.json (按天覆盖), 供
# admin_api /accounts/signin 渲染账号页「每日签到」列 —— Loomy 不进流水库,
# 这份缓存就是「今天领没领到」的本地唯一凭证。

def loomy_daily_one(acc):
    import loomy_client as lc
    userid = acc.get('userid', '')
    phone = acc.get('phone', '?')
    if not acc.get('enabled', True):
        log('Loomy', f'账号{phone} 已禁用, 跳过')
        return

    # ---- Web 账号 (cookies): 登录即领取 ----
    cookies = acc.get('cookies')
    if cookies:
        ok, me = lc.web_me(cookies)
        if not ok:
            lc.record_daily_result(userid, False, error='Web 会话失效, 请重新添加账号')
            log('Loomy', f'账号{phone} Web 会话失效, 未领取')
            return
        pok, ps = lc.web_request_json('GET', '/web/api/auth/points-summary', cookies)
        if pok and isinstance(ps, dict):
            d = ps.get('data') or {}
            lc.record_daily_result(userid, True, data={
                'currentBalance': d.get('permanent'),
                'dailyBalance': d.get('daily'),
                'dailyQuota': d.get('daily'),
            })
            log('Loomy', f"账号{phone} 今日登录成功! 永久={d.get('permanent')} "
                         f"每日额度={d.get('daily')}")
        else:
            lc.record_daily_result(userid, True, data=None,
                                   error=f'积分查询失败: {str(ps)[:120]}')
            log('Loomy', f'账号{phone} 登录成功但积分查询失败: {str(ps)[:120]}')
        return

    # ---- 桌面账号 (session): 显式领取 first-login ----
    session = acc.get('session', '')
    if not session:
        log('Loomy', f'账号{phone} 无 session, 跳过')
        return
    try:
        ok, r = lc.first_login_reward(session)
    except Exception as e:
        lc.record_daily_result(userid, False, error=str(e))
        log('Loomy', f'账号{phone} 领取异常: {e}')
        return
    if not ok or not isinstance(r, dict):
        lc.record_daily_result(userid, False, error=str(r)[:200])
        log('Loomy', f'账号{phone} 领取失败: {r}')
        return
    if r.get('code') != '000000':
        lc.record_daily_result(userid, False, error=f"code={r.get('code')} desc={r.get('desc')}")
        log('Loomy', f"账号{phone} 领取失败 code={r.get('code')} desc={r.get('desc')}")
        return
    d = r.get('data') or {}
    lc.record_daily_result(userid, True, data=d)
    log('Loomy', f"账号{phone} 领取成功! 当前余额={d.get('currentBalance')} "
                 f"每日={d.get('dailyBalance')}/{d.get('dailyQuota')} "
                 f"已领={d.get('alreadyProcessed')}")


# ================= TRAE 签到 =================

def trae_is_checked_in(data):
    if not data:
        return False
    if data.get('checked_in') is True:
        return True
    packs = data.get('user_entitlement_pack_list') or []
    key = f"checkin_{datetime.now().strftime('%Y%m%d')}"
    return any(key in (p.get('entitlement_base_info') or {}).get('entitlement_id', '')
               for p in packs)


def trae_checkin_one(acc, device_id):
    uid = acc.get('uid', '?')
    # 账号可自带独立 device_id, 优先于全局 (多账号各用不同 device 以绕过"一设备一账号"限制)
    device_id = acc.get('device_id') or device_id
    headers = {
        'authorization': f"Cloud-IDE-JWT {acc.get('token', '')}",
        'x-device-id': device_id, 'content-type': 'application/json',
        'user-agent': UA_TR, 'accept': '*/*',
    }
    base = 'https://api.trae.cn'
    code, raw = post_json(base + TRAE_STATUS_PATH, headers)
    if code != 200:
        log('TRAE签到', f'账号{uid} 状态查询失败 HTTP {code}: {raw[:100]}')
        return
    try:
        data = json.loads(raw)
    except Exception:
        log('TRAE签到', f'账号{uid} 状态解析失败')
        return
    # 打印 status 关键字段, 便于排查 (credits=当前积分, enable=签到功能是否开启)
    if isinstance(data, dict):
        dc = data.get('data') if isinstance(data.get('data'), dict) else data
        log('TRAE签到', f'账号{uid} status: checked_in={data.get("checked_in")} '
                         f'credits={dc.get("credits")} enable={dc.get("enable")}')
    if trae_is_checked_in(data):
        log('TRAE签到', f'账号{uid} 今日已签到')
        return True
    # claim: 9074 = "当前参与用户太多, 请稍后再试" (服务端临时繁忙)。
    # 单次运行只试 2 次(短间隔), 不长时间阻塞; 真正"反复补试"交给 daemon:
    # daemon 每 30 分钟跑一次 --trae-only, 一整天累计几十次机会, 足以蹭到 TRAE 放开窗口。
    max_retry = 2
    for attempt in range(1, max_retry + 1):
        code, raw = post_json(base + TRAE_CLAIM_PATH, headers)
        try:
            result = json.loads(raw)
        except Exception:
            result = {'code': code, 'raw': raw[:100]}
        c = result.get('code')
        if c == 0:
            log('TRAE签到', f'账号{uid} 签到成功! (第{attempt}次)')
            return True
        if c == 9074 and attempt < max_retry:
            wait = random.randint(20, 60)
            log('TRAE签到', f'账号{uid} 繁忙(9074), {wait}s 后重试 ({attempt}/{max_retry})')
            time.sleep(wait)
            continue
        m = result.get('message') or result.get('msg') or ''
        log('TRAE签到', f'账号{uid} claim code={c} message={m} raw={raw[:200]}')
        return False
    log('TRAE签到', f'账号{uid} 本次 2 次仍繁忙(9074), 稍后由 daemon 补试')
    return False


# ================= 主流程 =================

def set_trae_state(state):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(TRAE_SIGNIN_STATE, 'w', encoding='utf-8') as f:
            f.write(state)
    except Exception:
        pass


def get_trae_state():
    try:
        with open(TRAE_SIGNIN_STATE, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return ''


def main():
    force = '--force' in sys.argv
    do_renew = '--no-renew' not in sys.argv
    trae_only = '--trae-only' in sys.argv
    wb_only = '--wb-only' in sys.argv
    results = []

    # 统一配置: open-ai/config.json
    openai_cfg = load_json(OPENAI_CFG)
    providers = openai_cfg.get('providers') or {}
    trae = providers.get('trae') or {}
    device_id = (trae.get('device_id') or (trae.get('headers') or {}).get('x-device-id') or '')
    trae_accs = trae.get('accounts') or []

    # 缺失 device_id 的账号自动分配七位随机数并写回 config (多账号各配独立设备)
    ensure_account_device_ids(trae, openai_cfg)

    # --wb-only: 仅供 daemon 白天补签 WorkBuddy (跳过续期与 TRAE)
    if wb_only:
        # 2026-09-13 重写: 旧版用资源包预检 (_wb_pack_credited_today) 判"今日已入账",
        # 会把注册/订阅礼包误判为签到入账 (实测新账号注册当天被挡, 100 分没领),
        # 且查询失败时 fail-open 也跳过领取。现在统一交给 wb_checkin_one:
        # 其内部用官方 checkin-activity-status 的 today_checked_in/active 判定,
        # 幂等 (已签/未参与都直接返回), 重复调用无副作用。
        wb = providers.get('workbuddy') or {}
        wb_accs = wb.get('accounts') or []
        domain = wb.get('domain', 'www.workbuddy.cn')
        product = wb.get('product', 'SaaS')
        log('WB签到', f'--wb-only 补签检查 ({len(wb_accs)} 个账号)')
        for acc in wb_accs:
            wb_checkin_one(acc, domain, product)
        # 国际版同样走 --wb-only 补签 (daemon 白天补试也覆盖国际版账号)
        wbai = providers.get('workbuddy_intl') or {}
        wbai_accs = wbai.get('accounts') or []
        i_domain = wbai.get('domain', 'www.workbuddy.ai')
        i_product = wbai.get('product', 'workbuddy-ai')
        if wbai_accs:
            log('WB国际签到', f'--wb-only 补签检查 ({len(wbai_accs)} 个账号)')
            for acc in wbai_accs:
                wb_intl_checkin_one(acc, i_domain, i_product)
        log('WB补签', '本轮补签检查完成')
        sys.exit(0)

    # --trae-only: 仅供 daemon 白天补试 TRAE, 跳过 WB
    # 修复 (2026-09-13): --trae-only 现在也做 token 续期。旧版跳过续期, 一旦
    # 0 点全量签到被竞态吞掉, 无人续期 → token 过期 → 签到/采集全 401
    # (实测 09-13 04:29 过期, 采集器 401 持续到 12:54 才由侧车 cookie 兜底)。

    # 1) TRAE token 续期 (全量与 --trae-only 补试都做)
    if do_renew:
        log('续期', f'TRAE token 自动续期 ({len(trae_accs)} 个账号, 阈值 {RENEW_THRESHOLD // 3600}h)')
        changed = renew_trae_tokens(trae, force, device_id)
        if changed:
            openai_cfg['providers']['trae'] = trae
            save_json(OPENAI_CFG, openai_cfg)
            log('续期', '已保存新 token (重启 open-ai 后生效)')
        else:
            log('续期', '所有 token 均在有效期内')
        results.append(('TRAE续期', 'done'))

    # 2) WorkBuddy 签到 (--trae-only 补试时跳过, WB 走 --wb-only 独立节奏)
    if not trae_only:
        wb = providers.get('workbuddy') or {}
        wb_accs = wb.get('accounts') or []
        domain = wb.get('domain', 'www.workbuddy.cn')
        product = wb.get('product', 'SaaS')
        log('WB签到', f'WorkBuddy 签到 ({len(wb_accs)} 个账号)')
        for acc in wb_accs:
            wb_checkin_one(acc, domain, product)

        # 2.5) WorkBuddy 国际版签到 (host=www.workbuddy.ai)
        wbai = providers.get('workbuddy_intl') or {}
        wbai_accs = wbai.get('accounts') or []
        i_domain = wbai.get('domain', 'www.workbuddy.ai')
        i_product = wbai.get('product', 'workbuddy-ai')
        log('WB国际签到', f'WorkBuddy 国际版签到 ({len(wbai_accs)} 个账号)')
        for acc in wbai_accs:
            wb_intl_checkin_one(acc, i_domain, i_product)

        # 2.6) Loomy 每日登录积分 (providers.loomy.accounts)
        try:
            import loomy_client as lc
            loomy_accs = lc.load_accounts()
            log('Loomy', f'Loomy 每日登录 ({len(loomy_accs)} 个账号)')
            for acc in loomy_accs:
                loomy_daily_one(acc)
        except Exception as e:
            log('Loomy', f'Loomy 签到异常: {e}')

    # 3) TRAE 签到 (始终执行)
    log('TRAE签到', f'TRAE 签到 ({len(trae_accs)} 个账号)')
    # 无账号时视为未完成(避免误标 done 导致 daemon 不再补试)
    trae_ok = len(trae_accs) > 0
    for acc in trae_accs:
        if not trae_checkin_one(acc, device_id):
            trae_ok = False

    if trae_ok:
        # 状态带日期: daemon 按 'done:<今天>' 判断是否需要补试,
        # 避免昨天的裸 'done' 残留压制今天的补试 (实测 09-13 踩坑)
        set_trae_state('done:' + today_str())
        log('TRAE签到', '今日 TRAE 签到已完成 (state=done)')
    else:
        set_trae_state('pending')
        log('TRAE签到', '今日 TRAE 尚未签上 (state=pending), 稍后由 daemon 补试')

    print()
    log('结果', ' | '.join(f'{k}: {v}' for k, v in results) + f' | TRAE: {"done" if trae_ok else "pending"}')

    # 退出码: 0=TRAE 已签上(或本就今日已签) / 1=TRAE 仍繁忙(供 daemon 判断补试)
    sys.exit(0 if trae_ok else 1)


if __name__ == '__main__':
    main()
