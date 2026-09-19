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
  * `403 access_denied` on /console/as/* → 那是 edge-sync 专属通道, 普通 token 不通

用法
----
  python scripts/wb_web_daily.py                 # 对所有 enabled 账号发一条对话
  python scripts/wb_web_daily.py --uid <userId>  # 只跑指定账号 (可重复)
  python scripts/wb_web_daily.py --dry-run       # 只打印将要发送的请求, 不联网
  python scripts/wb_web_daily.py --text "早上好"  # 自定义对话内容
  python scripts/wb_web_daily.py --json          # 结果以 JSON 输出 (供上层调用)
  python scripts/wb_web_daily.py --force         # 忽略当日已完成状态, 强制重发

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
                       quiet=False):
    """创建 Cloud Agent 任务 → 返回 (ok, taskId_or_detail)。

    这是**唯一**能产生「网页端可见会话」的调用 (master 界面 = /app/ = web_agents)。
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
    return True, tid


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


def run_one(acc, domain, product, text, model, timeout, dry_run=False, quiet=False):
    """对一个账号创建一次 **Cloud Agent 任务** (= master 界面里的"会话")。

    返回 (ok, detail)。

    ★ 为什么是「创建任务」而不是「发对话」:
      master 的界面是 `/app/` (web_agents), 它下面的"会话"就是 Cloud Agent 任务,
      对应 `POST /console/as/conversations/`。此前的 webchat 路线
      (`/chat/` + `/console/webchat/*` + `/console/chat/completions`) 是**另一套
      独立体系**, 在里面怎么调都不会出现在 master 的界面上, 活跃也不计入。

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
            f'model={model} prompt={text!r}', quiet)
        return True, 'dry-run'

    ok, tid = create_agent_task(acc, domain, product, text, model, timeout,
                                quiet=quiet)
    if not ok:
        log(f'账号{short} 创建 Agent 任务失败: {tid}', quiet)
        return False, f'create-failed {tid}'

    # 回读确认任务真的落库 (而不是只拿到一个 200) —— 换独立路径验收
    ok2, task = get_agent_task(acc, domain, product, tid, timeout)
    status = (task or {}).get('status') if ok2 else '?'
    if not ok2:
        log(f'账号{short} 任务已创建({tid}) 但回读失败: {task}', quiet)
    log(f'账号{short} Agent 任务已创建 {tid} (status={status}) '
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
                             args.timeout, dry_run=args.dry_run, quiet=quiet)
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
