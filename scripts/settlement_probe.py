# -*- coding: utf-8 -*-
"""settlement_probe.py — WorkBuddy 国际版「每日 30 积分」结算探测 (2026-09-15)

03:20 由后台闹钟任务自动调用 (无需客户端在跑), 也可随时手动重跑:
    python3 scripts/settlement_probe.py [标签]

只读探测, 不做任何 claim (daemon 已自动 claim):
  1. /billing/meter/get-user-resource  → 今天 (09-15) 新入账的资源包 ← 决定性证据
  2. /portal/msg-center/message/list   → growth.travel_arrival 消息与 claim_status
  3. /activity/growth/buddy/info       → buddy 是否出现
输出: stdout + logs/settlement_verdict.log (追加)
"""
import json
import os
import ssl
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(BASE, 'config.json')
API = 'https://www.workbuddy.ai'


def load_acc():
    with open(CFG, encoding='utf-8') as f:
        cfg = json.load(f)
    return cfg['providers']['workbuddy_intl']['accounts'][0]


def call(acc, path, method='GET'):
    H = {
        'Accept': 'application/json', 'Content-Type': 'application/json',
        'Authorization': 'Bearer %s' % acc.get('accessToken', ''),
        'X-User-Id': acc.get('userId', ''),
        'X-Domain': 'www.workbuddy.ai', 'X-Product': 'workbuddy-ai',
        'X-Product-Code': 'workbuddy',
        'User-Agent': 'WorkBuddy/5.3.12 WorkBuddy/5.3.12 CLI/2.115.0',
    }
    data = json.dumps({}).encode() if method == 'POST' else None
    req = urllib.request.Request(API + path, data=data, headers=H, method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
            return r.status, json.loads(r.read().decode('utf-8', 'replace'))
    except Exception as e:
        return 0, {'_err': repr(e)}


def fmt_ts(ms):
    try:
        return time.strftime('%m-%d %H:%M:%S', time.localtime(float(ms) / 1000))
    except (TypeError, ValueError):
        return str(ms)


def main():
    label = ' '.join(sys.argv[1:]).strip() or '结算探测'
    lines = []
    net_fail = False  # 任一接口网络层失败 → 结论不可信, 必须复核

    def out(s):
        lines.append(s)

    acc = load_acc()
    out('[%s] === %s (uid=%s) ===' % (
        time.strftime('%Y-%m-%d %H:%M:%S'), label, acc.get('userId', '?')[:8]))

    # 1) 资源包 — 决定性证据 (⚠ 必须 POST, GET 404)
    today_packs = []
    code, d = call(acc, '/billing/meter/get-user-resource', 'POST')
    if code == 200:
        try:
            accounts = d['data']['Response']['Data']['Accounts']
        except Exception:
            accounts = []
        out('[pack] get-user-resource HTTP %s, 共 %d 个资源包:' % (code, len(accounts)))
        for a in accounts:
            ts = fmt_ts(a.get('CreateTime'))
            out('  - %s | %s | %s分 | CreateTime=%s' % (
                a.get('PackageName'), a.get('PackageCode'), a.get('CapacitySize'), ts))
            try:
                ct = float(a.get('CreateTime')) / 1000.0
                if time.localtime(ct).tm_mday == time.localtime().tm_mday:
                    today_packs.append((a.get('PackageName') or '', a.get('PackageCode') or '',
                                        a.get('CapacitySize'), ts))
            except (TypeError, ValueError):
                pass
    else:
        out('[pack] get-user-resource HTTP %s: %r' % (code, d.get('_err')))
        net_fail = True

    # 2) 消息中心
    code2, d2 = call(acc, '/portal/msg-center/message/list')
    travel, unclaimed = [], []
    if code2 == 200:
        msgs = ((d2.get('data') or {}).get('messages')) or []
        travel = [m for m in msgs if (m.get('biz_type') or '') == 'growth.travel_arrival']
        unclaimed = [m for m in travel
                     if ((m.get('ext') or {}).get('claim_status')) == 'unclaimed']
        out('[msg] HTTP %s, 总 %d 条, travel_arrival %d 条 (unclaimed %d)' % (
            code2, len(msgs), len(travel), len(unclaimed)))
        for m in travel[-5:]:
            ext = m.get('ext') or {}
            out('  - claim_status=%s reward=%s' % (
                ext.get('claim_status'), ext.get('reward_credit')))
    else:
        out('[msg] HTTP %s: %r' % (code2, d2.get('_err')))
        net_fail = True

    # 3) buddy 状态
    code3, d3 = call(acc, '/activity/growth/buddy/info')
    if code3 == 200:
        out('[buddy] HTTP %s buddy=%s' % (
            code3, json.dumps((d3.get('data') or {}).get('buddy'), ensure_ascii=False)))
    else:
        out('[buddy] HTTP %s' % code3)
        net_fail = True

    # 4) 判定
    act = [p for p in today_packs if 'code_007' in p[1].lower() or 'bonus' in p[0].lower()]
    other = [p for p in today_packs if p not in act]
    if net_fail:
        out('>>> 判定: ? 探测不可信 (接口网络异常) — 需复核, 不作为实验结论!')
        out('>>> VERIFY_NEEDED')
    elif act:
        out('>>> 判定: ★★★ 实验成功 — 今日新到活动包 %s; 纯 API 心跳即被判为活跃, 无客户端方案成立!' % (
            '; '.join('%s %s分 %s' % (p[0], p[2], p[3]) for p in act)))
    elif other:
        out('>>> 判定: ★ 今日有其他新包 %s (非活动包), 需人工核对是否与心跳相关' % (
            '; '.join('%s %s分 %s' % (p[0], p[2], p[3]) for p in other)))
    elif unclaimed:
        out('>>> 判定: ★ buddy 到站未领取 (%d 条 unclaimed), daemon 应会自动 claim; 入账需再等批量结算' % len(unclaimed))
    elif travel:
        out('>>> 判定: △ 已领取但今日未入账 — 批量结算可能有延迟, 明早再跑一次本脚本复核')
    else:
        out('>>> 判定: ✗ 实验失败 — 无新资源包、无到站消息; 纯 API 心跳不足以被服务端判定为活跃')

    text = '\n'.join(lines) + '\n'
    print(text, flush=True)
    with open(os.path.join(BASE, 'logs', 'settlement_verdict.log'), 'a', encoding='utf-8') as f:
        f.write(text + '\n')


if __name__ == '__main__':
    main()
