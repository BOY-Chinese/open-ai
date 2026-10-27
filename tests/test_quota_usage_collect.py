# -*- coding: utf-8 -*-
"""测试: Qoder / CodeArts 消耗入库（快照差值）—— 2026-10-24 新增

==============================================================
背景：
  这三个通道此前在 usage 表里**一行都没有**，于是积分看板把它们画成
  「今日 0 消耗」（Qoder CN / Qoder / CodeArts 都这样）。
  而它们上游**没有逐笔台账接口**，只有累计值：
    Qoder     /api/v2/quota/usage               → userQuota.used + addOnQuota.used
    CodeArts  snap-manager/v1/statistics/plugin → package_credit_used
  所以采集口径是「当天首次观测当基线，差值 = 当天消耗」。

本测试离线、用临时库，锁定的契约：
  1. **首次观测只建基线、绝不写行**（写 0 或写历史累计都是假数据）；
  2. 差值正确，且 entry_id 固定为 `quota-day-<日期>` → 反复采集**覆盖同一条**，
     不会把看板刷成一堆重复行；
  3. 累计值**变小**（池子重置）→ 重新基线，**绝不写负数**；
  4. 差值为 0 → 不写行；
  5. Qoder 的「已用」是两个池（userQuota + addOnQuota）相加；
  6. CodeArts 的「已用」从 `fetch_credit_stats()` 取；
  7. 关掉的账号不采；缺账号时给可读文案而不是抛异常。

运行: python -m unittest tests.test_quota_usage_collect -v
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'scripts'))

import usage_collector as U  # noqa: E402


class _DB(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.conn = sqlite3.connect(self.db, timeout=10)
        U.db_init(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db)

    def _rows(self, platform=None):
        sql = "SELECT platform, uid, entry_id, model, credits, source FROM usage"
        args = ()
        if platform:
            sql += " WHERE platform=?"
            args = (platform,)
        return list(self.conn.execute(sql, args))


class TestQuotaDayRow(_DB):
    """契约 1-4：基线 / 差值 / 重置 / 幂等。"""

    def setUp(self):
        super().setUp()
        self.t = int(time.time())
        self.day = time.strftime('%Y-%m-%d', time.localtime(self.t))

    def _feed(self, platform, uid, used, t, extra=None):
        """模拟采集器：取行 → 非空才入库（与 `_collect_quota_channel` 同口径）。

        `quota_day_row()` 本身**只算不写**（返回 row 给调用方），
        入库由 `db_upsert` 负责 —— 这里照着真实调用链走，别只测半边。
        """
        row = U.quota_day_row(self.conn, platform, uid, used, t, extra=extra)
        if row is not None:
            U.db_upsert(self.conn, [row])
        return row

    def test_first_observation_only_baselines(self):
        self.assertIsNone(self._feed('qoder_cn', 'u1', 10.0, self.t))
        self.assertEqual(self._rows(), [], "首次观测绝不能写行")
        snap = self.conn.execute(
            "SELECT base_used, last_used FROM quota_snapshot "
            "WHERE platform='qoder_cn' AND uid='u1'").fetchone()
        self.assertEqual(snap, (10.0, 10.0))

    def test_growth_produces_delta_row(self):
        self._feed('qoder_intl', 'u1', 10.0, self.t)
        row = self._feed('qoder_intl', 'u1', 13.5, self.t + 60)
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 'qoder_intl')
        self.assertEqual(row[2], f'quota-day-{self.day}', "entry_id 必须按天固定")
        self.assertAlmostEqual(row[5], 3.5)
        self.assertEqual(row[10], 'quota-delta', "source 要能一眼认出是差值行")
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0][4], 3.5)

    def test_same_day_repeated_collection_upserts_one_row(self):
        """反复采集只能有**一条**当天行（否则看板会刷成一堆重复行）。"""
        self._feed('qoder_intl', 'u1', 10.0, self.t)
        self._feed('qoder_intl', 'u1', 12.0, self.t + 10)
        self._feed('qoder_intl', 'u1', 15.0, self.t + 20)
        rows = self._rows()
        self.assertEqual(len(rows), 1, "同一天必须始终是同一条（upsert）")
        self.assertAlmostEqual(rows[0][4], 5.0, "应更新为最新差值（10→15）")

    def test_zero_delta_writes_nothing(self):
        self._feed('codearts', 'u1', 100.0, self.t)
        self.assertIsNone(self._feed('codearts', 'u1', 100.0, self.t + 5))
        self.assertEqual(self._rows(), [])

    def test_shrink_rebaselines_and_never_writes_negative(self):
        """累计值变小（池子重置 / 字段消失）→ 重新基线，绝不写负数。"""
        self._feed('codearts', 'u1', 500.0, self.t)
        self.assertIsNone(self._feed('codearts', 'u1', 20.0, self.t + 5))
        self.assertEqual(self._rows(), [], "变小不能算成消耗")
        snap = self.conn.execute(
            "SELECT base_used, last_used FROM quota_snapshot WHERE uid='u1'").fetchone()
        self.assertEqual(snap, (20.0, 20.0), "必须把基线重置到新值")
        # 重置之后继续增长 → 只算重置后的增量
        row = self._feed('codearts', 'u1', 23.0, self.t + 30)
        self.assertAlmostEqual(row[5], 3.0)

    def test_extra_carries_audit_fields(self):
        self._feed('qoder_intl', 'u1', 10.0, self.t, extra={'realm': 'intl'})
        row = self._feed('qoder_intl', 'u1', 11.0, self.t + 5, extra={'realm': 'intl'})
        extra = row[13]
        self.assertIn('"base_used": 10.0', extra)
        self.assertIn('"used_now": 11.0', extra)
        self.assertIn('"realm": "intl"', extra)

    # ---- 接续昨日基线（2026-10-24 补：避免「夜里关机 → 早上开机」整段丢消耗）----

    def _put_yesterday(self, platform, uid, last_used, days_ago=1):
        y = time.strftime('%Y-%m-%d', time.localtime(self.t - days_ago * 86400))
        self.conn.execute(
            'INSERT INTO quota_snapshot (platform,uid,day,base_used,last_used,updated_at)'
            ' VALUES (?,?,?,?,?,?)', (platform, uid, y, last_used, last_used, self.t - days_ago * 86400))
        self.conn.commit()
        return y

    def test_carries_over_yesterday_so_overnight_consumption_is_kept(self):
        """有「恰好昨天」的快照 → 用它当基线，今天首次采集就能出数字。"""
        y = self._put_yesterday('qoder_intl', 'u1', 10.0)
        row = self._feed('qoder_intl', 'u1', 16.0, self.t)
        self.assertIsNotNone(row, "接续后首轮就该产出一行")
        self.assertAlmostEqual(row[5], 6.0, "差值 = 昨天的最后观测 → 现在")
        self.assertIn(f'"carried_from": "{y}"', row[13])

    def test_does_not_carry_when_gap_exceeds_one_day(self):
        """中间断了两天以上 → **不接续**（免得把几天消耗砸到今天、造出假尖峰）。"""
        self._put_yesterday('qoder_intl', 'u1', 10.0, days_ago=3)
        self.assertIsNone(self._feed('qoder_intl', 'u1', 16.0, self.t))
        self.assertEqual(self._rows(), [])

    def test_does_not_carry_when_previous_is_larger(self):
        """昨天的累计值反而更大（池子重置）→ 不接续，也不写负数。"""
        self._put_yesterday('codearts', 'u1', 500.0)
        self.assertIsNone(self._feed('codearts', 'u1', 20.0, self.t))
        self.assertEqual(self._rows(), [])
        snap = self.conn.execute(
            "SELECT base_used FROM quota_snapshot WHERE uid='u1' AND day=?",
            (self.day,)).fetchone()
        self.assertEqual(snap, (20.0,), "应退回「以当前值为基线」")


class TestQoderUsed(unittest.TestCase):
    """契约 5：Qoder 的「已用」= 两个池相加。"""

    def setUp(self):
        self._orig = U._providers_module

    def tearDown(self):
        U._providers_module = self._orig

    def _stub(self, quota):
        class FakeQC:
            @staticmethod
            def account_quota(acc):
                return quota

        U._providers_module = lambda name: FakeQC

    def test_sums_both_pools(self):
        self._stub({"userQuota": {"used": 3.0}, "addOnQuota": {"used": 7.5}})
        used, err = U._qoder_used({})
        self.assertEqual(err, '')
        self.assertAlmostEqual(used, 10.5)

    def test_tolerates_missing_pool(self):
        """免费版账号没有 addOnQuota 字段 —— 不能因此报错。"""
        self._stub({"userQuota": {"used": 3.0}})
        used, err = U._qoder_used({})
        self.assertEqual(err, '')
        self.assertAlmostEqual(used, 3.0)

    def test_empty_response_reports_reason(self):
        self._stub({})
        used, err = U._qoder_used({})
        self.assertIsNone(used)
        self.assertIn('无响应', err)

    def test_unknown_shape_reports_reason(self):
        """形状变了要如实说「没认出来」，而不是静默当 0（会画成假零消耗）。"""
        self._stub({"totallyDifferent": {"x": 1}})
        used, err = U._qoder_used({})
        self.assertIsNone(used)
        self.assertIn('积分池', err)


class TestCodeartsUsed(unittest.TestCase):
    """契约 6：CodeArts 的「已用」来自 fetch_credit_stats()。"""

    def setUp(self):
        self._orig = U._providers_module

    def tearDown(self):
        U._providers_module = self._orig

    def test_reads_used(self):
        class FakeCA:
            @staticmethod
            def fetch_credit_stats(acc):
                return {"used": 242.22, "remain": 6257.78}, ''

        U._providers_module = lambda name: FakeCA
        used, err = U._codearts_used({})
        self.assertEqual(err, '')
        self.assertAlmostEqual(used, 242.22)

    def test_failure_propagates_reason(self):
        class FakeCA:
            @staticmethod
            def fetch_credit_stats(acc):
                return {}, 'statistics/plugin 里没有「已用」字段'

        U._providers_module = lambda name: FakeCA
        used, err = U._codearts_used({})
        self.assertIsNone(used)
        self.assertIn('已用', err)


class TestCollectQuotaChannels(_DB):
    """契约 7：通道采集的接线（关掉的账号不采、缺账号给文案）。"""

    def _cfg(self, **segs):
        return {'providers': segs}

    def test_disabled_account_is_skipped(self):
        called = []

        def used_fn(acc):
            called.append(acc.get('uid'))
            return 10.0, ''

        cfg = self._cfg(qoder_cn={'accounts': [
            {'uid': 'on', 'enabled': True},
            {'uid': 'off', 'enabled': False}]})
        U._collect_quota_channel(self.conn, cfg, int(time.time()),
                                 'qoder_cn', 'qoder_cn', used_fn)
        self.assertEqual(called, ['on'], "关掉的账号不该被采集")

    def test_no_accounts_is_readable_message(self):
        msg = U._collect_quota_channel(self.conn, self._cfg(), int(time.time()),
                                       'qoder_intl', 'qoder_intl',
                                       lambda acc: (1.0, ''))
        self.assertIn('无账号', msg)

    def test_first_round_reports_baselines_not_rows(self):
        cfg = self._cfg(codearts={'accounts': [{'uid': 'u1'}]})
        t = int(time.time())
        msg = U._collect_quota_channel(self.conn, cfg, t, 'codearts', 'codearts',
                                       lambda acc: (50.0, ''))
        self.assertIn('建基线 1 个', msg)
        self.assertEqual(self._rows('codearts'), [], "首轮不该产生消耗行")

    def test_second_round_writes_rows_when_grown(self):
        cfg = self._cfg(codearts={'accounts': [{'uid': 'u1'}]})
        t = int(time.time())
        vals = iter([50.0, 52.5])
        U._collect_quota_channel(self.conn, cfg, t, 'codearts', 'codearts',
                                 lambda acc: (next(vals), ''))
        msg = U._collect_quota_channel(self.conn, cfg, t + 5, 'codearts', 'codearts',
                                       lambda acc: (next(vals), ''))
        self.assertIn('记 1 个账号', msg)
        rows = self._rows('codearts')
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0][4], 2.5)

    def test_reports_no_new_delta_distinctly_from_baseline(self):
        """★ 已有基线但累计值没涨 → 必须报「无新增」。

        实测踩过：初版把「本轮才建基线」和「无新增」混成一个计数，
        于是日志显示「建基线 2 个」—— 而那两个账号的基线其实早就建好了。
        本项目历来的口径是「不报假数据」，日志也一样。
        """
        cfg = self._cfg(codearts={'accounts': [{'uid': 'u1'}]})
        t = int(time.time())
        first = U._collect_quota_channel(self.conn, cfg, t, 'codearts', 'codearts',
                                         lambda acc: (50.0, ''))
        self.assertIn('本轮建基线 1 个', first)
        second = U._collect_quota_channel(self.conn, cfg, t + 5, 'codearts', 'codearts',
                                          lambda acc: (50.0, ''))
        self.assertIn('无新增 1 个', second)
        self.assertNotIn('建基线', second, "已有基线时不该再报「建基线」")


class TestCodeartsFetchCreditStats(unittest.TestCase):
    """`fetch_credit_stats()` 的解析（离线打桩，不联网）。"""

    def setUp(self):
        from providers import codearts as CA
        self.CA = CA
        self._orig = CA.signed_get

    def tearDown(self):
        self.CA.signed_get = self._orig

    def _stub(self, payload):
        self.CA.signed_get = lambda acc, path: payload

    def test_reads_total_metric(self):
        self._stub({"package": {"spec_code": "codearts.agent.individual.trial"},
                    "metrics": [
                        {"name": "usageTotalPackageCredit",
                         "package_credit_amount": 6500.0,
                         "package_credit_used": 4.69,
                         "package_credit_remain": 6495.31},
                    ]})
        stats, err = self.CA.fetch_credit_stats({})
        self.assertEqual(err, '')
        self.assertAlmostEqual(stats['used'], 4.69)
        self.assertAlmostEqual(stats['remain'], 6495.31)
        self.assertAlmostEqual(stats['total'], 6500.0)

    def test_falls_back_to_summing_parts(self):
        self._stub({"metrics": [
            {"name": "usageBasicPackageCredit", "package_credit_used": 1.0},
            {"name": "usageBonusPackageCredit", "package_credit_used": 2.5},
        ]})
        stats, err = self.CA.fetch_credit_stats({})
        self.assertEqual(err, '')
        self.assertAlmostEqual(stats['used'], 3.5)

    def test_missing_used_field_reports_reason(self):
        self._stub({"package": {"spec_code": "x"},
                    "metrics": [{"name": "usageTotalPackageCredit",
                                 "package_credit_remain": 10.0}]})
        stats, err = self.CA.fetch_credit_stats({})
        self.assertEqual(stats, {})
        self.assertIn('已用', err)

    def test_request_failure_is_swallowed(self):
        def boom(acc, path):
            raise RuntimeError('network down')

        self.CA.signed_get = boom
        stats, err = self.CA.fetch_credit_stats({})
        self.assertEqual(stats, {})
        self.assertIn('失败', err)


if __name__ == '__main__':
    unittest.main(verbosity=2)