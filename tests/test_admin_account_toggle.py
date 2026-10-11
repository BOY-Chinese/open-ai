# -*- coding: utf-8 -*-
"""
测试: Loomy 账号「开关」端点 (POST /v1/admin/accounts/toggle)
==============================================================
回归 2026-10-11 的 bug: Loomy 账号被手动关闭后**再也开不回来** —— 点开关返回
HTTP 500 `TypeError: cannot unpack non-iterable NoneType object`。

根因: 合并行取反那段写成

    new_enabled = not any(a.get("enabled", True) is not False
                          for _i, a in (d, w) if a)

生成器表达式里 **解包发生在 `if a` 过滤之前** —— `(d, w)` 中缺席的那半边是
`None`, 于是 `for _i, a in (None, ...)` 当场抛 TypeError。Loomy 的「桌面 + Web」
两半边常只存在一边 (本机两个账号都只有桌面半边), 所以**必现**。

本套件把 config 指到临时文件, 直接调端点协程, 不联网、不碰真实配置。
运行: python -m unittest tests.test_admin_account_toggle -v
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import admin_api as A  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class LoomyToggleTest(unittest.TestCase):
    """Loomy 合并行的开关: 三种半边组合都要能正常取反。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tmp_cfg = os.path.join(self.tmpdir, 'config.json')
        self._orig = A.CONFIG_PATH
        A.CONFIG_PATH = self.tmp_cfg

    def tearDown(self):
        A.CONFIG_PATH = self._orig

    def _write(self, accounts):
        with open(self.tmp_cfg, 'w', encoding='utf-8') as f:
            json.dump({'providers': {'loomy': {'accounts': accounts}}},
                      f, ensure_ascii=False, indent=2)

    def _toggle(self, acc_id):
        return _run(A.toggle_account({'id': acc_id}))

    def _enabled(self):
        with open(self.tmp_cfg, encoding='utf-8') as f:
            return [a.get('enabled')
                    for a in json.load(f)['providers']['loomy']['accounts']]

    # ---------------- 只有桌面半边 (本机真实形态, 旧版必 500) ----------------

    def test_desktop_only_disabled_can_be_reenabled(self):
        """★ 核心回归: 只有桌面半边的账号, 关掉后必须能开回来。"""
        self._write([{'userid': 'u1', 'phone': '13800000001',
                      'session': 'sess', 'enabled': False}])
        res = self._toggle('Loomy:u1')
        self.assertTrue(res['ok'])
        self.assertTrue(res['enabled'])
        self.assertEqual(self._enabled(), [True])

    def test_desktop_only_enabled_can_be_disabled(self):
        self._write([{'userid': 'u1', 'phone': '13800000001',
                      'session': 'sess', 'enabled': True}])
        self.assertFalse(self._toggle('Loomy:u1')['enabled'])
        self.assertEqual(self._enabled(), [False])

    # ---------------- 只有 Web 半边 ----------------

    def test_web_only_disabled_can_be_reenabled(self):
        self._write([{'userid': 'u2', 'phone': '13800000002',
                      'kind': 'web', 'cookies': 'c', 'enabled': False}])
        res = self._toggle('Loomy:u2')
        self.assertTrue(res['enabled'])
        self.assertEqual(self._enabled(), [True])

    # ---------------- 两半边都齐 ----------------

    def test_both_halves_flip_together(self):
        """同手机号的桌面+Web 两条必须一起翻 —— 只切一边会让行状态永远对不上。"""
        self._write([
            {'userid': 'u3', 'phone': '13800000003', 'session': 's', 'enabled': False},
            {'userid': 'u3w', 'phone': '13800000003', 'kind': 'web',
             'cookies': 'c', 'enabled': False},
        ])
        res = self._toggle('Loomy:u3')
        self.assertTrue(res['enabled'])
        self.assertEqual(self._enabled(), [True, True])

    def test_row_enabled_if_any_half_enabled(self):
        """任一半边启用即算启用 → 取反后两边都关。"""
        self._write([
            {'userid': 'u4', 'phone': '13800000004', 'session': 's', 'enabled': True},
            {'userid': 'u4w', 'phone': '13800000004', 'kind': 'web',
             'cookies': 'c', 'enabled': False},
        ])
        self.assertFalse(self._toggle('Loomy:u4')['enabled'])
        self.assertEqual(self._enabled(), [False, False])

    # ---------------- 边界 ----------------

    def test_unknown_id_raises_404(self):
        self._write([{'userid': 'u1', 'phone': '13800000001',
                      'session': 's', 'enabled': False}])
        with self.assertRaises(Exception) as ctx:
            self._toggle('Loomy:nope')
        self.assertIn('404', str(ctx.exception))

    def test_missing_enabled_key_defaults_to_disabled_after_toggle(self):
        """没写 enabled 字段 = 默认启用 → 一次点击应变成关闭。"""
        self._write([{'userid': 'u5', 'phone': '13800000005', 'session': 's'}])
        self.assertFalse(self._toggle('Loomy:u5')['enabled'])
        self.assertEqual(self._enabled(), [False])


class NonLoomyToggleTest(unittest.TestCase):
    """非 Loomy 通道走另一条分支, 不能被本次改动带坏。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tmp_cfg = os.path.join(self.tmpdir, 'config.json')
        self._orig = A.CONFIG_PATH
        A.CONFIG_PATH = self.tmp_cfg

    def tearDown(self):
        A.CONFIG_PATH = self._orig

    def test_trae_toggle_roundtrip(self):
        with open(self.tmp_cfg, 'w', encoding='utf-8') as f:
            json.dump({'providers': {'trae': {'accounts': [
                {'uid': 't1', 'token': 'x', 'enabled': False}]}}},
                f, ensure_ascii=False, indent=2)
        res = _run(A.toggle_account({'id': 'Trae:t1'}))
        self.assertTrue(res['enabled'])
        with open(self.tmp_cfg, encoding='utf-8') as f:
            self.assertEqual(
                json.load(f)['providers']['trae']['accounts'][0]['enabled'], True)


if __name__ == '__main__':
    unittest.main()
