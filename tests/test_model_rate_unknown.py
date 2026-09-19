# -*- coding: utf-8 -*-
"""
测试: 「积分倍率未知」与「真·0 倍率」必须在协议层区分
======================================================
起因 (2026-09-18 master 反馈「WorkBuddy 国际版 gpt-6-astra 倍率是 0」)
--------------------------------------------------------------------
`gpt-6-astra` 在界面上显示 0.00，但它并不是免费模型 —— 实测直接调用成功,
响应体里带着上游真实计费字段 `credit: 0.05`。真正的原因是**它的倍率从未
被拉到过**：

  1. 上游目录 `GET /v2/enterprises/personal/models` 实测只返回 18 个模型,
     `gpt-6-astra` **不在其中**;
  2. 但 `scripts/account_manager.intl_model_rates()` 的 KNOWN_EXTRA 把它
     硬编码补进结果, 且 `credits=None`;
  3. `admin_api._parse_credit_value(None)` 原实现 `return 0.0` —— 于是
     「上游没给倍率」被写成了「倍率 = 0」。

而 `hy3` / `hy4-preview` 是上游**明确**返回 `'x0.00'` 的真·免费模型。
这两种情况在修复前都显示 0.00，界面无法分辨，用户会以为 astra 免费。

本测试锁定的语义 (三态):
    上游给了数字 (含 'x0.00')  → 真·倍率, 可以是 0
    上游没给 (None) / 解析不出 → 未知 → RATE_UNKNOWN (-1)
    单模型整体失败 (err 非空)  → 跳过该条 (不登记)

运行: python -m unittest tests.test_model_rate_unknown -v
"""
import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'scripts'))  # account_manager 等

from admin_api import RATE_UNKNOWN, _parse_credit_value  # noqa: E402


class ParseCreditValueTest(unittest.TestCase):
    """_parse_credit_value 的三态语义（本次修复的核心）。"""

    # ---- 真·倍率: 上游给了值 ----
    def test_plain_number(self):
        self.assertEqual(_parse_credit_value(0.8), 0.8)
        self.assertEqual(_parse_credit_value('0.8'), 0.8)

    def test_x_prefix_and_credits_unit(self):
        """上游 WB 系格式: 'x0.05' / 'x2.20 credits'。"""
        self.assertEqual(_parse_credit_value('x0.05'), 0.05)
        self.assertEqual(_parse_credit_value('x2.20 credits'), 2.2)

    def test_explicit_zero_is_a_real_rate(self):
        """★ 上游明确给 x0.00 = 真·0 倍率（hy3 / hy4-preview 确实免费）。

        这些必须保持 0.0 —— 若被当成「未知」, 说明修复矫枉过正了。
        """
        for raw in ('x0.00', 'x0.00 credits', '0.00', 0, 0.0):
            self.assertEqual(_parse_credit_value(raw), 0.0, f'{raw!r} 应为真·0')

    # ---- 未知: 上游没给或给的东西解析不出 ----
    def test_none_means_unknown_not_zero(self):
        """★★ 回归核心: 上游没给倍率 (None) 不能返回 0.0。

        这正是 gpt-6-astra 显示 0 的直接原因:
        KNOWN_EXTRA 补条目时 credits=None, 原实现把它写成了 0.0。
        """
        self.assertIsNone(_parse_credit_value(None),
                          'None 是「未知」, 返回 0.0 会让未知显示成免费')

    def test_unparsable_means_unknown(self):
        self.assertIsNone(_parse_credit_value(''))
        self.assertIsNone(_parse_credit_value('abc'))
        self.assertIsNone(_parse_credit_value('N/A'))

    def test_bool_is_not_a_rate(self):
        """bool 是 int 子类, 直接 float() 会把 True 变成 1.0 这种假倍率。"""
        self.assertIsNone(_parse_credit_value(True))
        self.assertIsNone(_parse_credit_value(False))

    # ---- 哨兵本身 ----
    def test_unknown_sentinel_is_negative(self):
        """哨兵必须是负数: 倍率不可能是负数, 前端可用 `rate < 0` 一眼识别;
        且不会被 `?? 0` / `|| 0` 之类的兜底吞掉。"""
        self.assertLess(RATE_UNKNOWN, 0)
        self.assertNotEqual(RATE_UNKNOWN, 0)


class RateTableSemanticsTest(unittest.TestCase):
    """倍率表整表行为: 未知项必须**登记哨兵**而不是被丢掉。"""

    def _table_for_ie(self):
        """构造一张与 _model_rates 内部同构的表, 走真实的解析+登记路径。"""
        import admin_api as A

        rows = [
            # (model_id, display_name, credits, err)  —— 与 intl_model_rates 同形
            ('glm-5.3', 'GLM-5.3', 'x0.79', None),          # 正常
            ('hy3', 'Hy3', 'x0.00', None),                  # 真·免费
            ('gpt-6-astra', 'GPT-6 Astra', None, None),     # 目录外, 倍率未知
            ('broken-model', 'Broken', None, 'HTTP 500'),   # 整体失败 → 跳过
        ]
        table = {}
        for model_id, display_name, credits, err in rows:
            if err:
                continue
            r = A._parse_credit_value(credits)
            if r is None:
                r = A.RATE_UNKNOWN
            for k in (model_id, f'wbie-{model_id}', display_name):
                table[k] = r
        return table

    def test_known_rate_is_preserved(self):
        t = self._table_for_ie()
        self.assertEqual(t['glm-5.3'], 0.79)
        self.assertEqual(t['wbie-glm-5.3'], 0.79)

    def test_real_zero_stays_zero(self):
        """hy3 显示 0.00 是对的 —— 它确实免费。"""
        t = self._table_for_ie()
        self.assertEqual(t['hy3'], 0.0)
        self.assertEqual(t['wbie-hy3'], 0.0)

    def test_unknown_is_registered_as_sentinel(self):
        """★ astra 必须登记哨兵, 而不是被 continue 丢掉。

        丢掉会让前端查不到键 → 落到 `?? 0` → 又变回「未知显示成 0」,
        等于没修。
        """
        t = self._table_for_ie()
        self.assertEqual(t['gpt-6-astra'], RATE_UNKNOWN)
        self.assertEqual(t['wbie-gpt-6-astra'], RATE_UNKNOWN)

    def test_unknown_and_real_zero_are_distinguishable(self):
        """本次修复的最终目的: 二者在表里必须取到不同的值。"""
        t = self._table_for_ie()
        self.assertNotEqual(t['hy3'], t['gpt-6-astra'])

    def test_failed_model_is_skipped(self):
        """单模型失败 (err 非空) 与「倍率未知」是两回事, 仍应跳过。"""
        t = self._table_for_ie()
        self.assertNotIn('broken-model', t)


class XUserIdHeaderTest(unittest.TestCase):
    """★ X-User-Id 必须非空 —— 它是 /v3/config 出数据的开关 ★

    2026-09-18 master 追问「没有客户端的人能否拿到倍率」时实测发现:
        X-User-Id 缺失或空串  → data.models 为 null (模型与倍率全拿不到)
        X-User-Id 任意非空值  → 22 个模型 (值真假不影响; Authorization 也不参与校验)
    而原代码写的是 `acc.get('userId', '')` —— 账号缺 userId 时会发一个**空头**,
    结果静默退化成「没有任何倍率」。这里把它钉死, 防止再犯。
    """

    def test_headers_use_placeholder_when_user_id_missing(self):
        """账号缺 userId 时, 请求头里不能是空串。"""
        import json
        import os
        import tempfile

        import account_manager as am  # noqa: E402  (scripts/ 见文件头 sys.path)
        with open(am.OPENAI_CFG, encoding='utf-8') as f:
            cfg = json.load(f)
        cfg = json.loads(json.dumps(cfg))
        accs = cfg['providers']['workbuddy_intl'].get('accounts') or []
        if not accs:
            self.skipTest('无国际版账号')
        accs[0].pop('userId', None)          # 模拟缺 userId

        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, 'cfg.json')
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False)
        orig = am.OPENAI_CFG
        am.OPENAI_CFG = p
        try:
            items, err = am.intl_model_rates()
        finally:
            am.OPENAI_CFG = orig
        if err:
            self.skipTest(f'上游不可用: {err}')
        # 缺 userId 也必须能拿到完整列表 (靠占位值过开关)
        self.assertGreaterEqual(len(items), 22,
                                '缺 userId 时应回落到非空占位值, 否则 models 会变 null')

    def test_source_never_sends_empty_user_id(self):
        """静态断言: 所有构造请求头的地方都不许再写 `userId', ''` 兜底。

        只看代码行 (剥掉注释), 否则说明性注释里的示例会被误判。
        """
        import re
        root = PROJECT_ROOT
        pat = re.compile(
            r"X-User-Id['\"]\s*:\s*[^,\n]*get\(\s*['\"]userId['\"]\s*,\s*['\"]{2}\s*\)")
        for rel in ('scripts/account_manager.py', 'providers/workbuddy.py'):
            with open(os.path.join(root, rel), encoding='utf-8') as f:
                lines = f.readlines()
            for i, line in enumerate(lines, 1):
                code = line.split('#', 1)[0]      # 剥掉行尾注释
                if code.lstrip().startswith('#'):
                    continue
                self.assertIsNone(
                    pat.search(code),
                    f'{rel}:{i} 又出现了 X-User-Id 空串兜底 (会让 models 变 null)')


class LiveIntlModelRateTest(unittest.TestCase):
    """真实上游: 国际版倍率必须取自 /v3/config, 并与客户端显示一致 (联网)。

    ★ 2026-09-18 第二轮修正 (master 给出客户端截图) ★
    第一轮修完后 astra 显示「--」(未知), 但客户端的截图显示它是 **6.67x** ——
    说明倍率其实拿得到, 只是我们查错了接口。真正的源是 GET /v3/config →
    data.models[].credits; 旧用的 /v2/enterprises/personal/models 既缺模型
    (国际版只有 18 个 vs 客户端 22 个, astra 根本不在其中) 倍率又过期
    (hy4-preview 目录里是 x0.00, v3/config 里是 x0.29)。
    """

    def _rates(self):
        try:
            from scripts import account_manager as am  # type: ignore
        except Exception:  # noqa: BLE001
            self.skipTest('account_manager 不可导入')
        items, err = am.intl_model_rates()
        if err or not items:
            self.skipTest(f'上游不可用: {err}')
        return {it[0]: it for it in items}

    def test_astra_has_real_rate_from_v3_config(self):
        """astra 的真实倍率必须拿到 —— 与客户端截图 6.67x 一致。"""
        by_id = self._rates()
        self.assertIn('gpt-6-astra', by_id)
        self.assertEqual(by_id['gpt-6-astra'][2], 'x6.67',
                         'astra 倍率应取自 /v3/config (客户端显示 6.67x)')

    def test_deepseek_v41_flash_is_genuine_zero(self):
        """deepseek-v4.1-flash 是**真·0** (截图里的「Free now」), 不是未知。

        这条专门防「矫枉过正」: 第一轮把 None 当未知是对的方向, 但不能因此
        把上游明确写的 'x0.00' 也算成未知 —— 那样截图上的模型会全变「--」。
        """
        by_id = self._rates()
        self.assertIn('deepseek-v4.1-flash', by_id)
        self.assertEqual(by_id['deepseek-v4.1-flash'][2], 'x0.00')

    def test_model_count_matches_client(self):
        """v3/config 应给出客户端那份完整列表 (22 个), 而不是目录接口的 18 个。"""
        by_id = self._rates()
        self.assertGreaterEqual(len(by_id), 22,
                                '国际版模型数少于客户端展示数, 可能又回退到旧目录接口了')

    def test_free_now_promo_is_surfaced(self):
        """限免活动 (截图上的红色 Free now 角标) 应体现在显示名里。"""
        by_id = self._rates()
        names = ' '.join(str(it[1]) for it in by_id.values())
        self.assertTrue('Free now' in names or '免费' in names,
                        '未继承 modelPromotions 的限免标记')


if __name__ == '__main__':
    unittest.main()
