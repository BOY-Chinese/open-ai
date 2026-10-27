# -*- coding: utf-8 -*-
"""
测试: 续期凭据落盘 / 合并口径 / 跨进程锁（2026-10-23 CodeArts 账号「烧死」事故）
================================================================================
事故链（三条，缺一条都不会死号）：
  1. 续期结果**只留内存**、从不落盘 → 重启后磁盘上还是已被消耗的旧 refresh token；
  2. `_load_accounts()` 用磁盘旧值**覆盖**内存新凭据（老实现直接 self.accounts = fresh）
     → 刚拿到的新 token 下一次请求就丢；
  3. 没有跨进程互斥 → 网关 / task / CLI / 桌面端任两个同时续期 = 同一个单次
     refresh token 被用两次 = 华为判「复用」→ 整条凭据链作废（STS5.1806）。

本文件对三层修复逐条设断言，并对**别的通道的账号零影响**设断言。
不联网、不动真实 config（全部走临时文件）。
运行: python -m unittest tests.test_cred_store -v
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

from providers import cred_store as CS  # noqa: E402


def _cfg():
    """两份账号：codearts（要改的那条）+ 另一个通道（必须原样不动）。"""
    return {
        "api_keys": [{"name": "k", "key": "sk-test"}],
        "providers": {
            "codearts": {"accounts": [
                {"uid": "uid-ca", "name": "CA", "enabled": True,
                 "refreshToken": "old-refresh", "expiresAt": 100,
                 "credentials": {"accessKeyId": "AK1", "securityToken": "ST1"}}]},
            "qoder_cn": {"accounts": [
                {"uid": "uid-qd", "accessToken": "old-access",
                 "refreshToken": "old-qd-refresh", "expiresAt": 100}]},
        },
    }


class CredStoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="credstore-")
        self.cfg_path = os.path.join(self.tmp, "config.json")
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(_cfg(), f, ensure_ascii=False)
        self._orig_path = CS.config_path
        CS.config_path = lambda: self.cfg_path
        store = CS._api_store()
        self._orig_store_path = store.OPENAI_CFG
        store.OPENAI_CFG = self.cfg_path
        # 锁目录也跟着临时目录走，避免污染真实 data/
        self._orig_lockdir = CS._lock_dir
        CS._lock_dir = lambda: self.tmp

    def tearDown(self):
        CS.config_path = self._orig_path
        CS._lock_dir = self._orig_lockdir
        CS._api_store().OPENAI_CFG = self._orig_store_path

    def read(self, seg="codearts", uid="uid-ca"):
        with open(self.cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        for acc in ((cfg.get("providers") or {}).get(seg) or {}).get("accounts") or []:
            if acc.get("uid") == uid:
                return acc
        return {}


class CredentialsNewerTest(unittest.TestCase):
    """合并口径：refreshedAt 优先，其次 expiresAt；缺失当 0。"""

    def test_refreshed_at_wins(self):
        self.assertTrue(CS.credentials_newer({"refreshedAt": 10}, {"refreshedAt": 5}))
        self.assertFalse(CS.credentials_newer({"refreshedAt": 5}, {"refreshedAt": 10}))

    def test_expires_at_fallback(self):
        self.assertTrue(CS.credentials_newer({"expiresAt": 200}, {"expiresAt": 100}))
        self.assertFalse(CS.credentials_newer({"expiresAt": 100}, {"expiresAt": 200}))
        self.assertFalse(CS.credentials_newer({"expiresAt": 100}, {"expiresAt": 100}))

    def test_refreshed_at_ignored_when_one_side_missing(self):
        """一边没记过 refreshedAt（旧版本写的配置）→ 退回比有效期，别被 0 骗了。"""
        self.assertFalse(CS.credentials_newer({"refreshedAt": 1, "expiresAt": 1},
                                             {"expiresAt": 100}))
        self.assertTrue(CS.credentials_newer({"refreshedAt": 5, "expiresAt": 500},
                                            {"expiresAt": 100}))

    def test_empty_side(self):
        self.assertTrue(CS.credentials_newer({"expiresAt": 1}, {}))
        self.assertFalse(CS.credentials_newer({}, {"expiresAt": 1}))
        self.assertFalse(CS.credentials_newer(None, {"expiresAt": 1}))


class ReadAccountTest(CredStoreBase):
    def test_reads_by_uid(self):
        acc = CS.read_account("codearts", "uid-ca")
        self.assertEqual(acc["refreshToken"], "old-refresh")

    def test_missing_cases(self):
        self.assertEqual(CS.read_account("codearts", "nobody"), {})
        self.assertEqual(CS.read_account("codearts", ""), {})
        self.assertEqual(CS.read_account("nosuch", "uid-ca"), {})


class PersistAccountTest(CredStoreBase):
    def test_persists_only_its_own_fields(self):
        ok = CS.persist_account("codearts", "uid-ca", {
            "refreshToken": "new-refresh", "expiresAt": 999,
            "refreshedAt": 1234, "credentials": {"accessKeyId": "AK2",
                                                 "securityToken": "ST2"},
            "unknownField": "x"})
        self.assertTrue(ok)
        acc = self.read()
        self.assertEqual(acc["refreshToken"], "new-refresh")
        self.assertEqual(acc["expiresAt"], 999)
        self.assertEqual(acc["credentials"]["securityToken"], "ST2")
        self.assertNotIn("unknownField", acc)          # 白名单外的字段不落盘
        self.assertEqual(acc["name"], "CA")            # 其余字段原样

    def test_other_channels_untouched(self):
        before = json.dumps(_cfg()["providers"]["qoder_cn"], sort_keys=True)
        CS.persist_account("codearts", "uid-ca", {"refreshToken": "new"})
        with open(self.cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertEqual(json.dumps(cfg["providers"]["qoder_cn"], sort_keys=True), before)
        self.assertEqual(cfg["api_keys"], [{"name": "k", "key": "sk-test"}])

    def test_rolling_backup_written(self):
        CS.persist_account("codearts", "uid-ca", {"refreshToken": "new"})
        self.assertTrue(os.path.exists(self.cfg_path + ".bak-auto"))
        with open(self.cfg_path + ".bak-auto", encoding="utf-8") as f:
            bak = json.load(f)
        self.assertEqual(bak["providers"]["codearts"]["accounts"][0]["refreshToken"],
                         "old-refresh")          # 备份是写之前那一版

    def test_unknown_uid_refused(self):
        self.assertFalse(CS.persist_account("codearts", "nobody", {"refreshToken": "x"}))
        self.assertFalse(CS.persist_account("codearts", "", {"refreshToken": "x"}))
        self.assertEqual(self.read()["refreshToken"], "old-refresh")

    def test_nothing_to_write(self):
        self.assertFalse(CS.persist_account("codearts", "uid-ca", {"unknown": 1}))
        self.assertFalse(CS.persist_account("codearts", "uid-ca", {"refreshToken": None}))

    def test_qoder_fields_are_different_from_codearts(self):
        CS.persist_account("qoder_cn", "uid-qd", {
            "accessToken": "new-access", "refreshToken": "new-qd",
            "expiresAt": 555, "refreshedAt": 9, "credentials": {"x": 1}})
        acc = self.read("qoder_cn", "uid-qd")
        self.assertEqual(acc["accessToken"], "new-access")
        self.assertNotIn("credentials", acc)           # Qoder 不该被写入 credentials


class AccountLockTest(CredStoreBase):
    def test_second_holder_times_out_but_proceeds(self):
        """拿不到锁也照常继续（fail-open），不是死等/抛错。"""
        with CS.account_lock("codearts", "uid-ca"):
            t0 = time.time()
            with CS.account_lock("codearts", "uid-ca", timeout=0.4) as held:
                self.assertFalse(held)                  # 没拿到
            self.assertLess(time.time() - t0, 5.0)

    def test_lock_released_after_use(self):
        with CS.account_lock("codearts", "uid-ca"):
            pass
        with CS.account_lock("codearts", "uid-ca", timeout=0.4) as held:
            self.assertTrue(held)

    def test_stale_lock_is_taken_over(self):
        path = CS._lock_path("codearts", "uid-ca")
        with open(path, "w") as f:
            f.write("dead")
        old = time.time() - CS._LOCK_STALE - 10
        os.utime(path, (old, old))
        with CS.account_lock("codearts", "uid-ca", timeout=0.5) as held:
            self.assertTrue(held)

    def test_different_accounts_do_not_block(self):
        with CS.account_lock("codearts", "uid-ca"):
            with CS.account_lock("qoder_cn", "uid-qd", timeout=0.4) as held:
                self.assertTrue(held)


class CodeArtsMergeTest(unittest.TestCase):
    """CodeArts `_load_accounts`：内存里更新的凭据不许被磁盘旧值覆盖。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="camerge-")
        self.cfg_path = os.path.join(self.tmp, "config.json")
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(_cfg(), f, ensure_ascii=False)
        self._orig_path = CS.config_path
        CS.config_path = lambda: self.cfg_path
        store = CS._api_store()
        self._orig_store_path = store.OPENAI_CFG
        store.OPENAI_CFG = self.cfg_path

        import providers.codearts as CA
        self.CA = CA
        self._orig_ca_path = CA._config_path
        CA._config_path = lambda: self.cfg_path

    def tearDown(self):
        CS.config_path = self._orig_path
        CS._api_store().OPENAI_CFG = self._orig_store_path
        self.CA._config_path = self._orig_ca_path

    def provider(self, memory_account):
        prov = self.CA.CodeArtsProvider({"_cfg_key": "codearts",
                                         "accounts": [memory_account]})
        return prov

    def test_memory_newer_survives_reload(self):
        mem = {"uid": "uid-ca", "refreshToken": "brand-new", "expiresAt": 9999,
               "refreshedAt": int(time.time()),
               "credentials": {"accessToken": "n", "accessKeyId": "AK9",
                               "secretAccessKey": "SK9", "securityToken": "ST9"}}
        prov = self.provider(mem)
        accounts = prov._load_accounts()
        self.assertEqual(accounts[0]["refreshToken"], "brand-new")
        self.assertEqual(accounts[0]["credentials"]["securityToken"], "ST9")

    def test_disk_newer_wins(self):
        mem = {"uid": "uid-ca", "refreshToken": "stale", "expiresAt": 1,
               "refreshedAt": 1}
        prov = self.provider(mem)
        accounts = prov._load_accounts()
        self.assertEqual(accounts[0]["refreshToken"], "old-refresh")
        self.assertEqual(accounts[0]["expiresAt"], 100)

    def test_force_refresh_not_skipped_by_disk_lookup(self):
        """★ 回归钉子：401 后的强制续期不许被「磁盘看起来更新」静默跳过。

        实测踩法：老调用点是 ``_ensure_token({**account, "expiresAt": 0})``，
        有效期被人为清零 → 磁盘那份永远「更新」→ 直接采用磁盘凭据返回 True，
        真正的续期一次都没发生，请求拿着旧凭据再吃一次 401。
        """
        CA = self.CA
        called = []
        orig_refresh, orig_parse = CA.refresh_token, CA.accounts_from_token_response
        orig_read = CS.read_account
        CA.refresh_token = lambda acc: called.append(1) or {"ok": 1}
        CA.accounts_from_token_response = lambda *a, **k: {
            "uid": "uid-ca", "refreshToken": "new-refresh",
            "expiresAt": int(time.time()) + 7200,
            "credentials": {"accessKeyId": "AK", "secretAccessKey": "SK",
                            "securityToken": "ST"}}
        CS.read_account = lambda seg, uid: {"uid": "uid-ca", "expiresAt": 100,
                                           "refreshToken": "old-refresh"}
        try:
            mem = {"uid": "uid-ca", "refreshToken": "old-refresh",
                   "expiresAt": int(time.time()) + 9999}
            prov = self.provider(mem)
            acc = prov.accounts[0]
            import asyncio
            self.assertTrue(asyncio.run(prov._ensure_token(acc, force=True)))
            self.assertTrue(called, "强制续期被跳过了")
            self.assertEqual(acc["refreshToken"], "new-refresh")
        finally:
            CA.refresh_token, CA.accounts_from_token_response = orig_refresh, orig_parse
            CS.read_account = orig_read


if __name__ == "__main__":
    unittest.main()
