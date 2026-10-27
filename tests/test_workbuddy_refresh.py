# -*- coding: utf-8 -*-
"""
测试: workbuddy / workbuddy 国际版续期落盘（2026-10-23 与 Qoder / CodeArts 对齐）
================================================================================
背景：CodeArts 因为「续期结果只留内存 + 用磁盘旧值覆盖内存 + 无跨进程互斥」
把账号玩死（refresh token 单次使用，复用即作废）。workbuddy 族此前是**同款写法**
（`_refresh_token` 只改内存里的 account），只是它的 refreshToken 是否轮换还没被
证实 —— 这里把同一套护栏接上，并钉住三个行为：

  1. `_refresh_token(force=True)` 真的打上游 + **成功即落盘**（补写 expiresAt /
     refreshedAt，供下次「谁更新」的比较）；
  2. 锁内重读磁盘发现别人刚续期过 → 直接采用，不再换第二次；
  3. workbuddy 族的账号主键是 **userId**（不是 uid），读写必须按它定位。

不联网：HTTP 客户端整体替换成假对象；config 走临时文件。
运行: python -m unittest tests.test_workbuddy_refresh -v
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

from providers import cred_store as CS  # noqa: E402
from providers import workbuddy as WB  # noqa: E402
from providers.workbuddy import WorkBuddyProvider  # noqa: E402
from providers.workbuddy_intl import WorkBuddyIntlProvider  # noqa: E402


def _jwt(exp: int) -> str:
    """造一个只有 exp 的假 JWT（_jwt_exp 只看 payload）。"""
    import base64
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return "x." + payload + ".y"


class FakeResp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code = status
        self._data = data or {}
        self.text = text

    def json(self):
        return self._data


class FakeClient:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    async def post(self, url, json=None, headers=None):
        self.calls.append((url, headers))
        return self.resp


class WorkBuddyRefreshBase(unittest.TestCase):
    """把 config 指向临时文件（含 workbuddy + workbuddy_intl 两条账号）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wbrefresh-")
        self.cfg_path = os.path.join(self.tmp, "config.json")
        self.old_at = _jwt(int(time.time()) - 10)
        self.new_at = _jwt(int(time.time()) + 30 * 86400)
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump({"providers": {
                "workbuddy": {"accounts": [
                    {"userId": "wb-user-1", "accessToken": self.old_at,
                     "refreshToken": "rt-old", "enabled": True}]},
                "workbuddy_intl": {"accounts": [
                    {"userId": "wb-intl-1", "accessToken": self.old_at,
                     "refreshToken": "rt-intl-old", "enabled": True}]},
            }}, f)
        store = CS._api_store()
        self._orig_store = store.OPENAI_CFG
        store.OPENAI_CFG = self.cfg_path
        self._orig_lockdir = CS._lock_dir
        CS._lock_dir = lambda: self.tmp

    def tearDown(self):
        CS._api_store().OPENAI_CFG = self._orig_store
        CS._lock_dir = self._orig_lockdir

    def disk(self, seg="workbuddy", uid="wb-user-1"):
        with open(self.cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        for acc in ((cfg.get("providers") or {}).get(seg) or {}).get("accounts") or []:
            if acc.get("userId") == uid:
                return acc
        return {}


class CredentialsNewerTest(unittest.TestCase):
    def test_jwt_exp_decides(self):
        now = int(time.time())
        self.assertTrue(WB._wb_credentials_newer(
            {"accessToken": _jwt(now + 100)}, {"accessToken": _jwt(now + 50)}))
        self.assertFalse(WB._wb_credentials_newer(
            {"accessToken": _jwt(now + 50)}, {"accessToken": _jwt(now + 100)}))

    def test_falls_back_to_refreshed_at(self):
        self.assertTrue(WB._wb_credentials_newer({"refreshedAt": 5},
                                                {"refreshedAt": 1}))
        self.assertFalse(WB._wb_credentials_newer({"refreshedAt": 1},
                                                 {"refreshedAt": 5}))

    def test_adopt_is_atomic_pair(self):
        acc = {"accessToken": "a", "refreshToken": "b"}
        WB._wb_adopt(acc, {"accessToken": "A", "refreshToken": "B",
                           "expiresAt": 9, "refreshedAt": 8})
        self.assertEqual((acc["accessToken"], acc["refreshToken"]), ("A", "B"))
        self.assertEqual((acc["expiresAt"], acc["refreshedAt"]), (9, 8))


class WorkBuddyRefreshTest(WorkBuddyRefreshBase):
    def provider(self, seg="workbuddy"):
        cls = WorkBuddyProvider if seg == "workbuddy" else WorkBuddyIntlProvider
        cfg = {"_cfg_key": seg,
               "accounts": [json.loads(json.dumps(self._account(seg)))]}
        return cls(cfg)

    def _account(self, seg):
        with open(self.cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg["providers"][seg]["accounts"][0]

    def test_refresh_persists_and_stamps_expiry(self):
        prov = self.provider()
        client = FakeClient(FakeResp(200, {"data": {
            "accessToken": self.new_at, "refreshToken": "rt-new"}}))
        prov.get_client = lambda: client
        acc = prov.accounts[0]
        self.assertTrue(asyncio.run(prov._refresh_token(acc, force=True)))
        self.assertEqual(acc["accessToken"], self.new_at)
        disk = self.disk()
        self.assertEqual(disk["accessToken"], self.new_at)
        self.assertEqual(disk["refreshToken"], "rt-new")
        self.assertGreater(disk["expiresAt"], time.time() + 20 * 86400)  # 来自 JWT
        self.assertGreater(disk["refreshedAt"], 0)
        self.assertTrue(os.path.exists(self.cfg_path + ".bak-auto"))

    def test_refresh_does_not_persist_on_failure(self):
        prov = self.provider()
        prov.get_client = lambda: FakeClient(FakeResp(500, {}, "boom"))
        acc = prov.accounts[0]
        self.assertFalse(asyncio.run(prov._refresh_token(acc, force=True)))
        disk = self.disk()
        self.assertEqual(disk["accessToken"], self.old_at)
        self.assertEqual(disk["refreshToken"], "rt-old")
        self.assertNotIn("refreshedAt", disk)

    def test_adopts_disk_credentials_when_newer(self):
        """别的进程刚续期过（磁盘 JWT 更晚）→ 直接采用，不再换第二次。"""
        disk_fresh = {"userId": "wb-user-1", "accessToken": self.new_at,
                      "refreshToken": "rt-from-other"}
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump({"providers": {"workbuddy": {"accounts": [disk_fresh]},
                                     "workbuddy_intl": {"accounts": []}}}, f)
        # 内存那份仍是旧的（模拟「本进程还没续期，另一个进程已经换过」）
        prov = WorkBuddyProvider({"_cfg_key": "workbuddy", "accounts": [
            {"userId": "wb-user-1", "accessToken": self.old_at,
             "refreshToken": "rt-old"}]})
        client = FakeClient(FakeResp(200, {"data": {"accessToken": _jwt(1)}}))
        prov.get_client = lambda: client
        acc = prov.accounts[0]
        self.assertTrue(asyncio.run(prov._refresh_token(acc, force=True)))
        self.assertEqual(acc["accessToken"], self.new_at)
        self.assertEqual(acc["refreshToken"], "rt-from-other")
        self.assertFalse(client.calls, "不该再去换一次")

    def test_no_refresh_token_returns_false(self):
        prov = self.provider()
        acc = {"userId": "x", "accessToken": self.old_at}
        self.assertFalse(asyncio.run(prov._refresh_token(acc, force=True)))

    def test_not_expired_is_a_noop_without_force(self):
        fresh = _jwt(int(time.time()) + 86400)
        cfg = {"_cfg_key": "workbuddy",
               "accounts": [{"userId": "wb-user-1", "accessToken": fresh,
                             "refreshToken": "rt-old"}]}
        prov = WorkBuddyProvider(cfg)
        client = FakeClient(FakeResp(200, {"data": {"accessToken": "x"}}))
        prov.get_client = lambda: client
        self.assertTrue(asyncio.run(prov._refresh_token(prov.accounts[0])))
        self.assertFalse(client.calls)

    def test_intl_uses_same_pipeline(self):
        prov = self.provider("workbuddy_intl")
        client = FakeClient(FakeResp(200, {"data": {
            "accessToken": self.new_at, "refreshToken": "rt-intl-new"}}))
        prov.get_client = lambda: client
        acc = prov.accounts[0]
        self.assertTrue(asyncio.run(prov._refresh_token(acc, force=True)))
        disk = self.disk("workbuddy_intl", "wb-intl-1")
        self.assertEqual(disk["refreshToken"], "rt-intl-new")
        self.assertGreater(disk["expiresAt"], time.time() + 20 * 86400)

    def test_intl_hits_intl_host(self):
        prov = self.provider("workbuddy_intl")
        client = FakeClient(FakeResp(200, {"data": {"accessToken": self.new_at}}))
        prov.get_client = lambda: client
        asyncio.run(prov._refresh_token(prov.accounts[0], force=True))
        self.assertTrue(client.calls)
        self.assertIn("workbuddy.ai", client.calls[0][0])


class CredStoreUserIdKeyTest(WorkBuddyRefreshBase):
    """workbuddy 段按 userId 定位（不是 uid）。"""

    def test_read_and_persist_by_user_id(self):
        acc = CS.read_account("workbuddy", "wb-user-1")
        self.assertEqual(acc["refreshToken"], "rt-old")
        self.assertEqual(CS.read_account("workbuddy", "nobody"), {})
        self.assertTrue(CS.persist_account("workbuddy", "wb-user-1",
                                          {"accessToken": "NEW"}))
        self.assertEqual(self.disk()["accessToken"], "NEW")
        # 另一段不受影响
        self.assertEqual(self.disk("workbuddy_intl", "wb-intl-1")["accessToken"],
                         self.old_at)

    def test_qoder_still_uses_uid(self):
        self.assertEqual(CS.UID_KEY_BY_SEGMENT.get("qoder_cn", "uid"), "uid")


if __name__ == "__main__":
    unittest.main()
