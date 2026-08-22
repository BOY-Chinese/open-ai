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

UA_WB = 'WorkBuddy/5.3.12 WorkBuddy/5.3.12 CLI/2.115.0'


def ts():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def log(msg):
    print(f'[{ts()}] {msg}')


def post_json(url, headers, body=b'{}', timeout=15):
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
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
    py = OPENAI_VENV_PY if os.path.isfile(OPENAI_VENV_PY) else sys.executable
    script = os.path.join(BASE, 'login_trae.py')
    print('\n>>> 即将打开 TRAE 网页登录, 请在弹出的浏览器中完成登录 <<<')
    subprocess.run([py, script], cwd=os.path.dirname(BASE))
    print('\n[提示] 若登录成功, 重启 open-ai (start.bat) 后新账号生效')


def add_workbuddy():
    py = OPENAI_VENV_PY if os.path.isfile(OPENAI_VENV_PY) else sys.executable
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


def menu():
    print()
    print('  [1] 刷新积分显示')
    print('  [2] 添加 TRAE 账号 (网页登录)')
    print('  [3] 添加 WorkBuddy 账号 (网页登录)')
    print('  [4] 重新连接')
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
        else:
            print('  无效选项')


if __name__ == '__main__':
    sys.exit(main())
