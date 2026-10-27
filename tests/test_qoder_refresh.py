# -*- coding: utf-8 -*-
"""
测试: Qoder 续期路由（2026-10-23「两个 Qoder 通道今天 21:48 会整条死掉」）
================================================================================
事故背景：``poll_device_login`` 存下来的凭据是 accessToken=``dt-…``、
refreshToken=``drt-…``，而 ``refresh_access_token`` 老实现判的是
``token.startswith("drt-")`` —— 设备账号两个分支都进不去（job 分支又被
``not refresh.startswith("drt-")`` 挡住），永远抛「无可用刷新凭据」：
**续期从来没成功过**。accessToken 一到期，CN / 国际两个通道同时全模型不可用，
只能重新扫码登录。第二个坑：设备续期响应里的字段叫 ``device_token``，
老实现只认 token / access_token，取到空值又静默当作「没有可用凭据」。

本文件把「按 refreshToken 前缀路由」「认 device_token」「以上游 expires_in /
expires_at 为准（dt- 是 30 天）」「续期成功立刻落盘」逐条钉住。
不联网：全部 monkeypatch 掉 http_json。
运行: python -m unittest tests.test_qoder_refresh -v
"""
import json
import os
import sys
import tempfile
import time
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

from providers import qoder_common as QC  # noqa: E402
from providers import cred_store as CS  # noqa: E402
from providers.qoder_cn import QoderCNProvider  # noqa: E402


class RefreshRoutingTest(unittest.TestCase):
    """按 refreshToken 前缀路由；设备族必须打到 deviceToken/refresh。"""

    def setUp(self):
        self.calls = []
        self._orig = QC.http_json

        def fake(url, **kw):
            self.calls.append((url, kw.get("data")))
            if url.endswith(QC.PATH_DEVICE_REFRESH):
                return {"device_token": "dt-new", "refresh_token": "drt-new",
                        "expires_in": 30 * 86400 * 1000}
            if url.endswith(QC.PATH_JOB_REFRESH):
                return {"token": "jrt-new", "refresh_token": "jrt-new-2"}
            if url.endswith(QC.PATH_JOB_EXCHANGE):
                return {"token": "jt-from-pat", "refresh_token": "jrt-from-pat"}
            raise AssertionError("未预期的端点: " + url)

        QC.http_json = fake

    def tearDown(self):
        QC.http_json = self._orig

    def _paths(self):
        return [u.split("/api/v1/")[-1] for u, _ in self.calls]

    def test_device_account_hits_device_refresh(self):
        """★ 回归钉子：dt- + drt- 的设备账号必须走 deviceToken/refresh。"""
        token, refresh, exp = QC.refresh_access_token({
            "realm": "cn", "accessToken": "dt-MLg123", "refreshToken": "drt-za9"})
        self.assertEqual(self._paths(), ["deviceToken/refresh"])
        self.assertEqual(token, "dt-new")            # 认 device_token 字段
        self.assertEqual(refresh, "drt-new")
        self.assertGreater(exp, time.time() + 20 * 86400)   # 30 天量级

    def test_job_account_hits_job_refresh(self):
        token, refresh, _exp = QC.refresh_access_token({
            "realm": "cn", "accessToken": "jt-abc", "refreshToken": "jrt-xyz"})
        self.assertEqual(self._paths(), ["jobToken/refresh"])
        self.assertEqual(token, "jrt-new")

    def test_pat_account_falls_back_to_exchange(self):
        token, _refresh, _exp = QC.refresh_access_token({
            "realm": "intl", "personalToken": "pt-abc"})
        self.assertEqual(self._paths(), ["jobToken/exchange"])
        self.assertEqual(token, "jt-from-pat")

    def test_no_credentials_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            QC.refresh_access_token({"realm": "cn"})
        self.assertIn("无可用刷新凭据", str(ctx.exception))

    def test_device_refresh_failure_then_no_token_is_loud(self):
        """响应里没有 token 时必须进 errs（老实现是静默返回空）。"""
        QC.http_json = lambda url, **kw: {"expires_in": 1000}
        with self.assertRaises(RuntimeError) as ctx:
            QC.refresh_access_token({"realm": "cn", "accessToken": "dt-x",
                                     "refreshToken": "drt-y"})
        self.assertIn("device refresh", str(ctx.exception))


class TokenExpiryTest(unittest.TestCase):
    """过期时刻以上游响应为准，别写死 6 小时。"""

    def test_expires_in_milliseconds(self):
        exp = QC._token_expiry({"expires_in": 3600 * 1000}, True, 0)
        self.assertAlmostEqual(exp, time.time() + 3600, delta=5)

    def test_expires_at_epoch_and_rfc3339(self):
        exp = QC._token_expiry({"expires_at": int((time.time() + 7200) * 1000)},
                               True, 0)
        self.assertAlmostEqual(exp, time.time() + 7200, delta=5)
        exp = QC._token_expiry({"expires_at": "2030-01-02T03:04:05Z"}, True, 0)
        self.assertGreater(exp, time.time())

    def test_device_default_is_30_days(self):
        exp = QC._token_expiry({}, True, 0)
        self.assertAlmostEqual(exp, time.time() + 30 * 86400, delta=10)

    def test_job_falls_back_to_old_value(self):
        self.assertEqual(QC._token_expiry({}, False, 12345), 12345)


class EnsureTokenPersistTest(unittest.TestCase):
    """续期成功后凭据必须**立刻落盘**（否则重启就回放已作废的旧 token）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qdrefresh-")
        self.cfg_path = os.path.join(self.tmp, "config.json")
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump({"providers": {"qoder_cn": {"accounts": [
                {"uid": "uid-qd", "realm": "cn", "accessToken": "dt-old",
                 "refreshToken": "drt-old", "expiresAt": 1}]}}}, f)
        store = CS._api_store()
        self._orig_store = store.OPENAI_CFG
        store.OPENAI_CFG = self.cfg_path
        self._orig_lockdir = CS._lock_dir
        CS._lock_dir = lambda: self.tmp
        self._orig_read = CS.read_account
        CS.read_account = lambda seg, uid: {}      # 模拟「没有别的进程刚续期过」
        self._orig_refresh = QC.refresh_access_token
        QC.refresh_access_token = lambda account: (
            "dt-brand-new", "drt-brand-new", int(time.time()) + 30 * 86400)

    def tearDown(self):
        CS._api_store().OPENAI_CFG = self._orig_store
        CS._lock_dir = self._orig_lockdir
        CS.read_account = self._orig_read
        QC.refresh_access_token = self._orig_refresh

    def _disk(self):
        with open(self.cfg_path, encoding="utf-8") as f:
            return json.load(f)["providers"]["qoder_cn"]["accounts"][0]

    def test_refresh_persists_new_credentials(self):
        prov = QoderCNProvider({"accounts": [{"uid": "uid-qd", "realm": "cn",
                                              "accessToken": "dt-old",
                                              "refreshToken": "drt-old",
                                              "expiresAt": 0}]})
        acc = prov.accounts[0]
        self.assertTrue(prov._refresh_locked(acc))
        self.assertEqual(acc["accessToken"], "dt-brand-new")
        disk = self._disk()
        self.assertEqual(disk["accessToken"], "dt-brand-new")
        self.assertEqual(disk["refreshToken"], "drt-brand-new")
        self.assertGreater(disk["refreshedAt"], 0)
        self.assertGreater(disk["expiresAt"], time.time() + 20 * 86400)

    def test_adopts_disk_credentials_when_newer(self):
        """别的进程刚续期过 → 直接采用磁盘那份，不再拿旧 refresh token 去换。"""
        CS.read_account = lambda seg, uid: {
            "uid": "uid-qd", "accessToken": "dt-from-other-process",
            "refreshToken": "drt-from-other-process",
            "expiresAt": int(time.time()) + 30 * 86400}
        called = []
        QC.refresh_access_token = lambda account: called.append(1) or ("x", "y", 0)
        prov = QoderCNProvider({"accounts": [{"uid": "uid-qd", "realm": "cn",
                                              "accessToken": "dt-old",
                                              "refreshToken": "drt-old",
                                              "expiresAt": 0}]})
        acc = prov.accounts[0]
        self.assertTrue(prov._refresh_locked(acc))
        self.assertEqual(acc["accessToken"], "dt-from-other-process")
        self.assertFalse(called)                   # 没有真的去换


if __name__ == "__main__":
    unittest.main()
