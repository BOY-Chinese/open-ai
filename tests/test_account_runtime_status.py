# -*- coding: utf-8 -*-
"""
测试: 四通道「真状态」接入 —— provider 运行态健康跟踪 + admin 合并
================================================================
背景（2026-09-19 trae 全通道断连事故）：trae 接入运行态后，本测试把
WorkBuddy / WorkBuddy 国际版 / Loomy 也接上 —— provider 在请求路径上
把「上游明确的鉴权拒绝」沉淀成 AccountHealth，admin_api 读取合并。

覆盖:
1. AccountHealth —— 标记/解除/凭据变化自动解除（新凭据给新机会）。
2. WorkBuddyProvider / LoomyProvider —— 鉴权拒绝标失效、成功清除、
   轮换跳过失效账号、全部失效给出可读结论（不发真实网络请求）。
3. admin_api._load_accounts_raw —— 三通道行均叠加运行态，每通道只探一次。

运行: python -m unittest tests.test_account_runtime_status -v
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
from providers.base import AccountHealth, Provider  # noqa: E402
from providers.loomy import LoomyProvider, AccountUnavailable  # noqa: E402
from providers import workbuddy as WB  # noqa: E402
from providers.workbuddy import WorkBuddyProvider  # noqa: E402


# ═══════════════════════ AccountHealth ═══════════════════════

class AccountHealthTest(unittest.TestCase):

    def test_mark_and_state(self):
        h = AccountHealth()
        self.assertEqual(h.state(), {})
        h.mark_invalid('u1', reason='测试')
        h.mark_invalid('u2')
        self.assertEqual(h.state(), {'u1': True, 'u2': True})
        self.assertTrue(h.is_invalid('u1'))

    def test_mark_ok_clears(self):
        h = AccountHealth()
        h.mark_invalid('u1')
        h.mark_ok('u1')
        self.assertFalse(h.is_invalid('u1'))

    def test_empty_uid_ignored(self):
        h = AccountHealth()
        h.mark_invalid('')
        h.mark_invalid(None)
        self.assertEqual(h.state(), {})

    def test_same_token_does_not_clear(self):
        """旧凭据上的失效判决, 凭据没变就不能翻案。"""
        h = AccountHealth()
        h.observe_token('u1', 'tok-a')
        h.mark_invalid('u1')
        h.observe_token('u1', 'tok-a')
        self.assertTrue(h.is_invalid('u1'))

    def test_token_change_clears(self):
        """新凭据 = 新机会: 换 token 自动解除失效 (重新登录/续期写回场景)。"""
        h = AccountHealth()
        h.observe_token('u1', 'tok-a')
        h.mark_invalid('u1')
        h.observe_token('u1', 'tok-b')
        self.assertFalse(h.is_invalid('u1'))


# ═══════════════════════ Provider 侧挂钩 ═══════════════════════

class _FakeResp:
    """httpx 流式响应的最小桩 (status/aread/aclose/aiter_lines)。"""

    def __init__(self, status, lines=()):
        self.status_code = status
        self._lines = list(lines)

    async def aread(self):
        return b'{}'

    async def aclose(self):
        return None

    def aiter_lines(self):
        async def _gen():
            for l in self._lines:
                yield l
        return _gen()


def _drain(agen):
    async def _run():
        out = []
        async for x in agen:
            out.append(x)
        return out
    return asyncio.run(_run())


class _MinimalProvider(Provider):
    name = 'minimal'

    def list_models(self):
        return []

    def normalize_model(self, model):
        return model

    async def chat(self, body):
        return {}

    async def stream(self, body):
        yield {}


class DefaultRuntimeStateTest(unittest.TestCase):

    def test_base_provider_returns_empty(self):
        """无健康跟踪的 provider: runtime_account_state 默认空表 (不炸)。"""
        self.assertEqual(_MinimalProvider().runtime_account_state(), {})


class WorkBuddyRuntimeTest(unittest.TestCase):
    """国内版 provider: 鉴权拒绝标失效 / 成功清除 / 轮换跳过失效账号。"""

    def setUp(self):
        self.accounts = [
            {'userId': 'u1', 'accessToken': 'a1', 'refreshToken': 'r1'},
            {'userId': 'u2', 'accessToken': 'a2', 'refreshToken': 'r2'},
        ]
        # _pick_account 每次实时读 config 文件 —— 打桩到临时文件
        self.tmp = tempfile.NamedTemporaryFile(
            mode='w', suffix='.json', delete=False, encoding='utf-8')
        json.dump({'providers': {'workbuddy': {'accounts': self.accounts}}},
                  self.tmp, ensure_ascii=False)
        self.tmp.close()
        self._orig_path = WB._WB_CONFIG_PATH
        WB._WB_CONFIG_PATH = self.tmp.name
        self.addCleanup(lambda: setattr(WB, '_WB_CONFIG_PATH', self._orig_path))
        self.addCleanup(lambda: os.unlink(self.tmp.name))
        self.prov = WorkBuddyProvider({
            'accounts': [dict(a) for a in self.accounts], 'switchEvery': 10,
        })

    def test_chat_auth_reject_marks_invalid(self):
        async def fake_upstream(up_body, account, retried=False):
            return _FakeResp(401)

        self.prov._request_upstream = fake_upstream
        with self.assertRaises(RuntimeError):
            _drain(self.prov._chat_events({'model': 'x'}, self.prov.accounts[0]))
        self.assertTrue(self.prov._health.is_invalid('u1'))

    def test_chat_success_marks_ok(self):
        async def fake_upstream(up_body, account, retried=False):
            return _FakeResp(200)

        self.prov._health.mark_invalid('u1')
        self.prov._request_upstream = fake_upstream
        _drain(self.prov._chat_events({'model': 'x'}, self.prov.accounts[0]))
        self.assertFalse(self.prov._health.is_invalid('u1'))

    def test_pick_skips_invalid_account(self):
        self.prov._health.mark_invalid('u1')
        acc = self.prov._pick_account()
        self.assertEqual(acc.get('userId'), 'u2', '失效账号应被轮换跳过')

    def test_pick_empty_when_all_invalid(self):
        for a in self.accounts:
            self.prov._health.mark_invalid(a['userId'])
        self.assertEqual(self.prov._pick_account(), {})

    def test_runtime_state_shape(self):
        self.prov._health.mark_invalid('u2')
        self.assertEqual(self.prov.runtime_account_state(), {'u2': True})


class LoomyRuntimeTest(unittest.TestCase):
    """Loomy provider: session 失效标失效 / 成功清除 / 轮换跳过与全失效结论。"""

    def setUp(self):
        self.prov = LoomyProvider({})
        self.fake_accounts = [
            {'userid': '26091', '_token': 's1', 'phone': '13800000001'},
            {'userid': '26092', '_token': 's2', 'phone': '13800000002'},
        ]
        self._orig_load = None
        import providers.loomy as LM
        self._LM = LM

        def fake_load():
            return [dict(a) for a in self.fake_accounts]

        self._orig_load = LM.load_config_accounts
        LM.load_config_accounts = fake_load
        self.addCleanup(lambda: setattr(LM, 'load_config_accounts', self._orig_load))

    def test_auth_reject_marks_invalid_and_rotates(self):
        calls = []

        async def fake_upstream(body, token):
            calls.append(token)
            # 第一个账号 401, 第二个账号成功
            return _FakeResp(401 if token == 's1' else 200)

        self.prov._request_upstream = fake_upstream
        _drain(self.prov._chat_events({'model': 'x'}))
        self.assertTrue(self.prov._health.is_invalid('26091'))
        self.assertFalse(self.prov._health.is_invalid('26092'))
        self.assertEqual(calls, ['s1', 's2'], '401 后应换下一个账号重试')

    def test_invalid_account_is_not_retried(self):
        """失效账号被轮换跳过 —— 不再浪费注定失败的请求。"""
        calls = []

        async def fake_upstream(body, token):
            calls.append(token)
            return _FakeResp(200)

        self.prov._request_upstream = fake_upstream
        self.prov._health.mark_invalid('26091')
        _drain(self.prov._chat_events({'model': 'x'}))
        self.assertEqual(calls, ['s2'], '失效账号不应再被打')

    def test_relogin_new_session_recovers(self):
        """重新登录(session 变化)自动解除失效, 无需重启网关。

        生产时序: 每次请求都会先 observe_token 记录当前 session, 失效标记
        只会落在「已观察过的凭据」上 —— 故先跑一轮成功请求建立凭据基线。
        本轮轮询谁先被起用取决于游标, 这里只断言语义: 失效标记已解除。
        """
        calls = []

        async def fake_upstream(body, token):
            calls.append(token)
            return _FakeResp(200)

        self.prov._request_upstream = fake_upstream
        _drain(self.prov._chat_events({'model': 'x'}))  # 基线: 观察到 s1/s2
        self.prov._health.mark_invalid('26091')         # session 被判死
        self.fake_accounts[0]['_token'] = 's1-new'      # 重新登录
        _drain(self.prov._chat_events({'model': 'x'}))  # observe → 解除失效
        self.assertFalse(self.prov._health.is_invalid('26091'))
        self.assertEqual(calls, ['s1', 's2'])

    def test_all_invalid_short_circuits(self):
        """全部失效 → 不再发任何请求, 直接给可读结论。"""
        sent = []

        async def fake_upstream(body, token):
            sent.append(token)
            return _FakeResp(200)

        self.prov._request_upstream = fake_upstream
        for a in self.fake_accounts:
            self.prov._health.mark_invalid(a['userid'])
        with self.assertRaises(AccountUnavailable):
            _drain(self.prov._chat_events({'model': 'x'}))
        self.assertEqual(sent, [], '全部失效时不应发出任何上游请求')

    def test_runtime_state_shape(self):
        self.prov._health.mark_invalid('26092')
        self.assertEqual(self.prov.runtime_account_state(), {'26092': True})


# ═══════════════════════ admin_api 接线 ═══════════════════════

def _cfg():
    """最小四通道 config (形状与真实 config.json 的 providers 段一致)。"""
    return {
        'providers': {
            'trae': {'port': 18787, 'listen': '127.0.0.1',
                     'accounts': [{'uid': 't1', 'token': 'x'}]},
            'workbuddy': {'domain': 'www.workbuddy.cn', 'product': 'SaaS',
                          'accounts': [{'userId': 'wb-1', 'accessToken': 'y'}]},
            'workbuddy_intl': {'domain': 'www.workbuddy.ai', 'product': 'wb-ai',
                               'accounts': [{'userId': 'wbie-1', 'accessToken': 'z'}]},
            'loomy': {'accounts': [
                {'userid': '26091', 'session': 'sess', 'phone': '13800000001',
                 'name': 'Loomy(13800000001)'},
            ]},
        },
    }


class LoadAccountsFourChannelTest(unittest.TestCase):
    """_load_accounts_raw: 四通道行均叠加运行态, 每通道只探一次。"""

    def setUp(self):
        self._orig_cfg = A._read_config
        self._orig_trae = A._trae_runtime_state
        self._orig_prov = A._provider_runtime_state
        self.cfg = _cfg()
        A._read_config = lambda: self.cfg
        A._trae_runtime_state = lambda cfg: self.trae_rt
        self.probe_calls = []

        def fake_prov_state(pkey):
            self.probe_calls.append(pkey)
            return self.prov_rt.get(pkey, {})

        A._provider_runtime_state = fake_prov_state
        self.addCleanup(lambda: setattr(A, '_read_config', self._orig_cfg))
        self.addCleanup(lambda: setattr(A, '_trae_runtime_state', self._orig_trae))
        self.addCleanup(lambda: setattr(A, '_provider_runtime_state', self._orig_prov))
        self.trae_rt = {}
        self.prov_rt = {}

    def _rows(self):
        return {r['id']: r for r in A._load_accounts_raw()}

    def test_wb_invalid_turns_red(self):
        self.prov_rt = {'workbuddy': {'wb-1': True}}
        rows = self._rows()
        self.assertEqual(rows['WorkBuddy:wb-1']['status'], 'disconnected')
        self.assertIn('已失效', rows['WorkBuddy:wb-1']['detail'])

    def test_wbie_and_loomy_stay_green_when_healthy(self):
        rows = self._rows()
        self.assertEqual(rows['WorkBuddy_IE:wbie-1']['status'], 'enabled')
        self.assertEqual(rows['Loomy:26091']['status'], 'enabled')

    def test_loomy_invalid_turns_red(self):
        self.prov_rt = {'loomy': {'26091': True}}
        rows = self._rows()
        self.assertEqual(rows['Loomy:26091']['status'], 'disconnected')
        self.assertIn('重新登录', rows['Loomy:26091']['detail'])

    def test_provider_missing_marks_channel_red(self):
        """provider 未注册(返回 None) → 该通道整列标红, 其它通道不受牵连。"""
        self.prov_rt = {'workbuddy': None, 'workbuddy_intl': None, 'loomy': None}
        rows = self._rows()
        for rid in ('WorkBuddy:wb-1', 'WorkBuddy_IE:wbie-1', 'Loomy:26091'):
            self.assertEqual(rows[rid]['status'], 'disconnected', rid)
            self.assertIn('未加载', rows[rid]['detail'])
        self.assertEqual(rows['Trae:t1']['status'], 'enabled')

    def test_each_channel_probed_once(self):
        self._rows()
        self.assertEqual(sorted(self.probe_calls),
                         ['loomy', 'workbuddy', 'workbuddy_intl'])


if __name__ == '__main__':
    unittest.main()
