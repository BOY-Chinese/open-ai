#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TRAE + WorkBuddy 逐笔消耗流水 · 自动采集与本地流水库
====================================================
两条使用路径:

  A. 自动采集 (随 daemon 每 30 分钟一轮, 打包给其他人后零操作):
       daemon.py 会调用 collect_all(), 增量抓取并写入 data/usage_history.db

  B. 手动使用:
       python scripts/usage_collector.py --collect      # 立即采集一轮 (daemon 同款)
       python scripts/usage_collector.py --collect-loop # 常驻采集循环 (不用 daemon 时)
       python scripts/usage_collector.py                # 查看本地库 (最近 7 天)
       python scripts/usage_collector.py --local --days 30
       python scripts/usage_collector.py --local --csv out.csv
       python scripts/usage_collector.py --local --json

接口来源 (前端 JS 逆向):
  TRAE      : POST api.trae.cn/trae/api/v1/pay/query_user_usage_group_by_session
              鉴权 Cloud-IDE-JWT <token> + x-device-id; usage_type=[7], page_size<=50
  WorkBuddy : POST copilot.tencent.com/billing/meter/get-user-request-usage
              鉴权 Bearer <accessToken> + X-User-Id + X-Enterprise-Id(=userId);
              支持 v1 页码式 (pageNum/pageSize, 回 total)
  WB 国际版  : POST www.workbuddy.ai/billing/meter/get-user-request-usage
              **路径与国内版完全一致**, 只差 host + X-Product-Code: workbuddy-ai
本地库: data/usage_history.db (SQLite, usage 表, 以 platform+uid+entry_id 去重)
        platform 取值: trae / workbuddy / workbuddy_intl
读取接口: scripts/credits_api.py (给前端用的只读查询 API, 见其文件头)
"""
import argparse
import csv
import json
import os
import sqlite3
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
DB_PATH = os.path.join(BASE, '..', 'data', 'usage_history.db')
LOG_PATH = os.path.join(BASE, '..', 'logs', 'collector.log')

TRAE_URL = 'https://api.trae.cn/trae/api/v1/pay/query_user_usage_group_by_session'
WB_REQ_URL = 'https://copilot.tencent.com/billing/meter/get-user-request-usage'

PAGE_SIZE_TRAE = 50      # TRAE 接口上限 50, 超过 400
PAGE_SIZE_WB = 100
MAX_PAGES = 40           # 单账号单轮上限 (TRAE 50*40=2000 条, WB 100*40=4000 条)
UA_WEB = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
          '(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0')
UA_WB = 'WorkBuddy/5.3.12 WorkBuddy/5.3.12 CLI/2.115.0'

# ---- WorkBuddy 系采集规格 (国内 / 国际) ----
# 两者接口路径**完全一致**, 只差 host 与产品标识; 国际版额外要求
# X-Product-Code: workbuddy-ai (实测确认, 见 MEMORY.md)。
# 新增 WorkBuddy 系区域时, 往这里加一条即可, 采集/入库/查询全链路自动覆盖。
WB_SPECS = (
    {'platform': 'workbuddy', 'provider': 'workbuddy',
     'base': 'https://copilot.tencent.com', 'label': 'WorkBuddy', 'short': 'wb',
     'default_domain': 'www.workbuddy.cn', 'default_product': 'SaaS'},
    {'platform': 'workbuddy_intl', 'provider': 'workbuddy_intl',
     'base': 'https://www.workbuddy.ai', 'label': 'WorkBuddy国际', 'short': 'wbai',
     'default_domain': 'www.workbuddy.ai', 'default_product': 'workbuddy-ai'},
)
WB_SPEC_BY_PLATFORM = {s['platform']: s for s in WB_SPECS}
WB_SPEC_BY_PROVIDER = {s['provider']: s for s in WB_SPECS}
WB_DEFAULT_SPEC = WB_SPEC_BY_PLATFORM['workbuddy']

COLLECT_INTERVAL = 5 * 60          # 采集周期: 5 分钟 (daemon 与 GUI 页面刷新同节奏)
WINDOW_DAYS = 3                    # 每轮回看窗口 (天): 周期 5min << 3d, 覆盖停机补漏
RETENTION_DAYS = 31                # 本地缓存保留 1 个月 (master 指定)


def now_ts():
    return int(time.time())


def log(msg):
    line = '[%s] %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass
    return line


def _post_json(url, headers, body, timeout=25):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method='POST')
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')
    except Exception as e:
        return 0, str(e)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ============ 数据库 ============

def db_init(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS usage (
        platform    TEXT NOT NULL,          -- trae / workbuddy
        uid         TEXT NOT NULL,          -- 账号标识 (trae uid / wb userId)
        entry_id    TEXT NOT NULL,          -- 唯一键: trae session_id / wb requestId
        ts          INTEGER NOT NULL,       -- 消耗时间 (unix 秒)
        model       TEXT,
        credits     REAL,
        cost_money  REAL,                   -- 仅 TRAE 提供折算金额
        input_tok   INTEGER,
        output_tok  INTEGER,
        cache_tok   INTEGER,
        source      TEXT,                   -- TRAE: usage_source / WB: client
        session_id  TEXT,
        preview     TEXT,
        extra       TEXT,                   -- JSON: 原始字段碎片 (mode/purpose/group...)
        collected_at INTEGER NOT NULL,
        PRIMARY KEY (platform, uid, entry_id)
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage (ts)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_usage_plat_uid ON usage (platform, uid)')
    # 积分获取表 (签到/赠送/补偿等正向入账)
    conn.execute('''CREATE TABLE IF NOT EXISTS gain (
        platform    TEXT NOT NULL,
        uid         TEXT NOT NULL,
        day         TEXT NOT NULL,          -- YYYY-MM-DD (本地日期)
        amount      REAL NOT NULL,          -- 当日累计获取积分
        kind        TEXT,                   -- checkin / gift / compensation
        updated_at  INTEGER NOT NULL,
        PRIMARY KEY (platform, uid, day, kind)
    )''')


def db_upsert(conn, rows):
    """批量写入 (重复 entry_id 覆盖更新). 返回 新增条数, 总条数"""
    if not rows:
        return 0, 0
    before = conn.execute('SELECT COUNT(*) FROM usage').fetchone()[0]
    conn.executemany('''INSERT INTO usage
        (platform, uid, entry_id, ts, model, credits, cost_money, input_tok, output_tok,
         cache_tok, source, session_id, preview, extra, collected_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(platform, uid, entry_id) DO UPDATE SET
            model=excluded.model, credits=excluded.credits, cost_money=excluded.cost_money,
            input_tok=excluded.input_tok, output_tok=excluded.output_tok, cache_tok=excluded.cache_tok,
            source=excluded.source, preview=excluded.preview, extra=excluded.extra,
            collected_at=excluded.collected_at''', rows)
    after = conn.execute('SELECT COUNT(*) FROM usage').fetchone()[0]
    return after - before, after


def db_prune(conn, keep_days=RETENTION_DAYS):
    cutoff = now_ts() - keep_days * 86400
    conn.execute('DELETE FROM usage WHERE ts < ?', (cutoff,))
    conn.execute('DELETE FROM gain WHERE updated_at < ?', (cutoff,))


# ============ 获取积分 (签到等) 采集 ============

def trae_checkin_status(acc, device_id):
    """TRAE 签到状态. 返回 (credits_当前积分 or None, checked_in, err)。"""
    headers = {'authorization': f"Cloud-IDE-JWT {acc.get('token', '')}",
               'x-device-id': acc.get('device_id') or device_id,
               'content-type': 'application/json',
               'user-agent': 'TRAE SOLO CN/2.3.70844', 'accept': '*/*'}
    code, raw = _post_json('https://api.trae.cn/trae/api/v2/ug/checkin_credits/status', headers, {})
    if code != 200:
        return None, False, f'HTTP {code}'
    try:
        d = json.loads(raw)
    except Exception:
        return None, False, '解析失败'
    return d.get('credits'), bool(d.get('checked_in')), None


def wb_checkin_status(headers, base='https://copilot.tencent.com'):
    """WorkBuddy 系签到状态. 返回 (today_credit or None, checked_in, err)。
    ⚠️ 实测: 账号 active=false 时该接口恒报 today_checked_in=False/today_credit=0,
    与实际签到入账脱节 (签到其实成功, 积分以资源包形式入账)。勿以此接口为统计依据。"""
    code, raw = _post_json(f'{base}/billing/meter/checkin-status', headers, {})
    if code != 200:
        return None, False, f'HTTP {code}'
    try:
        d = json.loads(raw)
    except Exception:
        return None, False, '解析失败'
    st = d.get('data') or {}
    return st.get('today_credit'), bool(st.get('today_checked_in')), None


def wb_today_packs(headers, base='https://copilot.tencent.com'):
    """WB 系今日入账的资源包 (签到/活动积分的真实凭证).
    返回 [(resource_id, package_name, capacity_size)], err。
    ⚠️ 必须用不带 X-Enterprise-Id 的头: 该头会把 get-user-resource 切到企业视角,
    个人账号的资源包会被隐藏 (返回 0 包)。"""
    h = {k: v for k, v in headers.items() if k != 'X-Enterprise-Id'}
    code, raw = _post_json(f'{base}/billing/meter/get-user-resource', h, {})
    if code != 200:
        return [], f'HTTP {code}'
    try:
        d = json.loads(raw)
    except Exception:
        return [], '解析失败'
    if d.get('code') not in (0, None):
        return [], f"code={d.get('code')}"
    accounts = ((d.get('data') or {}).get('Response', {}).get('Data') or {}).get('Accounts') or []
    today = time.strftime('%Y-%m-%d')
    out = []
    for a in accounts:
        ct = a.get('CreateTime')
        try:
            ct_day = time.strftime('%Y-%m-%d', time.localtime(float(ct) / 1000))
        except (TypeError, ValueError):
            continue
        if ct_day != today:
            continue
        out.append((str(a.get('ResourceId') or a.get('PackageCode') or a.get('DealName') or ''),
                    a.get('PackageName') or '', _f(a.get('CapacitySize'))))
    return out, None


def collect_gains(conn, cfg):
    """采集"获取积分": 每账号当日签到入账, 写 gain 表 (当日多次采集取 max)。
    TRAE: checkin_credits/status (checked_in → 每日 200)
    WorkBuddy: 扫 get-user-resource 今日入账的资源包 (每个资源包一条记录, ResourceId 去重)。
      ⚠️ 不能用 checkin-status: active=false 账号恒报未签, 与实际入账脱节。"""
    day = time.strftime('%Y-%m-%d')
    rows = []
    # ---- TRAE ----
    accounts, device_id = load_trae(cfg)
    for acc in accounts:
        if not acc.get('token'):
            continue
        credits, checked, err = trae_checkin_status(acc, device_id)
        if err:
            log(f'gain trae[{acc.get("uid", "?")}]: {err}')
            continue
        if checked:
            rows.append(('trae', str(acc.get('uid', '')), day, 200.0, 'checkin', now_ts()))
    # ---- WorkBuddy 系 (国内 + 国际): 今日入账资源包 ----
    for spec in WB_SPECS:
        plat, short = spec['platform'], spec['short']
        wb_accounts, domain, product = load_wb(cfg, spec)
        for acc in wb_accounts:
            if not acc.get('accessToken') or acc.get('enabled') is False:
                continue
            headers = wb_headers(acc, domain, product, base=spec['base'])
            packs, err = wb_today_packs(headers, base=spec['base'])
            if err:
                log(f'gain {short}[{acc.get("userId", "?")[:8]}]: {err}')
                continue
            if not packs:
                # 无今日入账包 → 尝试签到状态兜底 (活动期账号 active=true 时有值)
                credit, checked, err2 = wb_checkin_status(headers, base=spec['base'])
                if not err2 and checked and credit:
                    rows.append((plat, str(acc.get('userId', '')), day, _f(credit),
                                 'checkin', now_ts()))
                continue
            for rid, pkg, amount in packs:
                if amount > 0:
                    kind = f'pack:{pkg}'[:40]
                    rows.append((plat, str(acc.get('userId', '')), day, amount, kind, now_ts()))
            _ = rid  # rid 已并入 rows
    if rows:
        conn.executemany('''INSERT INTO gain (platform, uid, day, amount, kind, updated_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(platform, uid, day, kind) DO UPDATE SET
                amount = MAX(amount, excluded.amount), updated_at = excluded.updated_at''', rows)
    return len(rows)


# ============ TRAE 采集 ============

def load_trae(cfg):
    t = (cfg.get('providers') or {}).get('trae') or {}
    device_id = t.get('device_id') or (t.get('headers') or {}).get('x-device-id', '')
    return t.get('accounts') or [], device_id


def trae_fetch_all(token, device_id, start, end, max_pages=MAX_PAGES):
    """拉 TRAE 逐笔流水. 返回 (items, err); item 为原始 session 记录。"""
    headers = {'Content-Type': 'application/json', 'User-Agent': UA_WEB,
               'Origin': 'https://www.trae.cn', 'Referer': 'https://www.trae.cn/dashboard',
               'Authorization': f'Cloud-IDE-JWT {token}', 'x-device-id': device_id}
    items, err = [], None
    for page in range(1, max_pages + 1):
        body = {'start_time': int(start), 'end_time': int(end), 'page_num': page,
                'page_size': PAGE_SIZE_TRAE, 'usage_type': [7]}
        code, raw = _post_json(TRAE_URL, headers, body)
        if code == 401:
            return items, 'token 过期 (401)'
        if code != 200:
            err = f'HTTP {code}: {raw[:100]}'
            break
        try:
            d = json.loads(raw)
        except Exception:
            err = '响应解析失败'
            break
        chunk = d.get('user_usage_group_by_sessions') or []
        items.extend(chunk)
        if not chunk or len(chunk) < PAGE_SIZE_TRAE:
            break
    return items, err


def trae_rows(acc, items, collected_at):
    uid = str(acc.get('uid', ''))
    out = []
    for it in items:
        info = it.get('extra_info') or {}
        det = it.get('usage_group_details') or []
        credits = it.get('credits_float')
        if credits is None and det:
            credits = sum(_f(x.get('credits_float')) for x in det)
        extra = {'mode': it.get('mode'), 'product_type_list': it.get('product_type_list'),
                 'use_max_mode': it.get('use_max_mode')}
        out.append((('trae'), uid, str(it.get('session_id') or ''),
                    int(it.get('usage_time') or 0),
                    it.get('model_name') or '', _f(credits), _f(it.get('cost_money_float')),
                    int(info.get('input_token') or 0), int(info.get('output_token') or 0),
                    int(info.get('cache_read_token') or 0) + int(info.get('cache_write_token') or 0),
                    str(it.get('usage_source') or ''), (it.get('session_id') or '')[:12],
                    (it.get('user_input_preview') or '')[:120],
                    json.dumps(extra, ensure_ascii=False), collected_at))
    return out


def collect_trae(conn, cfg, collected_at, window_days=WINDOW_DAYS):
    accounts, device_id = load_trae(cfg)
    if not device_id:
        return 'trae: config 缺 device_id, 跳过'
    if not accounts:
        return 'trae: 无账号'
    end = now_ts() + 3600
    start = end - 3600 - window_days * 86400
    added_total, msgs = 0, []
    for acc in accounts:
        uid = acc.get('uid', '?')
        if not acc.get('token'):
            msgs.append(f'trae[{uid}]: 缺 token')
            continue
        items, err = trae_fetch_all(acc['token'], device_id, start, end)
        if err and not items:
            msgs.append(f'trae[{uid}]: 失败 ({err})')
            continue
        added, _n = db_upsert(conn, trae_rows(acc, items, collected_at))
        added_total += added
        msgs.append(f'trae[{uid}]: 抓到 {len(items)} 条, 新增 {added} 条' + (f' (警告: {err})' if err else ''))
    return '; '.join(msgs) or 'trae: 完成'


# ============ WorkBuddy 系采集 (国内 / 国际 共用一套逻辑) ============

def load_wb(cfg, spec=None):
    """读取某个 WorkBuddy 系平台的账号与 (domain, product)。
    spec 省略时默认国内版; 国际版传 WB_SPEC_BY_PLATFORM['workbuddy_intl']。"""
    spec = spec or WB_DEFAULT_SPEC
    prov = ((cfg.get('providers') or {}).get(spec['provider'])) or {}
    return (prov.get('accounts') or [],
            prov.get('domain', spec['default_domain']),
            prov.get('product', spec['default_product']))


def wb_headers(acc, domain, product, base='https://copilot.tencent.com'):
    """WorkBuddy 系逐笔流水请求头。

    ⚠️ request/daily-usage 必须带 X-Enterprise-Id (否则 400); 但 get-user-resource
    恰相反, 带了会切到企业视角、隐藏个人资源包 (wb_today_packs 已剔除该头)。
    国际版额外要求 X-Product-Code。
    """
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json',
               'Authorization': f"Bearer {acc.get('accessToken', '')}",
               'X-User-Id': acc.get('userId', ''),
               'X-Domain': domain, 'X-Product': product,
               'X-Enterprise-Id': acc.get('userId', ''),
               'User-Agent': UA_WB, 'Accept-Language': 'zh'}
    if '.ai' in base:
        headers['X-Product-Code'] = product
    return headers


def wb_fetch_all(headers, start_day, end_day, max_pages=MAX_PAGES,
                 base='https://copilot.tencent.com'):
    """v1 页码式拉 WB 系逐笔流水. 返回 (items, err)。"""
    url = f'{base}/billing/meter/get-user-request-usage'
    items, err = [], None
    for page in range(1, max_pages + 1):
        body = {'startTime': f'{start_day} 00:00:00', 'endTime': f'{end_day} 23:59:59',
                'pageNum': page, 'pageSize': PAGE_SIZE_WB}
        code, raw = _post_json(url, headers, body)
        if code in (401, 403):
            return items, f'token 无效 ({code})'
        if code != 200:
            err = f'HTTP {code}: {raw[:100]}'
            break
        try:
            d = json.loads(raw)
        except Exception:
            err = '响应解析失败'
            break
        if d.get('code') != 0:
            err = f"code={d.get('code')} {d.get('msg', '')[:80]}"
            break
        data = d.get('data') or {}
        chunk = data.get('data') or []
        items.extend(chunk)
        total = data.get('total') or 0
        if not chunk or len(items) >= total:
            break
    return items, err


def wb_rows(acc, items, collected_at, platform='workbuddy'):
    """把 WB 系逐笔记录转成 usage 表行。platform 区分国内/国际。"""
    uid = str(acc.get('userId', ''))
    out = []
    for it in items:
        # requestTime: "YYYY-MM-DD HH:MM:SS" (北京时间)
        try:
            ts = int(time.mktime(time.strptime(it.get('requestTime', ''), '%Y-%m-%d %H:%M:%S')))
        except Exception:
            ts = collected_at
        extra = {'agentPurpose': it.get('agentPurpose'), 'inputTrunc': (it.get('inputTrunc') or '')[:120]}
        out.append((platform, uid, str(it.get('requestId') or ''),
                    ts, it.get('model') or '', _f(it.get('credit')), 0.0,
                    0, 0, 0,
                    it.get('client') or '', (it.get('requestId') or '')[:12],
                    (it.get('input') or it.get('inputTrunc') or '')[:120],
                    json.dumps(extra, ensure_ascii=False), collected_at))
    return out


def collect_workbuddy(conn, cfg, collected_at, window_days=WINDOW_DAYS, spec=None):
    """采集某个 WorkBuddy 系平台 (国内/国际) 的逐笔消耗流水。

    spec 省略 = 国内版; 国际版传 WB_SPEC_BY_PLATFORM['workbuddy_intl']。
    两者接口路径一致, 只差 host / X-Product-Code, 故共用同一段逻辑。
    """
    spec = spec or WB_DEFAULT_SPEC
    plat, short = spec['platform'], spec['short']
    accounts, domain, product = load_wb(cfg, spec)
    if not accounts:
        return f'{plat}: 无账号'
    end_day = time.strftime('%Y-%m-%d')
    start_day = time.strftime('%Y-%m-%d', time.localtime(now_ts() - window_days * 86400))
    added_total, msgs = 0, []
    for acc in accounts:
        uid = acc.get('userId', '?')
        if not acc.get('accessToken'):
            msgs.append(f'{short}[{uid[:8]}]: 缺 accessToken')
            continue
        # 账号级 enabled=false 视为停用: 仍然采集历史, 但跳过以省请求
        if acc.get('enabled') is False:
            msgs.append(f'{short}[{uid[:8]}]: 已停用, 跳过')
            continue
        headers = wb_headers(acc, domain, product, base=spec['base'])
        items, err = wb_fetch_all(headers, start_day, end_day, base=spec['base'])
        if err and not items:
            msgs.append(f'{short}[{uid[:8]}]: 失败 ({err})')
            continue
        added, _n = db_upsert(conn, wb_rows(acc, items, collected_at, platform=plat))
        added_total += added
        msgs.append(f'{short}[{uid[:8]}]: 抓到 {len(items)} 条, 新增 {added} 条'
                    + (f' (警告: {err})' if err else ''))
    return '; '.join(msgs) or f'{plat}: 完成'


def collect_all_workbuddy(conn, cfg, collected_at, window_days=WINDOW_DAYS):
    """采集全部 WorkBuddy 系平台 (国内 + 国际)。新增区域时 WB_SPECS 加一条即可。"""
    return [collect_workbuddy(conn, cfg, collected_at, window_days, spec)
            for spec in WB_SPECS]


# ============ 汇总入口 ============

def collect_all(window_days=WINDOW_DAYS):
    """采集一轮 (TRAE + WorkBuddy 国内 + WorkBuddy 国际 消耗流水 + 获取积分) 并入库。
    返回汇总消息字符串。"""
    collected_at = now_ts()
    try:
        with open(OPENAI_CFG, encoding='utf-8') as f:
            cfg = json.load(f)
    except Exception as e:
        return f'config 读取失败: {e}'
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        db_init(conn)
        msgs = [collect_trae(conn, cfg, collected_at, window_days)]
        msgs.extend(collect_all_workbuddy(conn, cfg, collected_at, window_days))
        n_gain = collect_gains(conn, cfg)
        msgs.append(f'获取积分记录 {n_gain} 条')
        db_prune(conn)
        conn.commit()
    finally:
        conn.close()
    return f'[采集] {collected_at}: ' + ' | '.join(msgs)


# ============ 本地库查看 ============

def show_local(days=7, platform=None, uid=None, csv_path=None, as_json=False, limit=500):
    if not os.path.isfile(DB_PATH):
        log(f'本地流水库不存在 ({DB_PATH}), 先用 --collect 采集一轮')
        return 1
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cutoff = now_ts() - days * 86400
        sql = 'SELECT * FROM usage WHERE ts >= ?'
        args = [cutoff]
        if platform:
            sql += ' AND platform = ?'
            args.append(platform)
        if uid:
            sql += ' AND uid LIKE ?'
            args.append(uid + '%')
        sql += ' ORDER BY ts DESC LIMIT ?'
        args.append(limit)
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    items = [dict(r) for r in rows]
    if as_json:
        print(json.dumps(items, ensure_ascii=False, indent=1))
        return 0
    if csv_path:
        fields = ['platform', 'uid', 'ts', 'model', 'credits', 'cost_money',
                  'input_tok', 'output_tok', 'cache_tok', 'source', 'preview']
        with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
            w.writeheader()
            w.writerows(items)
        log(f'CSV 已导出: {csv_path} ({len(items)} 行)')
        return 0
    log(f'=== 本地流水库 最近 {days} 天 ({len(items)} 条{"+" if len(items) >= limit else ""}) ===')
    if not items:
        log('  (无记录, 等待 daemon 自动采集或运行 --collect)')
        return 0
    hdr = f"{'平台':<10} {'时间':<20} {'模型':<16} {'积分':>8} {'≈元':>8} {'输入':>9} {'输出':>8} {'账号'}"
    print(hdr)
    print('-' * len(hdr))
    for r in items:
        t = time.strftime('%m-%d %H:%M:%S', time.localtime(r['ts']))
        plat = {'trae': 'TRAE', 'workbuddy': 'WorkBuddy',
                'workbuddy_intl': 'WBuddy国际'}.get(r['platform'], r['platform'])
        print(f"{plat:<10} {t:<20} {(r['model'] or '-'):<16} {r['credits']:>8.3f} "
              f"{r['cost_money'] or 0:>8.4f} {r['input_tok'] or 0:>9} {r['output_tok'] or 0:>8} {r['uid'][:10]}")
    # 汇总
    total = sum(r['credits'] or 0 for r in items)
    by = {}
    for r in items:
        k = (r['platform'], r['model'] or '-')
        by.setdefault(k, [0.0, 0])
        by[k][0] += r['credits'] or 0
        by[k][1] += 1
    print('-' * len(hdr))
    print(f'合计 {len(items)} 条 | 积分 {total:.3f}')
    for (plat, model), (c, n) in sorted(by.items(), key=lambda x: -x[1][0]):
        print(f'  [{plat}] {model:<22} {c:>9.3f} 积分 ({n} 笔)')
    return 0


def collect_loop():
    """常驻采集循环 (daemon 不在时手动用)."""
    log('[collect-loop] 启动, 每 %d 分钟一轮' % (COLLECT_INTERVAL // 60))
    while True:
        try:
            log(collect_all())
        except Exception as e:
            log(f'[collect-loop] 异常: {e}')
        time.sleep(COLLECT_INTERVAL)


def main():
    ap = argparse.ArgumentParser(description='TRAE + WorkBuddy 逐笔消耗流水 · 自动采集与本地库')
    ap.add_argument('--collect', action='store_true', help='立即采集一轮并入库')
    ap.add_argument('--collect-loop', action='store_true', help='常驻采集循环 (默认关)')
    ap.add_argument('--local', action='store_true', help='查看本地流水库 (默认行为)')
    ap.add_argument('--days', type=int, default=7, help='查看最近 N 天 (默认 7)')
    ap.add_argument('--platform',
                    choices=['trae', 'workbuddy', 'workbuddy_intl'],
                    help='只看某平台')
    ap.add_argument('--uid', help='只看某账号 (uid 前缀)')
    ap.add_argument('--limit', type=int, default=500, help='最多显示条数 (默认 500)')
    ap.add_argument('--csv', metavar='PATH', help='本地库导出 CSV')
    ap.add_argument('--json', action='store_true', help='本地库输出 JSON')
    args = ap.parse_args()

    if args.collect_loop:
        collect_loop()
        return 0
    if args.collect:
        log(collect_all())
        return 0
    return show_local(days=args.days, platform=args.platform, uid=args.uid,
                      csv_path=args.csv, as_json=args.json, limit=args.limit)


if __name__ == '__main__':
    sys.exit(main())
