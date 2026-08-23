#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 网页登录 → 应用 token 脚本
====================================
基于抓包逆向的"网页转登录"链路:
  打开 www.workbuddy.cn 登录页(桌面端内嵌) → 用户扫码/密码登录 →
  页面自动调用 POST /console/login/enterprise?state=<uuid>
  (带 x-device-token 与登录 session cookie) → 返回 accessToken/refreshToken

本脚本不关心 x-device-token 如何生成: 用 Playwright 打开登录页让页面自己走完整流程,
直接监听捕获 /console/login/enterprise 的响应即可。

流程:
  1. Playwright 打开 WorkBuddy 登录页 (headful, 用户可见)
  2. 用户手动登录 (扫码/密码)
  3. 监听网络响应, 捕获 /console/login/enterprise 返回的 accessToken/refreshToken
  4. 从 accessToken JWT 解出 userId (sub)
  5. 写入 open-ai/config.json workbuddy.accounts (按 userId 更新/追加)
  6. 写入 open-ai/config.json (签到由内部 signin_all.py 读取)
  7. 自动关闭浏览器

用法:
  python login_workbuddy.py [--timeout 300]   # 等待登录的超时秒数 (默认 300)
"""
import base64
import json
import os
import sys
import time
import uuid

# 强制 stdout/stderr 用 utf-8, 避免 GBK 控制台因中文/emoji 崩溃
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# Playwright 由 open-ai/.venv 提供

HERE = os.path.dirname(os.path.abspath(__file__))
OPENAI_CFG = os.path.join(HERE, '..', 'config.json')
LOGIN_URL_TMPL = ('https://www.workbuddy.cn/login/?platform=workbuddy'
                  '&state={state}&version=5.3.12&loginSessionId={lsid}')
ENTERPRISE_MARK = '/console/login/enterprise'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0')


def jwt_user_id(token: str) -> str:
    try:
        parts = token.split('.')
        pad = parts[1] + '=' * (-len(parts[1]) % 4)
        p = json.loads(base64.urlsafe_b64decode(pad))
        return str(p.get('sub', ''))
    except Exception:
        return ''


def main():
    timeout = 300
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == '--timeout' and i + 1 < len(args):
            timeout = int(args[i + 1])

    from playwright.sync_api import sync_playwright

    state = str(uuid.uuid4())
    lsid = str(uuid.uuid4())
    login_url = LOGIN_URL_TMPL.format(state=state, lsid=lsid)

    print('=' * 60)
    print('  WorkBuddy 网页登录助手')
    print('  即将打开浏览器, 请在网页中完成登录 (扫码/密码)')
    print(f'  等待登录超时: {timeout} 秒')
    print('=' * 60)

    result = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel='msedge')
        ctx = browser.new_context(user_agent=UA)
        page = ctx.new_page()

        def on_response(resp):
            if ENTERPRISE_MARK in resp.url and resp.status == 200:
                try:
                    j = resp.json()
                except Exception:
                    return
                data = (j or {}).get('data') or {}
                at = data.get('accessToken', '')
                rt = data.get('refreshToken', '')
                if at:
                    result['accessToken'] = at
                    result['refreshToken'] = rt
                    result['userId'] = jwt_user_id(at)

        page.on('response', on_response)
        page.goto(login_url, wait_until='domcontentloaded', timeout=30000)
        print('[等待] 请在弹出的浏览器中登录 WorkBuddy 账号...')
        print('[提示] 登录后页面若弹"请求参数错误(400)"提示, 可忽略——登录实际已成功, 脚本会自动抓取 token')

        start = time.time()
        while time.time() - start < timeout:
            if result.get('accessToken'):
                break
            page.wait_for_timeout(1000)  # 关键: 必须用 playwright 的等待, 否则事件不实时投递

        if not result.get('accessToken'):
            print('[超时] 未捕获到 /console/login/enterprise 响应, 尝试兜底提取...')
            try:
                ls = page.evaluate(
                    "() => { const o = {}; for (let i=0;i<localStorage.length;i++){"
                    "const k=localStorage.key(i); o[k]=localStorage.getItem(k);} return o; }")
                print('[兜底] localStorage:', json.dumps(ls, ensure_ascii=False)[:2000])
            except Exception as e:
                print('[兜底] localStorage 读取失败:', e)
            try:
                cookies = ctx.cookies()
                print('[兜底] cookies:', json.dumps(
                    [{'name': c.get('name'), 'domain': c.get('domain'),
                      'value': (c.get('value') or '')[:80]} for c in cookies],
                    ensure_ascii=False)[:1500])
            except Exception as e:
                print('[兜底] cookies 读取失败:', e)
            print('[兜底] 当前页面 URL:', page.url)
            browser.close()
            sys.exit(1)

        at, rt, uid = result['accessToken'], result['refreshToken'], result['userId']
        print(f'[成功] 获取 accessToken: userId={uid} len={len(at)}')
        print(f'[成功] refreshToken: {"有" if rt else "无"} len={len(rt)}')

        # ---------- 写入 open-ai/config.json ----------
        cfg = json.load(open(OPENAI_CFG, encoding='utf-8'))
        wb = cfg['providers'].setdefault('workbuddy', {})
        accounts = wb.setdefault('accounts', [])
        found = False
        for acc in accounts:
            if acc.get('userId') == uid:
                acc['accessToken'] = at
                acc['refreshToken'] = rt
                found = True
                print(f'[更新] workbuddy 账号 {acc.get("userId")} token 已更新')
                break
        if not found:
            accounts.append({'accessToken': at, 'refreshToken': rt, 'userId': uid})
            print(f'[新增] workbuddy 新账号 {uid} 已加入 accounts')
        json.dump(cfg, open(OPENAI_CFG, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
        print('[写入] open-ai/config.json 完成 (签到由内部 signin_all.py 直接读此配置)')

        browser.close()
        print()
        print('登录完成, 浏览器已自动关闭')
        print('   提示: 重启 open-ai 网关后新账号即可参与轮询')


if __name__ == '__main__':
    main()
