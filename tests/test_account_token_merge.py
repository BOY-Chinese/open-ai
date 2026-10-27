# -*- coding: utf-8 -*-
"""
测试: 账号凭据合并方向 —— 重新登录后网关必须用上磁盘里的新 token
==================================================================
背景（2026-10-22 实测事故，国内版账号 uid=93350cbf）:
  网关进程比「重新登录」早 16 小时启动。`_pick_account()` 每次请求都重读
  config.json，却**无条件用内存里的旧 token 覆盖磁盘的新 token**，于是:
      旧 token 打上游 → HTTP 401
      → 拿旧 refreshToken 去续期 → HTTP 401
        {"code":12153, "invalid_grant: Offline user session not found"}
      → 账号被标失效 → 页面「断连」
      点「重新连接」只清内存红牌、不重载凭据 → 很快又红。
  实测把 config.json 里的 token 直连上游是 **200**（账号本身完全正常），
  所以这是纯插件缺陷，不是账号/上游问题。

覆盖:
1. `_mem_token_is_newer` —— 新旧判定与边界（空值 / 无法解析）。
2. `WorkBuddyProvider._pick_account` —— 磁盘新则用磁盘；内存新（provider 自己
   续过期）仍用内存（不能修坏原有能力）；AT/RT 成对替换，不许一半内存一半磁盘。
3. `WorkBuddyIntlProvider._pick_account` —— 国际版同口径（同一处缺陷）。

运行: python -m unittest tests.test_account_token_merge -v
"""
import base64
import json
import os
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from providers import workbuddy as WB                    # noqa: E402
from providers import workbuddy_intl as INTL             # noqa: E402
from providers.workbuddy import (                        # noqa: E402
    WorkBuddyProvider, _mem_token_is_newer,
)
from providers.workbuddy_intl import WorkBuddyIntlProvider  # noqa: E402

OLD_IAT = 1_700_000_000      # 旧凭据（比如网关启动时读到的）
NEW_IAT = 1_700_100_000      # 新凭据（用户重新登录 / 续期后写进 config.json）


def mk_jwt(user, iat, kind="Bearer"):
    """造一个含 iat/exp 的假 JWT（只关心 payload 能否被解析出时间）。"""
    hdr = base64.urlsafe_b64encode(
        json.dumps({"alg": "RS256"}).encode()).decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({
        "sub": user, "iat": iat, "exp": iat + 3600, "typ": kind,
    }).encode()).decode().rstrip("=")
    return f"{hdr}.{payload}.sig"


def acct(uid, at, rt):
    return {"userId": uid, "accessToken": at, "refreshToken": rt}


# ═══════════════════ 单元: 新旧判定 ═══════════════════

class MemTokenIsNewerTest(unittest.TestCase):

    def test_disk_newer(self):
        """磁盘 iat 更新 → 内存那份不算新（本次事故的直接断言）。"""
        self.assertFalse(_mem_token_is_newer(mk_jwt("u", OLD_IAT),
                                             mk_jwt("u", NEW_IAT)))

    def test_mem_newer(self):
        """provider 续期后内存更新 → 用内存（原能力不能修坏）。"""
        self.assertTrue(_mem_token_is_newer(mk_jwt("u", NEW_IAT),
                                            mk_jwt("u", OLD_IAT)))

    def test_equal_prefers_disk(self):
        self.assertFalse(_mem_token_is_newer(mk_jwt("u", NEW_IAT),
                                             mk_jwt("u", NEW_IAT)))

    def test_empty_mem_uses_disk(self):
        self.assertFalse(_mem_token_is_newer("", mk_jwt("u", NEW_IAT)))

    def test_empty_disk_uses_mem(self):
        self.assertTrue(_mem_token_is_newer(mk_jwt("u", OLD_IAT), ""))

    def test_unparseable_falls_back_to_disk(self):
        """两边都解析不出时间 → 以磁盘为准（磁盘=用户显式登录结果）。"""
        self.assertFalse(_mem_token_is_newer("not-a-jwt", "also-not-a-jwt"))

    def test_exp_used_when_no_iat(self):
        """没有 iat 时回退 exp 比较。"""
        hdr = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()
                                       ).decode().rstrip("=")
        def only_exp(exp):
            p = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()
                                         ).decode().rstrip("=")
            return f"{hdr}.{p}.sig"
        self.assertFalse(_mem_token_is_newer(only_exp(NEW_IAT), only_exp(NEW_IAT + 10)))
        self.assertTrue(_mem_token_is_newer(only_exp(NEW_IAT + 10), only_exp(NEW_IAT)))


# ═══════════════════ 合并行为（两条通道） ═══════════════════

class MergeCaseMixin:
    """两条通道共用的打桩夹具（不做 TestCase 基类，避免被重复收集）。"""

    provider_cls = WorkBuddyProvider
    segment = "workbuddy"

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                          delete=False, encoding="utf-8")
        tmp.close()
        self.path = tmp.name
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        self._orig_wb = WB._WB_CONFIG_PATH
        self.addCleanup(lambda: setattr(WB, "_WB_CONFIG_PATH", self._orig_wb))
        self._orig_intl = INTL._WB_CONFIG_PATH
        self.addCleanup(lambda: setattr(INTL, "_WB_CONFIG_PATH", self._orig_intl))
        # 两条通道的 _pick_account 都读 WB._WB_CONFIG_PATH（国际版是 import 进来的
        # 名字，故两个模块的绑定都要打桩到同一份临时文件）
        WB._WB_CONFIG_PATH = self.path
        INTL._WB_CONFIG_PATH = self.path

    def build(self, disk_accounts, mem_accounts):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"providers": {self.segment: {"accounts": disk_accounts}}},
                      f, ensure_ascii=False)
        return self.provider_cls({"accounts": [dict(a) for a in mem_accounts],
                                  "switchEvery": 1})

    def test_disk_newer_token_wins(self):
        """★ 本次事故的核心断言：重新登录写进磁盘的新 token 必须被用上。"""
        mem = mk_jwt("u1", OLD_IAT), mk_jwt("u1", OLD_IAT, "Offline")
        disk = mk_jwt("u1", NEW_IAT), mk_jwt("u1", NEW_IAT, "Offline")
        prov = self.build([acct("u1", *disk)], [acct("u1", *mem)])
        picked = prov._pick_account()
        self.assertEqual(picked.get("accessToken"), disk[0],
                         "磁盘上的新 token 被内存里的旧 token 覆盖了")
        self.assertEqual(picked.get("refreshToken"), disk[1],
                         "refreshToken 也要跟着用磁盘的新那份")

    def test_mem_refreshed_token_still_wins(self):
        """内存里是 provider 刚续期的新 token（磁盘还是旧的）→ 仍用内存。"""
        mem = mk_jwt("u1", NEW_IAT), mk_jwt("u1", NEW_IAT, "Offline")
        disk = mk_jwt("u1", OLD_IAT), mk_jwt("u1", OLD_IAT, "Offline")
        prov = self.build([acct("u1", *disk)], [acct("u1", *mem)])
        picked = prov._pick_account()
        self.assertEqual(picked.get("accessToken"), mem[0])
        self.assertEqual(picked.get("refreshToken"), mem[1])

    def test_no_mixed_pair(self):
        """AT/RT 不许一半内存一半磁盘（必须来自同一次签发）。"""
        mem = mk_jwt("u1", OLD_IAT), "MEM-RT"
        disk = mk_jwt("u1", NEW_IAT), "DISK-RT"
        prov = self.build([acct("u1", *disk)], [acct("u1", *mem)])
        picked = prov._pick_account()
        self.assertEqual((picked.get("accessToken"), picked.get("refreshToken")),
                         (disk[0], "DISK-RT"))

    def test_disk_without_token_keeps_mem(self):
        """磁盘条目没有 token（占位/空壳）→ 用内存里的。"""
        mem = mk_jwt("u1", OLD_IAT), "MEM-RT"
        prov = self.build([{"userId": "u1", "accessToken": "", "refreshToken": ""}],
                          [acct("u1", *mem)])
        picked = prov._pick_account()
        self.assertEqual(picked.get("accessToken"), mem[0])

    def test_disabled_account_not_picked(self):
        """磁盘上 enabled=false → 不参与轮换（顺带守住既有行为）。"""
        disk = mk_jwt("u1", NEW_IAT), mk_jwt("u1", NEW_IAT, "Offline")
        prov = self.build([{"userId": "u1", "accessToken": disk[0],
                            "refreshToken": disk[1], "enabled": False}],
                          [acct("u1", *disk)])
        self.assertEqual(prov._pick_account(), {})


class WorkBuddyMergeTest(MergeCaseMixin, unittest.TestCase):
    provider_cls = WorkBuddyProvider
    segment = "workbuddy"


class WorkBuddyIntlMergeTest(MergeCaseMixin, unittest.TestCase):
    provider_cls = WorkBuddyIntlProvider
    segment = "workbuddy_intl"


if __name__ == "__main__":
    unittest.main(verbosity=2)
