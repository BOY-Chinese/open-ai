#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 国际版 · 网页端每日活跃 (+30 积分)
=============================================
来源: 2026-09-18 侦察结论《WB网页端拿积分方案.md》(五期实验判决后的唯一幸存路径)。

一句话原理
----------
**网页端调一次 `POST /console/chat/completions` 发条对话, 即可拿到每日 +30 积分。**
不需要装客户端、不需要梯子、不需要心跳/长连接。

为什么是这条路 (五期对照实验的结论)
------------------------------------
| 期次 | 方案 | 结果 |
| 一期   | HTTP 轮询 (msg-summary + buddy/info)        | ✗ 不算活跃 |
| 三期   | 遥测上报 (/v2/report + dosage + config)      | ✗ 不算活跃 |
| 四期   | Centrifugo WS 长连接 (4 频道订阅)            | ✗ 不算活跃 |
| **五期** | **网页端真实发一条对话**                    | ✅ +30     |

四条「非使用行为」路径全部排除后, 唯一幸存假设 =「真实使用行为」。
2026-09-17→18 用两个账号做对照: 网页端账号 `ae1c0d1a…` 09-18 02:03:47 入账 +30,
客户端账号 `94211e45…` 00:54:58 入账 +30 → 网页端也能拿分, 结论成立。

★ 与旧签到链路的关系 (重要)
---------------------------
国际版**没有签到渠道**: 服务端对国际账号恒报 `active=false`,
`/billing/meter/daily-checkin` 必返回 10001「签到活动未开启或已过期」(终态, 补领无效)。
所以本脚本**不是**签到脚本, 它走的是「活跃奖励」路径 —— 积分由服务端**延迟自动入账**
(Bonus Pack, `TCACA_code_007`, 约 03:12 前后分批到账), 客户端**无法**通过接口主动领取,
也无法通过刷新主动获取。这正是界面上要如实告诉用户的一句话。

接口 (2026-09-19 **二次**修正: 走 Cloud Agent, 不是 webchat)
-----------------------------------------------------------
    POST /console/as/conversations/    {"prompt": "你好", "model": "hy3"}
                                       → {"id": "2101201130699665408", ...}

★ 这是 master 界面 `/app/task/<id>` 对应的**唯一**体系。
  此前两版都错在打了 `/chat/` 的 webchat 接口 (`/console/webchat/*` +
  `/console/chat/completions`), 那是**另一套独立体系** —— 在里面怎么调
  都不会出现在 master 界面上, 活跃也不计入:

  | 界面    | 接口前缀             | ID 形态              | 算活跃 |
  |---------|----------------------|----------------------|--------|
  | /chat/  | /console/webchat/*   | UUID (537f7031-…)    | ✗      |
  | /app/   | /console/as/*        | 长整型 (2100473510…) | ✅     |

  ⚠️ 教训: 前两版都在"自己调用的接口回读自己造的数据"里自证成功 (循环验证),
  只有换独立路径 (master 的界面 / 官方条款) 才暴露出来。

  ⚠️ `/console/as/conversations` **不带尾斜杠 → 403 access_denied**;
     带尾斜杠才 200。旧文档记的"别走这条通道"是误判。

消耗: `model=hy3` + 极简内容 → 实测 `CapacityUsedPrecise` 保持 0 (**免费**)。

四个实测踩过的坑 (详见方案文档第三节)
--------------------------------------
  * `11101` 不支持非流式      → 必须 `"stream": true`
  * `11128` 首条必须是 system → messages[0] 必须是 system 角色
  * `11102` 缺 model 字段     → 必须带 `"model": "hy3"` (免费模型, 实测 200)
  * `403 access_denied` on /console/as/* → 请求头不完整 / 路径缺尾斜杠

★★★ 三次修正 (2026-09-21): 光建会话 ≠ 有会话记录
--------------------------------------------------
master 报「会话列表里有, 点开却没有任何会话记录」。查证结论:
**只调 `POST /console/as/conversations/` 只会建出一个空壳会话** ——
列表里有名字("你好"), 点进去一条消息都没有, 且长期卡在 `working`。
因为 agent 从头到尾**没收到任何 prompt**。

真实网页端的完整动作是**两步**:
  ① `POST /console/as/conversations/` 建会话, 响应里带 `data.session`
     (`{sessionId, sandboxId, link:"https://…/acp", token:"<agentos JWT>", cwd}`)
  ② 用 `session.token` 打 `session.link` 走 **ACP 通道** (JSON-RPC over SSE)
     把用户消息真正发进去:
        先 GET 开 SSE 流(注册连接) → initialize → session/new → session/prompt
  走完 ② 后 SSE 会推 `agent_message_chunk` (模型逐字回复), 任务转 `completed`
  —— 这时会话里才**真的有记录**。

★ `session.token` 与账号 `accessToken` 是两回事: 前者是 agentos 签发的、
  限定该 sandbox 的短时 JWT (iss=agentos)。拿账号 token 打 link 只有 401。

★ ACP 的坑 (全部实测踩过):
  * 必须先开 SSE 流注册连接, 否则 JSON-RPC 报 400 `Acp-Connection-Id required`
    或 404 `connection not found`
  * Accept 必须**同时**含 `application/json` 与 `text/event-stream`, 否则 406
  * JSON-RPC 的 POST 返回 **202**, 应答是从 SSE 流里推回来的, 不是响应体
  * POST 需要 `Acp-Connection-Id` 头, 值与 SSE 流的一致

用法
----
  python scripts/wb_web_daily.py                 # 对所有 enabled 账号发一条对话
  python scripts/wb_web_daily.py --uid <userId>  # 只跑指定账号 (可重复)
  python scripts/wb_web_daily.py --dry-run       # 只打印将要发送的请求, 不联网
  python scripts/wb_web_daily.py --text "早上好"  # 自定义对话内容
  python scripts/wb_web_daily.py --json          # 结果以 JSON 输出 (供上层调用)
  python scripts/wb_web_daily.py --force         # 忽略当日已完成状态, 强制重发
  python scripts/wb_web_daily.py --no-deliver    # 只建会话, 不投递消息 (排障用)

网络波动的补救机制 (2026-09-18 新增)
------------------------------------
积分是**隔天延迟自动入账**的, 而漏打一天的代价是那 30 分**永久拿不回来**
(客户端没有主动领取接口)。反过来, 多打一两次几乎零成本 (hy3 免费)。
所以策略刻意偏向"宁可多试":

1. **退避重试** (post_sse_retry): 瞬时故障退避 3 次 (2s/6s/18s, 合计 ≤26s);
   **4xx 业务错误不重试** —— 请求本身不对, 重试一万次也是同一个错。
   重试总时长刻意压在 Broker 的 TASK_TIMEOUT(300s) 之内, 避免任务被强杀。
2. **逐账号当日去重** (data/.wb_intl_activity_state): 成功的账号当天不再重发。
   逐账号而非全局 —— 全局标记会让一个账号的成功掩盖其它账号的失败。
3. **失败保留重试机会**: 失败不写状态, daemon 每 30 分钟的 `--wb-only` 继续补,
   直到当天成功或跨天。

退出码: 0 = 至少一个账号发送成功 / 1 = 全部失败 / 2 = 无可用账号
"""
import argparse
import json
import os
import ssl
import sys
import threading
import uuid
import time
import urllib.error
import urllib.request
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# ★ 打包态 (PyInstaller onefile) 下 __file__ 指向 %TEMP%\_MEIxxxx 解包目录,
#   在这里拼 config/logs 会让装机后必炸。一律钉死安装根 (app_paths)。
try:
    import app_paths as _ap
    OPENAI_CFG = _ap.CONFIG_PATH
    LOGS_DIR = _ap.LOGS_DIR
    DATA_DIR = _ap.DATA_DIR
except Exception:  # noqa: BLE001 — 源码态兜底
    HERE = os.path.dirname(os.path.abspath(__file__))
    OPENAI_CFG = os.path.join(HERE, '..', 'config.json')
    LOGS_DIR = os.path.join(HERE, '..', 'logs')
    DATA_DIR = os.path.join(HERE, '..', 'data')

LOG_PATH = os.path.join(LOGS_DIR, 'wb_web_daily.log')
# 当日活跃状态 (JSON, 逐账号):
#   {"day": "YYYY-MM-DD", "done": ["<userId>", ...]}
#   —— 该账号今天已成功发过对话, 当天不再重发 (除非 --force)
#
# 为什么是**逐账号**而不是一个全局 done 标记:
#   多账号里只要有一个成功, 全局标记就会把其余账号的补跑一并挡掉 ——
#   那些账号当天就永久拿不到 +30 了。逐账号记录才能让失败的继续被补。
#
# 为什么带 day 字段而不是只写 'done':
#   裸 'done' 会变成昨天的残留, 把今天的补跑永久压制
#   (2026-09-13 TRAE 上踩过这个坑, 实测全天没有一条补试)。
ACTIVITY_STATE = os.path.join(DATA_DIR, '.wb_intl_activity_state')

INTL_BASE = 'https://www.workbuddy.ai'
CHAT_PATH = '/console/chat/completions'
# ★ 2026-09-19 二次修正: 真正生效的是 **Cloud Agent** 体系 (`/console/as/`),
#   不是 webchat (`/console/webchat/`)。详见文件头「两套体系」说明。
AS_CONVERSATIONS_PATH = '/console/as/conversations/'
# 旧 webchat 路径 (保留常量仅供排障对照; 不要再用于拿活跃)
LEGACY_WEBCHAT_PATH = '/console/webchat/conversations'
DEFAULT_MODEL = 'hy3'          # 免费模型 —— 实测创建任务后 CapacityUsedPrecise=0
DEFAULT_TEXT = '你好'           # 极简内容, 进一步压低消耗 (master 指定)
DEFAULT_TIMEOUT = 60
TAG = 'WB国际活跃'

# 网页端 UA —— 必须像浏览器, 不要用 "WorkBuddyAI/5.5.2"
# (那是**客户端** UA; 网页端接口用它会露出破绽, 且 Referer/Origin 也对不上)
UA_WEB_CHAT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
               '(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36')
WEB_ORIGIN = 'https://www.workbuddy.ai'
WEB_REFERER = 'https://www.workbuddy.ai/app/'   # master 界面是 /app/ (web_agents)


def ts():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def log(msg, quiet=False):
    line = f'[{ts()}] [{TAG}] {msg}'
    if not quiet:
        print(line)
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:  # noqa: BLE001
        pass


def load_config():
    with open(OPENAI_CFG, encoding='utf-8') as f:
        return json.load(f)


def today_str():
    return time.strftime('%Y-%m-%d')


def get_activity_state():
    """读当日活跃状态 → (day, set(done userIds))。

    文件损坏 / 缺席 / 不是今天 一律返回 ('', set()) —— 即"今天还没成功过",
    宁可多发一条 (免费模型), 也不要因为状态文件坏了而漏打一天。
    """
    try:
        with open(ACTIVITY_STATE, 'r', encoding='utf-8') as f:
            d = json.load(f)
    except Exception:  # noqa: BLE001
        return '', set()
    if not isinstance(d, dict) or d.get('day') != today_str():
        return '', set()
    done = d.get('done')
    if not isinstance(done, list):
        return d.get('day') or '', set()
    return d['day'], {str(x) for x in done}


def set_activity_state(done_uids):
    """写入今天的已完成账号集合 (原子替换: 先写临时文件再 rename)。

    ★ 用临时文件 + os.replace 而不是直接覆写 —— 旧版 config.json 事故的教训:
       `open(path,'w')` 截断后若进程被杀, 留下的就是半截文件。
    """
    tmp = ACTIVITY_STATE + '.tmp'
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'day': today_str(), 'done': sorted({str(x) for x in done_uids})},
                      f, ensure_ascii=False)
        os.replace(tmp, ACTIVITY_STATE)
    except Exception:  # noqa: BLE001
        try:
            os.remove(tmp)
        except Exception:  # noqa: BLE001
            pass


def account_done_today(uid):
    """该账号今天是否已成功发过 (逐账号去重)。"""
    _, done = get_activity_state()
    return str(uid) in done


def mark_account_done(uid):
    """标记某账号今天已完成 (保留同一天的其它账号记录)。"""
    _, done = get_activity_state()
    done.add(str(uid))
    set_activity_state(done)


def intl_segment(cfg):
    """取 providers.workbuddy_intl 段 (含账号与 domain/product)。"""
    return (cfg.get('providers') or {}).get('workbuddy_intl') or {}


def build_headers(acc, domain, product, extra=None):
    """网页端请求头 —— 按 webchat SPA 的 buildAuthenticationHeader 实测产物。

    ★ 2026-09-19 修正（此前用错了一整套头）：
      从网页端真实 bundle (`/chat/` 的 webchat app.js) 逆向出 `chatV2` 的实际请求：
        headers = {
          ...buildAuthenticationHeader(context),   // 见下
          "X-Request-Id":   <uuid>,
          "X-Message-Id":   <uuid>,
          "X-Conversation-Id": <conversationId>,   // ★ 必须有真实会话
        }
      `buildAuthenticationHeader` 产出（`tokenHeader` 默认 "Authentication"）：
        { "X-Username": <userId>, "Authentication": <token>,
          "X-Domain": <domain>, ["X-Enterprise-Id": <eid>] }

      旧版只发了 `Authorization: Bearer` + `X-User-Id` + `X-Product*` ——
      服务端**照样返回 200**（它认 Bearer），所以看起来"成功"，
      但请求不落在 webchat 的会话体系里 → 网页端看不到会话、活跃不计入。
      这正是 master 观察到「没有新会话生成」的根因。

    ⚠️ 不要带 X-Enterprise-Id —— 该头会把请求切到企业视角, 个人资源包被隐藏
    (与 get-user-resource 同一个坑, 见 usage_collector.wb_today_packs)。
    """
    token = acc.get('accessToken', '')
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'text/event-stream',
        # webchat 真实鉴权头（tokenHeader 默认 "Authentication"）
        'Authentication': token,
        'Authorization': f'Bearer {token}',   # 兼容保留: 服务端两种都认
        'X-Username': str(acc.get('userId', '')),
        'X-User-Id': str(acc.get('userId', '')),
        'X-Domain': domain,
        'X-Product': product,
        'X-Product-Code': product,
        'Origin': WEB_ORIGIN,
        'Referer': WEB_REFERER,
        'User-Agent': UA_WEB_CHAT,
    }
    if extra:
        headers.update(extra)
    return headers


def build_body(text, model, conversation_id='', message_id=''):
    """构造对话请求体 —— 按 webchat `chatV2` 的真实结构。

    四个实测坑全在这里规避:
      * messages[0] 必须是 system (否则 11128)
      * stream 必须 true (否则 11101)
      * model 必须显式给出 (否则 11102)

    ★ 2026-09-19 补充: 真实网页端还带 `msgs` / `conversation_id` / `message_id` /
      `topic` / `max_tokens`。只发 `messages` 也能拿到 200, 但那不是网页端的调用形态。
    """
    msgs = [
        {'role': 'system', 'content': 'You are a helpful assistant.'},
        {'role': 'user', 'content': text},
    ]
    return {
        'max_tokens': 4096,
        'messages': msgs,
        'msgs': msgs,
        'stream': True,
        'model': model,
        'conversation_id': conversation_id,
        'message_id': message_id,
        'topic': text,
    }


def post_sse(url, headers, payload, timeout=DEFAULT_TIMEOUT):
    """POST 并读取 SSE 流。

    返回 (ok, http_code, detail):
      * ok=True   —— HTTP 200 且真的收到了 SSE 数据行 (chat.completion.chunk)
      * ok=False  —— 传输失败 / 非 200 / 收到的是业务错误码

    ⚠️ 判定"成功"必须以 HTTP 200 + 收到 SSE chunk 为准, 不能用"没抛异常"兜底 ——
    旧签到脚本曾因兜底 `{'code': 0}` 把 15s 超时记成签到成功 (2026-09-12 两条假成功)。

    本函数**只发一次**, 不做重试 —— 重试策略统一收口在 `post_sse_retry`,
    便于测试钉住「可重试 vs 不可重试」的边界。
    """
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            code = resp.status
            body = resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', 'replace')
        return False, e.code, _short_err(raw)
    except Exception as e:  # noqa: BLE001 — 超时/DNS/TLS 一律按失败处理
        return False, 0, f'{type(e).__name__}: {e}'

    if code != 200:
        return False, code, _short_err(body)
    # HTTP 200 也要确认拿到真实 SSE 数据 —— 只有 : heartbeat 不算模型应答
    got_chunk = False
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith('data:'):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == '[DONE]':
            continue
        try:
            j = json.loads(chunk)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(j, dict) and j.get('choices') is not None:
            got_chunk = True
            break
    if not got_chunk:
        return False, code, f'HTTP 200 但未收到模型应答数据: {body[:120]}'
    # ★ 成功时把**原始 SSE 正文**作为 detail 返回 —— 上层要用它解析出
    #   assistant 回复并回写会话 (第 3 步)。旧版返回 'ok' 把正文丢了,
    #   导致回写只能塞空消息。
    return True, code, body


def _short_err(raw):
    """把上游错误体压成一行可读信息 (优先取 code/message)。"""
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            c = d.get('code')
            m = d.get('message') or d.get('msg') or ''
            if c is not None or m:
                return f'code={c} message={m}'[:200]
    except Exception:  # noqa: BLE001
        pass
    return (raw or '').strip()[:200]


# ================= Cloud Agent 会话 (真正的拿分链路) =================
# ★★ 2026-09-19 二次修正 —— 前两版都找错了体系, 这一版才是对的。
#
# master 给的证据: 他在浏览器里看到的会话 URL 是
#     https://www.workbuddy.ai/app/task/2100473510743687168
# 而脚本此前一直在打 `/chat/` 那套 webchat 接口 —— **两套完全独立的体系**:
#
#   | 界面        | 接口前缀                | ID 形态                  | 是否算活跃 |
#   |-------------|-------------------------|--------------------------|-----------|
#   | /chat/      | /console/webchat/*      | UUID (537f7031-…)        | ✗ 否      |
#   | /app/       | /console/as/*           | 长整型 (2100473510…)     | ✅ 是     |
#
# 实测: master 09-17 的真实会话 `2100473510743687168` (name="你好") 在
# `GET /console/as/conversations/` 里查得到, 且 manifest 明写
# `CLIENT_INFO_PLATFORM = web_agents` / `CLIENT_INFO_IDE_TYPE = WorkBuddy_Web`。
# 而 webchat 那套接口**永远看不到它** —— 这就是"网页端没有新会话"的真相。
#
# 为什么上一版「三步链路」也是错的: 它在 webchat 体系内自洽
# (建会话→对话→回写 都 200, 回读也能看到自己造的会话),
# 但那是**循环验证** —— 用错误的体系验证错误的体系, 永远自证成功。
# 教训: 验收必须**换一条独立路径**(master 的界面 / 官方条款 / 另一个接口体系),
# 不能拿"我调用的接口回读我自己造的数据"当证据。
#
# ⚠️ `/console/as/conversations` **不带尾斜杠** → 403 access_denied;
#    带尾斜杠 `/console/as/conversations/` 才 200。旧文档把这条记成
#    "别走这条通道", 是误判 (见 MEMORY.md)。
#
# 关于消耗 (master 指定 hy3 + 「你好」的理由, 已实测):
#   用 `model=hy3` + 极简内容创建任务后, 资源包 `CapacityUsedPrecise` 仍为 0
#   —— **不消耗积分**。任务会真实执行 (起 sandbox, status: CREATING→working→completed),
#   但 hy3 是免费模型。


def json_request(method, path, headers, payload=None, timeout=DEFAULT_TIMEOUT):
    """普通 JSON 请求 (非 SSE); 返回 (ok, status, data_or_detail)。

    ok 的判定: HTTP 200 **且** 响应体是可解析 JSON (且 code==0 当该字段存在)。
    与 post_sse 同一原则 —— 不用"没抛异常"兜底。
    """
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8') \
        if payload is not None else None
    req = urllib.request.Request(INTL_BASE + path, data=data, headers=headers,
                                 method=method)
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            code, raw = resp.status, resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return False, e.code, _short_err(e.read().decode('utf-8', 'replace'))
    except Exception as e:  # noqa: BLE001
        return False, 0, f'{type(e).__name__}: {e}'

    if code != 200:
        return False, code, _short_err(raw)
    try:
        d = json.loads(raw)
    except Exception:  # noqa: BLE001
        return False, code, f'响应非 JSON: {raw[:120]}'
    if isinstance(d, dict) and d.get('code') not in (0, None):
        return False, code, _short_err(raw)
    return True, code, d


def json_request_retry(method, path, headers, payload=None,
                       timeout=DEFAULT_TIMEOUT, quiet=False, tag=''):
    """带指数退避的 JSON 请求 (Cloud Agent 链路用)。

    ★ 2026-09-19 三版接入: 改走 /console/as/ 后请求走 `json_request`,
      而非 `post_sse` —— 原 `post_sse_retry` 不再触达 Cloud Agent 路径,
      重试机制在那次改动里**静默失效**了 (只剩 SSE 旧路径在用)。
      本函数把补救机制补回 Cloud Agent 链路。

    语义与 `post_sse_retry` 一致:
      瞬时故障 (传输失败 / 5xx / 429, 见 _retryable) 退避 3 次 (2s/6s/18s);
      4xx 业务错误 (含 403 access_denied) 不重试 —— 重试一万次也是同一个错。
    """
    last = (False, 0, 'no-attempt')
    for attempt in range(len(RETRY_BACKOFF) + 1):
        ok, code, d = json_request(method, path, headers, payload, timeout)
        if ok:
            if attempt:
                log(f'{tag}第 {attempt + 1} 次尝试成功 (退避后自愈)', quiet)
            return True, code, d
        last = (False, code, d)
        if not _retryable(code):
            return last  # 业务错误: 重试无意义
        if attempt >= len(RETRY_BACKOFF):
            break
        wait = RETRY_BACKOFF[attempt]
        log(f'{tag}瞬时失败 (HTTP {code}: {d}), {wait}s 后重试 '
            f'({attempt + 1}/{len(RETRY_BACKOFF)})', quiet)
        time.sleep(wait)
    return last


def create_agent_task(acc, domain, product, prompt, model, timeout=DEFAULT_TIMEOUT,
                       quiet=False, deliver=True):
    """创建 Cloud Agent 任务并**把消息真正发进去** → 返回 (ok, taskId_or_detail)。

    这是**唯一**能产生「网页端可见且有记录的会话」的调用
    (master 界面 = /app/ = web_agents)。

    ★ 2026-09-21 三次修正: 光建会话是不够的 —— 那样只会得到一个**空壳会话**:
      列表里有名字("你好"), 点开**没有任何记录**, 且长期卡在 working。
      必须接着走 ACP 通道把 prompt 真正送进去 (见 deliver_agent_prompt)。

    body: {prompt, model} —— 真实前端 CloudConversationOps.create 的形态
    (它还支持 projectId/cwd/expertId/locale/visibility/conversationOrigin/tags,
     本脚本只需最小集)。
    """
    h = build_headers(acc, domain, product,
                      {'Accept': 'application/json, text/plain, */*'})
    # ★ 用带退避重试的版本: 创建任务是拿分关键动作, 不能因瞬时抖动整天漏打
    ok, code, d = json_request_retry('POST', AS_CONVERSATIONS_PATH, h,
                                     {'prompt': prompt, 'model': model}, timeout,
                                     quiet=quiet, tag='建任务 ')
    if not ok:
        return False, f'HTTP {code} {d}'
    data = (d or {}).get('data') or {}
    tid = str(data.get('id') or data.get('conversationId') or '')
    if not tid:
        return False, f'建任务未返回 id: {str(d)[:150]}'

    if not deliver:
        return True, tid

    # ★ 关键补步: 把消息真正送进会话, 否则就是空壳 (点开无记录)
    session = data.get('session') or {}
    if not session:
        return False, f'建任务未返回 session 块, 无法投递: {tid}'
    ok2, detail2 = deliver_agent_prompt(session, prompt, timeout, quiet=quiet)
    if not ok2:
        # 空壳会话不算成功 —— 列表里会有个点开没记录的僵尸会话
        return False, f'会话 {tid} 消息投递失败: {detail2}'
    return True, tid


# ============ ACP 会话握手 (2026-09-21 三次修正: 真正的"有记录") ============
# ★★★ 这一节修的是 master 报的「会话列表点开没有会话记录」。
#
# 现象: 只调 `POST /console/as/conversations/` 建出来的任务,
#       在 `/app/` 列表里**有名字**("你好"), 但点进去**一条消息都没有**,
#       而且长期卡在 `working` —— 因为那只是建了个**空壳会话**:
#       agent 从未收到任何 prompt, 自然也没有任何对话记录。
#
# 真相: 真实网页端建完会话后, 还会**再走一条 ACP 通道**把用户消息真正发进去。
#   建会话响应里的 `data.session` 就是这套通道的入口:
#       {"sessionId":…, "sandboxId":…, "link":"https://…/acp",
#        "token":"<agentos JWT>", "cwd":"/workspace"}
#   ★ 注意 `session.token` 与账号的 accessToken **不是**一个东西 ——
#     它是 agentos 签发的、限定该 sandbox 的短时 JWT (iss=agentos)。
#     用账号 token 打 link 只会拿到 401。
#
# ACP 通道 = JSON-RPC over SSE (Streamable HTTP), 三步:
#   1. `GET <link>` 带 `Accept: text/event-stream` 开 SSE 长连接
#      —— 这一步会**注册连接**, 必须先开, 否则后面全报 400
#         `Acp-Connection-Id required` / 404 `connection not found`。
#   2. 同一 `Acp-Connection-Id` 头下 POST JSON-RPC:
#        initialize → session/new → session/prompt
#      POST 返回 202, **真正的应答从第 1 步的 SSE 流里推回来**。
#   3. `session/prompt` 才是"真的发了消息" —— 之后:
#        SSE 推 `session_info_update` / `agent_message_chunk`(模型逐字回复)
#        / `usage_update`, 任务从 working → completed。
#
# 实测 (2026-09-21): 走完这三步后模型真的回了
#   「你好！我是 WorkBuddy，有什么可以帮你的吗？…」
# 且任务状态落到 completed —— 会话里这才**有记录**。
#
# Accept 头必须同时含 `application/json` 与 `text/event-stream`,
# 只给一个会被 406 挡掉。
ACP_RPC_TIMEOUT = 30          # 单条 JSON-RPC POST 超时
ACP_STREAM_TIMEOUT = 90       # SSE 长连接最长驻留
ACP_STREAM_WAIT = 45          # 最多等模型回完多少秒


def _acp_open_stream(link, session_token, conn_id, sink, stop):
    """开 SSE 长连接并持续读事件 (后台线程)。

    ★ 连接必须先建立: ACP 服务端靠这条流注册 `Acp-Connection-Id`,
      没这条流, 后续 JSON-RPC 一律 400/404。
    事件按行解析, 把 JSON-RPC 消息塞进 `sink` (list), 供主线程判定进度。
    """
    hdr = {
        'Authorization': f'Bearer {session_token}',
        'Accept': 'text/event-stream',
        'Acp-Connection-Id': conn_id,
        'User-Agent': UA_WEB_CHAT,
    }
    try:
        req = urllib.request.Request(link, headers=hdr)
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=ACP_STREAM_TIMEOUT,
                                    context=ctx) as resp:
            sink.append({'__stream__': 'open'})
            buf = b''
            t0 = time.time()
            while not stop.is_set() and time.time() - t0 < ACP_STREAM_TIMEOUT:
                chunk = resp.read(1)
                if not chunk:
                    break
                buf += chunk
                if not buf.endswith(b'\n\n'):
                    continue
                block, buf = buf.decode('utf-8', 'replace').strip(), b''
                if not block or block.startswith(': heartbeat'):
                    continue
                for line in block.splitlines():
                    if not line.startswith('data:'):
                        continue
                    try:
                        sink.append(json.loads(line[5:].strip()))
                    except Exception:  # noqa: BLE001
                        continue
    except Exception as e:  # noqa: BLE001 — 流断开不影响主流程判定
        sink.append({'__stream__': f'closed:{type(e).__name__}'})
    finally:
        sink.append({'__stream__': 'done'})


def _acp_rpc(link, session_token, conn_id, method, params, rpc_id,
             timeout=ACP_RPC_TIMEOUT):
    """向 ACP 通道发一条 JSON-RPC。

    ★ Accept 必须同时含两种类型 —— 只给 application/json 会被
      406 "Client must accept both application/json and text/event-stream" 拒绝。
    成功时 HTTP 202 (应答不在这里, 在 SSE 流里), 故 ok 判定放宽到 2xx。
    """
    hdr = {
        'Authorization': f'Bearer {session_token}',
        'Accept': 'application/json, text/event-stream',
        'Content-Type': 'application/json',
        'Acp-Connection-Id': conn_id,
        'User-Agent': UA_WEB_CHAT,
    }
    payload = {'jsonrpc': '2.0', 'id': rpc_id, 'method': method, 'params': params}
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(link, data=data, headers=hdr, method='POST')
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            # 202 Accepted 是正常应答; 200 也接受
            if 200 <= resp.status < 300:
                return True, ''
            return False, f'HTTP {resp.status}'
    except urllib.error.HTTPError as e:
        return False, f'HTTP {e.code} {e.read().decode("utf-8", "replace")[:120]}'
    except Exception as e:  # noqa: BLE001
        return False, f'{type(e).__name__}: {e}'


def deliver_agent_prompt(session, text, timeout=DEFAULT_TIMEOUT, quiet=False):
    """把用户消息**真正发进** Cloud Agent 会话 (ACP 三步握手)。

    入参 `session` 是建会话响应里的 `data.session` 块。
    返回 (ok, detail):
      * ok=True  —— 走完 initialize → session/new → session/prompt,
                    且**看到模型回复** (agent_message_chunk) 或至少会话转为 completed
      * ok=False —— 任一步失败 (含握手未注册 / prompt 被拒 / 全程无任何事件)

    ★ 为什么必须做这一步: 只建会话不发消息 = 空壳会话, 列表里有名字、
      点开没记录, 活跃也不计入。这才是拿分与"有记录"的充要动作。
    """
    link = session.get('link')
    stoken = session.get('token')
    if not link or not stoken:
        return False, 'session 缺 link/token (无法进入 ACP 通道)'

    conn_id = str(uuid.uuid4())
    sink = []
    stop = threading.Event()
    th = threading.Thread(target=_acp_open_stream,
                          args=(link, stoken, conn_id, sink, stop), daemon=True)
    th.start()

    # 等 SSE 注册完成 (没等到就不必往下走, 后面必 400)
    ok_stream = False
    for _ in range(40):                      # 最多等 4s
        time.sleep(0.1)
        if any(isinstance(e, dict) and e.get('__stream__') == 'open' for e in sink):
            ok_stream = True
            break
    if not ok_stream:
        stop.set()
        return False, 'ACP 流未建立 (连接未注册)'

    ok, err = _acp_rpc(link, stoken, conn_id, 'initialize', {
        'protocolVersion': 1,
        'capabilities': {},
        'clientInfo': {'name': 'workbuddy-web', 'version': '1.0'},
    }, 1)
    if not ok:
        stop.set()
        return False, f'initialize 失败: {err}'
    time.sleep(0.8)

    ok, err = _acp_rpc(link, stoken, conn_id, 'session/new',
                       {'cwd': session.get('cwd') or '/workspace',
                        'mcpServers': []}, 2)
    if not ok:
        stop.set()
        return False, f'session/new 失败: {err}'
    time.sleep(1.5)

    # 取服务端回的 sessionId (没有就退回建会话时的 id)
    sid = ''
    for e in sink:
        if isinstance(e, dict) and e.get('id') == 2 and isinstance(e.get('result'), dict):
            r = e['result']
            sid = r.get('sessionId') or (r.get('session') or {}).get('id') or ''
            if sid:
                break
    if not sid:
        sid = str(session.get('sessionId') or '')

    ok, err = _acp_rpc(link, stoken, conn_id, 'session/prompt', {
        'sessionId': sid,
        'prompt': [{'type': 'text', 'text': text}],
    }, 3)
    if not ok:
        stop.set()
        return False, f'session/prompt 失败: {err}'

    # 等模型应答 —— 出现 agent_message_chunk 即证明消息真的被处理了。
    # ★ 判定用"看到内容"而不是"状态变 completed": 状态是**异步**落库的
    #   (实测 completed 可能滞后数秒到数分钟), 拿状态当唯一条件会误判失败。
    got_chunk = False
    got_done = False
    deadline = time.time() + ACP_STREAM_WAIT
    while time.time() < deadline:
        time.sleep(0.5)
        for e in sink:
            if not isinstance(e, dict) or 'method' not in e:
                continue
            upd = (e.get('params') or {}).get('update') or {}
            kind = upd.get('sessionUpdate')
            if kind == 'agent_message_chunk':
                got_chunk = True
            meta = (upd.get('_meta') or {}).get('codebuddy.ai') or {}
            if meta.get('status') == 'completed':
                got_done = True
        # 拿到模型回复即可收工 (不必等 completed 状态落库)
        if got_chunk:
            break
    stop.set()
    time.sleep(0.3)

    if got_chunk:
        return True, 'prompt 已送达, 模型已应答 (会话有记录)'
    if got_done:
        return True, 'prompt 已送达, 会话已 completed'
    return False, 'prompt 已发送但未观测到模型应答'


def get_agent_task(acc, domain, product, task_id, timeout=DEFAULT_TIMEOUT):
    """读 Cloud Agent 任务详情 → (ok, dict_or_detail)。验收用。"""
    h = build_headers(acc, domain, product,
                      {'Accept': 'application/json, text/plain, */*'})
    ok, code, d = json_request('GET', f'{AS_CONVERSATIONS_PATH}{task_id}', h,
                               None, timeout)
    if not ok:
        return False, f'HTTP {code} {d}'
    return True, ((d or {}).get('data') or {})


def list_agent_tasks(acc, domain, product, timeout=DEFAULT_TIMEOUT):
    """列出 Cloud Agent 任务 (验收用: 证明任务真的存在)。

    ⚠️ 路径**必须带尾斜杠** —— 不带会 403 access_denied
       (`/console/as/conversations` → 403, `/console/as/conversations/` → 200)。
    """
    h = build_headers(acc, domain, product,
                      {'Accept': 'application/json, text/plain, */*'})
    ok, code, d = json_request('GET', AS_CONVERSATIONS_PATH, h, None, timeout)
    if not ok:
        return False, f'HTTP {code} {d}'
    convs = ((d or {}).get('data') or {}).get('conversations') or []
    return True, convs


def _parse_sse_reply(raw):
    """从 SSE 流里拼出 assistant 正文 (用于回写会话)。"""
    out = []
    for line in (raw or '').splitlines():
        line = line.strip()
        if not line.startswith('data:'):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == '[DONE]':
            continue
        try:
            j = json.loads(chunk)
        except Exception:  # noqa: BLE001
            continue
        for ch in (j.get('choices') or []) if isinstance(j, dict) else []:
            delta = ch.get('delta') or {}
            piece = delta.get('content')
            if piece:
                out.append(piece)
    return ''.join(out)


# ================= 网络波动补救 =================
# 为什么要有这一层
# ----------------
# 积分是**隔天延迟自动入账**的, 而漏打一天的代价是那 30 分**永久拿不回来**
# (客户端没有主动领取接口, 无法补领)。反过来, 多打一两次的代价几乎为零
# (hy3 是免费模型)。所以策略明确偏向"宁可多试"。
#
# 重试分两类, 边界必须清楚:
#   * **可重试** —— 传输层失败 (超时/DNS/TLS/连接重置) 与 5xx/429。
#     这类是瞬时抖动, 隔几秒大概率就好了。
#   * **不可重试** —— 4xx 业务错误。11101/11128/11102 都是**请求本身**不对,
#     重试一万次也是同一个错; 403 是通道/权限问题。重试只会白等并刷日志。
#
# 退避 3 次: 2s → 6s → 18s (合计最多 26s)。最坏情况叠加 60s 超时, 单账号
# 上限约 3.5 分钟, 仍在 Broker 的 TASK_TIMEOUT(300s) 之内 —— 这点很关键:
# 若重试拖过 300s, 整个 signin 任务会被 Broker 杀掉, 反而更糟。
RETRY_BACKOFF = (2, 6, 18)
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def _retryable(code):
    """这个失败值不值得重试。

    code==0 是传输层失败 (见 post_sse 的兜底分支), 一律重试;
    其余只看状态码是否落在 RETRYABLE_STATUS。
    4xx 业务错误 (含 11101/11128/11102/403) 一律不重试。
    """
    if code == 0:
        return True
    return code in RETRYABLE_STATUS


def post_sse_retry(url, headers, payload, timeout=DEFAULT_TIMEOUT,
                   quiet=False, tag=''):
    """带指数退避的 POST; 返回 (ok, code, detail)。

    只对**瞬时故障**重试 (见 _retryable)。业务错误立即返回 —— 重试无意义,
    且会拖长任务时长、逼近 Broker 的 300s 超时。
    """
    last = (False, 0, 'no-attempt')
    for attempt in range(len(RETRY_BACKOFF) + 1):
        ok, code, detail = post_sse(url, headers, payload, timeout)
        if ok:
            if attempt:
                log(f'{tag}第 {attempt + 1} 次尝试成功 (退避后自愈)', quiet)
            # ★ 原样返回 post_sse 的 detail —— 那是 SSE 正文, 上层要用它
            #   解析 assistant 回复来回写会话。不要替换成 'ok'。
            return True, code, detail
        last = (ok, code, detail)
        if not _retryable(code):
            return last  # 业务错误: 重试无意义
        if attempt >= len(RETRY_BACKOFF):
            break
        wait = RETRY_BACKOFF[attempt]
        log(f'{tag}瞬时失败 (HTTP {code}: {detail[:80]}), {wait}s 后重试 '
            f'({attempt + 1}/{len(RETRY_BACKOFF)})', quiet)
        time.sleep(wait)
    return last


def run_one(acc, domain, product, text, model, timeout, dry_run=False, quiet=False,
            deliver=True):
    """对一个账号创建一次 **Cloud Agent 任务**并把消息真正送进会话
    (= master 界面 `/app/` 里那个"会话")。

    返回 (ok, detail)。

    ★ 为什么是「创建会话 + 投递消息」两步, 而不是只创建:
      master 的界面是 `/app/` (web_agents), 它下面的"会话"就是 Cloud Agent 任务。
      只调建会话接口会得到**空壳**: 列表里有名字, 点开没有记录, 活跃也不计入
      (2026-09-21 master 实报)。必须再走 ACP 把手艺消息发进去
      (`deliver_agent_prompt`), 会话才有记录。
      而 webchat 路线 (`/chat/` + `/console/webchat/*`) 是**另一套独立体系**,
      在里面怎么调都不会出现在 master 的界面上。

    ★ 消耗: 用 `model=hy3` + 极简内容, 实测 `CapacityUsedPrecise` 保持 0 —— 免费。
      任务仍会真实执行 (起 sandbox, CREATING → working → completed),
      这正是"真实使用行为"的形态。
    """
    uid = str(acc.get('userId') or '?')
    short = uid[:8]
    if acc.get('enabled') is False:
        log(f'账号{short} enabled=false, 跳过', quiet)
        return False, 'skipped(disabled)'
    if not acc.get('accessToken'):
        log(f'账号{short} 无 accessToken, 跳过 (需重新登录)', quiet)
        return False, 'skipped(no-token)'

    if dry_run:
        log(f'账号{short} [dry-run] POST {AS_CONVERSATIONS_PATH} '
            f'model={model} prompt={text!r} + ACP 投递', quiet)
        return True, 'dry-run'

    ok, tid = create_agent_task(acc, domain, product, text, model, timeout,
                                quiet=quiet, deliver=deliver)
    if not ok:
        log(f'账号{short} 创建/投递 Agent 会话失败: {tid}', quiet)
        return False, f'create-failed {tid}'

    # 回读确认任务真的落库 (而不是只拿到一个 200) —— 换独立路径验收
    ok2, task = get_agent_task(acc, domain, product, tid, timeout)
    status = (task or {}).get('status') if ok2 else '?'
    if not ok2:
        log(f'账号{short} 会话已创建({tid}) 但回读失败: {task}', quiet)
    log(f'账号{short} Agent 会话已创建并投递 {tid} (status={status}) '
        f'— 活跃由服务端延迟结算, 隔天入账', quiet)
    return True, f'ok task={tid}'


def main(argv=None):
    ap = argparse.ArgumentParser(description='WorkBuddy 国际版网页端每日活跃')
    ap.add_argument('--uid', action='append', default=[],
                    help='只跑指定 userId (可重复; 缺省=全部 enabled 账号)')
    ap.add_argument('--text', default=DEFAULT_TEXT, help='对话内容')
    ap.add_argument('--model', default=DEFAULT_MODEL, help='模型 (缺省 hy3)')
    ap.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument('--dry-run', action='store_true', help='只打印请求, 不联网')
    ap.add_argument('--json', action='store_true', help='以 JSON 输出结果')
    ap.add_argument('--quiet', action='store_true', help='不打印到 stdout (仍写日志)')
    ap.add_argument('--force', action='store_true',
                    help='忽略当日已成功状态, 强制再发一次')
    ap.add_argument('--no-deliver', dest='deliver', action='store_false',
                    help='只建会话、不通过 ACP 投递消息 (排障用; 会留下空壳会话)')
    args = ap.parse_args(argv)

    quiet = args.quiet or args.json

    # 当日去重的粒度是**逐账号** (见下面循环), 这里不做整体跳过 ——
    # 用一个全局"今天已完成"标记会把还没成功的账号一并挡掉, 那些账号
    # 当天就永久拿不到 +30。--dry-run 不写状态, 故演练不会污染当天判定。

    try:
        cfg = load_config()
    except Exception as e:  # noqa: BLE001
        log(f'读取 config.json 失败: {e}', quiet)
        return 2

    seg = intl_segment(cfg)
    domain = seg.get('domain', 'www.workbuddy.ai')
    product = seg.get('product', 'workbuddy-ai')
    accounts = seg.get('accounts') or []
    if args.uid:
        want = {str(u) for u in args.uid}
        accounts = [a for a in accounts if str(a.get('userId')) in want]
    if not accounts:
        log('providers.workbuddy_intl.accounts 为空 (或 --uid 无匹配), 无可跑账号',
            quiet)
        if args.json:
            print(json.dumps({'ok': False, 'results': [], 'reason': 'no-account'},
                             ensure_ascii=False))
        return 2

    results = []
    for acc in accounts:
        uid = str(acc.get('userId') or '')
        # 逐账号去重: 已成功的跳过, 失败的继续 (daemon 30 分钟一轮会再来)
        if not args.dry_run and not args.force and account_done_today(uid):
            log(f'账号{uid[:8]} 今日已完成, 跳过 (--force 可强制重发)', quiet)
            results.append({'userId': uid, 'name': acc.get('name') or '',
                            'ok': True, 'detail': 'skipped(already-done)'})
            continue
        ok, detail = run_one(acc, domain, product, args.text, args.model,
                             args.timeout, dry_run=args.dry_run, quiet=quiet,
                             deliver=args.deliver)
        if ok and not args.dry_run:
            mark_account_done(uid)
        results.append({
            'userId': uid,
            'name': acc.get('name') or '',
            'ok': ok,
            'detail': detail,
        })

    sent = [r for r in results if r['ok']]
    failed = [r for r in results if not r['ok'] and not r['detail'].startswith('skipped')]
    if args.json:
        print(json.dumps({
            'ok': bool(sent) and not failed,
            'sent': len(sent), 'failed': len(failed), 'total': len(results),
            'results': results,
        }, ensure_ascii=False))
    else:
        log(f'完成: 成功 {len(sent)} / 发送失败 {len(failed)} / 共 {len(results)} 个账号',
            quiet)

    if not sent:
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
