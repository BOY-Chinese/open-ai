# -*- coding: utf-8 -*-
"""
测试: 登录助手的浏览器必须是「包内自带 Chromium 优先」
=====================================================
背景（2026-09-16 portable 用户机事故）：
  本机版 / dev 版早在 v2.6 就把登录助手换成了 **Playwright 自带 Chromium**
  （`channel=None`），只有 portable 仓的三个 `login_*.py` 停在旧写法：

      browser = p.chromium.launch(headless=False, channel='msedge')

  `channel='msedge'` 是**强制**走系统 Edge，与「包内有没有 Chromium」无关 ——
  于是 portable 装机后「添加账号」拉起的仍是 Edge，master 在虚拟机上实测发现。
  更坑的是它**不报错**：`installer/build_resources.py` 老老实实把
  `ms-playwright/chromium-1234` 打进了安装包（+190MB），代码却压根不去用它，
  体积白涨、行为照旧。

为什么必须用 AST 而不是 grep 断言：
  三个脚本的**文档头**里都写着「旧版直接 p.chromium.launch(channel='msedge')」
  （那是在解释历史），grep 一定命中，无法区分「注释里的旧代码」与
  「真的还在这么调」。

三条规则（缺一条，装机后就会悄悄退回系统 Edge）：
  R1 每个 login_*.py 都要有模块级 `BROWSER_FALLBACK`，且**首元素是 chromium**；
  R2 `p.chromium.launch*` 的 `channel` 不许写死字面量（必须由降级链决定，
     chromium 档传 None = 用包内自带的那个）；
  R3 每个脚本都要调 `ensure_playwright_browsers_path()` —— 不指路的话
     Playwright 认为「浏览器未安装」，直接抛错并降级到系统 Edge。

运行: python -m unittest tests.test_login_browser -v
"""
import ast
import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(PROJECT_ROOT, 'scripts')
LOGIN_SCRIPTS = ('login_trae.py', 'login_workbuddy.py', 'login_workbuddy_intl.py')


def _parse(name):
    with open(os.path.join(SCRIPTS, name), encoding='utf-8') as f:
        return ast.parse(f.read(), filename=name)


def _module_fallback_order(tree):
    """返回模块级 BROWSER_FALLBACK 的元素字面量列表（取不到返回 None）。"""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == 'BROWSER_FALLBACK':
                    if isinstance(node.value, (ast.Tuple, ast.List)):
                        out = []
                        for e in node.value.elts:
                            out.append(e.value if isinstance(e, ast.Constant) else None)
                        return out
    return None


def _chromium_launch_calls(tree):
    """收集所有 p.chromium.launch*/launch_persistent_context(...) 调用节点。"""
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if (isinstance(f, ast.Attribute)
                and f.attr.startswith('launch')
                and isinstance(f.value, ast.Attribute)
                and f.value.attr == 'chromium'):
            calls.append(node)
    return calls


class LoginBrowserTest(unittest.TestCase):

    def test_login_scripts_exist(self):
        for name in LOGIN_SCRIPTS:
            self.assertTrue(os.path.isfile(os.path.join(SCRIPTS, name)), name)

    def test_r1_fallback_chain_starts_with_bundled_chromium(self):
        for name in LOGIN_SCRIPTS:
            order = _module_fallback_order(_parse(name))
            self.assertIsNotNone(order, f'{name} 缺少模块级 BROWSER_FALLBACK')
            self.assertEqual(order[0], 'chromium',
                             f'{name} 的首选浏览器必须是包内自带的 chromium，当前 {order}')
            self.assertIn('msedge', order,
                          f'{name} 的降级链应保留 msedge 作为最后一档兜底')

    def test_r2_channel_must_not_be_hardcoded(self):
        for name in LOGIN_SCRIPTS:
            for call in _chromium_launch_calls(_parse(name)):
                for kw in call.keywords:
                    if kw.arg != 'channel':
                        continue
                    self.assertNotIsInstance(
                        kw.value, ast.Constant,
                        f'{name}:{call.lineno} 把 channel 写成了字面量 '
                        f'{getattr(kw.value, "value", "?")!r} —— 这会**强制**走系统浏览器，'
                        '包内自带的 Chromium 永远不会被用到（portable 装机后拉起 Edge 的根因）')

    def test_r3_bundled_browsers_path_is_set(self):
        for name in LOGIN_SCRIPTS:
            src_names = {n.attr for n in ast.walk(_parse(name))
                         if isinstance(n, ast.Attribute)}
            src_calls = {n.func.attr for n in ast.walk(_parse(name))
                         if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
            self.assertTrue(
                'ensure_playwright_browsers_path' in src_names | src_calls,
                f'{name} 未调用 ensure_playwright_browsers_path()：打包态下 Playwright '
                '认不出 <root>\\ms-playwright，会判定「浏览器未安装」并降级到系统 Edge')


if __name__ == '__main__':
    unittest.main()
