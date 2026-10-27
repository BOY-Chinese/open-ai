# -*- coding: utf-8 -*-
"""测试: /credits/week 的「通道骨架」必须覆盖所有已知通道

==============================================================
事故（2026-10-24，master 报障）：
  「每周情况里没有柱子显示」—— Qoder CN / Qoder / CodeArts 三个通道
  **一根柱子都没有**。

根因：`admin_api.credits_week()` 里的七天骨架是**写死的四个通道**
（trae / WorkBuddy / WorkBuddy 国际 / Loomy），漏掉了 2026-10 新增的三个。
累加处写着 `if day in daily and ch in daily[day]`，于是新通道的流水
**被静默丢弃**；前端 `chart.ts` 其实早就配好了这三个通道的配色与图例，
所以界面上看起来像「这三家没数据」。

修法：
  1. 骨架改为从 `CHANNEL_TO_PLATFORM` 推导（单一来源，不再另抄一份清单）；
  2. 累加不再按「已知通道」过滤 —— 未登记的通道名也照实累加，
     **绝不静默丢数据**（宁可多一根柱子，也不要把数据藏起来）。

本测试锁定的契约：
  · `WEEK_CHANNELS` 与 `CHANNEL_TO_PLATFORM` 的键集**完全一致**；
  · 三个新通道在骨架里；
  · 端点返回的每一天都带**全部**通道键（前端 `<Bar dataKey={c.key}>`
    取不到键就画不出柱子）；
  · 新通道的积分真的落到对应的那一天；
  · 未登记的通道名**不被丢弃**。

运行: python -m unittest tests.test_credits_week_channels -v
"""
import asyncio
import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'scripts'))

import admin_api as A          # noqa: E402
import credits_api as api      # noqa: E402

DAY = dt.date.today().isoformat()


def _ts(day: str, hour: int = 12, minute: int = 0) -> int:
    import time
    y, m, d = (int(x) for x in day.split('-'))
    return int(time.mktime((y, m, d, hour, minute, 0, 0, 0, -1)))


class TestWeekChannelSkeleton(unittest.TestCase):
    """骨架的不变式（不需要数据库）。"""

    def test_week_channels_equals_channel_map(self):
        """★ 核心不变式：骨架来源必须与通道映射**同一份**。

        这条一旦失败，说明又出现了「另一个地方写了一遍清单」——
        正是本次事故的成因。
        """
        self.assertEqual(set(A.WEEK_CHANNELS), set(A.CHANNEL_TO_PLATFORM))

    def test_new_three_channels_are_in_skeleton(self):
        for ch in ('QoderCN', 'QoderIntl', 'CodeArts'):
            self.assertIn(ch, A.WEEK_CHANNELS,
                          f'{ch} 必须在周视图骨架里，否则它的柱子永远画不出来')

    def test_skeleton_is_derived_not_handwritten(self):
        """骨架必须**推导**而来，不能是手写的字面量 dict。

        用源码断言守住这条 —— 手写清单就是本次事故的形态。
        """
        import inspect
        src = inspect.getsource(A.credits_week)
        self.assertIn('WEEK_CHANNELS', src, '骨架必须由 WEEK_CHANNELS 生成')
        # 旧写法长这样：把四个通道名直接写进 dict
        for legacy in ('"WorkBuddy": 0.0', '"Loomy": 0.0'):
            self.assertNotIn(legacy, src, f'不该再出现手写通道清单: {legacy}')

    def test_no_silent_drop_filter(self):
        """累加处不能再有 `ch in daily[day]` 这种「不认识就丢掉」的过滤。"""
        import inspect
        src = inspect.getsource(A.credits_week)
        self.assertNotIn('and ch in daily[day]', src,
                         '这种过滤会把未登记的通道流水静默丢掉')


class TestWeekEndpoint(unittest.TestCase):
    """端到端：种子数据 → 调端点 → 检查每天都有全部通道键、且数值落对位置。"""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        import usage_collector as U
        conn = sqlite3.connect(self.db)
        U.db_init(conn)
        conn.executemany(
            'INSERT INTO usage (platform,uid,entry_id,ts,model,credits,cost_money,'
            'input_tok,output_tok,cache_tok,source,session_id,preview,extra,collected_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            [('qoder_cn', 'q1', 'e1', _ts(DAY, 10), '', 12.5, None, 0, 0, 0,
              'quota-delta', None, None, '{}', 0),
             ('qoder_intl', 'q2', 'e2', _ts(DAY, 11), '', 3.25, None, 0, 0, 0,
              'quota-delta', None, None, '{}', 0),
             ('codearts', 'c1', 'e3', _ts(DAY, 12), '', 7.0, None, 0, 0, 0,
              'quota-delta', None, None, '{}', 0),
             # 未登记的通道名：必须**照实累加**，不能被过滤掉
             ('brandnew', 'x1', 'e4', _ts(DAY, 13), '', 1.5, None, 0, 0, 0,
              's', None, None, '{}', 0)])
        conn.commit()
        conn.close()
        self._orig_db = api.DB_PATH
        api.DB_PATH = self.db

    def tearDown(self):
        api.DB_PATH = self._orig_db
        os.unlink(self.db)

    def _week(self):
        return asyncio.run(A.credits_week(offset=0, channel=None))

    def test_every_day_has_all_channel_keys(self):
        """★ 前端是 `<Bar dataKey={c.key}>`：取不到键就画不出柱子。"""
        out = self._week()
        days = out['daily']
        self.assertEqual(len(days), 7, '周视图必须是完整 7 列')
        for row in days:
            self.assertIn('day', row)
            for ch in A.WEEK_CHANNELS:
                self.assertIn(ch, row, f"{row['day']} 缺通道键 {ch}（柱子会消失）")

    def test_new_channel_credits_land_on_the_right_day(self):
        out = self._week()
        today_row = next(r for r in out['daily'] if r['day'] == DAY)
        self.assertAlmostEqual(today_row['QoderCN'], 12.5)
        self.assertAlmostEqual(today_row['QoderIntl'], 3.25)
        self.assertAlmostEqual(today_row['CodeArts'], 7.0)

    def test_unknown_channel_is_not_silently_dropped(self):
        """未登记的通道名也要出现 —— 宁多多一根柱子，也不要把数据藏起来。"""
        out = self._week()
        today_row = next(r for r in out['daily'] if r['day'] == DAY)
        self.assertAlmostEqual(today_row.get('brandnew', 0.0), 1.5)

    def test_other_days_are_zero_not_missing(self):
        out = self._week()
        for row in out['daily']:
            if row['day'] == DAY:
                continue
            self.assertEqual(row['QoderCN'], 0.0)
            self.assertEqual(row['CodeArts'], 0.0)


if __name__ == '__main__':
    unittest.main(verbosity=2)