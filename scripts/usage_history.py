#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TRAE 逐笔积分消耗流水查询
=========================
数据源 (trae.cn 官网 dashboard 同款接口, 前端 JS 逆向):
  POST https://api.trae.cn/trae/api/v1/pay/query_user_usage_group_by_session
  鉴权: Authorization: Cloud-IDE-JWT <config 里的 token> + x-device-id
  请求: {start_time, end_time, page_num, page_size(<=50), usage_type:[7]}
  返回: {total, user_usage_group_by_sessions: [{
          usage_time, session_id, model_name, mode,
          credits_float, amount_float, cost_money_float, dollar_float,
          usage_source, product_type_list, use_max_mode,
          extra_info: {input_token, output_token, cache_read_token, cache_write_token},
          usage_group_details: [...], user_input_preview }]}

注意:
  - usage_type 固定传 [7] (credits 计费类型, 数组形式; 传 0/1/2 或缺省均查不到数据或 400)
  - page_size 上限 50, 超过返回 HTTP 400
  - token 过期会 401, 请先在 open-ai 桌面端「账号管理」页重新连接, 或跑 signin_all.py 续期

用法:
  python scripts/usage_history.py                    # 最近 7 天, 所有账号
  python scripts/usage_history.py --days 30          # 最近 30 天
  python scripts/usage_history.py --uid 319013...    # 只查指定账号
  python scripts/usage_history.py --pages 3          # 只拉前 3 页 (每页 50 条)
  python scripts/usage_history.py --json             # 原始 JSON 输出
  python scripts/usage_history.py --csv out.csv      # 导出 CSV
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

USAGE_URL = 'https://api.trae.cn/trae/api/v1/pay/query_user_usage_group_by_session'
USAGE_TYPE_CREDITS = [7]
PAGE_SIZE = 50
MAX_PAGES = 200          # 安全上限 (50 * 200 = 1 万条)
UA_WEB = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
          '(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0')

SOURCE_LABEL = {1: 'IDE', 2: 'Web/Lite'}


def ts():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def log(msg):
    print(f'[{ts()}] {msg}')


def load_trae_cfg():
    with open(OPENAI_CFG, encoding='utf-8') as f:
        cfg = json.load(f)
    trae = (cfg.get('providers') or {}).get('trae') or {}
    device_id = trae.get('device_id') or (trae.get('headers') or {}).get('x-device-id', '')
    return trae.get('accounts') or [], device_id


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


def fetch_page(token, device_id, start, end, page_num, page_size=PAGE_SIZE):
    """拉一页流水. 返回 (total, items, err)。"""
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': UA_WEB,
        'Origin': 'https://www.trae.cn',
        'Referer': 'https://www.trae.cn/dashboard',
        'Authorization': f'Cloud-IDE-JWT {token}',
        'x-device-id': device_id,
    }
    body = {
        'start_time': int(start), 'end_time': int(end),
        'page_num': page_num, 'page_size': page_size,
        'usage_type': USAGE_TYPE_CREDITS,
    }
    code, raw = post_json(USAGE_URL, headers, body)
    if code == 401:
        return None, None, 'token 无效或已过期 (401), 请重新登录或续期'
    if code == 400:
        return None, None, f'参数被拒绝 (400): {raw[:120]}'
    if code != 200:
        return None, None, f'HTTP {code}: {raw[:120]}'
    try:
        d = json.loads(raw)
    except Exception:
        return None, None, '响应解析失败'
    if isinstance(d, dict) and d.get('code') not in (None, 0):
        return None, None, f"code={d.get('code')} msg={d.get('message', '')[:100]}"
    return (d.get('total') or 0), (d.get('user_usage_group_by_sessions') or []), None


def fetch_all(token, device_id, start, end, max_pages=MAX_PAGES):
    """翻页拉全部流水. 返回 (total, items, err)。"""
    total, items, err = fetch_page(token, device_id, start, end, 1)
    if err:
        return None, None, err
    if not items:
        return total, [], None
    all_items = list(items)
    pages = min((total + PAGE_SIZE - 1) // PAGE_SIZE or 1, max_pages)
    for p in range(2, pages + 1):
        _t, chunk, err = fetch_page(token, device_id, start, end, p)
        if err:
            return total, all_items, f'第 {p} 页拉取失败: {err}'
        if not chunk:
            break
        all_items.extend(chunk)
    return total, all_items, None


def fmt_time(sec):
    try:
        return time.strftime('%m-%d %H:%M:%S', time.localtime(int(sec)))
    except (TypeError, ValueError):
        return '-'


def source_label(v):
    return SOURCE_LABEL.get(v, str(v or '?'))


def entry_row(it):
    """一条流水 -> 展示行 dict。"""
    info = it.get('extra_info') or {}
    det = it.get('usage_group_details') or []
    credits = it.get('credits_float')
    if credits is None and det:
        credits = sum(_f(x.get('credits_float')) for x in det)
    return {
        'time': fmt_time(it.get('usage_time')),
        'model': it.get('model_name') or '-',
        'mode': (it.get('mode') or '').strip() or '-',
        'credits': credits or 0,
        'money': it.get('cost_money_float') or 0,
        'input': info.get('input_token') or 0,
        'output': info.get('output_token') or 0,
        'cache_read': info.get('cache_read_token') or 0,
        'source': source_label(it.get('usage_source')),
        'session': (it.get('session_id') or '')[:10],
        'preview': (it.get('user_input_preview') or '').replace('\n', ' ')[:28],
    }


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def print_table(rows, page_hint=None):
    if not rows:
        log('  (该时间段无消耗记录)')
        return
    hdr = f"{'时间':<14} {'模型':<16} {'倍率':<5} {'积分':>8} {'≈元':>8} {'输入tok':>9} {'输出tok':>8} {'缓存':>9} {'来源':<8} {'预览'}"
    log(hdr)
    log('  ' + '-' * len(hdr))
    for r in rows:
        log(f"{r['time']:<14} {r['model']:<16} {r['mode']:<5} {r['credits']:>8.3f} "
            f"{r['money']:>8.4f} {r['input']:>9} {r['output']:>8} {r['cache_read']:>9} "
            f"{r['source']:<8} {r['preview']}")
    if page_hint:
        log(f'  ({page_hint})')


def summarize(rows):
    if not rows:
        return
    total_c = sum(r['credits'] for r in rows)
    total_in = sum(r['input'] for r in rows)
    total_out = sum(r['output'] for r in rows)
    by_model = {}
    for r in rows:
        m = by_model.setdefault(r['model'], {'c': 0.0, 'n': 0})
        m['c'] += r['credits']
        m['n'] += 1
    log('-' * 62)
    log(f'  合计 {len(rows)} 笔 | 消耗积分 {total_c:.3f} | 输入 {total_in} tok | 输出 {total_out} tok')
    for m, v in sorted(by_model.items(), key=lambda x: -x[1]['c']):
        log(f"    {m:<20} {v['c']:>9.3f} 积分  ({v['n']} 笔)")


def query_account(acc, device_id, days, max_pages):
    """查单个账号. 返回 (rows, total, err)。"""
    token = acc.get('token', '')
    if not token:
        return None, None, '账号缺 token'
    end = int(time.time()) + 3600          # 容忍 1 小时时钟偏差
    start = end - 3600 - days * 86400
    total, items, err = fetch_all(token, device_id, start, end, max_pages)
    if err and not items:
        return None, None, err
    rows = [entry_row(x) for x in (items or [])]
    return rows, total, err


def csv_export(rows, path):
    fields = ['time', 'model', 'mode', 'credits', 'money', 'input', 'output',
              'cache_read', 'source', 'session', 'preview']
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    log(f'CSV 已导出: {path} ({len(rows)} 行)')


def main():
    ap = argparse.ArgumentParser(description='TRAE 逐笔积分消耗流水查询')
    ap.add_argument('--days', type=int, default=7, help='查询最近 N 天 (默认 7)')
    ap.add_argument('--uid', help='只查指定 uid 的账号')
    ap.add_argument('--pages', type=int, default=MAX_PAGES, help='每账号最多拉取页数 (每页 50 条)')
    ap.add_argument('--json', action='store_true', help='输出原始 JSON')
    ap.add_argument('--csv', metavar='PATH', help='导出 CSV 到指定路径')
    args = ap.parse_args()

    try:
        accounts, device_id = load_trae_cfg()
    except Exception as e:
        log(f'config 读取失败: {e}')
        return 1
    if not device_id:
        log('config 缺 device_id (providers.trae.device_id)')
        return 1
    if args.uid:
        accounts = [a for a in accounts if str(a.get('uid', '')).startswith(args.uid)]
        if not accounts:
            log(f'未找到 uid 前缀为 {args.uid} 的账号')
            return 1
    if not accounts:
        log('无 TRAE 账号, 请先在 open-ai 桌面端「账号管理」页添加')
        return 1

    log(f'=== TRAE 逐笔消耗流水 (最近 {args.days} 天) ===')
    exit_code = 0
    json_dump = {}
    for i, acc in enumerate(accounts, 1):
        name = acc.get('name') or acc.get('uid', f'账号{i}')
        log(f'--- {name} (uid={acc.get("uid", "?")})')
        rows, total, err = query_account(acc, device_id, args.days, args.pages)
        if rows is None:
            log(f'  查询失败: {err}')
            exit_code = 1
            continue
        if err:
            log(f'  [警告] {err} (以下为已拉到的部分数据)')
        if args.json:
            json_dump[str(acc.get('uid', i))] = {'total': total, 'items': rows}
        show = rows if len(rows) <= 300 else rows[:300]
        hint = None if len(rows) <= 300 else f'仅显示前 300 笔, 共 {len(rows)} 笔, 完整数据请用 --csv/--json'
        print_table(show, hint)
        summarize(rows)
        if args.csv:
            csv_export(rows, args.csv if len(accounts) == 1
                       else args.csv.rsplit('.', 1)[0] + f'_{acc.get("uid", i)}.' + (args.csv.rsplit('.', 1)[1] if '.' in args.csv else 'csv'))

    if args.json:
        print(json.dumps(json_dump, ensure_ascii=False, indent=1))
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
