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

# ★ 同 credits_api: 打包态 __file__ 相对路径会跟着 CWD 走, 必须钉死安装根。
try:
    import app_paths as _ap
    OPENAI_CFG = _ap.CONFIG_PATH
    DB_PATH = os.path.join(_ap.DATA_DIR, 'usage_history.db')
    LOG_PATH = os.path.join(_ap.LOGS_DIR, 'collector.log')
except Exception:  # 源码态单独运行的兜底
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

# ---- Loomy 采集 (本账号凭据, 台账接口) ----
# 消耗/获取统一走 loomyad.xunfei.cn 的 /api/v1/points/records 逐条台账:
#   debit  = 模型调用扣分
#   credit = 邀请/任务等发放 (每日登录发放不在台账, 由 signin 状态缓存补 gain)
#
# ⚠️ 鉴权整改: 早期版本用硬编码的共享 apiKey 读台账, 所有人的消耗都归到同一个
# 陌生账号 (uid 取 key 前 8 位)。现在逐账号用各自的登录态读取, uid = 账号 userid,
# 看板上每个账号的消耗各归各的。详见 LOOMY_鉴权整改文档。


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
    # ★ 2026-10-24：Qoder / CodeArts 的「配额快照」。
    #   这两家上游**只给累计值**（没有逐笔台账），所以只能拿「当天首次观测」当基线、
    #   用差值算当天消耗。基线必须落库，否则每次采集都从 0 重算。
    #   （platform/uid/day 唯一 → 每天一条基线；跨天自动重新基线。）
    conn.execute('''CREATE TABLE IF NOT EXISTS quota_snapshot (
        platform   TEXT NOT NULL,           -- qoder_cn / qoder_intl / codearts
        uid        TEXT NOT NULL,
        day        TEXT NOT NULL,           -- YYYY-MM-DD (本地日期)
        base_used  REAL NOT NULL,           -- 当天首次观测到的「累计已用」
        last_used  REAL NOT NULL,           -- 最近一次观测到的「累计已用」
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (platform, uid, day)
    )''')


def db_upsert(conn, rows):
    """批量写入 (重复 entry_id 覆盖更新). 返回 新增条数, 总条数

    ★ 写完立即 commit (2026-10-19 修「看板看不到消耗 / 除 Loomy 外全显示未签到」)。

    为什么必须在这里 commit, 而不是留给 collect_all() 最后一次性提交:
      本函数的调用方 (collect_trae / collect_workbuddy / collect_loomy) 都是
      「**先联网抓取, 抓完就写库**」的循环, 而整轮采集的联网部分要跑 3~5 分钟
      (实测 20 轮平均 218 秒, 最长 274 秒)。旧版只在整个 collect_all() 结束时
      commit 一次, 于是从第一次写库起, 写事务就一直挂着不提交, SQLite 对
      并发读连接持续返回 SQLITE_BUSY; 前端那条只读连接 (credits_api._connect,
      timeout=5s) 于是整整 3~5 分钟读不到数据 —— 表现为积分看板「今日消耗」
      恒为 0、账号页除 Loomy 外全渲染成「未签到」(Loomy 凭证走本地 JSON,
      不碰 SQLite, 所以唯独它正常)。
      现在改成「抓完一个账号就提交一批」: 写事务只覆盖 executemany 那一瞬,
      联网期间不持任何写锁, 前端随时能读。
    """
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
    conn.commit()
    after = conn.execute('SELECT COUNT(*) FROM usage').fetchone()[0]
    return after - before, after


def db_prune(conn, keep_days=RETENTION_DAYS):
    cutoff = now_ts() - keep_days * 86400
    conn.execute('DELETE FROM usage WHERE ts < ?', (cutoff,))
    conn.execute('DELETE FROM gain WHERE updated_at < ?', (cutoff,))
    # 清理同样立即提交: 保留期外的大批量 DELETE 会写出最大的一份 journal,
    # 若挂在长事务里, 正是把读连接挡在外面最久的那一段。
    conn.commit()


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


def wb_checkin_activity_status(headers, base='https://copilot.tencent.com'):
    """WorkBuddy 系签到活动状态 (官方在用接口 checkin-activity-status).
    返回 (today_credit or None, checked_in, active, err)。

    2026-09-13 切换: 旧 checkin-status 是废弃接口返回死数据 (国内账号实测
    active=false/streak=0, 同一时刻新接口 active=true/streak=13); 官方 web/IDE
    均只用本接口。active=false 表示活动对该账号未开启 (国际版无签到渠道,
    账号恒为此形态), 此时今日积分记 0 而不是误报。"""
    code, raw = _post_json(f'{base}/billing/meter/checkin-activity-status', headers, {})
    if code != 200:
        return None, False, None, f'HTTP {code}'
    try:
        d = json.loads(raw)
    except Exception:
        return None, False, None, '解析失败'
    st = d.get('data') or {}
    return st.get('today_credit'), bool(st.get('today_checked_in')), \
        bool(st.get('active')), None


# 注册/订阅类礼包: 创建于注册当天的 "Free Plan Subscription" 等, 不是签到入账
# (实测 2026-09-11 新账号注册当天收到 250+100 两个包, 被旧版误判为签到已入账,
#  挡住当天真实签到 → 100 分没领)。 Bonus / 签到 / 活跃奖励类才是积分入账凭证。
_REG_PACK_PATTERNS = (
    'free plan', 'subscription', 'trial', 'register', 'signup', 'newuser',
    '新手', '注册', '订阅', '体验计划',
)


def _is_registration_pack(pkg_name):
    """判断资源包名是否注册/订阅类礼包 (大小写不敏感)。"""
    name = (pkg_name or '').strip().lower()
    if not name:
        return False
    return any(p in name for p in _REG_PACK_PATTERNS)


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
    WorkBuddy: 官方 checkin-activity-status 为主 (today_checked_in),
      资源包兜底但排除注册/订阅类礼包。
      修复 (2026-09-13): 旧版把任意当日资源包都算签到入账, 注册当天的新手礼包
      (Free Plan Subscription / Bonus Pack) 被误判 → 签到被跳过 + gain 表污染;
      国际版无签到渠道 (active=false 恒定), 旧版兜底接口是废弃的 checkin-status,
      导致 gain 表 workbuddy_intl 从建库起 0 行。"""
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
    # ---- WorkBuddy 系 (国内 + 国际): 官方活动状态为主, 资源包兜底 ----
    for spec in WB_SPECS:
        plat, short = spec['platform'], spec['short']
        wb_accounts, domain, product = load_wb(cfg, spec)
        for acc in wb_accounts:
            if not acc.get('accessToken') or acc.get('enabled') is False:
                continue
            uid8 = str(acc.get('userId', ''))[:8]
            headers = wb_headers(acc, domain, product, base=spec['base'])
            # 1) 官方活动状态 (checkin-activity-status): active=false = 活动对
            #    该账号未开启 (国际版无渠道的常态), 今日积分记 0, 不再误报。
            credit, checked, active, err = wb_checkin_activity_status(
                headers, base=spec['base'])
            if err:
                log(f'gain {short}[{uid8}]: {err}')
                continue
            if checked:
                rows.append((plat, str(acc.get('userId', '')), day, _f(credit or 0),
                             'checkin', now_ts()))
                continue
            # 2) active=true 但 today_checked_in=false → 真没签, 记 0 (不写行,
            #    保持"gain 无记录=未签"语义, 但不再拿资源包冒充签到)。
            if active:
                continue
            # 3) active=false (无渠道/未参与) → 资源包兜底, 但排除注册/订阅类
            #    礼包, 只认 Bonus/签到类 (每日活跃奖励 +30 的延迟到账靠这条记录)。
            packs, err = wb_today_packs(headers, base=spec['base'])
            if err:
                log(f'gain {short}[{uid8}]: {err}')
                continue
            for rid, pkg, amount in packs:
                if amount > 0 and not _is_registration_pack(pkg):
                    rows.append((plat, str(acc.get('userId', '')), day, amount,
                                 f'pack:{pkg}'[:40], now_ts()))
    if rows:
        conn.executemany('''INSERT INTO gain (platform, uid, day, amount, kind, updated_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(platform, uid, day, kind) DO UPDATE SET
                amount = MAX(amount, excluded.amount), updated_at = excluded.updated_at''', rows)
        conn.commit()   # 同上: 签到凭证立即落库, 别让账号页的签到列白等一轮采集
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


# ============ Loomy 采集 (usage + gain) ============

def loomy_rows(items, uid, collected_at):
    """把某账号的 loomyad 台账条目转成 usage 表行 (只取 debit=模型调用扣分)。

    uid = config 里该账号的 userid —— 鉴权整改后每个账号用自己的登录态调上游,
    消耗天然归属到产生它的账号, 不再混在一个共享池里。
    credit 方向 (邀请/任务奖励) 不是消耗, 不进 usage 表。
    """
    out = []
    for it in items:
        if not isinstance(it, dict) or it.get('direction') != 'debit':
            continue
        ts = int(it.get('createdAt') or 0) or collected_at
        model = str(it.get('modelName') or '') or '未知'
        extra = {'description': it.get('description'),
                 'consumeSource': it.get('consumeSource'),
                 'dailyCycleDate': it.get('dailyCycleDate')}
        out.append(('loomy', uid, str(it.get('ledgerId') or ''),
                    ts, model, _f(it.get('pointsActual')), 0.0,
                    0, 0, 0,
                    it.get('consumeSource') or '', '', '',
                    json.dumps(extra, ensure_ascii=False), collected_at))
    return out


def collect_loomy(conn, cfg, collected_at, window_days=WINDOW_DAYS):
    """逐账号采集 Loomy 消耗流水 (各自台账, debit 方向) 写 usage 表。

    台账是全量倒序分页, 按窗口天数本地过滤; 幂等 (ledgerId 主键 upsert)。
    某账号凭据失效只影响它自己, 其余账号照常采集。
    """
    import loomy_client as lc
    day_start = time.strftime('%Y-%m-%d', time.localtime(now_ts() - window_days * 86400))
    results, errors = lc.ledger_all_accounts(page_size=100, max_pages=5)
    if not results:
        return 'loomy: 无可用账号 (请先在账号管理中登录)'
    cutoff = now_ts() - window_days * 86400
    total, added, per_acct = 0, 0, []
    for acc, items in results:
        uid = str(acc.get('userid') or '')
        if not uid:
            continue
        rows = [r for r in loomy_rows(items, uid, collected_at) if r[3] >= cutoff]
        a, _n = db_upsert(conn, rows)
        total += len(rows)
        added += a
        label = acc.get('phone') or uid
        per_acct.append(f'{label}={len(rows)}')
    msg = f'loomy: 抓到 {total} 条 (窗口 {day_start} 起), 新增 {added} 条' \
          f' [{", ".join(per_acct)}]'
    if errors:
        msg += f' (警告: {"; ".join(errors[:3])})'
    return msg


def loomy_gains(conn, cfg):
    """Loomy 获取积分凭证 → gain 表。

    两个来源:
      1. 台账 credit 方向 (邀请奖励/新手任务等), kind=description;
      2. 每日登录凭证 (data/loomy_signin_state.json, kind=daily-login) ——
         每日登录发放不进台账, 只有签到状态缓存里有。
    都按账号 uid 归属; 当日快照, upsert 主键 (platform, uid, day, kind) 幂等。
    """
    import loomy_client as lc
    day = time.strftime('%Y-%m-%d')
    n = 0
    try:
        results, errors = lc.ledger_all_accounts(page_size=100, max_pages=5)
        for err in errors:
            log(f'gain loomy: 台账抓取失败 ({err})')
        for acc, items in results:
            uid = str(acc.get('userid') or '')
            if not uid:
                continue
            for it in items:
                if not isinstance(it, dict) or it.get('direction') != 'credit':
                    continue
                if str(it.get('dailyCycleDate') or '') != day:
                    continue  # gain 表按本地日聚合, 只写当日
                conn.execute('''INSERT INTO gain (platform, uid, day, amount, kind, updated_at)
                    VALUES (?,?,?,?,?,?)
                    ON CONFLICT(platform, uid, day, kind) DO UPDATE SET
                        amount=excluded.amount, updated_at=excluded.updated_at''',
                             ('loomy', uid, day, _f(it.get('pointsActual')),
                              str(it.get('description') or 'credit'), now_ts()))
                conn.commit()   # 逐条提交: 台账分页要联网, 不把写锁带进下一轮请求
                n += 1
    except Exception as e:  # noqa: BLE001
        log(f'gain loomy: 台账读取异常 ({e})')
    # 每日登录凭证 → gain 表。
    # 同一手机号的桌面+Web 账号是同一个钱包（账号页已合并为一行），这里按
    # 手机号归组：uid 一律用桌面 userid（与 usage 表的消耗归属一致，看板
    # account_breakdown 收支才能对上）；**任一半边**有领取凭证就记一行，
    # 金额取各凭证 dailyBalance 的最大值 —— 绝不写两行（同钱包重复计数）。
    # ⚠️ 不能「有桌面账号就跳过 Web 凭证」：定时签到跑得早，凭证可能只在
    # Web 半边（桌面账号当天下午才登录），整组跳过会让看板丢掉当日获取。
    try:
        accs = lc.load_accounts()
        state = lc.load_daily_state() or {}
        groups: dict = {}
        for acc in accs:
            key = str(acc.get('phone') or acc.get('userid') or '')
            g = groups.setdefault(key, {'uid': '', 'entries': []})
            if acc.get('session'):
                # 桌面 userid 是该钱包的 canonical uid（后到覆盖 Web 的）
                g['uid'] = str(acc.get('userid') or '') or g['uid']
            elif not g['uid']:
                g['uid'] = str(acc.get('userid') or '')
            st = state.get(str(acc.get('userid') or ''))
            if isinstance(st, dict) and st.get('claimed'):
                g['entries'].append(st)
        for g in groups.values():
            if not g['entries'] or not g['uid']:
                continue
            amount = 0.0
            for st in g['entries']:
                try:
                    amount = max(amount, float(st.get('dailyBalance') or 0))
                except (TypeError, ValueError):
                    pass
            conn.execute('''INSERT INTO gain (platform, uid, day, amount, kind, updated_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(platform, uid, day, kind) DO UPDATE SET
                    amount=excluded.amount, updated_at=excluded.updated_at''',
                         ('loomy', g['uid'], day, amount, 'daily-login', now_ts()))
            conn.commit()
            n += 1
    except Exception as e:  # noqa: BLE001
        log(f'gain loomy: 状态缓存读取失败 ({e})')
    return n


# ============ 汇总入口 ============

# ============ Qoder / CodeArts 消耗（快照差值） ============
#
# 为什么是「差值」而不是逐笔：
#   ### / WorkBuddy / Loomy 都有**逐条台账**接口，能一行一笔地记。
#   而 Qoder 与 CodeArts 上游**只给累计值**：
#     Qoder     GET /api/v2/quota/usage               → userQuota.used + addOnQuota.used
#     CodeArts  GET snap-manager/v1/statistics/plugin → package_credit_used
#   没有「这一笔扣了多少」的接口。所以采集器**定期记一次累计值**，
#   拿「当天首次观测」当基线，差值就是当天消耗，
#   写成一条 platform × uid × 天的聚合行。
#
# 如实说明精度（这不是 bug）：
#   · 当天首次采集**之前**已经消耗掉的部分算不进去（那时还没有基线）；
#   · 粒度是「账号 × 天」一条聚合行，不是逐笔；
#   · 池子重置 / 字段消失会让累计值**变小** → 当场重新基线，绝不写负数。
#
# entry_id 固定为 `quota-day-<日期>`：同一天多次采集会**覆盖更新同一条**，
# 所以反复采集不会把看板刷成一堆重复行。

_QUOTA_CHANNELS = (
    # (流水库 platform 键, config.json providers 段键)
    ('qoder_cn', 'qoder_cn'),
    ('qoder_intl', 'qoder_intl'),
)


def _providers_module(name):
    """惰性导入 providers 下的模块。

    采集器自身只用标准库（urllib），但 Qoder / CodeArts 的接口要 COSY / AK-SK 签名，
    直接复用 providers 里的实现最稳（签名逻辑不该抄第二遍，抄一遍就多一处会腐烂的代码）。
    源码态与打包态都要保证项目根在 sys.path 上。
    """
    try:
        from app_paths import ROOT as root
    except Exception:  # 源码态兜底
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    import importlib
    return importlib.import_module('providers.' + name)


def _qoder_used(acc):
    """Qoder 账号的**累计已用**积分（两个池相加）。返回 (used|None, err)。"""
    try:
        qc = _providers_module('qoder_common')
        quota = qc.account_quota(acc) or {}
    except Exception as e:  # noqa: BLE001
        return None, f'quota 查询异常: {str(e)[:120]}'
    if not quota:
        return None, 'quota/usage 无响应（token 失效或网络不通）'

    def _used(node):
        if not isinstance(node, dict):
            return 0.0
        try:
            return float(node.get('used') or 0)
        except (TypeError, ValueError):
            return 0.0

    pools = [k for k in ('userQuota', 'addOnQuota', 'shared', 'credits', 'quota')
             if isinstance(quota.get(k), dict)]
    if not pools:
        return None, f"配额响应里没有已知积分池（keys={sorted(quota.keys())[:8]}）"
    return sum(_used(quota.get(k)) for k in pools), ''


# 「凭据过期」类错误的文本特征（与 providers/codearts.is_expiry_error_text 同口径）。
# 采集器这里自己留一份：它会把 providers 模块整体替换成替身（测试/降级），
# 不能假设对方一定实现了这个函数。
_EXPIRY_ERROR_HINTS = ("apig.0602", "security token has expired",
                       "sts5.1806", "apig.0301", "decrypt token fail")


def _is_expiry_error_text(text) -> bool:
    blob = str(text or '').lower()
    return any(h in blob for h in _EXPIRY_ERROR_HINTS)


def _codearts_used(acc):
    """CodeArts 账号的**累计已用**积分。返回 (used|None, err)。

    ★ 2026-10-30 修「又有账号没有正常返回积分」：采集器每 5 分钟一轮，而华为
      STS securityToken 寿命约 1 小时；`fetch_credit_stats()` 是**纯函数**
      （只拿 credentials 签名，从不续期），token 一过期整轮就报
      `Bad request: the security token has expired`，
      该账号当天的 quota 快照与「当日消耗」直接缺数。

      实锤（logs/collector.log）：两个账号**交替**出现这种失败，按小时成片 ——
      20:41~21:41 是 01a16c50，22:11~22:26 是 3df18448，23:46~23:56 又是 01a16c50；
      累计 1209 条同类报错。期间只要网关那边有请求（会续期并落盘）就自动恢复，
      所以表现为「时好时坏」。

      修法：临近过期先续期（复用 `refresh_account_token`，它自带跨进程互斥，
      不会把单次使用的 refresh token 玩坏）；遇到过期错误再续期重试一次。
    """
    try:
        ca = _providers_module('codearts')
    except Exception as e:  # noqa: BLE001
        return None, f'codearts 模块不可用: {str(e)[:120]}'
    uid = str(acc.get('uid') or '')[:8]
    refresh = getattr(ca, 'refresh_account_token', None)   # 替身模块可能没有
    try:
        exp = int(acc.get('expiresAt') or 0)
        if refresh is not None and (not exp or exp - 300 <= time.time()):
            if refresh(acc):
                log(f'quota codearts/{uid}: 凭据已续期')
    except Exception as e:  # noqa: BLE001
        log(f'quota codearts/{uid}: 续期异常（继续用现有凭据）: {str(e)[:100]}')
    try:
        stats, err = ca.fetch_credit_stats(acc)
    except Exception as e:  # noqa: BLE001
        stats, err = {}, f'statistics 查询异常: {str(e)[:120]}'
    if not stats and _is_expiry_error_text(err):
        # 过期错误 —— 强制续期后重试一次（仍失败就照实返回，不伪造 0）
        try:
            if refresh is not None:
                refresh(acc)
            stats, err = ca.fetch_credit_stats(acc)
        except Exception as e:  # noqa: BLE001
            err = f'{err} | 过期后重试异常: {str(e)[:80]}'
    if not stats:
        return None, err or 'statistics/plugin 无响应'
    return float(stats.get('used') or 0), ''


def quota_day_row(conn, platform, uid, used_now, collected_at, extra=None):
    """把「累计已用」换算成**当天**的一条 usage 行（幂等）。返回 row 或 None。

    返回 None 的两种情况（都**不该**写行 —— 写了就是假数据）：
      1) 今天第一次观测，且**没有可接续的昨日快照** → 只建基线。
         因为「今天 0 点到现在」已经消耗掉的那部分我们无从得知：
         写 0 是假的，写「历史累计」更是假的。
      2) 差值 <= 0 → 没有新增消耗（或池子刚重置）。

    ★ 接续昨日（2026-10-24 补，重要）：
      第一版把「当天首次观测」当基线，于是「昨天最后一次采集 → 今天第一次采集」
      之间消耗的积分**整段丢掉**。典型场景：夜里关机、早上才开程序 ——
      那段时间若用官方 IDE 消耗过积分，就永远记不上，看板会长期少算。
      现在：若存在**恰好是昨天**的最后一条快照，且它的累计值 <= 当前值，
      就用它当今天的基线（差值立刻产出今天第一条行）。
      只在「上一条恰好是昨天」时接续 —— 中间断了几天就**不接**，
      免得把几天的消耗全砸到某一天、在看板上造出假尖峰。
    """
    day = time.strftime('%Y-%m-%d', time.localtime(collected_at))
    uid = str(uid or '')
    used_now = float(used_now)
    cur = conn.execute(
        'SELECT base_used FROM quota_snapshot'
        ' WHERE platform=? AND uid=? AND day=?', (platform, uid, day)).fetchone()
    carried_from = ''
    if cur is None:
        yday = time.strftime('%Y-%m-%d', time.localtime(collected_at - 86400))
        prev = conn.execute(
            'SELECT day, last_used FROM quota_snapshot'
            ' WHERE platform=? AND uid=? AND day<? ORDER BY day DESC LIMIT 1',
            (platform, uid, day)).fetchone()
        base = used_now
        if prev and str(prev[0]) == yday and float(prev[1]) <= used_now:
            base = float(prev[1])
            carried_from = str(prev[0])
        conn.execute(
            'INSERT INTO quota_snapshot (platform,uid,day,base_used,last_used,updated_at)'
            ' VALUES (?,?,?,?,?,?)',
            (platform, uid, day, base, used_now, int(collected_at)))
        conn.commit()
        if not carried_from:
            return None          # 无可接续 → 只建基线，不写行
    else:
        base = float(cur[0])

    if used_now < base:
        # 池子重置 / 字段变动 → 重新基线，**绝不写负数**
        conn.execute(
            'UPDATE quota_snapshot SET base_used=?, last_used=?, updated_at=?'
            ' WHERE platform=? AND uid=? AND day=?',
            (used_now, used_now, int(collected_at), platform, uid, day))
        conn.commit()
        return None
    delta = used_now - base
    conn.execute(
        'UPDATE quota_snapshot SET last_used=?, updated_at=?'
        ' WHERE platform=? AND uid=? AND day=?',
        (used_now, int(collected_at), platform, uid, day))
    conn.commit()
    if delta <= 0:
        return None
    info = {'base_used': base, 'used_now': used_now,
            'delta': round(delta, 6), 'day': day}
    if carried_from:
        info['carried_from'] = carried_from
    if extra:
        info.update(extra)
    return (platform, uid, f'quota-day-{day}', int(collected_at), '',
            delta, None, 0, 0, 0, 'quota-delta', None, None,
            json.dumps(info, ensure_ascii=False), int(collected_at))


def _collect_quota_channel(conn, cfg, collected_at, platform, seg_key, used_fn):
    """采集一个通道的消耗（逐账号取累计值 → 换算成当天差值行）。

    报数口径（★ 2026-10-24 修：初版把后面两类混成一个「建基线」，会误报）：
      · 记 N 个账号的当日消耗 —— 本轮**写出了/更新了**当天那条行；
      · 本轮建基线 M 个       —— 今天**此前没有基线**，本轮才建（无可接续）；
      · 无新增 K 个           —— 已有基线，但累计值没涨（差值 0）；
      · 失败 F 个             —— 上游读不到累计值。
    """
    seg = (cfg.get('providers') or {}).get(seg_key) or {}
    accs = [a for a in (seg.get('accounts') or []) if isinstance(a, dict)]
    if not accs:
        return f'{platform}: 无账号（请先在账号管理中登录）'
    day = time.strftime('%Y-%m-%d', time.localtime(collected_at))
    done = fail = fresh = nod = 0
    for acc in accs:
        if acc.get('enabled', True) is False:
            continue                      # 关掉的账号不采（它也不该产生消耗）
        uid = str(acc.get('uid') or acc.get('userId') or '')
        if not uid:
            continue
        used, err = used_fn(acc)
        if used is None:
            log(f'quota {platform}/{uid[:8]}: {err}')
            fail += 1
            continue
        # 先看今天是否已有基线 —— 用来把「本轮才建基线」与「无新增」分开报
        had_base = conn.execute(
            'SELECT 1 FROM quota_snapshot WHERE platform=? AND uid=? AND day=?',
            (platform, uid, day)).fetchone() is not None
        row = quota_day_row(conn, platform, uid, used, collected_at,
                            extra={'realm': acc.get('realm') or ''})
        if row is None:
            if had_base:
                nod += 1
            else:
                fresh += 1
        else:
            db_upsert(conn, [row])
            done += 1
    msg = f'{platform}: 记 {done} 个账号的当日消耗'
    if fresh:
        msg += f', 本轮建基线 {fresh} 个'
    if nod:
        msg += f', 无新增 {nod} 个'
    if fail:
        msg += f', 失败 {fail} 个'
    return msg


def collect_quota_channels(conn, cfg, collected_at):
    """采集 Qoder CN / Qoder 国际 / CodeArts 三通道的消耗（2026-10-24 新增）。

    这三个通道此前**完全没有流水**（usage 表里一行都没有），所以积分看板上
    它们的消耗恒为 0 —— 本次补齐。数据源是上游的「累计已用」，口径见上方注释。
    """
    msgs = []
    for platform, seg_key in _QUOTA_CHANNELS:
        try:
            msgs.append(_collect_quota_channel(conn, cfg, collected_at,
                                               platform, seg_key, _qoder_used))
        except Exception as e:  # noqa: BLE001 — 单通道失败不该拖垮其它通道
            msgs.append(f'{platform}: 采集异常 {str(e)[:120]}')
    try:
        msgs.append(_collect_quota_channel(conn, cfg, collected_at,
                                           'codearts', 'codearts', _codearts_used))
    except Exception as e:  # noqa: BLE001
        msgs.append(f'codearts: 采集异常 {str(e)[:120]}')
    return msgs


def collect_all(window_days=WINDOW_DAYS):
    """采集一轮 (TRAE + WorkBuddy 国内 + WorkBuddy 国际 消耗流水 + 获取积分) 并入库。
    返回汇总消息字符串。

    ★ 事务边界 (2026-10-19 修): 本函数**不再**负责「攒到最后一次提交」——
    各写入点 (db_upsert / collect_gains / loomy_gains / db_prune) 各自写各提交。
    旧版整轮只有一个连接、只在末尾 commit 一次, 而这中间夹着 3~5 分钟的联网
    抓取, 写事务全程挂起 → 前端只读连接被 SQLITE_BUSY 挡住 → 积分看板恒 0、
    账号页除 Loomy 外全显示「未签到」(详见 db_upsert 的注释)。
    末尾这次 commit 只是兜底 (防御性收尾), 不承担主要职责。
    """
    collected_at = now_ts()
    try:
        with open(OPENAI_CFG, encoding='utf-8') as f:
            cfg = json.load(f)
    except Exception as e:
        return f'config 读取失败: {e}'
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        db_init(conn)
        msgs = [collect_trae(conn, cfg, collected_at, window_days)]
        msgs.extend(collect_all_workbuddy(conn, cfg, collected_at, window_days))
        msgs.append(collect_loomy(conn, cfg, collected_at, window_days))
        # ★ 2026-10-24 新增：Qoder CN / Qoder 国际 / CodeArts 的消耗
        #   （此前这三通道在 usage 表里一行都没有 → 看板恒显示 0 消耗）
        msgs.extend(collect_quota_channels(conn, cfg, collected_at))
        n_gain = collect_gains(conn, cfg)
        n_gain += loomy_gains(conn, cfg)
        msgs.append(f'获取积分记录 {n_gain} 条')
        db_prune(conn)
        conn.commit()   # 兜底收尾 (各写入点已自行提交)
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
