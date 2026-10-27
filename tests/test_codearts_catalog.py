# -*- coding: utf-8 -*-
"""
测试: CodeArts 目录/积分/探活三处修复（2026-10-23 master 反馈）
================================================================================
现象（master 原话）：
  「问题一没有显示积分，问题二我在 codearts 已经可以正常使用 AI 去工作了，
   但是 open-ai 检测模型状态现在还是弹出繁忙提示」

三个真实根因（全部实测确认）：

1. **积分接口找错了地方**。此前 `fetch_credits` 直接返回 None 并写「CodeArts 未提供
   余额接口」。实际官方桌面端用的是
   ``GET {snap}/snap-manager/v1/statistics/plugin``（PackageInfoService.doRequest），
   返回里的 `usageTotalPackageCredit.package_credit_remain` 就是门户上那个积分。
   opengw 的 `/api/v1/user/tokens/balance` 对任何路径都回 `invalid channel`
   —— 那是**福利网关**，不是积分网关。

2. **模型名发错**。上游只认内部 `model_id`（`deepseek-v4.1-flash`），发目录里的
   展示名（`DeepSeek-V4.1-Flash`）会得到 `InferHub.002002009.404 The model is not
   registered`。老代码把这条 404 误判成「需要福利头」，补头重试 → `4004.200
   benefit not found` → 归类成繁忙。**同一个模型，改成 model_id 立刻正常。**

3. **探活把自己打满了**。并发会话上限是**滚动窗口**（≈3 条/60 秒），而「检查模型」
   发的是极短请求：连查 4 个就把名额占满，之后每个都回 `TM.00001041`。
   实测：同 chat_id / 同 Session-Id 连发 4 条照样挂；30 秒间隔 6/6 全通。

本文件对三处修复逐条设断言。不联网（signed_get / client 全部替身）。
运行: python -m unittest tests.test_codearts_catalog -v
"""
import asyncio
import json
import os
import sys
import time
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

from providers import codearts as CA  # noqa: E402

ACCOUNT = {"uid": "u-ca", "credentials": {
    "accessKeyId": "AK", "secretAccessKey": "SK", "securityToken": "ST"}}

# 官方 statistics/plugin 的真实形状（2026-10-23 抓包，字段已精简）
STATS = {
    "package": {"spec_code": "codearts.agent.individual.trial",
                "package_name_cn": "体验版", "status": "normal",
                "is_credit_package": True},
    "metrics": [
        {"name": "usageTokenChatMessages", "usage_token_num": 211566, "show": True},
        {"name": "usageTotalPackageCredit", "package_credit_amount": 6500.0,
         "package_credit_used": 4.69, "package_credit_remain": 6495.31, "show": True},
        {"name": "usageBasicPackageCredit", "package_credit_amount": 500.0,
         "package_credit_used": 0.0, "package_credit_remain": 500.0, "show": True},
        {"name": "usageBonusPackageCredit", "package_credit_amount": 6000.0,
         "package_credit_used": 4.69, "package_credit_remain": 5995.31, "show": True},
    ],
    "show": {"metrics": True, "package": True},
}

BUILTIN = {"count": 4, "builtinModels": [
    {"model_id": "GLM-5.2", "model_name": "GLM-5.2", "display_enabled": True,
     "model_desc": "旗舰", "credit": [{"ratio": "0.05", "ratio_display": "0.7x"}]},
    {"model_id": "deepseek-v4.1-flash", "model_name": "DeepSeek-V4.1-Flash",
     "display_enabled": True, "model_desc": "快",
     "credit": [{"ratio": "0.05", "ratio_display": "0.18x"}]},
    {"model_id": "glm-5.3", "model_name": "GLM-5.3", "display_enabled": True,
     "credit": [{"ratio": "0.05", "ratio_display": "0.79x"}]},
    {"model_id": "kimi-k3", "model_name": "Kimi-K3", "display_enabled": True,
     "credit": [{"ratio": "0.05", "ratio_display": "2.38x"}]},
]}


class FakeSignedGet:
    """按 URL 关键字返回预设响应，并记录调用。"""

    def __init__(self, mapping=None):
        self.mapping = mapping or {}
        self.calls = []

    def __call__(self, account, url, agent_type=""):
        self.calls.append((url, agent_type))
        for key, resp in self.mapping.items():
            if key in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return {}

    def urls(self):
        return [u for u, _ in self.calls]


class CreditsTest(unittest.TestCase):
    """① 积分：statistics/plugin → usageTotalPackageCredit。"""

    def setUp(self):
        self.orig = CA.signed_get

    def tearDown(self):
        CA.signed_get = self.orig

    def test_reads_total_credit_remain(self):
        CA.signed_get = FakeSignedGet({"statistics/plugin": STATS})
        credits, err = CA.fetch_credits(ACCOUNT)
        self.assertEqual(err, "")
        self.assertAlmostEqual(credits, 6495.31, places=2)

    def test_uses_statistics_endpoint_without_agent_type(self):
        fake = FakeSignedGet({"statistics/plugin": STATS})
        CA.signed_get = fake
        CA.fetch_credits(ACCOUNT)
        self.assertIn("/snap-manager/v1/statistics/plugin", fake.urls()[0])
        self.assertEqual(fake.calls[0][1], "",
                         "官方客户端这个接口不带 Agent-Type，带了反而可能被拒")

    def test_falls_back_to_basic_plus_bonus(self):
        stats = {"metrics": [
            {"name": "usageBasicPackageCredit", "package_credit_remain": 500.0},
            {"name": "usageBonusPackageCredit", "package_credit_remain": 5995.31},
        ]}
        CA.signed_get = FakeSignedGet({"statistics/plugin": stats})
        credits, err = CA.fetch_credits(ACCOUNT)
        self.assertEqual(err, "")
        self.assertAlmostEqual(credits, 6495.31, places=2)

    def test_falls_back_to_amount_minus_used(self):
        stats = {"metrics": [{"name": "usageTotalPackageCredit",
                              "package_credit_amount": 100.0,
                              "package_credit_used": 40.0}]}
        CA.signed_get = FakeSignedGet({"statistics/plugin": stats})
        credits, _err = CA.fetch_credits(ACCOUNT)
        self.assertAlmostEqual(credits, 60.0, places=2)

    def test_unknown_shape_returns_none_with_reason(self):
        CA.signed_get = FakeSignedGet({"statistics/plugin": {"metrics": []}})
        credits, err = CA.fetch_credits(ACCOUNT)
        self.assertIsNone(credits, "读不到就该是 None（界面显示「—」），不能编 0")
        self.assertIn("没有积分字段", err)

    def test_request_error_is_reported(self):
        CA.signed_get = FakeSignedGet({"statistics/plugin": RuntimeError("boom")})
        credits, err = CA.fetch_credits(ACCOUNT)
        self.assertIsNone(credits)
        self.assertIn("boom", err)


class ModelKeyTest(unittest.TestCase):
    """② 展示名 → 上游 model_id（发错名字 = 404 = 被误判成繁忙）。"""

    def provider(self, models=None):
        p = CA.CodeArtsProvider({"_cfg_key": "codearts", "accounts": [ACCOUNT]})
        if models is not None:
            p._models = models
        return p

    def test_builtin_catalog_mapping(self):
        p = self.provider()
        self.assertEqual(p.model_key("DeepSeek-V4.1-Flash"), "deepseek-v4.1-flash")
        self.assertEqual(p.model_key("Kimi-K3"), "kimi-k3")
        self.assertEqual(p.model_key("GLM-5.3"), "glm-5.3")
        self.assertEqual(p.model_key("GLM-5.2"), "GLM-5.2")
        self.assertEqual(p.model_key("GLM-5.2-ArkTS-SPARK"), "glm-5.2-sft-harmony")

    def test_prefix_and_case_insensitive(self):
        p = self.provider()
        self.assertEqual(p.model_key("ca-DeepSeek-V4.1-Flash"), "deepseek-v4.1-flash")
        self.assertEqual(p.model_key("deepseek-v4.1-flash"), "deepseek-v4.1-flash")

    def test_uses_live_catalog_over_seed(self):
        p = self.provider([{"id": "NewModel", "key": "new-model-id",
                            "benefit": False}])
        self.assertEqual(p.model_key("NewModel"), "new-model-id")

    def test_build_request_sends_model_id(self):
        p = self.provider([{"id": "DeepSeek-V4.1-Flash",
                            "key": "deepseek-v4.1-flash", "benefit": False}])
        _cid, raw, payload = p._build_request(
            {"model": "DeepSeek-V4.1-Flash", "messages": []},
            "DeepSeek-V4.1-Flash", False)
        self.assertEqual(payload["model"], "deepseek-v4.1-flash")
        self.assertIn(b"deepseek-v4.1-flash", raw)


class BenefitFlagTest(unittest.TestCase):
    """②b 福利标记只来自目录，绝不来自种子/404。"""

    def test_seed_has_no_benefit_models(self):
        self.assertEqual(CA.SEED_BENEFIT_MODELS, (),
                         "种子里的福利模型会把普通模型误判成福利（本次事故根因）")

    def test_unknown_model_is_not_benefit(self):
        p = CA.CodeArtsProvider({"_cfg_key": "codearts", "accounts": [ACCOUNT]})
        self.assertFalse(p.is_benefit_model("deepseek-v4-flash-0731"))
        self.assertFalse(p.is_benefit_model("glm-5.3-flash"))
        self.assertFalse(p.is_benefit_model("DeepSeek-V4.1-Flash"))

    def test_catalog_benefit_flag_respected(self):
        p = CA.CodeArtsProvider({"_cfg_key": "codearts", "accounts": [ACCOUNT]})
        p._models = [{"id": "FreeModel", "key": "free-model", "benefit": True}]
        self.assertTrue(p.is_benefit_model("FreeModel"))
        self.assertTrue(p.is_benefit_model("ca-FreeModel"))


class FetchModelsTest(unittest.TestCase):
    """②c 目录：builtin 主源；福利渠道必须先 enabled。"""

    def setUp(self):
        self.orig = CA.signed_get

    def tearDown(self):
        CA.signed_get = self.orig

    def test_builtin_only_when_gateways_disabled(self):
        fake = FakeSignedGet({
            "model/builtin": BUILTIN,
            "benefit-gateway-config": {"enabled": False},
            "education-gateway-config": {"enabled": False},
        })
        CA.signed_get = fake
        models = CA.fetch_models(ACCOUNT)
        self.assertEqual([m["id"] for m in models],
                         ["GLM-5.2", "DeepSeek-V4.1-Flash", "GLM-5.3", "Kimi-K3"])
        self.assertTrue(all(not m["benefit"] for m in models))
        self.assertFalse(any("gateway/config" in u for u in fake.urls()),
                         "网关 disabled 时不该再去拉福利模型（纯噪音）")

    def test_benefit_channel_models_when_enabled(self):
        fake = FakeSignedGet({
            "model/builtin": BUILTIN,
            "benefit-gateway-config": {"enabled": True},
            "education-gateway-config": {"enabled": False},
            "gateway/config": {"error_code": "0000", "result": {"models": [
                {"model_id": "free-x", "model_name": "Free-X"}]}},
        })
        CA.signed_get = fake
        models = CA.fetch_models(ACCOUNT)
        by_id = {m["id"]: m for m in models}
        self.assertIn("Free-X", by_id)
        self.assertTrue(by_id["Free-X"]["benefit"])
        self.assertEqual(by_id["Free-X"]["key"], "free-x")

    def test_rates_come_from_builtin_credit(self):
        CA.signed_get = FakeSignedGet({"model/builtin": BUILTIN,
                                       "benefit-gateway-config": {"enabled": False},
                                       "education-gateway-config": {"enabled": False}})
        models = {m["id"]: m for m in CA.fetch_models(ACCOUNT)}
        self.assertAlmostEqual(models["DeepSeek-V4.1-Flash"]["rate"], 0.18)
        self.assertAlmostEqual(models["Kimi-K3"]["rate"], 2.38)
        self.assertAlmostEqual(models["GLM-5.2"]["rate"], 0.7)


class ProbeGateTest(unittest.TestCase):
    """③ 探活节流：串行 + 间隔 + 成功缓存。"""

    def test_caches_ok_results(self):
        g = CA.ProbeGate()
        g.release("m1", {"model": "m1", "status": "ok", "latencyMs": 1})
        hit = g.cached("m1")
        self.assertIsNotNone(hit)
        self.assertTrue(hit["cached"])

    def test_does_not_cache_failures(self):
        g = CA.ProbeGate()
        g.release("m1", {"model": "m1", "status": "busy"})
        self.assertIsNone(g.cached("m1"))
        g.release("m2", {"model": "m2", "status": "down"})
        self.assertIsNone(g.cached("m2"))

    def test_cache_expires(self):
        g = CA.ProbeGate()
        g._cache["m1"] = (time.time() - CA.PROBE_CACHE_TTL - 1,
                          {"model": "m1", "status": "ok"})
        self.assertIsNone(g.cached("m1"))

    def test_acquire_spaces_requests(self):
        g = CA.ProbeGate()
        g._last = time.time()
        orig_gap = CA.PROBE_GAP_SECS
        CA.PROBE_GAP_SECS = 0.2
        try:
            async def run():
                await g.acquire()
                g.release()
                t0 = time.time()
                await g.acquire()          # 第二次必须等满间隔
                waited = time.time() - t0
                g.release()
                return waited
            self.assertGreaterEqual(asyncio.run(run()), 0.18)
        finally:
            CA.PROBE_GAP_SECS = orig_gap

    def test_provider_exposes_gate(self):
        p = CA.CodeArtsProvider({"_cfg_key": "codearts", "accounts": [ACCOUNT]})
        self.assertIsInstance(p.probe_gate, CA.ProbeGate)


class ConcurrencyRetryTest(unittest.TestCase):
    """③b 撞上 TM.00001041 要等一会儿重试，而不是直接报繁忙。"""

    class Resp:
        def __init__(self, status, body=b""):
            self.status_code = status
            self._body = body

        async def aread(self):
            return self._body

        async def aclose(self):
            return None

    class Client:
        def __init__(self, responses):
            self.responses = list(responses)
            self.n = 0

        def build_request(self, *a, **k):
            return object()

        async def send(self, *a, **k):
            r = self.responses[min(self.n, len(self.responses) - 1)]
            self.n += 1
            return r

    def setUp(self):
        self.orig_wait = CA.CONCURRENCY_WAIT_SECS
        CA.CONCURRENCY_WAIT_SECS = 0.05   # 测试里别真等 25 秒
        self.p = CA.CodeArtsProvider({"_cfg_key": "codearts", "accounts": [ACCOUNT]})

    def tearDown(self):
        CA.CONCURRENCY_WAIT_SECS = self.orig_wait

    def _run(self, responses):
        p = self.p
        p.get_client = lambda: self.Client(responses)
        body = {"model": "GLM-5.2", "messages": []}
        return asyncio.run(p._open_stream(body, dict(ACCOUNT), "GLM-5.2"))

    def test_retries_after_concurrency_limit(self):
        limit = self.Resp(400, b'{"error_code":"TM.00001041",'
                               b'"error_msg":"concurrency limit"}')
        ok = self.Resp(200)
        resp, _cid = self._run([limit, ok])
        self.assertIs(resp, ok, "撞上限后应等待重试并拿到 200")

    def test_gives_up_after_budget(self):
        limit = self.Resp(400, b'{"error_code":"TM.00001041",'
                               b'"error_msg":"concurrency limit"}')
        orig_rounds = CA.CONCURRENCY_MAX_ROUNDS
        CA.CONCURRENCY_MAX_ROUNDS = 2
        try:
            with self.assertRaises(RuntimeError) as ctx:
                self._run([limit])
            self.assertIn("TM.00001041", str(ctx.exception))
        finally:
            CA.CONCURRENCY_MAX_ROUNDS = orig_rounds

    def test_other_errors_are_not_retried(self):
        bad = self.Resp(400, b'{"error_code":"X","error_msg":"bad"}')
        with self.assertRaises(RuntimeError) as ctx:
            self._run([bad])
        self.assertIn("bad", str(ctx.exception))


class ClassifyTest(unittest.TestCase):
    """繁忙 / 断连口径不变：并发上限与排队都算繁忙，认证失败才算断连。"""

    def test_concurrency_is_busy(self):
        import auto_router as A
        self.assertEqual(A._classify_check_error("TM.00001041 并发会话数已达上限"), "busy")

    def test_not_registered_is_down(self):
        import auto_router as A
        self.assertEqual(A._classify_check_error(
            "模型 X 未注册（上游内部名 y）：InferHub.002002009.404 not registered"), "down")

    def test_auth_failure_is_down(self):
        import auto_router as A
        self.assertEqual(A._classify_check_error("codearts 上游拒绝鉴权 HTTP 401"), "down")


if __name__ == "__main__":
    unittest.main()
