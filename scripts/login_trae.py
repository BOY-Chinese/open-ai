#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TRAE 网页登录 → 自动抓 cookie → 换 token 脚本
=============================================
流程:
  1. Playwright 打开 TRAE 登录页 (headful, 用户可见)
  2. 用户手动登录 (扫码/密码)
  3. 自动检测登录成功 (X-Cloudide-Session cookie 出现)
  4. 抓取全部 cookie → GetUserToken 换完整 token
  5. 自动写入 open-ai/config.json providers.trae.accounts (新账号追加) + cookie 字段
  6. 同步到自动签到 config.json (trae_accounts)
  7. 自动关闭浏览器

用法:
  python login_trae.py [--timeout 300]   # 等待登录的超时秒数 (默认 300)
"""
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# Playwright 由 open-ai/.venv 提供

HERE = os.path.dirname(os.path.abspath(__file__))
OPENAI_CFG = os.path.join(HERE, '..', 'config.json')
LOGIN_URL = 'https://www.trae.cn/login'
GET_TOKEN_URL = 'https://api.trae.cn/cloudide/api/v3/common/GetUserToken'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0'


def get_device_id():
    """从 open-ai/config.json providers.trae 读取设备 ID"""
    try:
        cfg = json.load(open(OPENAI_CFG, encoding='utf-8'))
        trae = (cfg.get('providers') or {}).get('trae') or {}
        return (trae.get('device_id') or (trae.get('headers') or {}).get('x-device-id') or '')
    except Exception:
        return ''


def get_token_by_cookie(cookie):
    req = urllib.request.Request(GET_TOKEN_URL, data=b'{}', method='POST', headers={
        'Cookie': cookie, 'User-Agent': UA, 'Content-Type': 'application/json',
        'Origin': 'https://www.trae.cn', 'Referer': 'https://www.trae.cn/',
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            j = json.loads(r.read().decode('utf-8', 'replace'))
            tok = j.get('Result', {}).get('Token', '')
            if tok:
                parts = tok.split('.')
                pad = parts[1] + '=' * (-len(parts[1]) % 4)
                p = json.loads(base64.urlsafe_b64decode(pad))
                uid = p.get('data', {}).get('id', '?')
                return tok, uid
            return None, None
    except Exception as e:
        print(f'[错误] GetUserToken 失败: {e}')
        return None, None


def verify_token(token):
    did = get_device_id()
    req = urllib.request.Request(
        'https://api.trae.cn/trae/api/v2/pay/ide_user_ent_usage',
        data=b'{"require_usage": true, "req_source": 1}', method='POST',
        headers={'Authorization': f'Cloud-IDE-JWT {token}', 'x-device-id': did,
                 'Content-Type': 'application/json', 'User-Agent': 'TRAE SOLO CN/2.3.70844'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


def main():
    timeout = 300
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == '--timeout' and i + 1 < len(args):
            timeout = int(args[i + 1])

    from playwright.sync_api import sync_playwright

    print('=' * 60)
    print('  TRAE 网页登录助手')
    print('  即将打开浏览器, 请在网页中完成登录 (扫码/密码)')
    print(f'  等待登录超时: {timeout} 秒')
    print('=' * 60)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel='msedge')
        ctx = browser.new_context(user_agent=UA)
        page = ctx.new_page()
        page.goto(LOGIN_URL, wait_until='domcontentloaded', timeout=30000)
        print('[等待] 请在弹出的浏览器中登录 TRAE 账号...')

        # 轮询检测登录成功: X-Cloudide-Session cookie 出现
        cookie_str = None
        start = time.time()
        while time.time() - start < timeout:
            time.sleep(2)
            cookies = ctx.cookies()
            has_session = any(c['name'] == 'X-Cloudide-Session' and c.get('value') for c in cookies)
            if has_session:
                parts = []
                for c in cookies:
                    if c.get('value'):
                        parts.append(f"{c['name']}={c['value']}")
                cookie_str = '; '.join(parts)
                break
            # 也检查 sessionid (部分登录态)
            has_sid = any(c['name'] in ('sessionid', 'sid_guard') and c.get('value') for c in cookies)
            if has_sid and cookie_str is None:
                cookie_str = '; '.join(f"{c['name']}={c['value']}" for c in cookies if c.get('value'))

        if not cookie_str:
            print('[超时] 未检测到登录成功 (X-Cloudide-Session 未出现)')
            browser.close()
            sys.exit(1)

        m = re.search(r'X-Cloudide-Session=([^;]+)', cookie_str)
        print(f'[成功] 检测到登录态! X-Cloudide-Session: {m.group(1)[:30]}...' if m else '[成功] 检测到登录态!')
        print('[步骤] 抓取 cookie 完成, 正在换取 token...')

        tok, uid = get_token_by_cookie(cookie_str)
        if not tok:
            print('[失败] 换 token 失败, cookie 可能不完整')
            browser.close()
            sys.exit(1)
        print(f'[成功] 获取 token: uid={uid} len={len(tok)}')

        ok = verify_token(tok)
        print(f'[验证] token 有效性: {"✅ 有效" if ok else "❌ 无效"}')

        if ok:
            # 写入 open-ai/config.json providers.trae.accounts
            cfg = json.load(open(OPENAI_CFG, encoding='utf-8'))
            trae = cfg.setdefault('providers', {}).setdefault('trae', {})
            found = False
            for acc in trae.get('accounts', []):
                if acc.get('uid') == uid:
                    acc['token'] = tok
                    acc['cookie'] = cookie_str
                    found = True
                    print(f'[更新] 账号 {acc.get("name", uid)} token+cookie 已更新')
                    break
            if not found:
                trae.setdefault('accounts', []).append({
                    'name': f'网页登录账号({uid})',
                    'uid': uid,
                    'token': tok,
                    'cookie': cookie_str,
                })
                print(f'[新增] 新账号 {uid} 已加入 accounts')
            json.dump(cfg, open(OPENAI_CFG, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
            print('[写入] open-ai/config.json providers.trae.accounts 完成')

        browser.close()
        print()
        print('✅ 登录完成, 浏览器已自动关闭')
        print('   提示: 重启 open-ai (start.bat) 后新账号即可参与轮询; cookie 13 天内 token 可自动续期')


if __name__ == '__main__':
    main()
