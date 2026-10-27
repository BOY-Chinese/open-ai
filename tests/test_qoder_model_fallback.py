# -*- coding: utf-8 -*-
"""测试: Qoder 通道「模型自动降级」开关（2026-10-24）

==============================================================
背景（真实事故）：
  某 Qoder 账号一天内积分被清零（userQuota 280/300 + addOnQuota 100/100 全用尽），
  而日志里的 RESOURCE_LIMIT 全部集中在 qfmodel / dfmodel / mmodel / gfmodel ——
  正是「用户选了模型 A → 上游说没资源 → 插件自动改用模型 B（往往更贵）→
  照常扣积分」这条路径。用户根本没要求换模型，却被扣了高价模型的费用。

master 的决定：「当前模型不行就不行」→ 默认关闭自动换模型。

本测试锁定三条契约：
  1. **默认关闭**：不写任何配置时，`_fallback_models()` 必须返回空列表，
     且 `_model_fallback_enabled()` 为 False。
  2. **配置可显式打开**：`providers.<通道>.modelFallback = true` 时恢复旧行为
     （返回候选列表）；`false` 时关闭。字符串 "on"/"off"/"1"/"0" 也要认。
  3. **开关优先级**：config.json 的值 > 环境变量 QD_RL_FALLBACK。

另附：`_truthy()` 的归一化各种脏输入都必须给出确定答案（不能抛异常）。

不联网、不写 config：只构造 provider 实例并断言纯逻辑。
运行: python -m unittest tests.test_qoder_model_fallback -v
"""
import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import providers.qoder_base as QB  # noqa: E402


def _make_provider(**overrides):
    """构造一个最小可用的 QoderProvider 实例（不联网）。

    直接用基类：realm='cn'，PREFIX='qdc-'。accounts 给一个假的即可 ——
    本测试只验证开关逻辑，不发起任何上游请求。
    """
    cfg = {
        "accounts": [{"uid": "test-uid", "realm": "cn", "accessToken": "dt-fake"}],
        "switchEvery": 5,
    }
    cfg.update(overrides)
    return QB.QoderProvider(cfg)


class TestTruthyNormalization(unittest.TestCase):
    """开关值归一化：脏输入也要给确定答案。"""

    def test_booleans(self):
        self.assertTrue(QB._truthy(True))
        self.assertFalse(QB._truthy(False))

    def test_strings_off(self):
        for v in ("0", "off", "false", "no", "OFF", "False", "disable", "disabled", " off "):
            with self.subTest(v=v):
                self.assertFalse(QB._truthy(v))

    def test_strings_on(self):
        for v in ("1", "on", "true", "yes", "TRUE", "On"):
            with self.subTest(v=v):
                self.assertTrue(QB._truthy(v))

    def test_numbers(self):
        self.assertTrue(QB._truthy(1))
        self.assertFalse(QB._truthy(0))

    def test_none_and_empty_use_default(self):
        self.assertFalse(QB._truthy(None, False))
        self.assertTrue(QB._truthy(None, True))
        self.assertFalse(QB._truthy("", False))
        self.assertTrue(QB._truthy("   ", True))


class TestFallbackDisabledByDefault(unittest.TestCase):
    """默认必须关闭 —— 这是本次修复的核心契约。"""

    def setUp(self):
        self._saved = os.environ.pop("QD_RL_FALLBACK", None)

    def tearDown(self):
        os.environ.pop("QD_RL_FALLBACK", None)
        if self._saved is not None:
            os.environ["QD_RL_FALLBACK"] = self._saved

    def test_class_default_is_false(self):
        self.assertFalse(QB.QoderProvider.RL_FALLBACK_DEFAULT)

    def test_enabled_returns_false_without_config(self):
        p = _make_provider()
        self.assertFalse(p._model_fallback_enabled())

    def test_fallback_models_empty_without_config(self):
        """最关键的断言：默认情况下没有任何可自动接替的模型。"""
        p = _make_provider()
        self.assertEqual(p._fallback_models("qfmodel"), [])
        self.assertEqual(p._fallback_models("auto"), [])

    def test_env_var_can_reenable_when_config_absent(self):
        os.environ["QD_RL_FALLBACK"] = "1"
        p = _make_provider()
        self.assertTrue(p._model_fallback_enabled())
        self.assertTrue(len(p._fallback_models("qfmodel")) > 0)


class TestFallbackConfigWins(unittest.TestCase):
    """config.json 的值优先于环境变量。"""

    def setUp(self):
        self._saved = os.environ.pop("QD_RL_FALLBACK", None)

    def tearDown(self):
        os.environ.pop("QD_RL_FALLBACK", None)
        if self._saved is not None:
            os.environ["QD_RL_FALLBACK"] = self._saved

    def test_config_true_enables(self):
        p = _make_provider(modelFallback=True)
        self.assertTrue(p._model_fallback_enabled())

    def test_config_false_disables(self):
        p = _make_provider(modelFallback=False)
        self.assertFalse(p._model_fallback_enabled())

    def test_config_string_values(self):
        self.assertTrue(_make_provider(modelFallback="on")._model_fallback_enabled())
        self.assertFalse(_make_provider(modelFallback="off")._model_fallback_enabled())

    def test_config_beats_env(self):
        """config 写了 false → 即使环境变量说 1 也必须关（Windows 启动脚本残留环境变量的场景）。"""
        os.environ["QD_RL_FALLBACK"] = "1"
        p = _make_provider(modelFallback=False)
        self.assertFalse(p._model_fallback_enabled())
        self.assertEqual(p._fallback_models("qfmodel"), [])

        os.environ["QD_RL_FALLBACK"] = "0"
        p2 = _make_provider(modelFallback=True)
        self.assertTrue(p2._model_fallback_enabled())

    def test_null_config_falls_through_to_env(self):
        """config 里显式写 null → 视同未配置，回落到环境变量。"""
        os.environ["QD_RL_FALLBACK"] = "1"
        p = _make_provider(modelFallback=None)
        self.assertTrue(p._model_fallback_enabled())


class TestFallbackCandidateContent(unittest.TestCase):
    """开关打开时，候选列表本身的契约（排除自身 / 排除 auto 类）。"""

    def test_excludes_self_and_auto_like(self):
        p = _make_provider(modelFallback=True)
        cands = p._fallback_models("qfmodel")
        self.assertNotIn("qfmodel", cands)
        for bad in ("auto", "fast", "ultimate", "performance", "efficient"):
            self.assertNotIn(bad, cands, f"{bad} 不应作为自动接替目标")

    def test_candidates_are_strings(self):
        p = _make_provider(modelFallback=True)
        for c in p._fallback_models("qfmodel"):
            self.assertIsInstance(c, str)
            self.assertTrue(c)


class TestShippedConfig(unittest.TestCase):
    """线上 config.json 的实际取值必须是关闭（防止有人改回去）。"""

    def test_config_json_has_fallback_off(self):
        import json
        path = os.path.join(PROJECT_ROOT, "config.json")
        if not os.path.exists(path):        # 打包环境/CI 可能没有，跳过
            self.skipTest("config.json 不存在")
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        provs = cfg.get("providers") or {}
        for ch in ("qoder_cn", "qoder_intl"):
            seg = provs.get(ch)
            if not seg:
                continue
            with self.subTest(channel=ch):
                self.assertIs(seg.get("modelFallback"), False,
                              f"{ch}.modelFallback 必须是 false（2026-10-24 决定：模型不行就不行）")

    def test_config_json_still_has_accounts(self):
        """改配置时绝不能把账号弄丢（本项目血泪教训）。"""
        import json
        path = os.path.join(PROJECT_ROOT, "config.json")
        if not os.path.exists(path):
            self.skipTest("config.json 不存在")
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        for ch in ("qoder_cn", "qoder_intl"):
            seg = (cfg.get("providers") or {}).get(ch) or {}
            accs = seg.get("accounts") or []
            with self.subTest(channel=ch):
                self.assertTrue(len(accs) > 0, f"{ch} 的账号列表不能为空")
                for a in accs:
                    self.assertTrue(a.get("accessToken"), f"{ch}/{a.get('nickname')} 缺 accessToken")


if __name__ == "__main__":
    unittest.main(verbosity=2)
