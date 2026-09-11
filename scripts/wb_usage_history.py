#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 逐笔积分消耗流水查询
==============================
数据源 (官网个人中心 www.codebuddy.cn/profile "套餐与用量" 同款接口, 前端逆向):
  POST https://copilot.tencent.com/billing/meter/get-user-request-usage   逐笔请求流水
  POST https://copilot.tencent.com/billing/meter/get-user-daily-usage     按天汇总
  鉴权: Authorization: Bearer <config 里的 accessToken> + X-User-Id
  关键: 必须带 X-Enterprise-Id 头 (个人账号传自己的 userId 即可, 否则 400 invalid params)

逐笔返回: {total, data: [{requestId, credit, model, client, requestTime,
                          inputTrunc/input(输入预览), agentPurpose}]}
分页两种:
  v1 页码式: {startTime, endTime, pageNum, pageSize}              → total + data
  v2 游标式: {startTime, endTime, timezone, pageSize, version:2, pageToken}
             → data + nextPageToken (官方新 UI 用法, input 预览为空)
按天返回: {total, data: [{date: "YYYY-MM-DD", credit: x.xx}]}

用法:
  python scripts/wb_usage_history.py                    # 最近 7 天逐笔, 所有账号
  python scripts/wb_usage_history.py --days 30          # 最近 30 天
  python scripts/wb_usage_history.py --daily            # 按天汇总视图
  python scripts/wb_usage_history.py --uid a95fdb       # 只查指定账号 (userId 前缀)
  python scripts/wb_usage_history.py --pages 3          # 只拉前 3 页 (每页 100 条)
  python scripts/wb_usage_history.py --json             # 原始 JSON
  python scripts/wb_usage_history.py --csv out.csv      # 导出 CSV
"""
import argparse
import csv
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
OPENAI_CFG = os.path.join(BASE, '..', 'config.json')

HOST = 'https://copilot.tencent.com'
REQ_URL = '/billing/meter/get-user-request-usage'
DAILY_URL = '/billing/meter/get-user-daily-usage'
PAGE_SIZE = 100
MAX_PAGES = 100          # 安全上限
UA_WB = 'WorkBuddy/5.3.12 WorkBuddy/5.3.12 CLI/2.115.0'

# ---- WorkBuddy 系平台规格 (国内 / 国际) ----
# 接口路径**完全一致**, 只差 host 与 X-Product-Code ——
# 国际版必须带 `X-Product-Code: workbuddy-ai` (实测确认)。
WB_PLATFORMS = {
    'workbuddy': {'provider': 'workbuddy', 'host': 'https://copilot.tencent.com',
                  'domain': 'www.workbuddy.cn', 'product': 'SaaS',
                  'label': 'WorkBuddy'},
    'workbuddy_intl': {'provider': 'workbuddy_intl', 'host': 'https://www.workbuddy.ai',
                       'domain': 'www.workbuddy.ai', 'product': 'workbuddy-ai',
                       'label': 'WorkBuddy 国际'},
}
# 当前生效平台 (由 --platform 设置); 默认国内版, 保持旧行为不变
PLATFORM = 'workbuddy'


def cur_spec():
    return WB_PLATFORMS[PLATFORM]


def cur_host():
    return cur_spec()['host']


def ts():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def log(msg):
    print(f'[{ts()}] {msg}')


def load_wb_cfg():
    """按当前平台读取账号与 (domain, product)。"""
    spec = cur_spec()
    with open(OPENAI_CFG, encoding='utf-8') as f:
        cfg = json.load(f)
    wb = (cfg.get('providers') or {}).get(spec['provider']) or {}
    return (wb.get('accounts') or [],
            wb.get('domain', spec['domain']),
            wb.get('product', spec['product']))


def post_json(url, headers, body, timeout=25):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method='POST')
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')
    except Exception as e:
        return 0, str(e)


def make_headers(acc, domain, product):
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'Authorization': f"Bearer {acc.get('accessToken', '')}",
        'X-User-Id': acc.get('userId', ''),
        'X-Domain': domain,
        'X-Product': product,
        'X-Enterprise-Id': acc.get('userId', ''),   # 个人账号也必须带, 传自己的 userId
        'User-Agent': UA_WB,
        'Accept-Language': 'zh',
    }
    # 国际版额外要求产品编码
    if '.ai' in cur_host():
        headers['X-Product-Code'] = product
    return headers


def fetch_page(headers, start, end, page_num, page_size=PAGE_SIZE):
    """v1 页码式拉一页逐笔流水. 返回 (total, items, err)。"""
    body = {'startTime': f'{start} 00:00:00', 'endTime': f'{end} 23:59:59',
            'pageNum': page_num, 'pageSize': page_size}
    code, raw = post_json(cur_host() + REQ_URL, headers, body)
    if code == 401 or code == 403:
        return None, None, f'token 无效或无权限 ({code}), 请重新登录'
    if code != 200:
        return None, None, f'HTTP {code}: {raw[:120]}'
    try:
        d = json.loads(raw)
    except Exception:
        return None, None, '响应解析失败'
    if d.get('code') != 0:
        return None, None, f"code={d.get('code')} msg={d.get('msg', '')[:100]}"
    data = d.get('data') or {}
    return (data.get('total') or 0), (data.get('data') or []), None


def fetch_all(headers, start, end, max_pages=MAX_PAGES):
    """v2 游标式翻页拉全部逐笔流水. 返回 (items, err)。"""
    items_all, token, err = [], '', None
    for page in range(1, max_pages + 1):
        body = {'startTime': f'{start} 00:00:00', 'endTime': f'{end} 23:59:59',
                'timezone': 'Asia/Shanghai', 'pageSize': PAGE_SIZE,
                'version': 2, 'pageToken': token}
        code, raw = post_json(cur_host() + REQ_URL, headers, body)
        if code != 200:
            err = f'HTTP {code}: {raw[:120]}'
            break
        try:
            d = json.loads(raw)
        except Exception:
            err = '响应解析失败'
            break
        if d.get('code') != 0:
            err = f"code={d.get('code')} msg={d.get('msg', '')[:100]}"
            break
        data = d.get('data') or {}
        chunk = data.get('data') or []
        items_all.extend(chunk)
        token = data.get('nextPageToken') or ''
        if not token or not chunk:
            break
    return items_all, err


def fetch_daily(headers, start, end):
    """按天汇总. 返回 ({date: credit}, err)。"""
    body = {'startTime': f'{start} 00:00:00', 'endTime': f'{end} 23:59:59',
            'pageNum': 1, 'pageSize': 366}
    code, raw = post_json(cur_host() + DAILY_URL, headers, body)
    if code != 200:
        return None, f'HTTP {code}: {raw[:120]}'
    try:
        d = json.loads(raw)
    except Exception:
        return None, '响应解析失败'
    if d.get('code') != 0:
        return None, f"code={d.get('code')} msg={d.get('msg', '')[:100]}"
    out = {}
    for it in (d.get('data') or {}).get('data') or []:
        out[it.get('date', '?')] = float(it.get('credit') or 0)
    return out, None


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def entry_row(it):
    """一条流水 -> 展示行 dict。"""
    preview = (it.get('inputTrunc') or it.get('input') or '').replace('\n', ' ').strip()
    return {
        'time': (it.get('requestTime') or '-').replace(' ', ' '),
        'model': it.get('model') or '-',
        'client': it.get('client') or '-',
        'credit': _f(it.get('credit')),
        'purpose': it.get('agentPurpose') or '-',
        'preview': preview[:34],
    }


def print_table(rows):
    if not rows:
        log('  (该时间段无消耗记录)')
        return
    hdr = f"{'时间':<20} {'模型':<16} {'客户端':<12} {'积分':>8} {'类型':<14} {'输入预览'}"
    log(hdr)
    log('  ' + '-' * len(hdr))
    for r in rows:
        log(f"{r['time']:<20} {r['model']:<16} {r['client']:<12} {r['credit']:>8.3f} "
            f"{r['purpose']:<14} {r['preview']}")


def summarize(rows):
    if not rows:
        return
    total_c = sum(r['credit'] for r in rows)
    by = {}
    for r in rows:
        m = by.setdefault(r['model'], {'c': 0.0, 'n': 0})
        m['c'] += r['credit']
        m['n'] += 1
    log('-' * 62)
    log(f'  合计 {len(rows)} 笔 | 消耗积分 {total_c:.3f}')
    for m, v in sorted(by.items(), key=lambda x: -x[1]['c']):
        log(f"    {m:<22} {v['c']:>9.3f} 积分  ({v['n']} 笔)")


def query_account(acc, headers, days, max_pages, daily_only):
    """查单账号. 返回 (rows_or_daily, total, err)。"""
    end = time.strftime('%Y-%m-%d')
    start = time.strftime('%Y-%m-%d', time.localtime(time.time() - days * 86400))
    if daily_only:
        d, err = fetch_daily(headers, start, end)
        return (d or {}), (len(d) if d else 0), err
    # 先拉一页拿 total
    total, _items, err = fetch_page(headers, start, end, 1, PAGE_SIZE)
    if err and total is None:
        return None, None, err
    items, err2 = fetch_all(headers, start, end, max_pages)
    if err2 and not items:
        return None, None, err2
    rows = [entry_row(x) for x in items]
    return rows, (total if total is not None else len(rows)), (err2 or err)


def csv_export(rows, path):
    fields = ['time', 'model', 'client', 'credit', 'purpose', 'preview']
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    log(f'CSV 已导出: {path} ({len(rows)} 行)')


def main():
    global PLATFORM
    ap = argparse.ArgumentParser(description='WorkBuddy 逐笔积分消耗流水查询 (国内 / 国际)')
    ap.add_argument('--platform', choices=list(WB_PLATFORMS), default='workbuddy',
                    help='平台: workbuddy(国内, 默认) / workbuddy_intl(国际)')
    ap.add_argument('--days', type=int, default=7, help='查询最近 N 天 (默认 7)')
    ap.add_argument('--uid', help='只查指定 userId 前缀的账号')
    ap.add_argument('--daily', action='store_true', help='按天汇总视图')
    ap.add_argument('--pages', type=int, default=MAX_PAGES, help='每账号最多拉取页数 (每页 100 条)')
    ap.add_argument('--json', action='store_true', help='输出原始 JSON')
    ap.add_argument('--csv', metavar='PATH', help='导出 CSV 到指定路径')
    args = ap.parse_args()
    PLATFORM = args.platform

    try:
        accounts, domain, product = load_wb_cfg()
    except Exception as e:
        log(f'config 读取失败: {e}')
        return 1
    if args.uid:
        accounts = [a for a in accounts if str(a.get('userId', '')).startswith(args.uid)]
        if not accounts:
            log(f'未找到 userId 前缀为 {args.uid} 的账号')
            return 1
    if not accounts:
        log(f"无 {cur_spec()['label']} 账号, 请先用 账号管理.bat 添加")
        return 1

    log(f"=== {cur_spec()['label']} 逐笔消耗流水 "
        f"(最近 {args.days} 天{' | 按天汇总' if args.daily else ''}) ===")
    exit_code = 0
    json_dump = {}
    for i, acc in enumerate(accounts, 1):
        name = acc.get('userId', f'账号{i}')
        log(f'--- 账号{i} (userId={name})')
        headers = make_headers(acc, domain, product)
        if not acc.get('accessToken'):
            log('  账号缺 accessToken, 跳过')
            exit_code = 1
            continue
        rows, total, err = query_account(acc, headers, args.days, args.pages, args.daily)
        if rows is None:
            log(f'  查询失败: {err}')
            exit_code = 1
            continue
        if err:
            log(f'  [警告] {err} (以下为已拉到的部分数据)')
        if args.daily:
            if not rows:
                log('  (该时间段无消耗记录)')
            else:
                log(f"  {'日期':<12} {'消耗积分':>10}")
                log('  ' + '-' * 26)
                for date in sorted(rows, reverse=True):
                    log(f"  {date:<12} {rows[date]:>10.3f}")
                log(f"  合计 {len(rows)} 天 | {sum(rows.values()):.3f} 积分")
            if args.json:
                json_dump[name] = rows
        else:
            if args.json:
                json_dump[name] = {'total': total, 'items': rows}
            show = rows if len(rows) <= 300 else rows[:300]
            hint = None if len(rows) <= 300 else f'仅显示前 300 笔, 共 {len(rows)} 笔, 完整数据请用 --csv/--json'
            print_table(show)
            if hint:
                log(f'  ({hint})')
            summarize(rows)
            if args.csv:
                csv_export(rows, args.csv if len(accounts) == 1
                           else args.csv.rsplit('.', 1)[0] + f'_{name}.' + (args.csv.rsplit('.', 1)[1] if '.' in args.csv else 'csv'))

    if args.json:
        print(json.dumps(json_dump, ensure_ascii=False, indent=1))
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
