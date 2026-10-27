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

    def __init__(self, status, lines=(), body=b'{}'):
        self.status_code = status
        self._lines = list(lines)
        self._body = body

    async def aread(self):
        return self._body

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

    # ───── 2026-10-24：内容审核拦截 ≠ 鉴权失效（修「假断连」）─────
    # 实测上游（讯飞星火）响应：
    #   HTTP 400 {"error":{"message":"内容可能包含敏感信息，无法处理，请调整后重试",
    #            "type":"data_inspection_failed","code":400}}
    # 旧版把 403/400 当鉴权失败 → mark_invalid() 标死账号 → 后续正常对话全挂。

    INSPECTION = json.dumps({"error": {
        "message": "内容可能包含敏感信息，无法处理，请调整后重试",
        "type": "data_inspection_failed", "param": None, "code": 400,
        "metadata": {"provider_name": "iflytek-spark-loomy"}}},
        ensure_ascii=False).encode('utf-8')

    def test_inspection_block_does_not_mark_invalid(self):
        """审核拦截：必须抛错、且**绝不**把账号标成失效、也不该换账号。"""
        calls = []

        async def fake_upstream(body, token):
            calls.append(token)
            return _FakeResp(400, body=self.INSPECTION)

        self.prov._request_upstream = fake_upstream
        with self.assertRaises(RuntimeError) as ctx:
            _drain(self.prov._chat_events({'model': 'x'}))
        self.assertIn('审核', str(ctx.exception))
        # ★ 核心断言：账号不能被标死
        self.assertFalse(self.prov._health.is_invalid('26091'))
        self.assertFalse(self.prov._health.is_invalid('26092'))
        # 也不该拿同一段内容去撞第二个账号
        self.assertEqual(len(calls), 1, '审核拦截不该换账号重试')

    def test_inspection_on_403_also_not_invalid(self):
        """403 但带审核特征 → 同样不能标死（旧版 403 无条件判鉴权失败）。"""
        async def fake_upstream(body, token):
            return _FakeResp(403, body=self.INSPECTION)

        self.prov._request_upstream = fake_upstream
        with self.assertRaises(RuntimeError):
            _drain(self.prov._chat_events({'model': 'x'}))
        self.assertFalse(self.prov._health.is_invalid('26091'))

    def test_info_403_without_auth_feature_not_marked_invalid(self):
        """403 但既不像审核也不像鉴权（限流/风控抖动）→ 换账号但**不标死**。

        实测上游在抖动时也会回 403；标死会造成「假断连」。
        """
        async def fake_upstream(body, token):
            if token == 's1':
                return _FakeResp(403, body=b'{"error":"forbidden"}')
            return _FakeResp(200, lines=['data: {"choices":[]}', 'data: [DONE]'])

        self.prov._request_upstream = fake_upstream
        _drain(self.prov._chat_events({'model': 'x'}))
        self.assertFalse(self.prov._health.is_invalid('26091'),
                         '无鉴权特征的 403 不允许标死账号')

    def test_real_401_still_marks_invalid(self):
        """真鉴权失效（401）必须保持原行为：标死 + 换账号。"""
        async def fake_upstream(body, token):
            return _FakeResp(401, body=b'{"error":"unauthorized"}')

        self.prov._request_upstream = fake_upstream
        with self.assertRaises(RuntimeError):
            _drain(self.prov._chat_events({'model': 'x'}))
        self.assertTrue(self.prov._health.is_invalid('26091'))
        self.assertTrue(self.prov._health.is_invalid('26092'))

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


# ═════════ 2026-10-14 假断连回归: 主动清牌入口 ═════════
#
# 事故现象：Loomy 通道 13800000000 这个账号「刚重新添加过，又显示断连」。
# 根因：失效标记活在**网关进程内存**里，而重新登录只写 config.json ——
# 两者之间原先没有任何通路；`observe_token` 的自动复位要等下一发该通道的
# 请求跑过请求路径才生效。trae 侧早有对等能力（/v1/admin/reconnect →
# healthCheck 里 acc.invalid=false），但 loomy / workbuddy* 三个进程内通道
# **都没有**主动清牌入口。

class ClearInvalidTest(unittest.TestCase):
    """AccountHealth.clear_invalid —— 单账号 / 全清 / 幂等。"""

    def test_clear_single_account(self):
        h = AccountHealth()
        h.mark_invalid('u1')
        h.mark_invalid('u2')
        self.assertEqual(h.clear_invalid('u1'), 1)
        self.assertFalse(h.is_invalid('u1'))
        self.assertTrue(h.is_invalid('u2'), '不能误伤其它账号')

    def test_clear_all(self):
        h = AccountHealth()
        for u in ('u1', 'u2', 'u3'):
            h.mark_invalid(u)
        self.assertEqual(h.clear_invalid(), 3)
        self.assertEqual([u for u in h.state() if h.is_invalid(u)], [])

    def test_clear_returns_zero_when_not_invalid(self):
        """幂等: 对本来就没红的账号清牌, 返回 0 且不报错。"""
        h = AccountHealth()
        self.assertEqual(h.clear_invalid('u1'), 0)
        h.mark_ok('u1')
        self.assertEqual(h.clear_invalid('u1'), 0)

    def test_clear_empty_uid_is_noop(self):
        h = AccountHealth()
        self.assertEqual(h.clear_invalid(''), 0)
        self.assertEqual(h.clear_invalid(None), 0)


class ProviderClearHookTest(unittest.TestCase):
    """Provider.clear_account_runtime —— 默认实现走 _health, 无 health 时空操作。"""

    def test_loomy_clear_hook(self):
        cfg = {'loomy': {'accounts': [
            {'userid': '26091', 'session': 's1', 'phone': '13800000000'}]}}
        with tempfile.TemporaryDirectory() as d:
            self._patch_config(d, cfg)
            prov = LoomyProvider({})
            prov._health.mark_invalid('26091')
            self.assertTrue(prov.runtime_account_state().get('26091'))
            self.assertEqual(prov.clear_account_runtime('26091'), 1)
            self.assertFalse(prov.runtime_account_state().get('26091'))

    def test_provider_without_health_is_safe_noop(self):
        """没有 _health 的 provider 调清牌应是安全空操作（返回 0, 不抛）。"""
        class _Bare(Provider):
            name = 'bare'

            def list_models(self): return []
            def normalize_model(self, m): return m
            async def chat(self, body): return {}
            async def stream(self, body): yield {}

        self.assertEqual(_Bare().clear_account_runtime('u1'), 0)

    def _patch_config(self, d, cfg):
        p = os.path.join(d, 'config.json')
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(cfg, f)
        import providers.loomy as L
        orig = L.CONFIG_PATH
        L.CONFIG_PATH = p
        self.addCleanup(lambda: setattr(L, 'CONFIG_PATH', orig))


class AdminClearWiringTest(unittest.TestCase):
    """admin_api._clear_channel_runtime —— 通道名映射与未注册通道的容错。"""

    def setUp(self):
        self.orig = A._clear_channel_runtime
        self.addCleanup(lambda: setattr(A, '_clear_channel_runtime', self.orig))

    def test_returns_false_when_provider_not_registered(self):
        """provider 未注册 → False, 绝不抛异常打断管理接口。

        注意: 单测进程里 `import main` 是成功的（main 可导入且 PROVIDERS 已
        被注册），所以这里显式把该通道从 PROVIDERS 摘掉来模拟「未加载」，
        而不是依赖 import 失败。
        """
        import main as gateway  # noqa: E402
        orig_providers = getattr(gateway, 'PROVIDERS', None)
        gateway.PROVIDERS = dict(orig_providers or {})
        gateway.PROVIDERS.pop('loomy', None)
        self.addCleanup(lambda: setattr(gateway, 'PROVIDERS', orig_providers or {}))
        self.assertFalse(A._clear_channel_runtime('loomy'))

    def test_calls_provider_clear_with_uid(self):
        seen = []

        class _FakeProv:
            def clear_account_runtime(self, uid=None):
                seen.append(uid)
                return 1

        import main as gateway  # noqa: E402
        orig_providers = getattr(gateway, 'PROVIDERS', None)
        gateway.PROVIDERS = {'loomy': _FakeProv()}
        self.addCleanup(lambda: setattr(gateway, 'PROVIDERS', orig_providers or {}))
        self.assertTrue(A._clear_channel_runtime('loomy', '26092'))
        self.assertEqual(seen, ['26092'])


class RefreshClearsRuntimeTest(unittest.TestCase):
    """★ 端到端复现原事故: 账号被标红 → 点「刷新」→ 列表应立刻转绿。

    这是用户报的那句「我明明刚重新添加过，又显示断连」的最小复现场景。
    """

    def setUp(self):
        self.cfg = _cfg()
        self.orig_cfg = A._read_config
        self.orig_trae = A._trae_runtime_state
        self.orig_prov = A._provider_runtime_state
        self.orig_clear = A._clear_channel_runtime
        A._read_config = lambda: self.cfg
        A._trae_runtime_state = lambda cfg: {}
        # 内存态: loomy 的 26091 被标红（模拟旧 session 被判死后的残留红牌）
        self.rt = {'loomy': {'26091': True}}
        A._provider_runtime_state = lambda pkey: self.rt.get(pkey, {})
        self.addCleanup(lambda: setattr(A, '_read_config', self.orig_cfg))
        self.addCleanup(lambda: setattr(A, '_trae_runtime_state', self.orig_trae))
        self.addCleanup(lambda: setattr(A, '_provider_runtime_state', self.orig_prov))
        self.addCleanup(lambda: setattr(A, '_clear_channel_runtime', self.orig_clear))

    def test_refresh_clears_stale_flag_and_turns_green(self):
        # 清牌入口接到同一个内存态上（模拟真实 provider._health.clear_invalid）
        def fake_clear(pkey, uid=None):
            d = self.rt.setdefault(pkey, {})
            if uid is None:
                d.clear()
            else:
                d[uid] = False
            return True

        A._clear_channel_runtime = fake_clear
        rows = {r['id']: r for r in A._load_accounts_raw()}
        self.assertEqual(rows['Loomy:26091']['status'], 'disconnected',
                         '前置: 有残留红牌时应显示断连')

        # 用户点「刷新」→ 应复位标记后重读 → 转绿
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            A.refresh_accounts({'channel': 'Loomy'}))
        rows = {r['id']: r for r in A._load_accounts_raw()}
        self.assertEqual(rows['Loomy:26091']['status'], 'enabled',
                         '清牌后应立刻转绿, 不该还挂着旧红牌')
        self.assertEqual(rows['Loomy:26091']['detail'], '')


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
        """每个通道**恰好**探一次运行态（懒探测 + 结果缓存）。

        2026-10：三新通道（Qoder 国内/国际 + CodeArts）与旧三家同规则 ——
        它们都是**进程内 provider**，运行态走 _PROVIDER_INSTANCE_KEYS 映射；
        config 段名带下划线、注册键带连字符，两者都要在表里登记，否则
        probe 会去查一个不存在的键（漏登记时本条断言先炸）。
        """
        self._rows()
        self.assertEqual(sorted(self.probe_calls),
                         ['codearts', 'loomy', 'qoder_cn', 'qoder_intl',
                          'workbuddy', 'workbuddy_intl'])


if __name__ == '__main__':
    unittest.main()
