# -*- coding: utf-8 -*-
"""TaskScheduler.tick 调度状态机回归测试 (2026-09-13 修复的四个调度 bug)。

覆盖:
1. 每日全量签到"启动成功才写状态" (0 点竞态: 旧版先写后启, 被防重入吞掉后
   当天全量签到永不执行 — 实测 2026-09-13 TRAE 全天没跑、token 过期 401)。
2. --wb-only 独立 30 分钟节奏 + 写回时间戳 (旧版借用 TRAE_RETRY_STATE 且不写回,
   每 10 秒拉起一次, 全天上万次无效请求)。
3. .trae_signin_state 状态带日期 (旧裸 'done' 残留会永久压制次日的 --trae-only)。
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import app_runtime as ar


class TickStateMachineTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.tmp = tmp
        self._orig = (ar.SIGNIN_STATE, ar.TRAE_STATE,
                      ar.TRAE_RETRY_STATE, ar.WB_RETRY_STATE)
        ar.SIGNIN_STATE = os.path.join(tmp, 'signin')
        ar.TRAE_STATE = os.path.join(tmp, 'trae_state')
        ar.TRAE_RETRY_STATE = os.path.join(tmp, 'trae_retry')
        ar.WB_RETRY_STATE = os.path.join(tmp, 'wb_retry')
        self.calls = []
        self.sched = mock.Mock(spec=['_busy', 'run_signin', 'run_collect'])
        self.sched._busy.side_effect = lambda tag: self._busy.get(tag)
        self.sched.run_signin.side_effect = self._record
        self.sched.run_collect.return_value = None
        self._busy = {}
        # TRAE 今日已签 (避免补试分支干扰)
        self._write(ar.TRAE_STATE, 'done:' + time.strftime('%Y-%m-%d'))

    def tearDown(self):
        (ar.SIGNIN_STATE, ar.TRAE_STATE,
         ar.TRAE_RETRY_STATE, ar.WB_RETRY_STATE) = self._orig

    # ---- helpers ----
    def _record(self, args=None):
        self.calls.append(list(args or []))

    @staticmethod
    def _write(path, val):
        with open(path, 'w', encoding='utf-8') as f:
            f.write(val)

    def _tick(self):
        ar.TaskScheduler.tick(self.sched)

    def _wb_calls(self):
        return sum(1 for c in self.calls if c == ['--wb-only'])

    # ---- 1. 每日全量签到竞态 ----
    def test_full_signin_runs_then_marks_state(self):
        self._tick()
        self.assertIn([], self.calls)
        self.assertEqual(self._read(ar.SIGNIN_STATE), time.strftime('%Y-%m-%d'))

    def test_busy_signin_not_swallowed(self):
        """签到在跑时: 不启动、不写状态, 下一轮自动重试 (0 点竞态修复)。"""
        self._busy['signin'] = True
        os.remove(ar.SIGNIN_STATE) if os.path.exists(ar.SIGNIN_STATE) else None
        self._tick()
        self.assertNotIn([], self.calls)
        self.assertFalse(os.path.exists(ar.SIGNIN_STATE))
        self._busy.pop('signin')
        self._tick()
        self.assertIn([], self.calls)
        self.assertEqual(self._read(ar.SIGNIN_STATE), time.strftime('%Y-%m-%d'))

    def test_full_signin_only_once_per_day(self):
        self._tick()
        self._tick()
        self.assertEqual(sum(1 for c in self.calls if c == []), 1)

    # ---- 2. wb-only 节奏 ----
    def test_wb_only_throttled_to_30min(self):
        self._tick()                       # 首轮: wb-only 触发并写时间戳
        n1 = self._wb_calls()
        self.calls.clear()                 # 只统计本拍增量
        self._tick()                       # 立刻再拍: 不得触发
        n2 = self._wb_calls()
        self.assertEqual((n1, n2), (1, 0))

    def test_wb_only_refires_after_interval(self):
        self._write(ar.WB_RETRY_STATE, str(time.time() - 3600))
        self._tick()
        self.assertEqual(self._wb_calls(), 1)

    def test_wb_only_uses_own_state_key(self):
        """wb-only 不得借用/改写 TRAE_RETRY_STATE (旧版设计污染)。"""
        # 先让全量签到完成 (否则首拍的全量签到分支本就会写 TRAE_RETRY_STATE)
        self._write(ar.SIGNIN_STATE, time.strftime('%Y-%m-%d'))
        self._write(ar.TRAE_RETRY_STATE, '123456')
        self._tick()
        self.assertEqual(self._read(ar.TRAE_RETRY_STATE), '123456')
    # ---- 3. TRAE 状态带日期 ----
    def test_stale_bare_done_does_not_block_retry(self):
        """昨天的裸 'done' 残留不得压制今天的补试 (09-13 实测踩坑)。"""
        self._write(ar.SIGNIN_STATE, time.strftime('%Y-%m-%d'))
        self._write(ar.TRAE_STATE, 'done')          # 旧格式
        self._write(ar.TRAE_RETRY_STATE, '0')
        self._tick()
        self.assertIn(['--trae-only'], self.calls)

    def test_today_done_suppresses_retry(self):
        self._write(ar.SIGNIN_STATE, time.strftime('%Y-%m-%d'))
        self._write(ar.TRAE_STATE, 'done:' + time.strftime('%Y-%m-%d'))
        self._write(ar.TRAE_RETRY_STATE, str(time.time()))
        self._tick()
        self.assertNotIn(['--trae-only'], self.calls)

    def test_retry_fires_after_interval(self):
        self._write(ar.SIGNIN_STATE, time.strftime('%Y-%m-%d'))
        self._write(ar.TRAE_STATE, 'pending')
        self._write(ar.TRAE_RETRY_STATE, str(time.time() - 1800))
        self._tick()
        self.assertIn(['--trae-only'], self.calls)

    # ---- 读文件 helper ----
    @staticmethod
    def _read(path):
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().strip()


if __name__ == '__main__':
    unittest.main()
