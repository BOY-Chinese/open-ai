#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 账号管理器 — 显示各账号积分 / 添加账号
=========================================
数据源:
  TRAE      : open-ai/config.json  providers.trae.accounts[]  积分接口 /trae/api/v2/pay/ide_user_ent_usage
  WorkBuddy : open-ai/config.json  providers.workbuddy.accounts[]  积分接口 /billing/meter/get-user-resource
  WB 国际版 : open-ai/config.json  providers.workbuddy_intl.accounts[]  (host=www.workbuddy.ai)

功能:
  [1] 刷新积分    — 重新查询 TRAE + WorkBuddy + 国际版 所有账号积分
  [2] 添加 TRAE 账号   — 打开网页登录, 自动抓 token 入池 (login_trae.py)
  [3] 添加 WorkBuddy 账号 — 打开网页登录, 自动抓 token 入池 (login_workbuddy.py)
  [9] 添加 WorkBuddy 国际版账号 — 网页登录, 入池 (login_workbuddy_intl.py)
  [Q] 退出

用法:  python account_manager.py [--once]   # --once 显示一次积分后直接退出
"""
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# ★ 打包态 (PyInstaller onefile) __file__ 指向 %TEMP%\_MEIxxxx 解包临时目录,
#   用它拼 config/脚本路径会读错位置 (bug 家族 #4/#5, 见 MEMORY.md)。
#   一律钉死安装根 (app_paths) 再派生; 源码态走 except 兜底, 行为不变。
try:
    import app_paths as _ap
    OPENAI_ROOT = _ap.ROOT
except Exception:  # 源码态: __file__ 是绝对路径, 兜底安全
    OPENAI_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(OPENAI_ROOT, 'scripts')  # = 本文件所在 scripts/ 目录 (打包态不漂移)
# open-ai 自包含: 所有配置统一在 open-ai/config.json
OPENAI_CFG = os.path.join(OPENAI_ROOT, 'config.json')
OPENAI_VENV_PY = os.path.join(OPENAI_ROOT, '.venv', 'Scripts', 'python.exe')

# ---- v2.4 进程品牌化: 短命脚本用 task shim (任务管理器显示 open-ai 品牌) ----
OPENAI_TASK_SHIM = os.path.join(OPENAI_ROOT, 'runtime', 'Scripts',
                                'open-ai-task.exe')
# 打包态没有 runtime\ 这一层, 品牌 exe 直接躺在安装根
OPENAI_TASK_SHIM_ROOT = os.path.join(OPENAI_ROOT, 'open-ai-task.exe')


def _task_shim():
    """品牌载体路径 (源码态 runtime\\Scripts 或打包态安装根); 都没有则空串。

    与 admin_api._task_shim() 同策略 —— 两处必须一致, 否则同一个脚本
    在不同入口下会显示成不同进程名。
    """
    for p in (OPENAI_TASK_SHIM, OPENAI_TASK_SHIM_ROOT):
        if os.path.isfile(p):
            return p
    return ''


def _script_py():
    """优先 open-ai-task.exe shim (品牌化), 回退 venv python。"""
    shim = _task_shim()
    if shim:
        return shim
    if os.path.isfile(OPENAI_VENV_PY):
        return OPENAI_VENV_PY
    return sys.executable

UA_WB = 'WorkBuddy/5.3.12 WorkBuddy/5.3.12 CLI/2.115.0'


def ts():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def log(msg):
    print(f'[{ts()}] {msg}')


def post_json(url, headers, body=b'{}', timeout=15, method='POST'):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')
    except Exception as e:
        return 0, str(e)


# ============ TRAE 积分 ============

def trae_credits(acc, device_id):
    code, raw = post_json(
        'https://api.trae.cn/trae/api/v2/pay/ide_user_ent_usage',
        {
            'Authorization': f"Cloud-IDE-JWT {acc.get('token', '')}",
            'x-device-id': device_id,
            'Content-Type': 'application/json',
            'User-Agent': 'TRAE SOLO CN/2.3.70844',
        },
        json.dumps({'require_usage': True, 'req_source': 2}).encode())
    if code != 200:
        return None, None, f'HTTP {code}'
    try:
        d = json.loads(raw)
    except Exception:
        return None, None, '响应解析失败'
    # 分类: product_id==209 -> Work 池; 其他 -> 通用池 (app.asar/抓包逆向确认)
    general = work = 0.0
    for p in d.get('user_entitlement_pack_list') or []:
        bi = p.get('entitlement_base_info') or {}
        limit = (bi.get('quota') or {}).get('credits_limit') or 0
        if limit <= 0:
            continue
        used = (p.get('usage') or {}).get('credits_amount') or 0
        remain = limit - used
        if (bi.get('product_id') or 0) == 209:
            work += remain
        else:
            general += remain
    return general, work, None


SERVER_ADMIN = 'http://127.0.0.1:18787/v1/admin/accounts'


# ============ 模型列表 + 积分消耗倍率 ============
# 说明: "积分消耗速度" = UI 上模型名称右侧显示的倍率(x)。
#   - TRAE      : batch_get_detail_param 响应的 display_contact_config.consumption_rate.data.rate
#   - WorkBuddy : GET /v2/enterprises/personal/models 的模型 credits 字段 (形如 "x0.17")


def _fsat(n):
    try:
        return float(n)
    except (TypeError, ValueError):
        return 0.0


def _trae_headers(token, device_id):
    return {
        'Content-Type': 'application/json',
        'request-traffic-type': 'prod',
        'User-Agent': 'TraeClient/TTNet',
        'x-app-id': '6eefa01c-1036-4c7e-9ca5-d891f63bfcd8',
        'x-device-id': device_id,
        'x-ide-token': token,
        'x-bridge-transport': 'aha',
        'x-machine-id': 'a87a98343e9bf53b5497aa8984b3773b0d4fd1d7e75e3eb2cf6e94e3f3603a12',
        'x-ide-version': '0.1.50',
        'x-ide-version-code': '20260811',
        'x-ide-version-type': 'stable',
        'x-lgw-req-sdk-type': '3',
        'x-lscbd-aid': '787976',
        'x-lscbd-platform': 'windows',
        'x-ss-dp': '787976',
        'package-type': 'stable_cn',
        'x-os-version': 'Windows 11 Pro',
        'x-device-brand': 'MEOW R16 Pro',
        'x-device-cpu': 'AMD',
        'x-device-type': 'windows',
        'app-version': '0.1.50',
        'Accept': '*/*',
        'referer': 'https://trae-api-cn.mchost.guru/api/ide/v1/batch_get_detail_param',
    }


def trae_model_rates():
    """拉取 TRAE 全部模型及其积分倍率。
    返回 [(config_name, display_name, model_name, rate, err_or_None)], err 非 None 表示整体失败。
    上游会为同一模型返回多个内部功能配置 (如 refactor_scoper/finder/planner/incrementer
    四条管道配置、glm-5.2_advisor_* 变体等), 它们的展示名与上游模型 id 完全相同,
    按 (display_name, model_name) 归并只保留一条 (倍率取组内已知值), 避免列表重复显示。"""
    try:
        with open(OPENAI_CFG, encoding='utf-8') as _f:
            cfg = json.load(_f)
    except Exception as e:
        return None, f'config 读取失败: {e}'
    trae = (cfg.get('providers') or {}).get('trae') or {}
    accs = trae.get('accounts') or []
    device_id = trae.get('device_id') or (trae.get('headers') or {}).get('x-device-id', '')
    if not accs:
        return None, '无 TRAE 账号'
    acc = accs[0]
    token = acc.get('token', '')
    body = {
        'functions': ['builder', 'builder_v3', 'chat_v3', 'code_reviewer',
                      'code_review_summary', 'refactor', 'solo_agent',
                      'solo_agent_lite', 'multimodal', 'voice_chat',
                      'voice_transcription', 'voice_summary', 'assistant'],
        'agent_type': '',
        'current_config_info': {'config_name': '', 'is_custom_model': False},
        'mode_type': 0, 'access_type': 1,
        'ab_force_vids': '', 'ab_autotest_advanced_mode': 0,
        'show_custom_model': True,
    }
    code, raw = post_json('https://api5-normal.mchost.guru/api/ide/v1/batch_get_detail_param',
                          _trae_headers(token, device_id),
                          json.dumps(body).encode(), timeout=30)
    if code != 200:
        return None, f'HTTP {code}'
    try:
        d = json.loads(raw)
    except Exception:
        return None, '响应解析失败'
    cfg = d.get('data', d) if isinstance(d, dict) else d
    lists = []
    for k in ('function_configs', 'param_config_list', 'config_list'):
        if isinstance(cfg, dict) and isinstance(cfg.get(k), list):
            lists = cfg[k]
            break
    if not lists:
        return None, f"响应无模型列表: {str(d)[:80]}"
    items = []
    seen = set()
    for item in lists:
        if not isinstance(item, dict):
            continue
        configs = item.get('config_info_list')
        if configs is None:
            configs = [item]
        for ci in configs:
            if not isinstance(ci, dict):
                continue
            name = ci.get('config_name', '')
            display = (ci.get('display_config') or {}).get('display_name') or name
            # 倍率: display_contact_config 是 JSON 字符串
            rate = None
            dcc = ci.get('display_contact_config') or ''
            if isinstance(dcc, str):
                try:
                    dccj = json.loads(dcc)
                    cd = (dccj.get('consumption_rate') or {}).get('data') or {}
                    r = cd.get('rate')
                    if r is not None:
                        rate = _fsat(r)
                except Exception:
                    pass
            elif isinstance(dcc, dict):
                cd = (dcc.get('consumption_rate') or {}).get('data') or {}
                r = cd.get('rate')
                if r is not None:
                    rate = _fsat(r)
            # 单个模型 (config) 优先取第一个 model_detail 的 model_name
            mdl_name = name
            mdl_list = ci.get('model_detail_list') or []
            if mdl_list and isinstance(mdl_list[0], dict):
                mdl_name = mdl_list[0].get('model_name') or name
            key = (name, mdl_name)
            if key in seen:
                continue
            seen.add(key)
            items.append((name, display, mdl_name, rate, None))
    # ---- 按 (展示名, 上游模型名) 归并去重 (展示名比较不区分大小写) ----
    # 上游同一模型会带多个内部功能配置 (refactor_* 管道、*_advisor* 变体、
    # summary_mobile、code-review-judge 等), 展示名与上游 id 完全相同;
    # 有的还只差大小写 (官方 "GLM-4.7" vs 内部 "glm-4.7")。归并为一条,
    # 保留最"正式"的: 倍率已知优先 → config_name 最短优先 → 先出现优先;
    # 展示名实质不同 (同一上游 id 服务不同入口, 如 Seed-Code 与 summary) 不归并。
    merged = {}
    for name, disp, mdl_name, rate, _e in items:
        k = ((disp or '').strip().lower(), mdl_name)
        cur = merged.get(k)
        if cur is None:
            merged[k] = [name, disp, mdl_name, rate]
            continue
        if (rate is not None and cur[3] is None) or \
           (rate is not None and cur[3] is not None and len(name) < len(cur[0])) or \
           (rate is None and cur[3] is None and len(name) < len(cur[0])):
            merged[k] = [name, disp, mdl_name, rate]
    items = [tuple(v) + (None,) for v in merged.values()]
    if not items:
        return None, f'未解析到模型: {str(d)[:80]}'
    return items, None


def _wb_family_model_rates(provider_key, host, default_domain, default_product,
                           known_extra=None):
    """WorkBuddy 系 (国内/国际) 模型目录拉取 —— 主源 GET /v3/config。

    ★ 2026-09-18 换源 (master 反馈「国际版 gpt-6-astra 倍率为 0」) ★
    ------------------------------------------------------------------
    原先只用 GET /v2/enterprises/personal/models (下称「目录接口」)。实测该接口
    **不是客户端真正用的数据源**, 它有两个致命缺陷:

      1. **模型不全**: 国际版目录只返回 18 个, 而客户端实际展示 22 个。
         `gpt-6-astra` 根本不在目录里 —— 代码只好在 KNOWN_EXTRA 里硬编码补一条,
         且因目录没有它的 credits, 补出来的倍率是 None → 界面显示 0.00。
         但客户端截图里它明明写着 **6.67x**。
      2. **倍率会过期**: 目录里 hy4-preview 是 'x0.00', 而 v3/config 里是
         'x0.29' —— 目录那份是旧的。

    真正的主源是 GET /v3/config → data.models[].credits, 实测:
        gpt-6-astra          'x6.67'   ← 与客户端截图 6.67x 完全一致
        deepseek-v4.1-flash  'x0.00'   ← 与截图 0.00x 一致 (真·0, 非未知)
    国内版同接口更全: 52 个模型 vs 目录接口 30 个 (差额里含 glm-5.0-turbo /
    minimax-m2.7 / hy4-preview-dev 等目录漏掉的模型)。

    因此本函数改为: **v3/config 优先, 失败再回退目录接口**(保持旧行为不劣化)。
    另顺带取 data.modelPromotions 里的限免活动 (badge 'Free now', factor 0) ——
    它与截图上的红色「Free now」角标同源, 但注意: 活动只是**展示层**的营销
    信息, 其 modelIds 的 credits 本来就已经是 'x0.00', 故不参与倍率计算,
    仅在限免模型的显示名后加标记, 避免与「未知(--)」混淆。

    known_extra: 两源都不返回但可直接调用的模型 (id -> 显示名), 兜底合并。
    返回 [(model_id, display_name, credits, err_or_None)], err 非 None 表示整体失败。
    """
    try:
        with open(OPENAI_CFG, encoding='utf-8') as _f:
            cfg = json.load(_f)
    except Exception as e:
        return None, f'config 读取失败: {e}'
    prov = (cfg.get('providers') or {}).get(provider_key) or {}
    accs = prov.get('accounts') or []
    if not accs:
        return None, f'无 WorkBuddy 账号 ({provider_key})'
    acc = accs[0]
    domain = prov.get('domain', default_domain)
    product = prov.get('product', default_product)
    if not acc.get('accessToken'):
        return None, '账号缺 accessToken'
    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'Authorization': f"Bearer {acc.get('accessToken', '')}",
        # ★ X-User-Id 必须**非空**, 否则 /v3/config 直接返回 models:null ★
        # 实测 (2026-09-18): 该头才是 /v3/config 出数据的开关 ——
        #   X-User-Id 缺失或空串  → models: null (拿不到任何模型与倍率)
        #   X-User-Id 任意非空值  → 22 个模型 (值真假不影响, Authorization 也不参与校验)
        # 账号里缺 userId 时若不兜底, 这里会发一个空头, 结果静默变成「无倍率」。
        # 故给一个非空占位值 —— 只为过开关, 不代表任何真实身份。
        'X-User-Id': acc.get('userId') or 'open-ai',
        'X-Domain': domain,
        'X-Product': product,
        'User-Agent': UA_WB,
        'Accept-Language': 'zh',
    }
    if host.endswith('workbuddy.ai'):
        headers['X-Product-Code'] = product

    def _fetch(path):
        """GET 一个端点并返回解析后的 data 段 (失败返回 None)。"""
        try:
            code, raw = post_json(f'https://{host}{path}', headers,
                                  None, timeout=20, method='GET')
            if code != 200:
                return None
            d = json.loads(raw)
        except Exception:  # noqa: BLE001
            return None
        return d.get('data') if isinstance(d, dict) else None

    # ── 主源: /v3/config (客户端真正用的那份, 模型最全、倍率最新) ──
    models = None
    promotions = {}
    cfg_data = _fetch('/v3/config')
    if isinstance(cfg_data, dict) and isinstance(cfg_data.get('models'), list):
        models = cfg_data['models']
        # 限免活动 (badge 'Free now'): 仅用于给显示名加标记, 不参与倍率计算 ——
        # 其 credits 本身已是 'x0.00', 改倍率反而会掩盖真值。
        for p in (cfg_data.get('modelPromotions') or []):
            if not isinstance(p, dict) or not p.get('enabled'):
                continue
            if (p.get('discount') or {}).get('factor') != 0:
                continue
            for mid in (p.get('modelIds') or []):
                promotions[mid] = (p.get('badge') or {}).get('label') or 'Free'

    # ── 回退: /v2/enterprises/personal/models (旧主源; v3/config 不可用时沿用) ──
    if models is None:
        dir_data = _fetch('/v2/enterprises/personal/models')
        if isinstance(dir_data, dict) and isinstance(dir_data.get('models'), list):
            models = dir_data['models']
        else:
            return None, f'HTTP/解析失败 ({host})'

    items = []
    for m in models:
        if not isinstance(m, dict):
            continue
        mid = m.get('id', '')
        if not mid:
            continue
        name = m.get('name') or mid
        credits = m.get('credits')
        # credits 为空字符串 = 上游对该模型未标注倍率 (如国内版 auto / 各类
        # 内部 completion 模型)。**保持 None**, 由 admin_api 转成「未知」哨兵 ——
        # 不能当成 0, 那正是本次要修的「未知显示成 0」。注意这与 'x0.00'
        # (上游明确写 0) 是两回事, 后者仍应正常显示 0.00。
        if credits == '':
            credits = None
        if mid in promotions:
            name = f'{name} ({promotions[mid]})'
        items.append((mid, name, credits, None))
    # 两源都没返回、但可直接调用的已知模型 (现在的 v3/config 已覆盖 astra,
    # 保留此兜底是为了上游接口再变时不至于连条目一起消失)
    known_ids = {i[0] for i in items}
    items.extend((mid, name, None, None)
                 for mid, name in (known_extra or {}).items() if mid not in known_ids)
    if not items:
        return None, '未解析到模型'
    return items, None


def wb_model_rates():
    """拉取 WorkBuddy 全部模型及其积分倍率 (模型 credits 字段, 形如 "x0.17")。
    返回 [(model_id, display_name, credits, err_or_None)], err 非 None 表示整体失败。"""
    return _wb_family_model_rates('workbuddy', 'copilot.tencent.com',
                                  'www.codebuddy.cn', 'SaaS')


def intl_model_rates():
    """拉取 WorkBuddy 国际版 (www.workbuddy.ai) 全部模型及其积分倍率。
    返回 [(model_id, display_name, credits, err_or_None)]"""
    # 兜底条目: 主源 /v3/config 现在已包含这两个模型 (且带真实倍率:
    # astra=x6.67 / deepseek-v4.1-flash=x0.00), 此处仅在主源与目录接口
    # **双双**不可用时保证条目不消失 —— 那种情况下倍率为 None(未知),
    # 由 admin_api 转成哨兵显示为「--」, 不再冒充 0 倍率。
    KNOWN_EXTRA = {
        'deepseek-v4.1-flash': 'DeepSeek V4.1 Flash',
        'gpt-6-astra': 'GPT-6 Astra',
    }
    return _wb_family_model_rates('workbuddy_intl', 'www.workbuddy.ai',
                                  'www.workbuddy.ai', 'workbuddy-ai',
                                  known_extra=KNOWN_EXTRA)


def show_model_rates():
    """控制台: 列出 TRAE + WorkBuddy + WorkBuddy 国际版 全部模型及倍率。"""
    print()
    print('=== TRAE 模型 + 积分倍率 ===')
    items, err = trae_model_rates()
    if err:
        print(f'  获取失败: {err}')
    else:
        if items:
            for name, disp, mdl, rate, e in items:
                if e or rate is None:
                    print(f'  {disp:<24} (倍率未知)   [{mdl}]')
                else:
                    print(f'  {disp:<24} {rate:>6.2f}x   [{mdl}]')
    print()
    print('=== WorkBuddy 模型 + 积分倍率 ===')
    items, err = wb_model_rates()
    if err:
        print(f'  获取失败: {err}')
    else:
        if items:
            for mid, name, credits, e in items:
                if credits is None:
                    print(f'  {name:<22}  (无倍率)')
                else:
                    print(f'  {name:<22} {credits}')
    print()
    print('=== WorkBuddy 国际版 模型 + 积分倍率 ===')
    items, err = intl_model_rates()
    if err:
        print(f'  获取失败: {err}')
    else:
        if items:
            for mid, name, credits, e in items:
                if credits is None:
                    print(f'  {name:<22}  (无倍率)')
                else:
                    print(f'  {name:<22} {credits}')
    print()


def server_invalid_map():
    """从运行中的 server.js 查询哪些账号被标记为[已失效] (仅内存标记, 不删账号)。"""
    try:
        req = urllib.request.Request(SERVER_ADMIN, method='GET')
        with urllib.request.urlopen(req, timeout=5) as resp:
            d = json.loads(resp.read().decode('utf-8', 'replace'))
        return {a.get('uid'): bool(a.get('invalid')) for a in (d.get('accounts') or [])}
    except Exception:
        return {}


def show_trae_accounts():
    try:
        with open(OPENAI_CFG, encoding='utf-8') as _f:
            cfg = json.load(_f)
    except Exception as e:
        log(f'配置读取失败: {e}')
        return
    trae = (cfg.get('providers') or {}).get('trae') or {}
    device_id = trae.get('device_id') or (trae.get('headers') or {}).get('x-device-id', '')
    accs = trae.get('accounts') or []
    if not accs:
        log('无账号')
        return
    invalid_map = server_invalid_map()
    for i, a in enumerate(accs, 1):
        tag = ' [已失效]' if invalid_map.get(a.get('uid')) else ''
        g, w, err = trae_credits(a, device_id)
        if err:
            log(f'账号{i}{tag} — 积分查询失败: {err}')
        else:
            log(f'账号{i}{tag} — 积分 {g + w:.2f}（{g:.2f}+{w:.2f}）')


# ============ WorkBuddy 积分 ============
# 真实接口: POST /billing/meter/get-user-resource (app.asar 逆向确认)
# Accounts[].CycleCapacityRemainPrecise = 各资源包周期内剩余积分

def _fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def wb_credits(acc, domain, product):
    if not acc.get('accessToken'):
        return None, '账号缺 accessToken'
    # 国际版 (www.workbuddy.ai) 走同一接口不同主机; 国内走 copilot.tencent.com
    host = domain if domain.endswith('.ai') else 'copilot.tencent.com'
    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'Authorization': f"Bearer {acc.get('accessToken', '')}",
        'X-User-Id': acc.get('userId') or 'open-ai',   # 不可用空串, 见上文字段说明
        'X-Domain': domain,
        'X-Product': product,
        'User-Agent': UA_WB,
        'Accept-Language': 'zh',
    }
    if host.endswith('workbuddy.ai'):
        headers['X-Product-Code'] = product
    code, raw = post_json(
        f'https://{host}/billing/meter/get-user-resource', headers)
    if code != 200:
        return None, f'HTTP {code}'
    try:
        d = json.loads(raw)
    except Exception:
        return None, '响应解析失败'
    try:
        accounts = d['data']['Response']['Data']['Accounts']
    except (KeyError, TypeError):
        return None, f"code={d.get('code')} msg={d.get('msg')}"
    total = sum(_fnum(a.get('CycleCapacityRemainPrecise')) for a in accounts)
    return total, None


def _show_wb_family_accounts(provider_key, label, default_domain, default_product):
    """WorkBuddy 系 (国内/国际) 账号积分显示。"""
    try:
        with open(OPENAI_CFG, encoding='utf-8') as _f:
            cfg = json.load(_f)
    except Exception as e:
        log(f'{label} 配置读取失败: {e}')
        return
    prov = (cfg.get('providers') or {}).get(provider_key) or {}
    accs = prov.get('accounts') or []
    domain = prov.get('domain', default_domain)
    product = prov.get('product', default_product)
    if not accs:
        log('无账号')
        return
    for i, a in enumerate(accs, 1):
        total, err = wb_credits(a, domain, product)
        if err:
            log(f'账号{i} — 积分查询失败: {err}')
        else:
            log(f'账号{i} — 积分 {total:.2f}')


def show_workbuddy_accounts():
    _show_wb_family_accounts('workbuddy', 'WorkBuddy', 'www.workbuddy.cn', 'SaaS')


def show_workbuddy_intl_accounts():
    _show_wb_family_accounts('workbuddy_intl', 'WorkBuddy 国际版',
                             'www.workbuddy.ai', 'workbuddy-ai')


# ============ 添加账号 ============

def add_trae():
    py = _script_py()
    script = os.path.join(BASE, 'login_trae.py')
    print('\n>>> 即将打开 TRAE 网页登录, 请在弹出的浏览器中完成登录 <<<')
    subprocess.run([py, script], cwd=os.path.dirname(BASE))
    print('\n[提示] 若登录成功, 重启 open-ai (start.bat) 后新账号生效')


def add_workbuddy():
    py = _script_py()
    script = os.path.join(BASE, 'login_workbuddy.py')
    print('\n>>> 即将打开 WorkBuddy 网页登录, 请在弹出的浏览器中完成登录 <<<')
    subprocess.run([py, script], cwd=os.path.dirname(BASE))
    print('\n[提示] 若登录成功, 重启 open-ai 网关后新账号生效')


def add_workbuddy_intl():
    py = _script_py()
    script = os.path.join(BASE, 'login_workbuddy_intl.py')
    print('\n>>> 即将打开 WorkBuddy 国际版 (www.workbuddy.ai) 网页登录, 请在弹出的浏览器中完成登录 <<<')
    subprocess.run([py, script], cwd=os.path.dirname(BASE))
    print('\n[提示] 若登录成功, 重启 open-ai 网关后新账号生效')


# ============ 重新连接 (用户自测所有 provider 的 token 是否可用) ============

def reconnect_all():
    """重新连接全部 provider: TRAE(经 server.js) + WorkBuddy(直连), 逐个账号报告结果。"""
    print()
    log('=== 重新连接全部 provider ===')

    # ---- TRAE: 交给运行中的 server.js 重新验证全部 trae 账号 ----
    try:
        req = urllib.request.Request(
            'http://127.0.0.1:18787/v1/admin/reconnect', method='POST')
        with urllib.request.urlopen(req, timeout=30) as resp:
            d = json.loads(resp.read().decode('utf-8', 'replace'))
        accs = d.get('accounts') or []
        if not accs:
            log('trae: 无账号 (server.js 可能未运行或 config 无 trae 账号)')
        else:
            for i, a in enumerate(accs, 1):
                if a.get('invalid'):
                    log(f'trae账号{i} 连接失败 (已失效, 请用 [2] 重新网页登录)')
                else:
                    log(f'trae账号{i} 连接成功')
    except Exception as e:
        log(f'trae: 重新连接失败 (server.js 可能未运行): {type(e).__name__} {e}')

    # ---- WorkBuddy: 直连计费接口验证每个账号 token ----
    for _key, _label, _hint, _dd, _dp in (
            ('workbuddy', 'workbuddy', '[3]', 'www.workbuddy.cn', 'SaaS'),
            ('workbuddy_intl', 'workbuddy-intl(国际版)', '[9]', 'www.workbuddy.ai', 'workbuddy-ai')):
        try:
            with open(OPENAI_CFG, encoding='utf-8') as _f:
                cfg = json.load(_f)
            prov = (cfg.get('providers') or {}).get(_key) or {}
            accs = prov.get('accounts') or []
            domain = prov.get('domain', _dd)
            product = prov.get('product', _dp)
            if not accs:
                log(f'{_label}: 无账号')
                continue
            for i, a in enumerate(accs, 1):
                total, err = wb_credits(a, domain, product)
                if err:
                    log(f'{_label}账号{i} 连接失败 ({err}, 请用 {_hint} 重新网页登录)')
                else:
                    log(f'{_label}账号{i} 连接成功')
        except Exception as e:
            log(f'{_label}: 重新连接失败: {type(e).__name__} {e}')


# ============ 主流程 ============

def refresh():
    print()
    print('=' * 62)
    print('trae：')
    show_trae_accounts()
    print('-' * 62)
    print('workbuddy：')
    show_workbuddy_accounts()
    print('-' * 62)
    print('workbuddy 国际版：')
    show_workbuddy_intl_accounts()
    print('=' * 62)


def show_usage_history():
    """TRAE 逐笔消耗流水 (网页 dashboard 同款接口, 详见 usage_history.py)。"""
    py = _script_py()
    script = os.path.join(BASE, 'usage_history.py')
    if not os.path.isfile(script):
        print('未找到 usage_history.py')
        return
    days = input('  查询最近几天? (回车=7): ').strip()
    args = [py, script]
    if days.isdigit() and int(days) > 0:
        args += ['--days', days]
    print()
    subprocess.run(args, cwd=os.path.dirname(BASE))


def show_wb_usage_history():
    """WorkBuddy 逐笔消耗流水 (官网个人中心同款接口, 详见 wb_usage_history.py)。"""
    py = _script_py()
    script = os.path.join(BASE, 'wb_usage_history.py')
    if not os.path.isfile(script):
        print('未找到 wb_usage_history.py')
        return
    days = input('  查询最近几天? (回车=7): ').strip()
    args = [py, script]
    if days.isdigit() and int(days) > 0:
        args += ['--days', days]
    print()
    subprocess.run(args, cwd=os.path.dirname(BASE))


def show_local_usage():
    """查看本地流水库 (daemon 每 30 分钟自动采集的逐笔消耗)。"""
    py = _script_py()
    script = os.path.join(BASE, 'usage_collector.py')
    if not os.path.isfile(script):
        print('未找到 usage_collector.py')
        return
    print('\n  [1] 查看本地流水库')
    print('  [2] 立即采集一轮')
    choice = input('  请选择 (回车=1): ').strip() or '1'
    args = [py, script]
    if choice == '2':
        args += ['--collect']
    else:
        days = input('  查询最近几天? (回车=7): ').strip()
        if days.isdigit() and int(days) > 0:
            args += ['--days', days]
    print()
    subprocess.run(args, cwd=os.path.dirname(BASE))


def menu():
    print()
    print('  [1] 刷新积分显示')
    print('  [2] 添加 TRAE 账号 (网页登录)')
    print('  [3] 添加 WorkBuddy 账号 (网页登录)')
    print('  [4] 重新连接')
    print('  [5] 模型列表 + 积分倍率')
    print('  [6] TRAE 逐笔消耗流水 (实时查询)')
    print('  [7] WorkBuddy 逐笔消耗流水 (实时查询)')
    print('  [8] 消耗流水本地库 (自动采集汇总)')
    print('  [9] 添加 WorkBuddy 国际版账号 (网页登录)')
    print('  [Q] 退出')


def main():
    once = '--once' in sys.argv
    refresh()
    if once:
        return 0
    while True:
        menu()
        choice = input('  请选择: ').strip().lower()
        if choice in ('q', 'quit', 'exit', ''):
            print('  再见!')
            return 0
        elif choice == '1':
            refresh()
        elif choice == '2':
            add_trae()
        elif choice == '3':
            add_workbuddy()
        elif choice == '4':
            reconnect_all()
        elif choice == '5':
            show_model_rates()
        elif choice == '6':
            show_usage_history()
        elif choice == '7':
            show_wb_usage_history()
        elif choice == '8':
            show_local_usage()
        elif choice == '9':
            add_workbuddy_intl()
        else:
            print('  无效选项')


if __name__ == '__main__':
    sys.exit(main())
