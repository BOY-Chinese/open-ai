#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
open-ai 内部签到脚本 (不依赖任何外部脚本/目录)
============================================
合并原 auto_renew.py + checkin_all.py 的功能, 数据源全部来自 open-ai 自身 config:
  * TRAE token 自动续期   : config.json providers.trae (accounts[].cookie -> GetUserToken)
  * WorkBuddy 每日签到    : config.json providers.workbuddy (copilot.tencent.com)
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


def _wb_pack_credited_today(acc, domain, product):
    """检查该 WB 账号今天是否有入账的资源包 (签到积分到账的真实凭证)。"""
    headers = {
        'Accept': 'application/json', 'Content-Type': 'application/json',
        'Authorization': f"Bearer {acc.get('accessToken', '')}",
        'X-User-Id': acc.get('userId', ''),
        'X-Domain': domain, 'X-Product': product,
        # ⚠️ 不能带 X-Enterprise-Id: 会切到企业视角, 个人资源包被隐藏
        'User-Agent': UA_WB,
    }
    code, raw = post_json('https://copilot.tencent.com/billing/meter/get-user-resource', headers)
    if code != 200:
        return True   # 查询失败时保守放行, 不阻断正常流程
    try:
        d = json.loads(raw)
        accounts = d['data']['Response']['Data']['Accounts']
    except Exception:
        return True
    today = time.strftime('%Y-%m-%d')
    for a in accounts:
        ct = a.get('CreateTime')
        try:
            if time.strftime('%Y-%m-%d', time.localtime(float(ct) / 1000)) == today:
                return True
        except (TypeError, ValueError):
            continue
    return False


def wb_checkin_one(acc, domain, product):
    uid = acc.get('userId', '?')
    headers = {
        'Accept': 'application/json', 'Content-Type': 'application/json',
        'Authorization': f"Bearer {acc.get('accessToken', '')}",
        'X-User-Id': uid, 'X-Domain': domain, 'X-Product': product,
        'User-Agent': UA_WB,
    }
    base = 'https://copilot.tencent.com'
    code, raw = post_json(base + '/billing/meter/checkin-status', headers)
    if code != 200:
        log('WB签到', f'账号{uid} 状态查询失败 HTTP {code}')
        return
    try:
        d = json.loads(raw)
        st = d.get('data') or {}
    except Exception:
        log('WB签到', f'账号{uid} 状态解析失败: {raw[:100]}')
        return
    if st.get('today_checked_in'):
        log('WB签到', f'账号{uid} 今日已签到 (streak={st.get("streak_days", 0)})')
        return
    # 注意: 不依赖 status 的 active 字段判断是否跳过 —— 实测即使 active=false,
    # daily-checkin 也照常返回 code:0/credit:100 (每日基础积分可领)。
    # 若 active=false 就跳过, 会漏签。改为始终调用 daily-checkin,
    # 由服务端幂等处理 (重复领取返回 400 / 已签)。
    if not st.get('active'):
        log('WB签到', f'账号{uid} active=false 但仍尝试直接领取')
    code, raw = post_json(base + '/billing/meter/daily-checkin', headers)
    try:
        result = json.loads(raw)
    except Exception:
        result = {'code': code, 'raw': raw[:100]}
    c = result.get('code')
    if c == 0:
        credit = (result.get('data') or {}).get('credit', '')
        streak = (result.get('data') or {}).get('streak_days', '')
        log('WB签到', f'账号{uid} 签到成功! credit={credit} streak={streak}')
    elif c == 10001:
        # ⚠️ 10001 不能盲信为"已签": 实测 0 点跨天边界服务端会误报 10001,
        # 而实际积分没到账 (当日无入账资源包)。用资源包校验, 无入账则补领一次。
        if _wb_pack_credited_today(acc, domain, product):
            log('WB签到', f'账号{uid} 今日已签到(10001, 资源包已入账)')
        else:
            log('WB签到', f'账号{uid} 10001 但无入账包(疑似跨天误报), 补领...')
            time.sleep(3)
            code2, raw2 = post_json(base + '/billing/meter/daily-checkin', headers)
            try:
                r2 = json.loads(raw2)
            except Exception:
                r2 = {}
            if r2.get('code') == 0:
                credit2 = (r2.get('data') or {}).get('credit', '')
                log('WB签到', f'账号{uid} 补领成功! credit={credit2}')
            else:
                log('WB签到', f'账号{uid} 补领仍失败 code={r2.get("code")} '
                              f'(若持续, 检查 {today_str()} 资源包是否入账)')
    elif c == 400:
        log('WB签到', f'账号{uid} 今日已签到(HTTP 400/重复领取)')
    else:
        log('WB签到', f'账号{uid} 签到失败 code={c} message={result.get("message") or result.get("msg") or ""} raw={raw[:200]}')


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
        wb = providers.get('workbuddy') or {}
        wb_accs = wb.get('accounts') or []
        domain = wb.get('domain', 'www.workbuddy.cn')
        product = wb.get('product', 'SaaS')
        log('WB签到', f'--wb-only 补签检查 ({len(wb_accs)} 个账号)')
        for acc in wb_accs:
            if not _wb_pack_credited_today(acc, domain, product):
                wb_checkin_one(acc, domain, product)
            else:
                log('WB签到', f"账号{acc.get('userId', '?')} 今日已入账, 跳过")
        log('WB补签', '本轮补签检查完成')
        sys.exit(0)

    # --trae-only: 仅供 daemon 白天补试 TRAE, 跳过续期与 WB
    if not trae_only:
        # 1) TRAE token 续期
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

        # 2) WorkBuddy 签到
        wb = providers.get('workbuddy') or {}
        wb_accs = wb.get('accounts') or []
        domain = wb.get('domain', 'www.workbuddy.cn')
        product = wb.get('product', 'SaaS')
        log('WB签到', f'WorkBuddy 签到 ({len(wb_accs)} 个账号)')
        for acc in wb_accs:
            wb_checkin_one(acc, domain, product)

    # 3) TRAE 签到 (始终执行)
    log('TRAE签到', f'TRAE 签到 ({len(trae_accs)} 个账号)')
    # 无账号时视为未完成(避免误标 done 导致 daemon 不再补试)
    trae_ok = len(trae_accs) > 0
    for acc in trae_accs:
        if not trae_checkin_one(acc, device_id):
            trae_ok = False

    if trae_ok:
        set_trae_state('done')
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
