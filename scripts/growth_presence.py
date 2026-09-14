# -*- coding: utf-8 -*-
"""growth_presence.py — WorkBuddy 国际版「每日活跃奖励 +30」存在感实验 (2026-09-14)

背景 (见 MEMORY.md 国际版签到章节):
  +30 不是 billing 的签到 (checkin-activity-status 恒 active=false),
  而是 /activity/growth/buddy 「Buddy 成长计划」的活动奖励 (TCACA_code_007)。
  09-13 客户端使用当晚 → 09-14 03:12 +30 到账; 09-12/09-13 纯网关 API 流量无奖励。

本脚本验证: 在客户端完全不开的前提下, 由网关复刻客户端的后台心跳——
  - GET  /portal/msg-center/message/summary   (客户端 30s 轮询)
  - GET  /activity/growth/buddy/info          (客户端 60s 轮询)
  - 消息中心出现 growth.travel_arrival 且 claim_status=unclaimed 时自动
    POST /activity/growth/buddy/travel/claim  (幂等, 无奖励时 code:0 data:{})
  - 每 10 分钟扫一次 get-user-resource, 新资源包即记录 (对照到账时间)

若次日 03:12 前后 +30 照常到账 → 纯 API 心跳即可替代客户端, 可常驻自动化。
日志: logs/growth_presence.log
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
import ssl

# ★ 同 credits_api: 钉死安装根, 打包态不受 CWD 影响。
try:
    import app_paths as _ap
    CFG_PATH = _ap.CONFIG_PATH
    LOG_PATH = os.path.join(_ap.LOGS_DIR, 'growth_presence.log')
except Exception:  # 源码态单独运行的兜底
    BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    CFG_PATH = os.path.join(BASE, 'config.json')
    LOG_PATH = os.path.join(BASE, 'logs', 'growth_presence.log')
API = 'https://www.workbuddy.ai'
MSG_SUMMARY_INTERVAL = 30      # 客户端 summary 轮询节奏
BUDDY_INTERVAL = 60            # 客户端 buddy 轮询节奏
PACK_WATCH_INTERVAL = 600      # 资源包对照 10 分钟
DAILY_CLAIM_AT = '09:05'       # 每天}一次主动 claim (幂等)

_ssl = ssl.create_default_context()
_known_packs = set()
_last_daily_claim_date = ''


def log(msg):
    line = '[%s] %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass
    print(line, flush=True)


def load_account():
    with open(CFG_PATH, encoding='utf-8') as f:
        cfg = json.load(f)
    acc = ((cfg.get('providers') or {}).get('workbuddy_intl') or {}).get('accounts') or []
    if not acc:
        raise SystemExit('config 里没有 workbuddy_intl 账号')
    return acc[0]


def headers(acc):
    return {
        'Accept': 'application/json', 'Content-Type': 'application/json',
        'Authorization': 'Bearer %s' % acc.get('accessToken', ''),
        'X-User-Id': acc.get('userId', ''),
        'X-Domain': 'www.workbuddy.ai', 'X-Product': 'workbuddy-ai',
        # ★ msg-center 必须: 客户端身份头 (缺它报 17043 client not supported)
        'X-Product-Code': 'workbuddy',
        'User-Agent': 'WorkBuddy/5.3.12 WorkBuddy/5.3.12 CLI/2.115.0',
    }


def call(acc, path, method='GET', body=None):
    data = json.dumps(body or {}).encode() if method == 'POST' else None
    req = urllib.request.Request(API + path, data=data, headers=headers(acc), method=method)
    try:
        with urllib.request.urlopen(req, timeout=15, context=_ssl) as resp:
            return resp.status, resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')
    except Exception as e:
        return 0, str(e)


def jparse(raw):
    try:
        return json.loads(raw)
    except Exception:
        return None


def check_messages(acc):
    code, raw = call(acc, '/portal/msg-center/message/list')
    if code != 200:
        log('[msg] list HTTP %s: %s' % (code, raw[:120]))
        return
    d = jparse(raw) or {}
    msgs = (d.get('data') or {}).get('messages') or []
    if not msgs:
        return
    for m in msgs:
        biz = m.get('biz_type') or ''
        if biz != 'growth.travel_arrival':
            continue
        ext = m.get('ext') or {}
        log('[msg] 到站消息! claim_status=%s reward_credit=%s msg_id=%s' % (
            ext.get('claim_status'), ext.get('reward_credit'), m.get('msg_id')))
        if ext.get('claim_status') == 'unclaimed':
            c2, raw2 = call(acc, '/activity/growth/buddy/travel/claim', 'POST')
            r2 = jparse(raw2) or {}
            log('[claim] travel/claim → HTTP %s code=%s data=%s' % (
                c2, r2.get('code'), json.dumps(r2.get('data') or {}, ensure_ascii=False)))
            if r2.get('code') == 0 and (r2.get('data') or {}):
                log('[claim] ★ 领取成功! 等待批量入账 (预计数小时后)')


def check_buddy(acc, iteration):
    if iteration % max(1, BUDDY_INTERVAL // MSG_SUMMARY_INTERVAL):
        return
    code, raw = call(acc, '/activity/growth/buddy/info')
    if code != 200:
        log('[buddy] HTTP %s: %s' % (code, raw[:100]))
        return
    d = jparse(raw) or {}
    buddy = (d.get('data') or {}).get('buddy')
    if buddy:
        log('[buddy] ★ Buddy 出现: %s' % json.dumps(buddy, ensure_ascii=False)[:300])
    # buddy=null 属常态, 不刷日志


def watch_packs(acc):
    global _known_packs
    # ⚠ 必须 POST (GET → 404), 与 usage_collector.wb_today_packs 同姿势
    code, raw = call(acc, '/billing/meter/get-user-resource', 'POST')
    if code != 200:
        log('[pack] watch HTTP %s: %s' % (code, raw[:100]))
        return
    d = jparse(raw) or {}
    try:
        accounts = d['data']['Response']['Data']['Accounts']
    except Exception:
        return
    for a in accounts:
        rid = a.get('PackageCode') or a.get('DealName') or ''
        ct = a.get('CreateTime')
        try:
            ts = time.strftime('%m-%d %H:%M:%S', time.localtime(float(ct) / 1000))
        except (TypeError, ValueError):
            ts = '?'
        if rid and rid not in _known_packs:
            _known_packs.add(rid)
            log('[pack] 新资源包: %s | %s | %s分 | CreateTime=%s' % (
                a.get('PackageName'), rid, a.get('CapacitySize'), ts))


def daily_claim(acc):
    global _last_daily_claim_date
    today = time.strftime('%Y-%m-%d')
    if _last_daily_claim_date == today:
        return
    if time.strftime('%H:%M') < DAILY_CLAIM_AT:
        return
    _last_daily_claim_date = today
    code, raw = call(acc, '/activity/growth/buddy/travel/claim', 'POST')
    d = jparse(raw) or {}
    log('[daily] 每日主动 claim → HTTP %s code=%s data=%s' % (
        code, d.get('code'), json.dumps(d.get('data') or {}, ensure_ascii=False)))


def main():
    acc = load_account()
    log('=== 存在感实验启动: uid=%s 客户端进程应保持关闭 ===' % acc.get('userId', '?')[:8])
    iteration = 0
    last_pack_watch = 0.0
    while True:
        try:
            check_messages(acc)
            check_buddy(acc, iteration)
            daily_claim(acc)
            if time.time() - last_pack_watch >= PACK_WATCH_INTERVAL:
                last_pack_watch = time.time()
                watch_packs(acc)
        except Exception as e:
            log('[err] %r' % (e,))
        iteration += 1
        time.sleep(MSG_SUMMARY_INTERVAL)


if __name__ == '__main__':
    main()
