#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 账号管理器 — 显示各账号积分 / 添加账号
=========================================
数据源:
  TRAE      : open-ai/config.json  providers.trae.accounts[]  积分接口 /trae/api/v2/pay/ide_user_ent_usage
  WorkBuddy : open-ai/config.json  providers.workbuddy.accounts[]  积分接口 /billing/meter/get-user-resource

功能:
  [1] 刷新积分    — 重新查询 TRAE + WorkBuddy 所有账号积分
  [2] 添加 TRAE 账号   — 打开网页登录, 自动抓 token 入池 (login_trae.py)
  [3] 添加 WorkBuddy 账号 — 打开网页登录, 自动抓 token 入池 (login_workbuddy.py)
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

BASE = os.path.dirname(os.path.abspath(__file__))
# open-ai 自包含: 所有配置统一在 open-ai/config.json
OPENAI_CFG = os.path.join(BASE, '..', 'config.json')
OPENAI_VENV_PY = os.path.join(BASE, '..', '.venv', 'Scripts', 'python.exe')

# ---- v2.4 进程品牌化: 短命脚本用 task shim (任务管理器显示 open-ai 品牌) ----
OPENAI_TASK_SHIM = os.path.join(BASE, '..', 'runtime', 'Scripts',
                                'open-ai-task.exe')


def _script_py():
    """优先 open-ai-task.exe shim (品牌化), 回退 venv python。"""
    if os.path.isfile(OPENAI_TASK_SHIM):
        return OPENAI_TASK_SHIM
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


def wb_model_rates():
    """拉取 WorkBuddy 全部模型及其积分倍率 (模型 credits 字段, 形如 "x0.17")。
    返回 [(model_id, display_name, credits, err_or_None)], err 非 None 表示整体失败。"""
    try:
        with open(OPENAI_CFG, encoding='utf-8') as _f:
            cfg = json.load(_f)
    except Exception as e:
        return None, f'config 读取失败: {e}'
    wb = (cfg.get('providers') or {}).get('workbuddy') or {}
    accs = wb.get('accounts') or []
    if not accs:
        return None, '无 WorkBuddy 账号'
    acc = accs[0]
    domain = wb.get('domain', 'www.codebuddy.cn')
    product = wb.get('product', 'SaaS')
    if not acc.get('accessToken'):
        return None, 'WorkBuddy 账号缺 accessToken'
    code, raw = post_json(
        'https://copilot.tencent.com/v2/enterprises/personal/models',
        {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Authorization': f"Bearer {acc.get('accessToken', '')}",
            'X-User-Id': acc.get('userId', ''),
            'X-Domain': domain,
            'X-Product': product,
            'User-Agent': UA_WB,
            'Accept-Language': 'zh',
        },
        None, timeout=20, method='GET')
    if code != 200:
        return None, f'HTTP {code}'
    try:
        d = json.loads(raw)
    except Exception:
        return None, '响应解析失败'
    models = ((d.get('data') or {}).get('models')) if isinstance(d, dict) else None
    if models is None:
        return None, f"响应无 models: {str(d)[:80]}"
    items = []
    for m in models:
        if not isinstance(m, dict):
            continue
        mid = m.get('id', '')
        name = m.get('name') or mid
        credits = m.get('credits')
        items.append((mid, name, credits, None))
    if not items:
        return None, '未解析到模型'
    return items, None


def show_model_rates():
    """控制台: 列出 TRAE + WorkBuddy 全部模型及倍率。"""
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
    code, raw = post_json(
        'https://copilot.tencent.com/billing/meter/get-user-resource',
        {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Authorization': f"Bearer {acc.get('accessToken', '')}",
            'X-User-Id': acc.get('userId', ''),
            'X-Domain': domain,
            'X-Product': product,
            'User-Agent': UA_WB,
            'Accept-Language': 'zh',
        })
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


def show_workbuddy_accounts():
    try:
        with open(OPENAI_CFG, encoding='utf-8') as _f:
            cfg = json.load(_f)
    except Exception as e:
        log(f'WorkBuddy 配置读取失败: {e}')
        return
    wb = (cfg.get('providers') or {}).get('workbuddy') or {}
    accs = wb.get('accounts') or []
    domain = wb.get('domain', 'www.workbuddy.cn')
    product = wb.get('product', 'SaaS')
    if not accs:
        log('无账号')
        return
    for i, a in enumerate(accs, 1):
        total, err = wb_credits(a, domain, product)
        if err:
            log(f'账号{i} — 积分查询失败: {err}')
        else:
            log(f'账号{i} — 积分 {total:.2f}')


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
    try:
        with open(OPENAI_CFG, encoding='utf-8') as _f:
            cfg = json.load(_f)
        wb = (cfg.get('providers') or {}).get('workbuddy') or {}
        accs = wb.get('accounts') or []
        domain = wb.get('domain', 'www.workbuddy.cn')
        product = wb.get('product', 'SaaS')
        if not accs:
            log('workbuddy: 无账号')
        else:
            for i, a in enumerate(accs, 1):
                total, err = wb_credits(a, domain, product)
                if err:
                    log(f'workbuddy账号{i} 连接失败 ({err}, 请用 [3] 重新网页登录)')
                else:
                    log(f'workbuddy账号{i} 连接成功')
    except Exception as e:
        log(f'workbuddy: 重新连接失败: {type(e).__name__} {e}')


# ============ 主流程 ============

def refresh():
    print()
    print('=' * 62)
    print('trae：')
    show_trae_accounts()
    print('-' * 62)
    print('workbuddy：')
    show_workbuddy_accounts()
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
        else:
            print('  无效选项')


if __name__ == '__main__':
    sys.exit(main())
