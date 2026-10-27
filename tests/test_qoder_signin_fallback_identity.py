# -*- coding: utf-8 -*-
"""测试: Qoder 签到「跨区域设备身份」兜底（2026-10-24）

==============================================================
背景：
  上游**按「设备」发放**每日签到 —— 同一台机器、每个区域、每天只有一份。
  实测证据（2026-10-24）：同一时刻 账号 A 有 CLAIM_BENEFIT(CLAIMED)，
  而同机同区域的 账号 B 只有 VIEW_DETAILS；把 账号 B 换成**本机另一套**桌面端
  身份（UMID env=0）后，CLAIM_BENEFIT 立刻出现且为 CLAIMABLE，实际领取成功
  （addOnQuota 0 → 100，isQuotaExceeded true → false）。

  因此新增兜底：本区域身份拿不到签到活动时，改用另一区域的桌面端身份再查一次。
  开关：config.json 的 providers.<qoder_cn|qoder_intl>.signinFallbackIdentity。

本测试锁定：
  1. **默认关闭** —— 不写开关时行为与旧版完全一致（不额外发请求）；
  2. **取值解析** —— "cn"/"intl"/"auto"/true/false/缺失 各自映射到什么；
  3. **换了个寂寞** —— 指定与账号自身区域相同的身份时视为关闭（不做无意义请求）；
  4. **风险身份的缓存必须按身份分开** —— 否则两套身份会串（致命且难查）；
  5. **checkin 的兜底路径** —— 本区域查不到 → 用兜底身份查到 → 按该身份领取，
     且结果里带 identity 标记；本区域能查到则**绝不**多发一次请求。

不联网、不写 config：全部通过 monkeypatch 把网络层与配置文件挡掉。
运行: python -m unittest tests.test_qoder_signin_fallback_identity -v
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from providers import qoder_common as qc  # noqa: E402


def _daily_campaign():
    """一个「今天这一期」的签到活动（字段按实测响应形状）。"""
    now = int(time.time())
    return {
        "campaignId": "01a15aa4-d837-7bd2-963c-cac97df5854e",
        "campaignKey": "act-20261021-490",
        "actionType": "CLAIM_BENEFIT",
        "claimStatus": "CLAIMABLE",
        "startAt": now - 3600,
        "endAt": now + 3600,
        "benefit": {"kind": "CREDITS", "amount": 100},
    }


class _NoMachineEnv(unittest.TestCase):
    """基类：清掉 QD_MACHINE_* 环境变量。

    它们一旦存在，risk_identity 会**直接采用环境变量**而跳过 UMID 组件，
    本类测试就测不到「哪套身份」了。
    """

    KEYS = ("QD_MACHINE_TOKEN", "QD_MACHINE_CODE", "QD_MACHINE_TYPE",
            "QD_SIGNIN_FALLBACK_IDENTITY")

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in self.KEYS}

    def tearDown(self):
        for k, v in self._saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        qc._RISK_CACHE.clear()


class TestFallbackSettingParsing(_NoMachineEnv):
    """signinFallbackIdentity 的取值解析（用临时 config.json）。"""

    def _with_config(self, seg_extra, realm="intl"):
        """把 app_paths.CONFIG_PATH 指向临时配置，返回该 provider 的判定结果。"""
        import app_paths as ap
        cfg = {"providers": {"qoder_" + realm: dict(
            {"enabled": True, "switchEvery": 5, "accounts": [
                {"uid": "u1", "nickname": "n", "realm": realm,
                 "accessToken": "dt-fake", "enabled": True}]},
            **seg_extra)}}
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        orig = ap.CONFIG_PATH
        ap.CONFIG_PATH = path
        try:
            acc = cfg["providers"]["qoder_" + realm]["accounts"][0]
            return qc.signin_fallback_app_env(acc)
        finally:
            ap.CONFIG_PATH = orig
            os.unlink(path)

    def test_absent_is_off(self):
        """契约 1：不写开关 = 关闭（旧行为）。"""
        self.assertIsNone(self._with_config({}))

    def test_false_variants_are_off(self):
        for v in (False, "false", "off", "0", "no", "none", "disabled", ""):
            with self.subTest(value=v):
                self.assertIsNone(self._with_config({"signinFallbackIdentity": v}))

    def test_cn_maps_to_env_0(self):
        """intl 账号指定 "cn" → UMID env=0（本机国内版桌面端身份）。"""
        self.assertEqual(self._with_config({"signinFallbackIdentity": "cn"}), "0")

    def test_intl_maps_to_env_3(self):
        """cn 账号指定 "intl" → UMID env=3。"""
        self.assertEqual(
            self._with_config({"signinFallbackIdentity": "intl"}, realm="cn"), "3")

    def test_auto_picks_the_other_region(self):
        """auto/true → 自动选「另一个区域」那套。"""
        for v in ("auto", True, "other", "cross", 1, "on", "yes"):
            with self.subTest(value=v):
                self.assertEqual(
                    self._with_config({"signinFallbackIdentity": v}), "0")
                self.assertEqual(
                    self._with_config({"signinFallbackIdentity": v}, realm="cn"), "3")

    def test_same_region_is_off(self):
        """契约 3：指定与自身区域相同 → 视为关闭（不做无意义的二次请求）。"""
        self.assertIsNone(self._with_config({"signinFallbackIdentity": "intl"}))
        self.assertIsNone(
            self._with_config({"signinFallbackIdentity": "cn"}, realm="cn"))

    def test_cfg_missing_or_broken_is_off(self):
        """配置读不到/坏掉 → 一律当关闭，绝不因此抛异常。"""
        import app_paths as ap
        orig = ap.CONFIG_PATH
        ap.CONFIG_PATH = "/nonexistent/definitely/not/here.json"
        try:
            self.assertIsNone(qc.signin_fallback_app_env(
                {"uid": "u1", "realm": "intl"}))
        finally:
            ap.CONFIG_PATH = orig

    def test_env_var_fallback_when_config_silent(self):
        """config 没写时，环境变量仍可兜底（便于排障）。"""
        os.environ["QD_SIGNIN_FALLBACK_IDENTITY"] = "cn"
        self.assertEqual(self._with_config({}), "0")


class TestRiskIdentityPerEnv(_NoMachineEnv):
    """契约 4：risk_identity 必须把 app_env 传给组件，且缓存按身份分开。"""

    def setUp(self):
        super().setUp()
        self.seen = []
        self._orig_run = subprocess.run
        self._orig_comp = qc._umid_component
        qc._umid_component = lambda realm: "fake-runtime-info.exe"  # noqa: ARG005

        def fake_run(cmd, **kw):  # noqa: ARG001
            env = str(cmd[1])
            self.seen.append(env)
            code = "690fc5ab76f54db8" if env == "0" else "e656a6847603fdf0"
            out = json.dumps({"machineToken": "tok-" + env,
                              "machineCode": code,
                              "machineType": "mt"}) + "\n"
            return subprocess.CompletedProcess(cmd, 0, out.encode("utf-8"), b"")

        subprocess.run = fake_run
        qc._RISK_CACHE.clear()

    def tearDown(self):
        subprocess.run = self._orig_run
        qc._umid_component = self._orig_comp
        qc._RISK_CACHE.clear()
        super().tearDown()

    def test_env_is_passed_through(self):
        v, err = qc.risk_identity("intl", "u1", "0")
        self.assertEqual(err, "")
        self.assertEqual(v["machineCode"], "690fc5ab76f54db8")
        self.assertEqual(self.seen, ["0"])

    def test_two_envs_do_not_share_cache(self):
        """★ 这条是本次最容易写错、后果最严重的点：两套身份串了就会
        「以为是兜底，其实还是用原身份」，症状是兜底永远无效。"""
        v0, _ = qc.risk_identity("intl", "u1", "0")
        v3, _ = qc.risk_identity("intl", "u1", "3")
        self.assertEqual(v0["machineCode"], "690fc5ab76f54db8")
        self.assertEqual(v3["machineCode"], "e656a6847603fdf0")
        self.assertEqual(self.seen, ["0", "3"])
        # 再各取一次：命中缓存，不应再调组件
        self.assertEqual(qc.risk_identity("intl", "u1", "0")[0]["machineCode"],
                         "690fc5ab76f54db8")
        self.assertEqual(qc.risk_identity("intl", "u1", "3")[0]["machineCode"],
                         "e656a6847603fdf0")
        self.assertEqual(self.seen, ["0", "3"], "命中缓存后不该再调组件")

    def test_default_env_follows_realm(self):
        qc.risk_identity("intl", "u2")
        qc.risk_identity("cn", "u3")
        self.assertEqual(self.seen, ["3", "0"])

    def test_desktop_headers_carry_the_requested_identity(self):
        h = qc.desktop_headers({"uid": "u1", "realm": "intl",
                                "accessToken": "dt-fake"}, "intl", "0")
        self.assertEqual(h.get("cosy-machinecode"), "690fc5ab76f54db8")
        self.assertEqual(h.get("cosy-clienttype"), qc.DESKTOP_CLIENT_TYPE)


class TestCheckinFallbackPath(_NoMachineEnv):
    """契约 5：checkin 的兜底接线。"""

    def setUp(self):
        super().setUp()
        self.calls = []          # 每次 _campaigns 的 app_env
        self.claims = []         # 每次 claim_campaign 的 app_env
        self._orig = (qc._campaigns, qc.claim_campaign,
                      qc.signin_fallback_app_env, qc.checkin_status,
                      qc.account_quota)

    def tearDown(self):
        (qc._campaigns, qc.claim_campaign,
         qc.signin_fallback_app_env, qc.checkin_status,
         qc.account_quota) = self._orig
        super().tearDown()

    def _wire(self, own_items, fb_items, fb_env="0"):
        qc.signin_fallback_app_env = lambda account: fb_env  # noqa: ARG005
        # 把 sash 兜底与配额查询也挡掉：本测试只关心「活动平台」这一层的兜底，
        # 联网会让用例变慢且不稳定。
        qc.checkin_status = lambda account: (False, {"unavailable": True})  # noqa: ARG005
        qc.account_quota = lambda account: {}  # noqa: ARG005

        def fake_campaigns(account, app_env=None):  # noqa: ARG001
            self.calls.append(app_env)
            # ★ 注意 fb_env=None 时不能把 app_env=None 当成「兜底」——
            #   否则「开关关闭」的用例会误走兜底分支（本用例踩过）。
            if fb_env is not None and app_env == fb_env:
                return fb_items, ""
            return own_items, ""

        def fake_claim(account, campaign, app_env=None):  # noqa: ARG001
            self.claims.append(app_env)
            return {"ok": True, "credited": True, "credits": 100,
                    "status": "CLAIMED", "already": False,
                    "msg": "签到成功 +100 积分"}

        qc._campaigns = fake_campaigns
        qc.claim_campaign = fake_claim

    _VIEW = {"actionType": "VIEW_DETAILS", "claimStatus": "CLAIMED"}

    def test_falls_back_when_own_identity_has_none(self):
        """本区域只有 VIEW_DETAILS → 用兜底身份查到并领取。"""
        self._wire([self._VIEW], [_daily_campaign()])
        acc = {"uid": "u1", "realm": "intl", "accessToken": "dt-fake",
               "nickname": "账号 B"}
        r = qc.checkin(acc)
        self.assertTrue(r["credited"])
        self.assertEqual(r["amount"], 100)
        self.assertEqual(self.calls, [None, "0"], "应先查本区域，再查兜底身份")
        self.assertEqual(self.claims, ["0"], "领取必须用**兜底身份**，否则领不到")
        self.assertEqual(r.get("identity"), "fallback_env_0")

    def test_no_extra_request_when_own_identity_works(self):
        """本区域就能查到 → 绝不额外发第二次请求（省一次风控暴露）。"""
        self._wire([_daily_campaign()], [_daily_campaign()])
        r = qc.checkin({"uid": "u1", "realm": "intl", "accessToken": "dt-fake"})
        self.assertTrue(r["credited"])
        self.assertEqual(self.calls, [None])
        self.assertEqual(self.claims, [None])
        self.assertEqual(r.get("identity"), "realm")

    def test_disabled_keeps_old_behaviour(self):
        """开关关闭（fb_env=None）→ 只查本区域，**绝不**用兜底身份发请求。"""
        self._wire([self._VIEW], [_daily_campaign()], fb_env=None)
        r = qc.checkin({"uid": "u1", "realm": "intl", "accessToken": "dt-fake"})
        self.assertEqual(self.calls[0], None)
        self.assertNotIn("0", self.calls, "关闭兜底时不该出现兜底身份的请求")
        self.assertEqual(self.claims, [])
        self.assertFalse(r["credited"])

    def test_fallback_found_nothing_does_not_fake_success(self):
        """两套身份都查不到 → 绝不能编造成功（历史事故的同类风险）。"""
        self._wire([self._VIEW], [self._VIEW])
        r = qc.checkin({"uid": "u1", "realm": "intl", "accessToken": "dt-fake"})
        self.assertTrue(r["ok"])
        self.assertFalse(r["credited"])
        self.assertEqual(r["credits"], 0)
        self.assertIn(r["reason"], ("no_campaign", "sash_claimed", "sash_claim",
                                    "sash_error"))
        self.assertEqual(self.claims, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)