#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
积分流水只读查询 API —— 给前端的接口层
======================================
前端 (GUI / Web / 任何消费方) **只 import 本模块, 不要自己写 SQL 碰
data/usage_history.db**。表结构与平台枚举都在这里收口, 以后新增平台
(例如 WorkBuddy 系再加区域) 前端零改动。

设计约定 (改前端时按这个契约来)
--------------------------------
* 所有函数返回 **纯 dict / list / 基本类型**, 可直接 `json.dumps` 给前端。
* 平台用稳定 key, 不要硬编码中文:
      'trae' / 'workbuddy' / 'workbuddy_intl'
  中文展示名一律走 `platforms()` 拿 (带 label)。
* 日期: 入参用 `'YYYY-MM-DD'` 本地日期 (**闭区间**), 也接受 unix 秒;
  出参同时给 `day`(`'YYYY-MM-DD'`) 与 `ts`(unix 秒), 前端按需取。
* 时区: 统一用本机本地时区 (与采集器一致)。
* 本模块**只读**, 写入由 `scripts/usage_collector.py` 每 5 分钟完成。
* 库不存在 / 表为空时返回空结果而不是抛异常, 前端不用做存在性判断。

快速上手
--------
    import credits_api as api

    api.platforms()                                  # 平台枚举 + 中文名
    api.overview(days=7)                             # 仪表盘一次拿全 ★推荐
    api.usage_rows('2026-09-01', '2026-09-11')        # 逐笔流水
    api.usage_daily('2026-09-01', '2026-09-11')       # 按天×平台 (画柱状图)
    api.gains_summary('2026-09-01', '2026-09-11')     # 获取积分合计
    api.accounts()                                    # 平台 → 账号列表(带名称)

命令行自检 (同时是 JSON 契约的示例输出)
---------------------------------------
    python scripts/credits_api.py --days 7 --json          # 完整仪表盘
    python scripts/credits_api.py --days 7                 # 人读摘要
    python scripts/credits_api.py --days 7 --json --platform workbuddy_intl
"""
import argparse
import json
import os
import sqlite3
import sys
import time

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
OPENAI_CFG = os.path.join(BASE, '..', 'config.json')
DB_PATH = os.path.join(BASE, '..', 'data', 'usage_history.db')

# 平台枚举: 前端用它渲染下拉/图例/表头, 不要自己写死中文。
#   key      —— 稳定标识, 与 usage.platform / gain.platform 一致
#   label    —— 中文展示名
#   short    —— 短标签 (窄列/图表用)
#   provider —— config.json providers 下的配置段名
#   color    —— 建议配色 (图表图例), 前端可覆盖
PLATFORMS = (
    {'key': 'trae', 'label': 'TRAE', 'short': 'TRAE',
     'provider': 'trae', 'color': '#e05555'},
    {'key': 'workbuddy', 'label': 'WorkBuddy', 'short': 'WB',
     'provider': 'workbuddy', 'color': '#4a7fe0'},
    {'key': 'workbuddy_intl', 'label': 'WorkBuddy 国际', 'short': 'WB国际',
     'provider': 'workbuddy_intl', 'color': '#2fa87a'},
)
PLATFORM_KEYS = tuple(p['key'] for p in PLATFORMS)
_BY_KEY = {p['key']: p for p in PLATFORMS}


# ==================== 基础工具 ====================

def platform_label(key):
    """平台 key → 中文展示名 (未知 key 原样返回)。"""
    return (_BY_KEY.get(key) or {}).get('label') or key


def parse_day(value):
    """'YYYY-MM-DD' 或 unix 秒 → unix 秒 (当天 00:00:00, 本地时区)。"""
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    return int(time.mktime(time.strptime(s[:10], '%Y-%m-%d')))


def day_str(ts):
    """unix 秒 → 'YYYY-MM-DD' (本地时区)。"""
    return time.strftime('%Y-%m-%d', time.localtime(int(ts)))


def _range(start, end):
    """(start, end) 本地日期/秒 → 闭区间 (t0, t1) unix 秒。"""
    t0 = parse_day(start)
    t1 = parse_day(end) + 86399
    return (t0, t1) if t0 <= t1 else (t1 - 86399, t0 + 86399)


def _default_range(days=7):
    """最近 N 天 (含今天) 的 (start_day, end_day)。"""
    end = time.time()
    return day_str(end - (days - 1) * 86400), day_str(end)


def _connect():
    """打开只读连接; 库不存在返回 None (调用方按空结果处理)。"""
    if not os.path.isfile(DB_PATH):
        return None
    try:
        conn = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def _rows(conn, sql, args=()):
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except sqlite3.Error:
        return []


# ==================== 元信息 ====================

def platforms():
    """平台枚举 (前端渲染下拉/图例用)。返回 [{key,label,short,provider,color}]。"""
    return [dict(p) for p in PLATFORMS]


def has_data():
    """流水库是否存在且有记录。"""
    conn = _connect()
    if conn is None:
        return False
    try:
        return bool(_rows(conn, 'SELECT 1 FROM usage LIMIT 1'))
    finally:
        conn.close()


def available_range():
    """库里实际有数据的日期范围; 无数据返回 None。"""
    conn = _connect()
    if conn is None:
        return None
    try:
        r = _rows(conn, 'SELECT MIN(ts) a, MAX(ts) b, COUNT(*) n FROM usage')
        if not r or not r[0]['n']:
            return None
        return {'first_day': day_str(r[0]['a']), 'last_day': day_str(r[0]['b']),
                'first_ts': r[0]['a'], 'last_ts': r[0]['b'], 'count': r[0]['n']}
    finally:
        conn.close()


def accounts():
    """各平台账号列表 (uid → 显示名)。

    返回 {'trae': [{'uid','name','enabled'}], 'workbuddy': [...], 'workbuddy_intl': [...]}
    前端用它把流水行里的 uid 换成可读账号名。
    """
    try:
        with open(OPENAI_CFG, encoding='utf-8') as f:
            cfg = json.load(f)
    except Exception:
        return {k: [] for k in PLATFORM_KEYS}
    prov = cfg.get('providers') or {}
    out = {}
    for p in PLATFORMS:
        seg = prov.get(p['provider']) or {}
        items = []
        for a in (seg.get('accounts') or []):
            uid = str(a.get('uid') or a.get('userId') or '')
            items.append({'uid': uid,
                          'name': a.get('name') or uid,
                          'enabled': a.get('enabled', True) is not False})
        out[p['key']] = items
    return out


def account_name_map():
    """{platform: {uid: 显示名}} —— 前端拼表格时一次性取回。"""
    return {k: {a['uid']: a['name'] for a in v} for k, v in accounts().items()}


def _label_for(platform, uid, name_map):
    """账号展示名: config 里的 name → 否则 '平台短名_uid后4位'。"""
    nm = (name_map.get(platform) or {}).get(str(uid))
    if nm:
        return nm
    short = (_BY_KEY.get(platform) or {}).get('short') or platform
    return f'{short}_{str(uid)[-4:]}' if uid else short


# ==================== 消耗流水 (usage) ====================

def usage_rows(start=None, end=None, platform=None, uid=None, limit=2000):
    """逐笔消耗流水 (新 → 旧)。

    返回 [{day, ts, time_str, platform, platform_label, uid, account,
           model, credits, cost_money, source, preview}]
    """
    if start is None or end is None:
        start, end = _default_range(7)
    t0, t1 = _range(start, end)
    conn = _connect()
    if conn is None:
        return []
    try:
        sql = ('SELECT platform, uid, model, ts, credits, cost_money, source, preview '
               'FROM usage WHERE ts >= ? AND ts <= ?')
        args = [t0, t1]
        if platform:
            sql += ' AND platform = ?'
            args.append(platform)
        if uid:
            sql += ' AND uid LIKE ?'
            args.append(str(uid) + '%')
        sql += ' ORDER BY ts DESC LIMIT ?'
        args.append(int(limit))
        rows = _rows(conn, sql, args)
    finally:
        conn.close()
    nm = account_name_map()
    out = []
    for r in rows:
        out.append({
            'day': day_str(r['ts']),
            'ts': r['ts'],
            'time_str': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r['ts'])),
            'platform': r['platform'],
            'platform_label': platform_label(r['platform']),
            'uid': r['uid'],
            'account': _label_for(r['platform'], r['uid'], nm),
            'model': r['model'] or '',
            'credits': float(r['credits'] or 0),
            'cost_money': float(r['cost_money'] or 0),
            'source': r['source'] or '',
            'preview': r['preview'] or '',
        })
    return out


def usage_daily(start=None, end=None, platform=None):
    """按「天 × 平台」聚合的消耗 (画柱状图用)。

    返回 [{day, platform, platform_label, credits, count}], 已按天升序、
    天内按 PLATFORMS 顺序排列。**空天数不会出现**, 前端自行补齐 X 轴。
    """
    if start is None or end is None:
        start, end = _default_range(7)
    t0, t1 = _range(start, end)
    conn = _connect()
    if conn is None:
        return []
    try:
        sql = ("SELECT date(ts,'unixepoch','localtime') d, platform, "
               "COALESCE(SUM(credits),0) c, COUNT(*) n FROM usage "
               "WHERE ts >= ? AND ts <= ?")
        args = [t0, t1]
        if platform:
            sql += ' AND platform = ?'
            args.append(platform)
        sql += ' GROUP BY d, platform ORDER BY d ASC'
        rows = _rows(conn, sql, args)
    finally:
        conn.close()
    order = {k: i for i, k in enumerate(PLATFORM_KEYS)}
    rows.sort(key=lambda r: (r['d'], order.get(r['platform'], 99)))
    return [{'day': r['d'], 'platform': r['platform'],
             'platform_label': platform_label(r['platform']),
             'credits': float(r['c'] or 0), 'count': int(r['n'] or 0)} for r in rows]


def usage_summary(start=None, end=None, platform=None):
    """消耗合计 (总量 + 分平台 + 分模型)。

    platform 给定时只统计该平台 (总量/分平台/分模型全部收窄到它)。
    返回 {total, total_count, by_platform: [...], by_model: [...], start_day, end_day}
    """
    if start is None or end is None:
        start, end = _default_range(7)
    start_day, end_day = day_str(parse_day(start)), day_str(parse_day(end))
    t0, t1 = _range(start, end)
    conn = _connect()
    if conn is None:
        return {'total': 0.0, 'total_count': 0, 'by_platform': [], 'by_model': [],
                'start_day': start_day, 'end_day': end_day}
    where = 'WHERE ts >= ? AND ts <= ?'
    args = [t0, t1]
    if platform:
        where += ' AND platform = ?'
        args.append(platform)
    try:
        by_p = _rows(conn, f'SELECT platform, COALESCE(SUM(credits),0) c, COUNT(*) n '
                           f'FROM usage {where} GROUP BY platform', args)
        by_m = _rows(conn, f'SELECT platform, model, COALESCE(SUM(credits),0) c, COUNT(*) n '
                           f'FROM usage {where} GROUP BY platform, model ORDER BY c DESC', args)
    finally:
        conn.close()
    order = {k: i for i, k in enumerate(PLATFORM_KEYS)}
    by_p.sort(key=lambda r: order.get(r['platform'], 99))
    return {
        'start_day': start_day, 'end_day': end_day,
        'total': round(sum(float(r['c'] or 0) for r in by_p), 4),
        'total_count': sum(int(r['n'] or 0) for r in by_p),
        'by_platform': [{'platform': r['platform'],
                         'platform_label': platform_label(r['platform']),
                         'credits': round(float(r['c'] or 0), 4),
                         'count': int(r['n'] or 0)} for r in by_p],
        'by_model': [{'platform': r['platform'],
                      'platform_label': platform_label(r['platform']),
                      'model': r['model'] or '',
                      'credits': round(float(r['c'] or 0), 4),
                      'count': int(r['n'] or 0)} for r in by_m],
    }


# ==================== 获取积分 (gain) ====================

def gains_daily(start=None, end=None, platform=None):
    """按「天 × 平台」聚合的获取积分 (签到/资源包入账)。

    返回 [{day, platform, platform_label, amount, kinds:[...]}]
    """
    if start is None or end is None:
        start, end = _default_range(7)
    s, e = day_str(parse_day(start)), day_str(parse_day(end))
    conn = _connect()
    if conn is None:
        return []
    try:
        sql = ('SELECT day, platform, COALESCE(SUM(amount),0) a, COUNT(*) n '
               'FROM gain WHERE day >= ? AND day <= ?')
        args = [s, e]
        if platform:
            sql += ' AND platform = ?'
            args.append(platform)
        sql += ' GROUP BY day, platform ORDER BY day ASC'
        rows = _rows(conn, sql, args)
        kinds = _rows(conn, 'SELECT day, platform, kind FROM gain '
                            'WHERE day >= ? AND day <= ? ORDER BY day ASC', (s, e))
    finally:
        conn.close()
    kmap = {}
    for k in kinds:
        kmap.setdefault((k['day'], k['platform']), [])
        if k['kind'] and k['kind'] not in kmap[(k['day'], k['platform'])]:
            kmap[(k['day'], k['platform'])].append(k['kind'])
    order = {k: i for i, k in enumerate(PLATFORM_KEYS)}
    rows.sort(key=lambda r: (r['day'], order.get(r['platform'], 99)))
    return [{'day': r['day'], 'platform': r['platform'],
             'platform_label': platform_label(r['platform']),
             'amount': round(float(r['a'] or 0), 4),
             'kinds': kmap.get((r['day'], r['platform']), [])} for r in rows]


def gains_summary(start=None, end=None, platform=None):
    """获取积分合计 (总量 + 分平台); platform 给定时只统计该平台。"""
    if start is None or end is None:
        start, end = _default_range(7)
    s, e = day_str(parse_day(start)), day_str(parse_day(end))
    conn = _connect()
    if conn is None:
        return {'start_day': s, 'end_day': e, 'total': 0.0, 'by_platform': []}
    sql = ('SELECT platform, COALESCE(SUM(amount),0) a, COUNT(*) n '
           'FROM gain WHERE day >= ? AND day <= ?')
    args = [s, e]
    if platform:
        sql += ' AND platform = ?'
        args.append(platform)
    sql += ' GROUP BY platform'
    try:
        rows = _rows(conn, sql, args)
    finally:
        conn.close()
    order = {k: i for i, k in enumerate(PLATFORM_KEYS)}
    rows.sort(key=lambda r: order.get(r['platform'], 99))
    return {
        'start_day': s, 'end_day': e,
        'total': round(sum(float(r['a'] or 0) for r in rows), 4),
        'by_platform': [{'platform': r['platform'],
                         'platform_label': platform_label(r['platform']),
                         'amount': round(float(r['a'] or 0), 4),
                         'count': int(r['n'] or 0)} for r in rows],
    }


# ==================== 仪表盘聚合 (前端首选) ====================

def overview(days=7, start=None, end=None, platform=None):
    """一次拿全仪表盘需要的数据 —— 前端**推荐只用这个函数**。

    返回:
      {
        'range':   {start_day, end_day, days},
        'platforms': [...],            # 始终返回全部平台 (图例/下拉要稳定)
        'accounts':  {platform: [...]},
        'usage':   {'start_day','end_day','total','total_count','by_platform','by_model'},
        'gains':   {'start_day','end_day','total','by_platform'},
        'usage_daily': [...],          # 柱状图: 天×平台
        'gains_daily': [...],
        'net':     {'gained': x, 'spent': y, 'delta': x-y},   # 获取 - 消耗
        'has_data': bool,
        'available': {...} | None,     # 库里实际有数据的范围
        'generated_at': unix 秒,
      }

    传 platform 时: usage/gains/net/daily **全部收窄到该平台**;
    platforms/accounts 仍返回全集 (下拉与图例要稳定, 前端自己挑)。
    """
    if start is None or end is None:
        start, end = _default_range(days)
    start_day, end_day = day_str(parse_day(start)), day_str(parse_day(end))
    us = usage_summary(start_day, end_day, platform)
    gs = gains_summary(start_day, end_day, platform)
    return {
        'range': {'start_day': start_day, 'end_day': end_day, 'days': days,
                  'platform': platform},
        'platforms': platforms(),
        'accounts': accounts(),
        'usage': us,
        'gains': gs,
        'usage_daily': usage_daily(start_day, end_day, platform),
        'gains_daily': gains_daily(start_day, end_day, platform),
        'net': {'gained': gs['total'], 'spent': us['total'],
                'delta': round(gs['total'] - us['total'], 4)},
        'has_data': has_data(),
        'available': available_range(),
        'generated_at': int(time.time()),
    }


def account_breakdown(start=None, end=None, platform=None):
    """按账号汇总 (消耗 + 获取), 前端「账号维度」视图用。

    返回 [{platform, platform_label, uid, account, spent, gained, net, count}]
    """
    if start is None or end is None:
        start, end = _default_range(7)
    t0, t1 = _range(start, end)
    s, e = day_str(parse_day(start)), day_str(parse_day(end))
    conn = _connect()
    if conn is None:
        return []
    uwhere, uargs = 'ts >= ? AND ts <= ?', [t0, t1]
    gwhere, gargs = 'day >= ? AND day <= ?', [s, e]
    if platform:
        uwhere += ' AND platform = ?'
        uargs.append(platform)
        gwhere += ' AND platform = ?'
        gargs.append(platform)
    try:
        spent = _rows(conn, f'SELECT platform, uid, COALESCE(SUM(credits),0) c, COUNT(*) n '
                            f'FROM usage WHERE {uwhere} GROUP BY platform, uid', uargs)
        gained = _rows(conn, f'SELECT platform, uid, COALESCE(SUM(amount),0) a FROM gain '
                             f'WHERE {gwhere} GROUP BY platform, uid', gargs)
    finally:
        conn.close()
    nm = account_name_map()
    gmap = {(r['platform'], r['uid']): float(r['a'] or 0) for r in gained}
    order = {k: i for i, k in enumerate(PLATFORM_KEYS)}
    out = []
    for r in spent:
        g = gmap.pop((r['platform'], r['uid']), 0.0)
        out.append({'platform': r['platform'],
                    'platform_label': platform_label(r['platform']),
                    'uid': r['uid'],
                    'account': _label_for(r['platform'], r['uid'], nm),
                    'spent': round(float(r['c'] or 0), 4),
                    'gained': round(g, 4),
                    'net': round(g - float(r['c'] or 0), 4),
                    'count': int(r['n'] or 0)})
    # 只有获取、没有消耗的账号也要出现
    for (plat, uid), g in gmap.items():
        out.append({'platform': plat, 'platform_label': platform_label(plat),
                    'uid': uid, 'account': _label_for(plat, uid, nm),
                    'spent': 0.0, 'gained': round(g, 4), 'net': round(g, 4), 'count': 0})
    out.sort(key=lambda r: (order.get(r['platform'], 99), -r['spent']))
    return out


# ==================== 命令行 (JSON 契约示例) ====================

def _render_text(ov):
    r = ov['range']
    print(f"=== 积分流水概览 {r['start_day']} ~ {r['end_day']} ({r['days']} 天) ===")
    if not ov['has_data']:
        print('  (流水库为空, 等 daemon 采集或运行 usage_collector.py --collect)')
        return
    avail = ov.get('available') or {}
    print(f"  库内数据范围: {avail.get('first_day')} ~ {avail.get('last_day')}"
          f" ({avail.get('count')} 条)")
    print()
    print(f"  获取积分合计: {ov['gains']['total']:.2f}")
    for g in ov['gains']['by_platform']:
        print(f"      {g['platform_label']:<16} {g['amount']:>10.2f}")
    print(f"  消耗积分合计: {ov['usage']['total']:.2f}  ({ov['usage']['total_count']} 笔)")
    for u in ov['usage']['by_platform']:
        print(f"      {u['platform_label']:<16} {u['credits']:>10.2f}  ({u['count']} 笔)")
    n = ov['net']
    print(f"  净增: {n['delta']:+.2f}")
    print()
    print('  按天消耗:')
    days = sorted({d['day'] for d in ov['usage_daily']})
    for d in days:
        parts = [f"{x['platform_label']}={x['credits']:.2f}"
                 for x in ov['usage_daily'] if x['day'] == d]
        print(f"      {d}  " + '  '.join(parts))


def main():
    ap = argparse.ArgumentParser(description='积分流水只读查询 API (给前端的接口层)')
    ap.add_argument('--days', type=int, default=7, help='最近 N 天 (默认 7)')
    ap.add_argument('--start', help="起始日期 YYYY-MM-DD (给了就忽略 --days)")
    ap.add_argument('--end', help='结束日期 YYYY-MM-DD (闭区间)')
    ap.add_argument('--platform', choices=list(PLATFORM_KEYS), help='只看某平台')
    ap.add_argument('--by-account', action='store_true', help='输出按账号汇总')
    ap.add_argument('--json', action='store_true', help='输出 JSON (前端契约示例)')
    args = ap.parse_args()

    if args.by_account:
        data = account_breakdown(args.start, args.end) if args.start and args.end \
            else account_breakdown(*_default_range(args.days))
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(f"{'平台':<16}{'账号':<24}{'消耗':>10}{'获取':>10}{'净':>10}")
            for r in data:
                print(f"{r['platform_label']:<16}{r['account'][:22]:<24}"
                      f"{r['spent']:>10.2f}{r['gained']:>10.2f}{r['net']:>10.2f}")
        return 0

    ov = overview(days=args.days, start=args.start, end=args.end, platform=args.platform)
    if args.json:
        print(json.dumps(ov, ensure_ascii=False, indent=2))
    else:
        _render_text(ov)
    return 0


if __name__ == '__main__':
    sys.exit(main())
