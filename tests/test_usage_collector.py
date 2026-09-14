# -*- coding: utf-8 -*-
"""
测试: scripts/usage_collector.py 的 WorkBuddy 系(国内/国际)采集接线
==================================================================
全部离线: 网络函数被替换成假的, 只验证「规格 → host/请求头/platform 字段」的接线,
确保国际版流水与获取积分都能落到正确的 platform 上。
运行: python -m unittest tests.test_usage_collector -v
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'scripts'))

import usage_collector as U  # noqa: E402


class SpecRegistryTest(unittest.TestCase):
    def test_both_regions_registered(self):
        keys = {s['platform'] for s in U.WB_SPECS}
        self.assertEqual(keys, {'workbuddy', 'workbuddy_intl'})

    def test_intl_spec_fields(self):
        s = U.WB_SPEC_BY_PLATFORM['workbuddy_intl']
        self.assertEqual(s['provider'], 'workbuddy_intl')
        self.assertEqual(s['base'], 'https://www.workbuddy.ai')
        self.assertEqual(s['default_domain'], 'www.workbuddy.ai')
        self.assertEqual(s['default_product'], 'workbuddy-ai')

    def test_cn_spec_unchanged(self):
        s = U.WB_SPEC_BY_PLATFORM['workbuddy']
        self.assertEqual(s['base'], 'https://copilot.tencent.com')
        self.assertEqual(s['default_product'], 'SaaS')

    def test_provider_index(self):
        self.assertIs(U.WB_SPEC_BY_PROVIDER['workbuddy_intl'],
                      U.WB_SPEC_BY_PLATFORM['workbuddy_intl'])


class HeaderTest(unittest.TestCase):
    ACC = {'userId': 'u1', 'accessToken': 'tok'}

    def test_cn_headers_have_no_product_code(self):
        h = U.wb_headers(self.ACC, 'www.workbuddy.cn', 'SaaS',
                         base='https://copilot.tencent.com')
        self.assertNotIn('X-Product-Code', h)
        self.assertEqual(h['X-Enterprise-Id'], 'u1')   # 流水接口必须带

    def test_intl_headers_carry_product_code(self):
        h = U.wb_headers(self.ACC, 'www.workbuddy.ai', 'workbuddy-ai',
                         base='https://www.workbuddy.ai')
        self.assertEqual(h['X-Product-Code'], 'workbuddy-ai')
        self.assertEqual(h['X-Domain'], 'www.workbuddy.ai')
        self.assertEqual(h['Authorization'], 'Bearer tok')


class RowBuildTest(unittest.TestCase):
    ITEM = {'requestId': 'req-1', 'credit': 1.5, 'model': 'deepseek-v4.1-flash',
            'client': 'WorkBuddy', 'requestTime': '2026-09-11 10:00:00'}

    def test_platform_is_parameterized(self):
        cn = U.wb_rows({'userId': 'u1'}, [self.ITEM], 1, platform='workbuddy')
        intl = U.wb_rows({'userId': 'u1'}, [self.ITEM], 1, platform='workbuddy_intl')
        self.assertEqual(cn[0][0], 'workbuddy')
        self.assertEqual(intl[0][0], 'workbuddy_intl')
        # 其余字段一致 (除了 platform)
        self.assertEqual(cn[0][1:], intl[0][1:])

    def test_entry_id_and_credits(self):
        row = U.wb_rows({'userId': 'u1'}, [self.ITEM], 1, platform='workbuddy_intl')[0]
        self.assertEqual(row[2], 'req-1')
        self.assertEqual(row[5], 1.5)
        self.assertEqual(row[4], 'deepseek-v4.1-flash')


class CollectIntlTest(unittest.TestCase):
    """验证 collect_workbuddy 走国际版规格时: 用对 host、写对 platform。"""

    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        U.db_init(self.conn)
        self.calls = []
        self._orig_fetch = U.wb_fetch_all
        self._orig_log = U.log

        def fake_fetch(headers, start_day, end_day, max_pages=U.MAX_PAGES,
                       base='https://copilot.tencent.com'):
            self.calls.append({'base': base, 'headers': headers})
            return [{'requestId': 'r1', 'credit': 2.0, 'model': 'm1',
                     'client': 'WorkBuddy', 'requestTime': '2026-09-11 10:00:00'}], None
        U.wb_fetch_all = fake_fetch
        U.log = lambda m: m

    def tearDown(self):
        U.wb_fetch_all = self._orig_fetch
        U.log = self._orig_log
        self.conn.close()

    def _cfg(self, **intl_over):
        intl = {'domain': 'www.workbuddy.ai', 'product': 'workbuddy-ai',
                'accounts': [{'userId': 'intl-1', 'accessToken': 'tok'}]}
        intl.update(intl_over)
        return {'providers': {
            'workbuddy': {'domain': 'www.workbuddy.cn', 'product': 'SaaS',
                          'accounts': [{'userId': 'cn-1', 'accessToken': 'tok'}]},
            'workbuddy_intl': intl}}

    def test_intl_spec_uses_intl_host(self):
        cfg = self._cfg()
        spec = U.WB_SPEC_BY_PLATFORM['workbuddy_intl']
        U.collect_workbuddy(self.conn, cfg, 1, spec=spec)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]['base'], 'https://www.workbuddy.ai')
        self.assertEqual(self.calls[0]['headers']['X-Product-Code'], 'workbuddy-ai')

    def test_intl_rows_land_with_intl_platform(self):
        cfg = self._cfg()
        U.collect_workbuddy(self.conn, cfg, 1,
                            spec=U.WB_SPEC_BY_PLATFORM['workbuddy_intl'])
        plats = [r[0] for r in self.conn.execute('SELECT platform FROM usage')]
        self.assertEqual(plats, ['workbuddy_intl'])

    def test_cn_spec_uses_cn_host(self):
        cfg = self._cfg()
        U.collect_workbuddy(self.conn, cfg, 1,
                            spec=U.WB_SPEC_BY_PLATFORM['workbuddy'])
        self.assertEqual(self.calls[0]['base'], 'https://copilot.tencent.com')
        self.assertNotIn('X-Product-Code', self.calls[0]['headers'])
        plats = [r[0] for r in self.conn.execute('SELECT platform FROM usage')]
        self.assertEqual(plats, ['workbuddy'])

    def test_default_spec_is_cn(self):
        """不传 spec 时保持旧行为 (国内版), 向后兼容。"""
        cfg = self._cfg()
        U.collect_workbuddy(self.conn, cfg, 1)
        self.assertEqual(self.calls[0]['base'], 'https://copilot.tencent.com')

    def test_collect_all_workbuddy_covers_both(self):
        cfg = self._cfg()
        msgs = U.collect_all_workbuddy(self.conn, cfg, 1)
        self.assertEqual(len(msgs), len(U.WB_SPECS))
        bases = sorted(c['base'] for c in self.calls)
        self.assertEqual(bases, ['https://copilot.tencent.com',
                                 'https://www.workbuddy.ai'])
        plats = sorted(r[0] for r in self.conn.execute('SELECT platform FROM usage'))
        self.assertEqual(plats, ['workbuddy', 'workbuddy_intl'])

    def test_disabled_account_skipped(self):
        cfg = self._cfg(accounts=[{'userId': 'intl-1', 'accessToken': 'tok',
                                   'enabled': False}])
        U.collect_workbuddy(self.conn, cfg, 1,
                            spec=U.WB_SPEC_BY_PLATFORM['workbuddy_intl'])
        self.assertEqual(self.calls, [])

    def test_no_accounts_is_noop(self):
        cfg = self._cfg(accounts=[])
        msg = U.collect_workbuddy(self.conn, cfg, 1,
                                  spec=U.WB_SPEC_BY_PLATFORM['workbuddy_intl'])
        self.assertIn('无账号', msg)
        self.assertEqual(self.calls, [])


class GainsIntlTest(unittest.TestCase):
    """验证获取积分 (gain) 对国际版也写对 platform。"""

    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        U.db_init(self.conn)
        self._orig = (U.wb_today_packs, U.wb_checkin_activity_status,
                      U.trae_checkin_status, U.log)
        # TRAE 无账号 → 不参与; WB 系两个平台 active=false (无渠道形态),
        # 走资源包兜底分支, 各返回一个今日入账包 (Bonus 类)
        U.wb_today_packs = lambda h, base='': ([('rid', 'Bonus Pack', 100.0)], None)
        U.wb_checkin_activity_status = lambda h, base='': (None, False, False, None)
        U.trae_checkin_status = lambda *a, **k: (None, False, 'skip')
        U.log = lambda m: m

    def tearDown(self):
        (U.wb_today_packs, U.wb_checkin_activity_status,
         U.trae_checkin_status, U.log) = self._orig
        self.conn.close()

    def _cfg(self):
        acc = lambda uid: [{'userId': uid, 'accessToken': 'tok'}]
        return {'providers': {
            'trae': {'accounts': []},
            'workbuddy': {'accounts': acc('cn-1')},
            'workbuddy_intl': {'domain': 'www.workbuddy.ai',
                               'product': 'workbuddy-ai',
                               'accounts': acc('intl-1')}}}

    def test_intl_gain_row_written(self):
        n = U.collect_gains(self.conn, self._cfg())
        self.assertEqual(n, 2)   # 国内 1 + 国际 1
        rows = dict((r[0], (r[1], r[2])) for r in
                    self.conn.execute('SELECT platform, uid, amount FROM gain'))
        self.assertIn('workbuddy_intl', rows)
        uid, amount = rows['workbuddy_intl']
        self.assertEqual(uid, 'intl-1')
        self.assertEqual(amount, 100.0)

    def test_registration_pack_excluded(self):
        """注册/订阅类礼包不算签到入账 (2026-09-11 新账号 100 分被误判吞掉的根因)。"""
        U.wb_today_packs = lambda h, base='': (
            [('rid1', 'Free Plan Subscription', 100.0),
             ('rid2', 'Bonus Pack', 30.0)], None)
        U.collect_gains(self.conn, self._cfg())
        kinds = [r[0] for r in self.conn.execute('SELECT kind FROM gain')]
        self.assertEqual(len(kinds), 2)   # 每平台只记 Bonus Pack, 排除订阅礼包
        self.assertTrue(all('Free Plan' not in k for k in kinds))

    def test_active_account_uses_checkin_row(self):
        """active=true + today_checked_in=true → 直接写 checkin 行, 不扫资源包。"""
        U.wb_checkin_activity_status = lambda h, base='': (100, True, True, None)
        U.wb_today_packs = lambda h, base='': (_ for _ in ()).throw(
            AssertionError('active 已签时不应再扫资源包'))
        n = U.collect_gains(self.conn, self._cfg())
        self.assertEqual(n, 2)
        kinds = [r[0] for r in self.conn.execute('SELECT kind FROM gain')]
        self.assertEqual(set(kinds), {'checkin'})

    def test_active_not_checked_writes_nothing(self):
        """active=true 但未签 → 不写行 (gain 无记录=未签), 也不冒充资源包入账。"""
        U.wb_checkin_activity_status = lambda h, base='': (0, False, True, None)
        U.wb_today_packs = lambda h, base='': ([('rid', 'Bonus Pack', 5.0)], None)
        n = U.collect_gains(self.conn, self._cfg())
        self.assertEqual(n, 0)

    def test_gain_day_is_today(self):
        U.collect_gains(self.conn, self._cfg())
        days = {r[0] for r in self.conn.execute('SELECT day FROM gain')}
        self.assertEqual(days, {time.strftime('%Y-%m-%d')})

    def test_intl_gain_uses_intl_base(self):
        seen = []
        U.wb_today_packs = lambda h, base='': (seen.append(base) or
                                               ([('rid', 'p', 5.0)], None))
        U.collect_gains(self.conn, self._cfg())
        self.assertIn('https://www.workbuddy.ai', seen)
        self.assertIn('https://copilot.tencent.com', seen)


if __name__ == '__main__':
    unittest.main()
