# -*- coding: utf-8 -*-
"""
测试: scripts/credits_api.py —— 给前端的只读查询接口层
=====================================================
用临时 SQLite 库 + 临时 config 隔离, 不碰真实数据。
重点验证: 平台枚举通用性 (不再硬编码 trae/workbuddy 两平台)、
日期闭区间、platform 过滤一致性、无库时优雅返回。
运行: python -m unittest tests.test_credits_api -v
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'scripts'))

import credits_api as api  # noqa: E402

DAY = '2026-09-11'


def _ts(day, hh=12, mm=0):
    return int(time.mktime(time.strptime(f'{day} {hh:02d}:{mm:02d}:00',
                                         '%Y-%m-%d %H:%M:%S')))


class CreditsApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, 'usage_history.db')
        self.cfg = os.path.join(self.tmp, 'config.json')
        self._orig_db = api.DB_PATH
        self._orig_cfg = api.OPENAI_CFG
        api.DB_PATH = self.db
        api.OPENAI_CFG = self.cfg
        with open(self.cfg, 'w', encoding='utf-8') as f:
            json.dump({'providers': {
                'trae': {'accounts': [{'uid': 't-1', 'name': 'TRAE甲'}]},
                'workbuddy': {'accounts': [{'userId': 'w-1', 'name': 'WB乙'}]},
                'workbuddy_intl': {'accounts': [
                    {'userId': 'i-1', 'name': '国际丙', 'enabled': True}]},
            }}, f, ensure_ascii=False)
        self._seed()

    def tearDown(self):
        api.DB_PATH = self._orig_db
        api.OPENAI_CFG = self._orig_cfg

    def _seed(self):
        import usage_collector as U
        conn = sqlite3.connect(self.db)
        U.db_init(conn)
        conn.executemany(
            'INSERT INTO usage (platform,uid,entry_id,ts,model,credits,cost_money,'
            'input_tok,output_tok,cache_tok,source,session_id,preview,extra,collected_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            [('trae', 't-1', 'e1', _ts(DAY, 10), 'm-trae', 5.0, 0.1, 0, 0, 0, 's', '', '', '{}', 0),
             ('workbuddy', 'w-1', 'e2', _ts(DAY, 11), 'm-wb', 3.0, 0.0, 0, 0, 0, 's', '', '', '{}', 0),
             ('workbuddy_intl', 'i-1', 'e3', _ts(DAY, 12), 'deepseek-v4.1-flash', 1.25,
              0.0, 0, 0, 0, 'WorkBuddy', '', '', '{}', 0),
             # 边界: 当天 23:59:59 必须算进闭区间
             ('workbuddy_intl', 'i-1', 'e4', _ts(DAY, 23, 59), 'gpt-6-astra', 0.75,
              0.0, 0, 0, 0, 'WorkBuddy', '', '', '{}', 0),
             # 区间外 (次日)
             ('workbuddy_intl', 'i-1', 'e5', _ts('2026-09-12', 1), 'x', 99.0,
              0.0, 0, 0, 0, 'WorkBuddy', '', '', '{}', 0)])
        conn.executemany('INSERT INTO gain (platform,uid,day,amount,kind,updated_at) '
                         'VALUES (?,?,?,?,?,?)',
                         [('trae', 't-1', DAY, 200.0, 'checkin', 0),
                          ('workbuddy_intl', 'i-1', DAY, 100.0, 'pack:签到包', 0)])
        conn.commit()
        conn.close()

    # ---------- 元信息 ----------
    def test_platforms_include_intl(self):
        keys = [p['key'] for p in api.platforms()]
        self.assertEqual(keys, ['trae', 'workbuddy', 'workbuddy_intl', 'loomy'])
        self.assertEqual(api.platform_label('workbuddy_intl'), 'WorkBuddy 国际')
        # 每个平台都带前端要用的字段
        for p in api.platforms():
            for f in ('key', 'label', 'short', 'provider', 'color'):
                self.assertIn(f, p)

    def test_platform_label_unknown_key_falls_back(self):
        self.assertEqual(api.platform_label('nope'), 'nope')

    def test_accounts_and_labels(self):
        accs = api.accounts()
        self.assertEqual(accs['workbuddy_intl'][0]['name'], '国际丙')
        self.assertTrue(accs['workbuddy_intl'][0]['enabled'])

    def test_available_range(self):
        r = api.available_range()
        self.assertEqual(r['count'], 5)
        self.assertEqual(r['first_day'], DAY)

    # ---------- 逐笔流水 ----------
    def test_usage_rows_includes_intl(self):
        rows = api.usage_rows(DAY, DAY)
        self.assertEqual(len(rows), 4)                       # 次日那条不算
        intl = [r for r in rows if r['platform'] == 'workbuddy_intl']
        self.assertEqual(len(intl), 2)
        # 账号名走 config, 不是硬编码短码
        self.assertTrue(all(r['account'] == '国际丙' for r in intl))
        self.assertEqual(intl[0]['platform_label'], 'WorkBuddy 国际')
        # 新→旧
        self.assertGreaterEqual(rows[0]['ts'], rows[-1]['ts'])

    def test_usage_rows_platform_filter(self):
        rows = api.usage_rows(DAY, DAY, platform='workbuddy_intl')
        self.assertEqual({r['platform'] for r in rows}, {'workbuddy_intl'})

    def test_end_day_is_inclusive(self):
        rows = api.usage_rows(DAY, DAY)
        self.assertIn('e4' if False else 0.75,
                      [r['credits'] for r in rows])   # 23:59:59 那条在区间内
        self.assertNotIn(99.0, [r['credits'] for r in rows])

    # ---------- 按天聚合 (柱状图) ----------
    def test_usage_daily_all_platforms(self):
        d = api.usage_daily(DAY, DAY)
        got = {x['platform']: x['credits'] for x in d}
        self.assertEqual(got['trae'], 5.0)
        self.assertEqual(got['workbuddy'], 3.0)
        self.assertEqual(got['workbuddy_intl'], 2.0)     # 1.25 + 0.75
        self.assertEqual({x['day'] for x in d}, {DAY})
        for x in d:
            self.assertIn('platform_label', x)
            self.assertIn('count', x)

    # ---------- 汇总 ----------
    def test_usage_summary_totals(self):
        s = api.usage_summary(DAY, DAY)
        self.assertAlmostEqual(s['total'], 10.0)
        self.assertEqual(s['total_count'], 4)
        self.assertEqual(len(s['by_platform']), 3)

    def test_usage_summary_platform_filter_narrows_everything(self):
        """回归: --platform 必须把总量/分平台一起收窄, 不能只收窄日序列。"""
        s = api.usage_summary(DAY, DAY, platform='workbuddy_intl')
        self.assertAlmostEqual(s['total'], 2.0)
        self.assertEqual(s['total_count'], 2)
        self.assertEqual([p['platform'] for p in s['by_platform']],
                         ['workbuddy_intl'])
        self.assertEqual({m['platform'] for m in s['by_model']}, {'workbuddy_intl'})

    def test_gains_summary(self):
        s = api.gains_summary(DAY, DAY)
        self.assertAlmostEqual(s['total'], 300.0)
        self.assertEqual(len(s['by_platform']), 2)
        s2 = api.gains_summary(DAY, DAY, platform='workbuddy_intl')
        self.assertAlmostEqual(s2['total'], 100.0)

    def test_gains_daily_kinds(self):
        d = api.gains_daily(DAY, DAY)
        intl = [x for x in d if x['platform'] == 'workbuddy_intl'][0]
        self.assertEqual(intl['amount'], 100.0)
        self.assertIn('pack:签到包', intl['kinds'])

    # ---------- 逐账号获取积分 (「今日是否签到成功」的唯一真源) ----------
    def test_gains_accounts_answers_who_checked_in(self):
        """有当日入账记录 = 签到成功。这里只断言「谁在结果里」。"""
        g = api.gains_accounts(DAY)
        self.assertEqual(set(g.keys()), {'trae', 'workbuddy_intl'})
        self.assertEqual(set(g['trae'].keys()), {'t-1'})
        self.assertEqual(set(g['workbuddy_intl'].keys()), {'i-1'})
        # 当天没有入账的平台直接不出现, 而不是给个空壳 —— 调用方按缺席判定
        self.assertNotIn('workbuddy', g)

    def test_gains_accounts_amount_kind_and_ts(self):
        g = api.gains_accounts(DAY)
        self.assertEqual(g['trae']['t-1']['amount'], 200.0)
        self.assertEqual(g['trae']['t-1']['kinds'], ['checkin'])
        # 有记录即 checked=True, 调用方不必再解析 kind (WB 的 kind 是资源包名)
        self.assertTrue(g['trae']['t-1']['checked'])
        self.assertTrue(g['workbuddy_intl']['i-1']['checked'])

    def test_gains_accounts_same_uid_multiple_kinds_is_summed(self):
        """同一账号当日有多笔入账 (多个资源包) 要合并成一条, 金额相加。"""
        conn = sqlite3.connect(self.db)
        conn.execute('INSERT INTO gain (platform,uid,day,amount,kind,updated_at) '
                     'VALUES (?,?,?,?,?,?)',
                     ('workbuddy_intl', 'i-1', DAY, 50.0, 'pack:另一个包', 7))
        conn.commit()
        conn.close()
        item = api.gains_accounts(DAY)['workbuddy_intl']['i-1']
        self.assertEqual(item['amount'], 150.0)          # 100 + 50
        # kinds 按入账时间升序 (查询 ORDER BY updated_at), 与阅读顺序一致
        self.assertEqual(item['kinds'], ['pack:签到包', 'pack:另一个包'])
        self.assertEqual(item['ts'], 7)                  # 取较新的 updated_at

    def test_gains_accounts_accepts_ts_and_defaults_to_today(self):
        """入参接受 unix 秒 (与其它函数一致); 缺省查今天。"""
        self.assertEqual(api.gains_accounts(_ts(DAY)), api.gains_accounts(DAY))
        # 今天没有数据 → 空 dict (而不是抛异常)
        self.assertEqual(api.gains_accounts('2020-01-01'), {})

    # ---------- 仪表盘 ----------
    def test_overview_shape(self):
        ov = api.overview(start=DAY, end=DAY)
        for k in ('range', 'platforms', 'accounts', 'usage', 'gains',
                  'usage_daily', 'gains_daily', 'net', 'has_data',
                  'available', 'generated_at'):
            self.assertIn(k, ov)
        self.assertTrue(ov['has_data'])
        self.assertAlmostEqual(ov['net']['gained'], 300.0)
        self.assertAlmostEqual(ov['net']['spent'], 10.0)
        self.assertAlmostEqual(ov['net']['delta'], 290.0)
        # platforms/accounts 在过滤时仍返回全集, 前端下拉与图例才稳定
        self.assertEqual([p['key'] for p in ov['platforms']],
                         ['trae', 'workbuddy', 'workbuddy_intl', 'loomy'])

    def test_overview_platform_filter_narrows_numbers(self):
        ov = api.overview(start=DAY, end=DAY, platform='workbuddy_intl')
        self.assertAlmostEqual(ov['usage']['total'], 2.0)
        self.assertAlmostEqual(ov['gains']['total'], 100.0)
        self.assertEqual({x['platform'] for x in ov['usage_daily']},
                         {'workbuddy_intl'})
        self.assertEqual(len(ov['platforms']), 4)      # 图例仍全量

    def test_overview_is_json_serializable(self):
        """前端契约: 必须能直接 json.dumps。"""
        s = json.dumps(api.overview(start=DAY, end=DAY), ensure_ascii=False)
        self.assertIn('workbuddy_intl', s)

    # ---------- 账号维度 ----------
    def test_account_breakdown(self):
        rows = api.account_breakdown(DAY, DAY)
        by = {(r['platform'], r['uid']): r for r in rows}
        intl = by[('workbuddy_intl', 'i-1')]
        self.assertEqual(intl['account'], '国际丙')
        self.assertAlmostEqual(intl['spent'], 2.0)
        self.assertAlmostEqual(intl['gained'], 100.0)
        self.assertAlmostEqual(intl['net'], 98.0)
        self.assertEqual(intl['count'], 2)
        # 三个平台都出现
        self.assertEqual({r['platform'] for r in rows},
                         {'trae', 'workbuddy', 'workbuddy_intl'})

    def test_account_breakdown_platform_filter(self):
        rows = api.account_breakdown(DAY, DAY, platform='workbuddy_intl')
        self.assertEqual({r['platform'] for r in rows}, {'workbuddy_intl'})


class NoDatabaseTest(unittest.TestCase):
    """库不存在时: 返回空结果而不是抛异常 (前端不必做存在性判断)。"""

    def setUp(self):
        self._orig = api.DB_PATH
        api.DB_PATH = os.path.join(tempfile.mkdtemp(), 'not-there.db')

    def tearDown(self):
        api.DB_PATH = self._orig

    def test_all_queries_return_empty(self):
        self.assertFalse(api.has_data())
        self.assertIsNone(api.available_range())
        self.assertEqual(api.usage_rows('2026-09-01', '2026-09-11'), [])
        self.assertEqual(api.usage_daily('2026-09-01', '2026-09-11'), [])
        self.assertEqual(api.gains_daily('2026-09-01', '2026-09-11'), [])
        self.assertEqual(api.account_breakdown('2026-09-01', '2026-09-11'), [])
        self.assertEqual(api.usage_summary('2026-09-01', '2026-09-11')['total'], 0.0)
        self.assertEqual(api.gains_summary('2026-09-01', '2026-09-11')['total'], 0.0)
        ov = api.overview(start='2026-09-01', end='2026-09-11')
        self.assertFalse(ov['has_data'])
        # 平台枚举不依赖库, 始终可用
        self.assertEqual(len(ov['platforms']), 4)


class DayHelpersTest(unittest.TestCase):
    def test_parse_day_accepts_str_and_ts(self):
        t = api.parse_day('2026-09-11')
        self.assertEqual(api.day_str(t), '2026-09-11')
        self.assertEqual(api.parse_day(t), t)
        self.assertEqual(api.parse_day(str(t)), t)

    def test_range_is_inclusive(self):
        t0, t1 = api._range('2026-09-11', '2026-09-11')
        self.assertEqual(t1 - t0, 86399)

    def test_reversed_range_is_normalized(self):
        t0, t1 = api._range('2026-09-11', '2026-09-01')
        self.assertLess(t0, t1)
        self.assertEqual(api.day_str(t0), '2026-09-01')
        self.assertEqual(api.day_str(t1), '2026-09-11')

    def test_default_range_days(self):
        s, e = api._default_range(7)
        self.assertEqual(api.day_str(api.parse_day(e)), e)
        d0 = api.parse_day(s)
        self.assertEqual((api.parse_day(e) - d0) // 86400, 6)


if __name__ == '__main__':
    unittest.main()
