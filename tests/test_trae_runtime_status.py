# -*- coding: utf-8 -*-
"""
测试: 账号管理页「真状态」接入 —— Trae 运行态合并判定
====================================================
背景（2026-09-19 trae 全通道断连事故）：账号状态原先纯离线读 config，
trae server 运行时标记的 invalid 只活在其进程内存里，页面永远显示
绿「启用」，直接误导排障方向。本次把 trae server 的
GET /v1/admin/accounts（{uid: invalid}）真正接进 admin_api：

1. `_trae_row_status` —— 单行终判：服务未响应 / 运行态失效 / 维持离线判据。
2. `_trae_runtime_state` —— 探测 trae server：解析、端口取 config、失败→None。
3. `_load_accounts_raw` —— 接线：Trae 行叠加运行态，且**每次列表只探一次**。

不联网、不写真实 config：urlopen / _trae_runtime_state 全部打桩。
运行: python -m unittest tests.test_trae_runtime_status -v
"""
import json
import os
import sys
import unittest
import urllib.error

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import admin_api as A  # noqa: E402


TRAE_T1 = {'uid': 't1', 'token': 'x', 'name': '网页登录账号(t1)'}
TRAE_T2 = {'uid': 't2', 'token': 'y', 'name': '网页登录账号(t2)'}
WB_ACC = {'userId': 'wb-1', 'accessToken': 'z'}


def _cfg():
    """最小 config（形状与真实 config.json 的 providers 段一致）。"""
    return {
        'providers': {
            'trae': {'device_id': 'dev', 'port': 18787, 'listen': '127.0.0.1',
                     'accounts': [TRAE_T1, TRAE_T2]},
            'workbuddy': {'domain': 'www.workbuddy.cn', 'product': 'SaaS',
                          'accounts': [WB_ACC]},
        },
    }


class _FakeResp:
    """urlopen 打桩用的最小响应对象（支持 with 语法）。"""

    def __init__(self, payload):
        self._payload = json.dumps(payload).encode('utf-8')

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TraeRowStatusTest(unittest.TestCase):
    """_trae_row_status: 单行 (status, detail) 终判。"""

    def test_runtime_none_means_service_down(self):
        status, detail = A._trae_row_status('enabled', 't1', None)
        self.assertEqual(status, 'disconnected')
        self.assertIn('服务未响应', detail)

    def test_runtime_invalid_means_account_dead(self):
        status, detail = A._trae_row_status('enabled', 't1', {'t1': True})
        self.assertEqual(status, 'disconnected')
        self.assertIn('已失效', detail)

    def test_runtime_valid_keeps_offline_verdict(self):
        status, detail = A._trae_row_status('enabled', 't1', {'t1': False})
        self.assertEqual(status, 'enabled')
        self.assertEqual(detail, '')

    def test_unknown_uid_keeps_offline_verdict(self):
        """运行态里没有的 uid（如刚加的账号）→ 交给离线判据。"""
        status, detail = A._trae_row_status('enabled', 't9', {'t1': False})
        self.assertEqual(status, 'enabled')
        self.assertEqual(detail, '')


class TraeRuntimeStateTest(unittest.TestCase):
    """_trae_runtime_state: 探测 trae server 的 {uid: invalid}。"""

    def test_parses_accounts_payload(self):
        payload = {'accounts': [{'uid': 'u1', 'invalid': True},
                                {'uid': 'u2', 'invalid': False},
                                {'name': '无uid的行'}]}
        A_urlopen = A.urllib.request.urlopen
        A.urllib.request.urlopen = lambda req, timeout: _FakeResp(payload)
        try:
            rt = A._trae_runtime_state(_cfg())
        finally:
            A.urllib.request.urlopen = A_urlopen
        self.assertEqual(rt, {'u1': True, 'u2': False})

    def test_url_uses_config_port(self):
        """端口/监听必须取 config（providers.trae.port/listen），不写死 18787。"""
        seen = {}

        def fake_urlopen(req, timeout):
            seen['url'] = req.full_url
            seen['timeout'] = timeout
            return _FakeResp({'accounts': []})

        A_urlopen = A.urllib.request.urlopen
        A.urllib.request.urlopen = fake_urlopen
        try:
            cfg = _cfg()
            cfg['providers']['trae']['port'] = 19999
            A._trae_runtime_state(cfg)
        finally:
            A.urllib.request.urlopen = A_urlopen
        self.assertIn(':19999/v1/admin/accounts', seen['url'])
        self.assertLessEqual(seen['timeout'], 1.5, '探测超时不得拖慢账号列表')

    def test_unreachable_returns_none(self):
        A_urlopen = A.urllib.request.urlopen

        def boom(req, timeout):
            raise urllib.error.URLError('connection refused')

        A.urllib.request.urlopen = boom
        try:
            self.assertIsNone(A._trae_runtime_state(_cfg()))
        finally:
            A.urllib.request.urlopen = A_urlopen


class LoadAccountsWiringTest(unittest.TestCase):
    """_load_accounts_raw: Trae 行叠加运行态，其余通道不受牵连。"""

    def setUp(self):
        self._orig_cfg = A._read_config
        self._orig_probe = A._trae_runtime_state
        self._orig_prov = A._provider_runtime_state
        self.cfg = _cfg()
        A._read_config = lambda: self.cfg
        self.probe_calls = []

        def fake_probe(cfg):
            self.probe_calls.append(cfg)
            return self.runtime

        A._trae_runtime_state = fake_probe
        # 其余通道的进程内 provider 运行态: 桩成空表 (隔离 import main)
        A._provider_runtime_state = lambda pkey: {}
        self.addCleanup(lambda: setattr(A, '_read_config', self._orig_cfg))
        self.addCleanup(lambda: setattr(A, '_trae_runtime_state', self._orig_probe))
        self.addCleanup(lambda: setattr(A, '_provider_runtime_state', self._orig_prov))
        self.runtime = {'t1': True, 't2': False}

    def _rows(self):
        rows = A._load_accounts_raw()
        return {r['id']: r for r in rows}

    def test_invalid_account_turns_red(self):
        rows = self._rows()
        self.assertEqual(rows['Trae:t1']['status'], 'disconnected')
        self.assertIn('已失效', rows['Trae:t1']['detail'])

    def test_valid_account_stays_green(self):
        rows = self._rows()
        self.assertEqual(rows['Trae:t2']['status'], 'enabled')

    def test_probe_called_once_for_two_accounts(self):
        """运行态探测每次列表只发一次，不逐账号打。"""
        self._rows()
        self.assertEqual(len(self.probe_calls), 1)

    def test_service_down_marks_all_trae_red_only(self):
        """18787 连不上 → trae 全行标红，WorkBuddy 不受牵连。"""
        self.runtime = None
        rows = self._rows()
        self.assertEqual(rows['Trae:t1']['status'], 'disconnected')
        self.assertEqual(rows['Trae:t2']['status'], 'disconnected')
        self.assertIn('服务未响应', rows['Trae:t1']['detail'])
        self.assertEqual(rows['WorkBuddy:wb-1']['status'], 'enabled')
        self.assertEqual(rows['WorkBuddy:wb-1']['detail'], '')

    def test_all_rows_carry_detail_field(self):
        """detail 字段恒在（前端暂不展示，API 先行，形状稳定）。"""
        for r in A._load_accounts_raw():
            self.assertIn('detail', r)


if __name__ == '__main__':
    unittest.main()
