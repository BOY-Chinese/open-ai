# -*- coding: utf-8 -*-
"""
check_login_browser.py — 打包前门禁: 登录助手必须用「包内自带 Chromium」
=====================================================================
背景（2026-09-16 portable 用户机事故）:
  本机版 / dev 版早在 v2.6 就把三个登录脚本换成了 Playwright 自带 Chromium
  (`channel=None`), 只有 portable 仓的脚本停在旧写法:

      browser = p.chromium.launch(headless=False, channel='msedge')

  `channel='msedge'` 会**强制**拉系统 Edge —— 与「包内有没有 Chromium」无关。
  后果是装机后「添加账号」拉起的仍是 Edge（master 在虚拟机上实测发现），
  而 `installer/build_resources.py` 已经把 ms-playwright/chromium-1234 老老实实
  打进安装包（+190MB）: 体积白涨、行为照旧, **且不报错** —— 最难查的一类。

  这类「注释里写着已修复、代码里没改」的漂移, grep 是查不出来的（三个脚本的
  文档头都写着 `channel='msedge'` 那段历史）。故用 AST 判**代码**, 不判文本。

规则（与 tests/test_login_browser.py 同源, 逐条对应）:
  R1 每个 scripts/login_*.py 必须有模块级 BROWSER_FALLBACK, 首元素是 chromium
  R2 p.chromium.launch* 的 channel 不许是字面量（必须由降级链决定）
  R3 每个脚本都要调 ensure_playwright_browsers_path()（否则打包态认不出
     <root>\\ms-playwright, 判定「浏览器未安装」→ 降级回系统 Edge）

用法: python check_login_browser.py   (build_exe.bat / build_all.py 构建前调用)
退出码: 0 = 通过; 1 = 违规（不产出安装包）
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, 'scripts')
LOGIN_SCRIPTS = ('login_trae.py', 'login_workbuddy.py', 'login_workbuddy_intl.py')


def _fallback_order(tree):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == 'BROWSER_FALLBACK':
                    if isinstance(node.value, (ast.Tuple, ast.List)):
                        return [e.value if isinstance(e, ast.Constant) else None
                                for e in node.value.elts]
    return None


def _launch_calls(tree):
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if (isinstance(f, ast.Attribute) and f.attr.startswith('launch')
                and isinstance(f.value, ast.Attribute) and f.value.attr == 'chromium'):
            out.append(node)
    return out


def check_file(path):
    """返回该脚本的违规说明列表（空 = 合规）。"""
    name = os.path.basename(path)
    try:
        with open(path, encoding='utf-8') as f:
            tree = ast.parse(f.read(), filename=path)
    except (SyntaxError, UnicodeDecodeError) as e:
        return ['%s 解析失败: %s' % (name, e)]

    bad = []
    order = _fallback_order(tree)
    if order is None:
        bad.append('%s: 缺少模块级 BROWSER_FALLBACK (降级链)' % name)
    elif order[0] != 'chromium':
        bad.append('%s: BROWSER_FALLBACK 首选是 %r, 必须是包内自带的 chromium'
                   % (name, order[0]))

    for call in _launch_calls(tree):
        for kw in call.keywords:
            if kw.arg == 'channel' and isinstance(kw.value, ast.Constant):
                bad.append('%s:%d channel 写死为 %r —— 会强制走系统浏览器, '
                           '包内自带的 Chromium 永远用不到'
                           % (name, call.lineno, kw.value.value))

    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    if 'ensure_playwright_browsers_path' not in attrs:
        bad.append('%s: 未调用 ensure_playwright_browsers_path() —— 打包态认不出 '
                   r'<root>\ms-playwright, 会判定「浏览器未安装」并降级到系统 Edge'
                   % name)
    return bad


def main():
    violations = []
    scanned = 0
    for name in LOGIN_SCRIPTS:
        path = os.path.join(SCRIPTS, name)
        if not os.path.isfile(path):
            violations.append('%s 不存在（登录助手缺失, 添加账号会 501）' % name)
            continue
        scanned += 1
        violations.extend(check_file(path))

    print('[check_login_browser] scanned %d login scripts' % scanned)
    if violations:
        print('[check_login_browser] FAIL - 登录助手浏览器不合规:')
        for v in violations:
            print('  ' + v)
        print('修法: 参照 dev 仓 scripts/login_*.py 的 launch_browser()/'
              'BROWSER_FALLBACK（chromium→chrome→msedge→firefox, chromium 档传 channel=None）')
        return 1
    print('[check_login_browser] OK - 三个登录脚本均优先使用包内自带 Chromium')
    return 0


if __name__ == '__main__':
    sys.exit(main())
