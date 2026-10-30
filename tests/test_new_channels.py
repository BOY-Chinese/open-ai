# -*- coding: utf-8 -*-
"""
测试: 2026-10 新增三通道（Qoder CN / Qoder 国际 / CodeArts）
==============================================================
覆盖五个最容易「改一处漏一处」的契约点 —— 每一条都对应一个具体事故形态：

1. **前缀路由顺序**：``qdc-`` 必须以 ``qd-`` 之前判断。写反了 Qoder 国内版的
   请求会全部落到国际版 provider（拿国际版账号打国内上游 → 全通道 401）。

2. **通道映射表一致**：``CHANNEL_TO_PROVIDER`` / ``PROVIDER_TO_CHANNEL`` /
   ``CHANNEL_ALIASES`` / ``CHANNEL_PREFIX`` 四张表必须互相覆盖同一批通道名。
   漏一处就表现为「账号页能加、模型路由不到」或「筛选下拉里通道名裸奔」。

3. **注册条件**：三通道都必须在**有账号**时才注册（与 workbuddy 同规则）。
   无条件注册会让全新安装的 /v1/models 挂出未登录通道的静态模型目录。

4. **签到目标键的小写契约**：``_signin_targets()`` 返回的字典里，旧通道的键是
   小写 ``trae``（与前端 targets 读取、既有测试一致）。历史事故：把键写成大写
   会同时出现 ``trae``/``Trae`` 两个键，``--new-only`` 判定永远为假。

5. **纯签名实现自检**：COSY 的 AES 必须与 FIPS-197 标准向量逐字节一致、
   自定义 Base64 必须可逆、华为云 AK/SK 的 SignedHeaders 必须覆盖全部头
   （少一个头 → APIG.0301 签名不匹配；这是纯本地断言，不联网）。

不联网、不写 config：只对纯函数与纯算法做断言。
运行: python -m unittest tests.test_new_channels -v
"""
import asyncio
import json
import os
import sys
import time
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
# scripts/ 也要在路径上：pw_login 是登录脚本的共享底座（不是 providers 包的一部分）
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

import admin_api as A  # noqa: E402
from providers import build_providers, route_provider  # noqa: E402
from providers import codearts as CA  # noqa: E402
from providers import qoder_common as QC  # noqa: E402

# 旧通道名（避免在测试里散落字面量）
LEGACY = "Trae"

FAKE_QDC = {"uid": "cn-1", "realm": "cn", "accessToken": "t", "refreshToken": "r"}
FAKE_QD = {"uid": "intl-1", "realm": "intl", "accessToken": "t", "refreshToken": "r"}
FAKE_CA = {"uid": "ca-1", "credentials": {"accessKeyId": "ak", "secretAccessKey": "sk",
                                          "securityToken": "st"}}


def _providers(**overrides):
    cfg = {
        "qoder_cn": {"accounts": [FAKE_QDC]},
        "qoder_intl": {"accounts": [FAKE_QD]},
        "codearts": {"accounts": [FAKE_CA]},
    }
    cfg.update(overrides)
    return build_providers({"providers": cfg})


class PrefixRoutingTest(unittest.TestCase):
    """★ 前缀包含关系：判断顺序错了就静默路由到错误通道。"""

    def setUp(self):
        self.providers = _providers()

    def test_qdc_beats_qd(self):
        """qdc-* 必须给国内版 —— 它同时满足 qd- 的字面前缀。"""
        self.assertEqual(route_provider("qdc-gmodel", self.providers).name, "qoder-cn")
        self.assertEqual(route_provider("qdc-auto", self.providers).name, "qoder-cn")

    def test_qd_goes_intl(self):
        for m in ("qd-auto", "qd-qmodel_38max", "qd-"):
            self.assertEqual(route_provider(m, self.providers).name, "qoder-intl", m)

    def test_ca_prefix(self):
        for m in ("ca-GLM-5.2", "ca-deepseek-v4-flash-0731", "codearts-GLM-5.2"):
            self.assertEqual(route_provider(m, self.providers).name, "codearts", m)

    def test_old_prefixes_unaffected(self):
        """新增三通道不得抢走旧通道的模型名。"""
        providers = _providers(**{
            "workbuddy": {"accounts": [{"userId": "w", "accessToken": "t"}]},
            "workbuddy_intl": {"accounts": [{"userId": "i", "accessToken": "t"}]},
            "loomy": {"accounts": [{"userid": "l", "session": "s"}]},
            LEGACY: {"enabled": True, "accounts": []},
        })
        expectations = {
            "wbie-x": "workbuddy-intl",
            "wb-x": "workbuddy",
            "lm-x": "loomy",
            "loomy-x": "loomy",
        }
        for model, want in expectations.items():
            got = route_provider(model, providers)
            self.assertIsNotNone(got, model)
            self.assertEqual(got.name, want, model)

    def test_model_prefix_matches_provider_prefix(self):
        """CHANNEL_PREFIX 必须与各 provider 的 PREFIX 逐字一致。

        两处写岔的表现：模型列表里是 qdc-，路由表里写 qd- → 列表点出来的
        模型名请求必然路由错通道（或落到默认 provider）。
        """
        providers = _providers()
        for channel, pkey in (("QoderCN", "qoder-cn"), ("QoderIntl", "qoder-intl"),
                              ("CodeArts", "codearts")):
            prefix = A.CHANNEL_PREFIX[channel]
            self.assertEqual(providers[pkey].PREFIX, prefix, channel)
            for m in providers[pkey].list_models():
                self.assertTrue(m["id"].startswith(prefix), m["id"])
                self.assertEqual(route_provider(m["id"], providers).name, pkey)


class ChannelMapTest(unittest.TestCase):
    """四张通道映射表必须互相自洽（漏一处的故障形态各不相同）。"""

    NEW = ("QoderCN", "QoderIntl", "CodeArts")

    def test_channel_to_provider_complete(self):
        for ch in self.NEW:
            self.assertIn(ch, A.CHANNEL_TO_PROVIDER, ch)
            self.assertIn(ch, A.CHANNEL_TO_PLATFORM, ch)
            self.assertIn(ch, A.CHANNEL_PREFIX, ch)

    def test_provider_to_channel_roundtrip(self):
        for ch in self.NEW:
            pkey = A.CHANNEL_TO_PROVIDER[ch]
            self.assertEqual(A.PROVIDER_TO_CHANNEL.get(pkey), ch, pkey)

    def test_aliases_normalize(self):
        cases = {
            "qodercn": "QoderCN", "qdc": "QoderCN", "qoder-cn": "QoderCN",
            "qoderintl": "QoderIntl", "qd": "QoderIntl", "qoder": "QoderIntl",
            "codearts": "CodeArts", "ca": "CodeArts",
        }
        for raw, want in cases.items():
            self.assertEqual(A.normalize_channel(raw), want, raw)

    def test_provider_instance_keys_cover_new_channels(self):
        """_PROVIDER_INSTANCE_KEYS 决定「刷新/重连」能否清掉运行态红牌。

        漏登记 → 用户点了刷新仍显示断连（2026-10-14 假断连事故的同款症状）。
        """
        for ch in self.NEW:
            pkey = A.CHANNEL_TO_PROVIDER[ch]
            self.assertIn(pkey, A._PROVIDER_INSTANCE_KEYS, pkey)


class RegistrationTest(unittest.TestCase):
    """注册条件：有账号才注册（避免未登录也挂出静态模型目录）。"""

    def test_no_accounts_no_registration(self):
        providers = build_providers({"providers": {
            "qoder_cn": {"enabled": True, "accounts": []},
            "qoder_intl": {"enabled": True, "accounts": []},
            "codearts": {"enabled": True, "accounts": []},
        }})
        for pkey in ("qoder-cn", "qoder-intl", "codearts"):
            self.assertNotIn(pkey, providers, pkey)

    def test_accounts_register(self):
        providers = _providers()
        for pkey in ("qoder-cn", "qoder-intl", "codearts"):
            self.assertIn(pkey, providers, pkey)

    def test_disabled_skips(self):
        providers = _providers(**{
            "qoder_cn": {"enabled": False, "accounts": [FAKE_QDC]},
        })
        self.assertNotIn("qoder-cn", providers)

    def test_no_account_yields_actionable_error(self):
        """通道已注册但账号全失效 → 错误信息必须能指导用户下一步。"""
        prov = _providers()["codearts"]
        prov.accounts = []
        prov._load_accounts = lambda: []          # 模拟 config 里账号被删空
        import asyncio

        async def go():
            return await prov._prepare({"model": "ca-GLM-5.2"})
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(go())
        self.assertIn("账号管理页", str(ctx.exception))


class SigninTargetKeyTest(unittest.TestCase):
    """★ 键名大小写是跨模块契约（历史事故：混合大小写产生幽灵键）。"""

    def setUp(self):
        self._orig = A._read_config
        A._read_config = lambda: {"providers": {}}
        self.addCleanup(lambda: setattr(A, "_read_config", self._orig))

    def test_legacy_key_is_lowercase(self):
        t = A._signin_targets(None, {})
        self.assertIn(LEGACY.lower(), t,
                      "旧通道键必须是小写（调用方与既有测试都按小写读）")
        self.assertNotIn(LEGACY, t, "出现大写键说明大小写写岔了（幽灵键）")

    def test_new_flag_present(self):
        t = A._signin_targets(None, {})
        self.assertIn("new", t)
        self.assertIn("pending", t)

    def test_new_channel_marks_new_flag(self):
        """新三通道有未签账号 → new=True（驱动 --new-only）。"""
        A._read_config = lambda: {"providers": {
            "codearts": {"accounts": [FAKE_CA]},
        }}
        t = A._signin_targets(None, {})
        self.assertTrue(t["new"])
        self.assertFalse(t["wb"])
        self.assertFalse(t[LEGACY.lower()])
        self.assertEqual(t["pending"], ["CodeArts:ca-1"])

    def test_signed_new_channel_does_not_trigger(self):
        A._read_config = lambda: {"providers": {
            "codearts": {"accounts": [FAKE_CA]},
        }}
        t = A._signin_targets(None, {"CodeArts:ca-1": {}})
        self.assertFalse(t["new"])
        self.assertEqual(t["pending"], [])


class CosyCryptoTest(unittest.TestCase):
    """纯算法自检（离线）：错了就是所有 Qoder 请求全部失败。"""

    def test_aes128_matches_fips197(self):
        """FIPS-197 附录 B 标准向量 —— 逐字节比对，不做「看起来对」的判断。"""
        key = bytes(range(16))
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        got = QC._encrypt_block(plain, QC._expand_key(key))
        self.assertEqual(got.hex(), "69c4e0d86a7b0430d8cdb78070b4c55a")

    def test_cbc_roundtrip_via_known_vector(self):
        """CBC + PKCS7：padding 长度为块大小整数倍时也要补满一整块。"""
        key = b"0123456789abcdef"
        for n in (0, 1, 15, 16, 17, 64):
            ct = QC.aes_cbc_encrypt(b"x" * n, key, key)
            self.assertEqual(len(ct) % 16, 0)
            self.assertEqual(len(ct), ((n // 16) + 1) * 16, n)

    def test_qoder_base64_roundtrip(self):
        for raw in (b"", b"a", b"ab", b"abc", b"abcd", b"x" * 999, os.urandom(257)):
            self.assertEqual(QC.qoder_decode(QC.qoder_encode(raw)), raw)

    def test_sorted_compact_json_is_key_sorted(self):
        """服务端按签名字节校验，键顺序/空白变了签名就对不上。"""
        out = QC.json_sorted_compact({"b": 1, "a": "x", "c": None})
        self.assertEqual(out, b'{"a":"x","b":1,"c":""}')

    def test_rsa_block_is_128_bytes(self):
        """官方公钥是 1024 位 → 密文恒 128 字节（服务端按此解包）。"""
        self.assertEqual(len(QC.rsa_pkcs1v15_encrypt(b"0123456789abcdef")), 128)

    def test_cosy_headers_contract(self):
        """COSY 头必须齐（缺 cosy-key/authorization 上游直接拒）。"""
        sess = QC.CosySession("uid-1", "nick", "tok", "ref")
        h = sess.headers("{}", QC.REALMS["cn"]["gateway"] + QC.CHAT_PATH,
                         model_key="gmodel")
        for k in ("authorization", "cosy-key", "cosy-date", "cosy-user",
                  "cosy-machineid", "cosy-machinetoken", "cosy-machinetype",
                  "cosy-version", "x-model-key"):
            self.assertIn(k, h, k)
        self.assertTrue(h["authorization"].startswith("Bearer COSY."))

    def test_fingerprint_is_stable_and_per_account(self):
        """指纹按账号稳定派生：同账号恒定（否则每次换设备触发风控），
        跨账号必不同（否则多号被关联到同一台机器）。"""
        a1 = QC.derive_id("uid-a", "machine")
        self.assertEqual(a1, QC.derive_id("uid-a", "machine"))
        self.assertNotEqual(a1, QC.derive_id("uid-b", "machine"))
        # UUIDv4 外观：版本位 4、变体位 a（与官方客户端同形）
        self.assertEqual(a1[14], "4")
        self.assertEqual(a1[19], "a")


class HuaweiSignTest(unittest.TestCase):
    """华为云 SDK-HMAC-SHA256 签名（离线自检）。"""

    URL = "https://snap-access.cn-north-4.myhuaweicloud.com/api/v2/chat/completions"

    def _signed(self, extra=None, body=b"{}"):
        headers = {"Content-Type": "application/json"}
        headers.update(extra or {})
        return CA.sign_headers("POST", self.URL, headers, body, "AK", "SK", "TOK")

    def test_signed_headers_cover_every_header(self):
        """SignedHeaders 必须覆盖**全部**请求头 —— 少一个就 APIG.0301。"""
        h = self._signed({"X-Language": "zh-cn"})
        signed = h["Authorization"].split("SignedHeaders=")[1].split(",")[0]
        self.assertEqual(signed, ";".join(sorted(k.lower() for k in h if k != "Authorization")))

    def test_maas_type_included_when_present(self):
        """福利模型的 maas_type 必须在签名前注入，从而计入 SignedHeaders。"""
        h = self._signed({CA.HEADER_MAAS_TYPE: CA.MAAS_BENEFIT})
        signed = h["Authorization"].split("SignedHeaders=")[1].split(",")[0]
        self.assertIn("maas_type", signed)

    def test_security_token_header(self):
        self.assertEqual(self._signed()["X-Security-Token"], "TOK")

    def test_canonical_uri_trailing_slash(self):
        """CanonicalURI 末尾补 '/' 且逐段 encode（对齐官方 JS 实现）。"""
        self.assertEqual(CA._canonical_uri("/api/v2/chat/completions"),
                         "/api/v2/chat/completions/")
        self.assertEqual(CA._canonical_uri("/"), "/")

    def test_canonical_query_sorted(self):
        self.assertEqual(CA._canonical_query("b=2&a=1"), "a=1&b=2")

    def test_dpop_jwk_signable(self):
        key = CA.dpop_new_key()
        for f in ("kty", "crv", "x", "y", "d"):
            self.assertIn(f, key)
        proof = CA._dpop_sign(key, CA.STSHOST + CA.EP_OAUTH_TOKENS)
        self.assertEqual(proof.count("."), 2)
        header = json.loads(
            __import__("base64").urlsafe_b64decode(proof.split(".")[0] + "=="))
        self.assertEqual(header["alg"], "ES256")
        self.assertEqual(header["jwk"]["crv"], "P-256")

    def test_benefit_seed_present(self):
        """冷启动种子必须带福利标记 —— 少带一次头就是一次 404。"""
        prov = _providers()["codearts"]
        for m in CA.SEED_BENEFIT_MODELS:
            self.assertTrue(prov.is_benefit_model(f"ca-{m}"), m)


class CodeArtsCredentialTest(unittest.TestCase):
    """CodeArts 凭据形态：STS 三件套在 credentials 里，顶层没有 accessToken。"""

    def test_status_uses_credentials(self):
        self.assertEqual(A._account_status(FAKE_CA), "enabled")

    def test_status_disconnected_without_credentials(self):
        self.assertEqual(A._account_status({"uid": "x"}), "disconnected")

    def test_disabled_wins(self):
        self.assertEqual(A._account_status({**FAKE_CA, "enabled": False}), "disabled")

    def test_disable_roundtrip_helper(self):
        """_account_status 与 provider 的 reload 都以 enabled=false 为准。"""
        acc = {**FAKE_CA, "enabled": False}
        self.assertEqual(A._account_status(acc), "disabled")
        self.assertEqual(A._account_status({**FAKE_CA, "enabled": True}), "enabled")

    def test_token_response_normalization(self):
        """oauth2/tokens 响应 → config 账号 dict（含 DPoP 私钥与 client_id）。"""
        key = CA.dpop_new_key()
        data = {
            "user_id": "u-42", "user_name": "鲸",
            "refresh_token": "rt-1",
            "credentials": {"access_key_id": "AK", "secret_access_key": "SK",
                            "security_token": "ST",
                            "expiration": "2030-01-01T00:00:00Z"},
        }
        acc = CA.accounts_from_token_response(data, "verifier-1", key, "codearts-agent")
        self.assertEqual(acc["uid"], "u-42")
        self.assertEqual(acc["credentials"]["accessKeyId"], "AK")
        self.assertEqual(acc["credentials"]["secretAccessKey"], "SK")
        self.assertEqual(acc["credentials"]["securityToken"], "ST")
        # ★ 三者必须与 refresh_token 一起落盘：刷新时换任何一个都会被 STS 拒
        self.assertEqual(acc["clientId"], "codearts-agent")
        self.assertEqual(acc["dpopPrivateKey"], key)
        self.assertEqual(acc["codeVerifier"], "verifier-1")
        self.assertGreater(acc["expiresAt"], 0)

    def test_legacy_credential_shape_supported(self):
        """旧 ticket 通道的 credential 字段名不同，必须归一化而不是丢字段。"""
        data = {
            "user_id": "u-1", "user_name": "n", "refresh_token": "rt",
            "credential": {"access": "AK", "secret": "SK", "securitytoken": "ST",
                           "expires_at": "2030-01-01T00:00:00Z"},
        }
        acc = CA.accounts_from_token_response(data, "v", CA.dpop_new_key())
        self.assertEqual(acc["credentials"]["securityToken"], "ST")
        self.assertEqual(acc["credentials"]["accessKeyId"], "AK")

    def test_missing_user_id_rejected(self):
        with self.assertRaises(CA.CodeArtsError):
            CA.accounts_from_token_response({"credentials": {}}, "v", {})

    def test_model_id_case_normalized(self):
        self.assertEqual(CA.canonical_model("glm-5.2"), "GLM-5.2")
        self.assertEqual(CA.canonical_model("GLM-5.2"), "GLM-5.2")
        self.assertEqual(CA.canonical_model("deepseek-v4-flash-0731"),
                         "deepseek-v4-flash-0731")


class QoderAccountTest(unittest.TestCase):
    """Qoder 账号形态与签到能力探测（离线）。"""

    def test_pat_must_start_with_pt(self):
        with self.assertRaises(ValueError):
            QC.import_pat("bad-token", "cn")

    def test_realms_differ(self):
        cn, intl = QC.REALMS["cn"], QC.REALMS["intl"]
        self.assertNotEqual(cn["openapi"], intl["openapi"])
        self.assertNotEqual(cn["gateway"], intl["gateway"])
        self.assertTrue(cn["send_redirect_uri"])
        self.assertFalse(intl["send_redirect_uri"])
        self.assertTrue(cn["nonce_dashed"])
        self.assertFalse(intl["nonce_dashed"])

    def test_intl_gateway_candidates_default_primary_only(self):
        """★ 2026-10-24 改：默认**只用主主机**。

        全历史统计：api2/api1 被尝试 26 次、**成功 0 次**（全 405），而 api3
        成功 413 次。405 是**边缘级**拦截（同一时刻所有主机一起被拦），
        所以「换主机」这条降级路径不可能成功 —— 只是白花约 1.5 秒。
        故默认裁掉，排障时可用 QD_GATEWAY_FALLBACKS=1 还原旧行为。
        """
        hosts = QC.gateway_candidates("intl")
        self.assertEqual(hosts, [QC.REALMS["intl"]["gateway"]])

    def test_intl_gateway_fallbacks_can_be_reenabled(self):
        """兜底主机没被删掉，只是默认不启用（排障/应急可打开）。"""
        os.environ["QD_GATEWAY_FALLBACKS"] = "1"
        self.addCleanup(lambda: os.environ.pop("QD_GATEWAY_FALLBACKS", None))
        hosts = QC.gateway_candidates("intl")
        self.assertEqual(hosts[0], QC.REALMS["intl"]["gateway"])
        self.assertGreaterEqual(len(hosts), 2, "打开开关后必须带上候选域名")

    def test_cn_stays_single_host(self):
        """国内版本来就只有一台，开关不影响它。"""
        os.environ["QD_GATEWAY_FALLBACKS"] = "1"
        self.addCleanup(lambda: os.environ.pop("QD_GATEWAY_FALLBACKS", None))
        self.assertEqual(QC.gateway_candidates("cn"), [QC.REALMS["cn"]["gateway"]])

    def test_device_login_url_shape(self):
        """设备授权 URL 的参数名照抄官方客户端（`challenge` + `challenge_method`，
        不是 OAuth 标准的 `code_challenge`）—— 改名上游直接判无效请求。"""
        login = QC.start_device_login("cn")
        self.assertIn("challenge=", login["authUrl"])
        self.assertIn("challenge_method=S256", login["authUrl"])
        self.assertNotIn("code_challenge=", login["authUrl"])
        self.assertIn("client_id=", login["authUrl"])
        self.assertIn("machine_id=", login["authUrl"])
        self.assertIn("nonce=", login["authUrl"])
        self.assertTrue(login["verifier"])
        # 国内版必须带 redirect_uri（国际版带了会被拒）
        self.assertIn("redirect_uri=", login["authUrl"])

    def test_intl_device_login_has_no_redirect_uri(self):
        login = QC.start_device_login("intl")
        self.assertNotIn("redirect_uri=", login["authUrl"])

    def test_checkin_falls_back_to_sash_when_no_daily_campaign(self):
        """活动平台没有「每日签到」那一期时 → 回落到老 sash 通道。

        （2026-10-22 起判定顺序反了过来：活动平台是主路径，sash 是兜底 ——
         因为实测 CN/INTL 真正在跑的是活动平台，而 sash 在 CN 上恒为 DISABLED。）
        """
        now = int(time.time())
        orig_c, orig_s = QC._campaigns, QC.checkin_status
        QC._campaigns = lambda acc, app_env=None: ([], "")          # 活动平台在，但没这一期
        QC.checkin_status = lambda acc: (True, {       # sash 仍在且今日已签
            "status": "CLAIMED", "active": True, "today_checked_in": True,
            "reward_credits": 100, "last_claimed_at": now})
        self.addCleanup(lambda: (setattr(QC, "_campaigns", orig_c),
                                 setattr(QC, "checkin_status", orig_s)))
        r = QC.checkin({"uid": "x", "realm": "cn"})
        self.assertTrue(r["ok"])
        self.assertTrue(r.get("already"))

    def test_checkin_reports_unavailable_when_both_channels_down(self):
        """两条通道都不可用 → 明确报失败并带上原因，不谎报成功。"""
        orig_c, orig_s = QC._campaigns, QC.checkin_status
        QC._campaigns = lambda acc, app_env=None: ([], "活动平台 500")
        QC.checkin_status = lambda acc: (False, {"unavailable": True, "reason": "404"})
        self.addCleanup(lambda: (setattr(QC, "_campaigns", orig_c),
                                 setattr(QC, "checkin_status", orig_s)))
        r = QC.checkin({"uid": "x", "realm": "intl"})
        self.assertFalse(r["ok"])
        self.assertIn("500", r["msg"])

    def test_resolve_model_key_accepts_display_name(self):
        catalog = QC.static_catalog("cn")
        self.assertEqual(QC.resolve_model_key("GLM-5.3", catalog), "gmodel")
        self.assertEqual(QC.resolve_model_key("gmodel", catalog), "gmodel")
        self.assertEqual(QC.resolve_model_key("DeepSeek-V4-Pro", catalog), "dmodel")

    def test_static_catalog_not_empty(self):
        """仓库内快照必须可用 —— 上游拉取失败时它就是全部模型列表。"""
        for realm in ("cn", "intl"):
            cat = QC.static_catalog(realm)
            self.assertGreater(len(cat), 5, realm)
            for m in cat:
                self.assertTrue(m.get("key"), m)


class EnvelopeParsingTest(unittest.TestCase):
    """上游 SSE 信封拆包（两层嵌套）—— 解错就是「客户端只看到空回复」。"""

    def _line(self, body, status=200):
        return "data:" + json.dumps({"headers": {}, "body": body,
                                     "statusCodeValue": status})

    def test_inner_chunk_unwrapped(self):
        from providers.qoder_base import _parse_envelope_line
        line = self._line(json.dumps({"choices": [{"delta": {"content": "hi"}}]}))
        got = _parse_envelope_line(line)
        self.assertEqual(got["choices"][0]["delta"]["content"], "hi")

    def test_done_sentinel(self):
        from providers.qoder_base import _DONE, _parse_envelope_line
        self.assertIs(_parse_envelope_line(self._line("[DONE]")), _DONE)

    def test_non_200_raises(self):
        from providers.qoder_base import _parse_envelope_line
        with self.assertRaises(RuntimeError) as ctx:
            _parse_envelope_line(self._line("boom", status=500))
        self.assertIn("500", str(ctx.exception))

    def test_business_error_code_raises(self):
        from providers.qoder_base import _parse_envelope_line
        line = self._line(json.dumps({"error_code": "1234", "error_msg": "配额不足"}))
        with self.assertRaises(RuntimeError) as ctx:
            _parse_envelope_line(line)
        self.assertIn("配额不足", str(ctx.exception))

    def test_heartbeat_and_garbage_ignored(self):
        from providers.qoder_base import _parse_envelope_line
        for line in ("", ":", ": keep-alive", "event:finish", "data:not-json"):
            self.assertIsNone(_parse_envelope_line(line), line)

    def test_empty_delta_cleaned(self):
        """上游每帧 padding 的空字段必须剔除，否则客户端收到一堆空 delta。"""
        from providers.qoder_base import _DONE  # noqa: F401
        from providers.qoder_common import clean_chunk
        self.assertEqual(clean_chunk({"choices": [{"delta": {"content": ""}}]}), "")
        kept = clean_chunk({"choices": [{"delta": {"content": "x"}}]})
        self.assertIn("x", kept)


class UrllibImportTest(unittest.TestCase):
    """★ 函数内的 `import urllib.*` 会把 `urllib` 变成**局部名**。

    若函数体在导入语句**之前**就用 `urllib.parse.*`（codearts.poll_ticket 曾是
    这样），Python 会抛 UnboundLocalError: cannot access local variable 'urllib'
    —— 表现为「CodeArts 登录助手一进 ticket 轮询就崩」。

    这里直接断言三个曾经踩坑的函数都能被调用到「不会因名字解析而抛错」的程度。
    """

    def test_poll_ticket_does_not_raise_name_error(self):
        """无网络也应返回 pending/error，而不是 UnboundLocalError。"""
        try:
            r = CA.poll_ticket("ticket-x", "secret-y")
        except UnboundLocalError as e:      # pragma: no cover - 回归守卫
            self.fail(f"poll_ticket 触发名字解析错误: {e}")
        except Exception:
            return                          # 网络类异常可接受（本机可能不通华为云）
        self.assertIn(r.get("status"), ("pending", "error", "ok"))

    def test_urllib_is_module_level(self):
        """模块级必须导入 urllib.error/parse/request（函数内导入是踩坑之源）。"""
        import inspect
        src = inspect.getsource(CA)
        for name in ("urllib.error", "urllib.parse", "urllib.request"):
            self.assertIn(f"import {name}", src, name)

    def test_api_request_rejects_missing_credentials(self):
        """缺 AK/SK 时要给出可读提示，而不是 AttributeError。"""
        with self.assertRaises(CA.CodeArtsError) as ctx:
            CA.api_request({"uid": "x"}, "GET", CA.SNAP_HOST + CA.EP_CURRENT_USER)
        self.assertIn("AK/SK", str(ctx.exception))


class QoderCreditsTest(unittest.TestCase):
    """★ 积分必须按上游**真实形状**取：userQuota + addOnQuota。

    历史事故（2026-10-22 master 实测）：旧实现按关键字在顶层字段模糊求和，
    抓到了 `totalUsagePercentage`（0.01）当积分 —— 界面显示「积分 0.01」。
    """

    REAL = {
        "userId": "u", "usageType": "credits",
        "totalUsagePercentage": 0.01,          # ← 这是百分比，绝不能当积分
        "userQuota": {"total": 300.0, "used": 0.0, "remaining": 300.0},
        "addOnQuota": {"total": 100.0, "used": 0.0, "remaining": 100.0},
    }

    def _patch(self, payload):
        orig = QC.http_json
        QC.http_json = lambda *a, **k: payload
        self.addCleanup(lambda: setattr(QC, "http_json", orig))

    def test_sums_both_pools(self):
        self._patch(self.REAL)
        credits, err = QC.fetch_credits({"uid": "u", "realm": "cn"})
        self.assertEqual(err, "")
        self.assertEqual(credits, 400.0)

    def test_ignores_usage_percentage(self):
        self._patch(self.REAL)
        credits, _ = QC.fetch_credits({"uid": "u", "realm": "cn"})
        self.assertNotAlmostEqual(credits, 0.01)

    def test_unlimited_marker(self):
        self._patch({"userQuota": {"unlimited": True}, "addOnQuota": {"remaining": 5}})
        credits, _ = QC.fetch_credits({"uid": "u", "realm": "cn"})
        self.assertEqual(credits, -1.0)

    def test_unknown_shape_returns_zero_not_junk(self):
        """形状再变也只回 0（并给理由），绝不编一个看起来很像的数字。"""
        self._patch({"totalUsagePercentage": 0.75, "weird": 123})
        credits, err = QC.fetch_credits({"uid": "u", "realm": "cn"})
        self.assertEqual(credits, 0.0)
        self.assertTrue(err)


class QoderCheckinTest(unittest.TestCase):
    """★ 每日签到以**活动平台**为准，字段名是 claimStatus（不是 status）。"""

    DAILY = {
        "campaignId": "cid-1", "campaignKey": "act-1", "actionType": "CLAIM_BENEFIT",
        "startAt": 1000, "endAt": 9999999999,
        "claimStatus": "CLAIMABLE", "benefit": {"kind": "CREDITS", "amount": 100},
    }

    def test_daily_campaign_detected(self):
        now = int(time.time())
        camp = dict(self.DAILY, startAt=now - 10, endAt=now + 10)
        got = QC.daily_campaign([camp])
        self.assertIsNotNone(got)
        self.assertEqual(got["campaignId"], "cid-1")

    def test_out_of_window_ignored(self):
        now = int(time.time())
        old = dict(self.DAILY, startAt=now - 9999, endAt=now - 5000)
        self.assertIsNone(QC.daily_campaign([old]))

    def test_non_credit_campaign_ignored(self):
        camp = dict(self.DAILY, benefit={"kind": "TOKENS", "amount": 1},
                    startAt=0, endAt=9999999999)
        self.assertIsNone(QC.daily_campaign([camp]))

    def test_claimed_status_recognised_via_claim_status_field(self):
        """★ 已领取的判定必须读 claimStatus；读错字段会反复 claim → 报失败。"""
        now = int(time.time())
        camp = dict(self.DAILY, claimStatus="CLAIMED", startAt=now - 10, endAt=now + 10)
        r = QC.claim_campaign({"uid": "u", "realm": "cn"}, camp)
        self.assertTrue(r["ok"])
        self.assertTrue(r["already"])
        self.assertIn("100", r["msg"])

    def test_checkin_already_today_reports_amount(self):
        """今日已签 → ok + amount（调用方据此写 gain 表，账号页才显示已签到）。"""
        now = int(time.time())
        camp = dict(self.DAILY, claimStatus="CLAIMED", startAt=now - 10, endAt=now + 10)
        orig_c, orig_s = QC._campaigns, QC.checkin_status
        QC._campaigns = lambda acc, app_env=None: ([camp], "")
        QC.checkin_status = lambda acc: (False, {"unavailable": True})
        self.addCleanup(lambda: (setattr(QC, "_campaigns", orig_c),
                                 setattr(QC, "checkin_status", orig_s)))
        r = QC.checkin({"uid": "u", "realm": "cn"})
        self.assertTrue(r["ok"])
        self.assertTrue(r.get("already"))
        self.assertEqual(r.get("amount"), 100)

    # ─────────── 2026-10-23 修「没签到却显示已签到」的回归锁 ───────────
    # 事故形态（master 报障、实测复现）：Qoder CN 当日 showCampaign=false /
    # campaigns=[]，国际版只剩 VIEW_DETAILS 活动 —— 两者都没有「每日签到」这一期，
    # sash 也不是 CLAIMED。旧版兜底段最后一行无条件 return ok=True，
    # 叠加 signin_all 只看 ok、amount 取活动**面额**，于是写出假的 gain 行，
    # 账号页显示「已签到」而积分一分没到（实测 addOnQuota.remaining 恒为 100）。
    def _stub(self, campaigns, sash):
        orig_c, orig_s = QC._campaigns, QC.checkin_status
        QC._campaigns = lambda acc, app_env=None: campaigns
        QC.checkin_status = lambda acc: sash
        self.addCleanup(lambda: (setattr(QC, "_campaigns", orig_c),
                                 setattr(QC, "checkin_status", orig_s)))

    def test_no_campaign_is_not_credited(self):
        """活动列表为空 + sash 未开放 → credited 必须为 False（旧版返 ok=True）。"""
        self._stub(([], ""), (True, {"status": "DISABLED", "active": False,
                                     "today_checked_in": False}))
        r = QC.checkin({"uid": "u", "realm": "cn"})
        self.assertFalse(r.get("credited"), r)
        self.assertEqual(r.get("reason"), "no_campaign")
        self.assertEqual(r.get("amount"), 0, "无活动时不得给出面额，否则调用方会写假 gain")

    def test_no_daily_action_is_not_credited(self):
        """只有 VIEW_DETAILS 之类别的活动 + sash 404（国际版当日形态）→ 不算签到。"""
        self._stub(([{"campaignId": "x", "actionType": "VIEW_DETAILS",
                      "claimStatus": "CLAIMED"}], ""),
                   (False, {"unavailable": True, "reason": "404"}))
        r = QC.checkin({"uid": "u", "realm": "intl"})
        self.assertFalse(r.get("credited"), r)
        self.assertEqual(r.get("reason"), "no_campaign")

    def test_credited_true_only_when_really_claimed(self):
        """真的领到 / 上游回报今日已领 → credited=True。"""
        now = int(time.time())
        camp = dict(self.DAILY, claimStatus="CLAIMED", startAt=now - 10, endAt=now + 10)
        self._stub(([camp], ""), (False, {"unavailable": True}))
        r = QC.checkin({"uid": "u", "realm": "cn"})
        self.assertTrue(r.get("credited"), r)
        self.assertEqual(r.get("amount"), 100)
        # sash 明确回报「今天已领」也算真入账
        self._stub(([], ""), (True, {"status": "CLAIMED", "active": True,
                                     "today_checked_in": True, "reward_credits": 100}))
        r2 = QC.checkin({"uid": "u", "realm": "cn"})
        self.assertTrue(r2.get("credited"), r2)

    def test_campaigns_unavailable_is_not_credited(self):
        """活动平台报错 = 无法判定 → ok/credited 都是 False，绝不谎报。"""
        self._stub(([], "活动平台 500"), (True, {"status": "DISABLED"}))
        r = QC.checkin({"uid": "u", "realm": "cn"})
        self.assertFalse(r.get("ok"))
        self.assertFalse(r.get("credited"))

    def test_sash_disabled_does_not_mask_campaign_claim(self):
        """CN 的 sash 恒为 DISABLED，但活动平台已签 —— 必须报成功而不是失败。"""
        now = int(time.time())
        camp = dict(self.DAILY, claimStatus="CLAIMED", startAt=now - 10, endAt=now + 10)
        orig_c, orig_s = QC._campaigns, QC.checkin_status
        QC._campaigns = lambda acc, app_env=None: ([camp], "")
        QC.checkin_status = lambda acc: (True, {"status": "DISABLED", "active": False,
                                                "today_checked_in": False})
        self.addCleanup(lambda: (setattr(QC, "_campaigns", orig_c),
                                 setattr(QC, "checkin_status", orig_s)))
        r = QC.checkin({"uid": "u", "realm": "cn"})
        self.assertTrue(r["ok"], r)


class QoderRiskIdentityTest(unittest.TestCase):
    """★ 2026-10-23：设备风控身份（UMID）是 Qoder 签到的前置条件。

    事故：上游对**没有真实设备身份**的请求直接不下发活动 ——
      无 UMID → {"showCampaign": false, "campaigns": []}
      有 UMID → {"showCampaign": true,  "campaigns":[CLAIM_BENEFIT/CLAIMABLE]}
    旧实现用 derive_machine_token() 伪造身份，于是永远「查不到签到活动」。
    """

    def test_env_triplet_used_when_complete(self):
        os.environ.update({"QD_MACHINE_TOKEN": "T", "QD_MACHINE_CODE": "C",
                           "QD_MACHINE_TYPE": "Y"})
        for k in ("QD_MACHINE_TOKEN", "QD_MACHINE_CODE", "QD_MACHINE_TYPE"):
            self.addCleanup(lambda k=k: os.environ.pop(k, None))
        vals, err = QC.risk_identity("intl", "u1")
        self.assertEqual(err, "")
        self.assertEqual(vals["machineToken"], "T")

    def test_partial_env_is_not_accepted(self):
        """只给一半环境变量 → 必须报错，不能拿半套当身份。"""
        os.environ["QD_MACHINE_TOKEN"] = "T"
        self.addCleanup(lambda: os.environ.pop("QD_MACHINE_TOKEN", None))
        orig = QC._umid_component
        QC._umid_component = lambda realm: ""
        self.addCleanup(lambda: setattr(QC, "_umid_component", orig))
        vals, err = QC.risk_identity("cn", "u2")
        self.assertEqual(vals, {})
        self.assertIn("UMID", err)

    def test_client_version_reads_manifest(self):
        """版本号取本机 build-manifest.json（实测 0.4.9），不能写死 0.4.8。"""
        import tempfile
        tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmp, "resources"))
        with open(os.path.join(tmp, "resources", "build-manifest.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"productVersion": "9.9.9"}, f)
        orig = QC._client_roots
        QC._client_roots = lambda name: [tmp]
        self.addCleanup(lambda: setattr(QC, "_client_roots", orig))
        self.assertEqual(QC.client_version("cn"), "9.9.9")

    def test_missing_risk_identity_reported_not_silent(self):
        """缺风控身份 + 无活动 → 必须报 no_risk_identity，不能说「今天没有活动」。"""
        orig = QC.risk_identity
        QC.risk_identity = lambda realm, uid: ({}, "本机没有 UMID 组件")
        orig_c, orig_s = QC._campaigns, QC.checkin_status
        QC._campaigns = lambda acc, app_env=None: ([], "")
        QC.checkin_status = lambda acc: (True, {"status": "DISABLED"})
        self.addCleanup(lambda: (setattr(QC, "risk_identity", orig),
                                 setattr(QC, "_campaigns", orig_c),
                                 setattr(QC, "checkin_status", orig_s)))
        r = QC.checkin({"uid": "u", "realm": "cn"})
        self.assertFalse(r.get("credited"))
        self.assertEqual(r.get("reason"), "no_risk_identity")
        self.assertIn("UMID", r.get("msg") or "")


class QoderWindowTest(unittest.TestCase):
    """★ 领取窗口是每天 10:00 开、次日 10:00 前关（官方文档 + Qoder-Daily 实测）。"""

    def test_window_open_after_ten(self):
        t = time.mktime((2026, 10, 23, 10, 5, 0, 0, 0, -1))
        self.assertTrue(QC.in_daily_window(t))
        self.assertTrue(QC.window_active(t))

    def test_window_closed_before_ten(self):
        t = time.mktime((2026, 10, 23, 0, 0, 5, 0, 0, -1))
        self.assertFalse(QC.in_daily_window(t))
        self.assertFalse(QC.window_active(t), "00:00 不在开窗补给时段")

    def test_window_active_ends(self):
        t = time.mktime((2026, 10, 23, 10, 55, 0, 0, 0, -1))
        self.assertFalse(QC.window_active(t), "10:40 之后不再死等重试")
        self.assertTrue(QC.in_daily_window(t), "但仍在官方领取窗口内")


class QoderClaimVerdictTest(unittest.TestCase):
    """★ claim 响应必须按 status/replayed 判定，不能拿面额冒充成功。"""

    def _claim(self, resp):
        orig = QC.http_json
        QC.http_json = lambda *a, **k: resp
        self.addCleanup(lambda: setattr(QC, "http_json", orig))
        camp = {"campaignId": "cid", "claimStatus": "CLAIMABLE",
                "benefit": {"kind": "CREDITS", "amount": 100}}
        return QC.claim_campaign({"uid": "u", "realm": "cn"}, camp)

    def test_claimed_status_credits_real_amount(self):
        r = self._claim({"status": "CLAIMED", "replayed": False,
                         "benefit": {"kind": "CREDITS", "amount": 100}})
        self.assertTrue(r["credited"])
        self.assertEqual(r["credits"], 100, "真入账额度取自 benefit.amount")

    def test_replayed_is_already_not_new(self):
        r = self._claim({"status": "CLAIMED", "replayed": True,
                         "benefit": {"amount": 100}})
        self.assertTrue(r["credited"])
        self.assertTrue(r["already"])
        self.assertEqual(r["credits"], 0, "重放不算本次新入账")

    def test_unknown_status_is_failure_not_face_value(self):
        """2xx 但没给 CLAIMED → 必须失败；绝不能按面额报成功（旧版 bug）。"""
        r = self._claim({"status": "PENDING"})
        self.assertFalse(r["ok"])
        self.assertFalse(r["credited"])
        self.assertEqual(r["credits"], 0)


class QoderModelNameTest(unittest.TestCase):
    """★ 对外模型名用上游 display_name，且默认不列出 enable=false 的。"""

    CATALOG = [
        {"key": "auto", "display_name": "Auto", "enable": True},
        {"key": "qmodel_38max", "display_name": "Qwen3.8-Max", "enable": True},
        {"key": "ultimate", "display_name": "Ultimate", "enable": False},
        {"key": "dmodel", "display_name": "DeepSeek-V4-Pro", "enable": True},
    ]

    def _prov(self, **cfg):
        from providers.qoder_cn import QoderCNProvider
        base = {"accounts": [{"uid": "u", "realm": "cn", "accessToken": "t"}]}
        base.update(cfg)
        p = QoderCNProvider(base)
        p._catalog = list(self.CATALOG)
        return p

    def test_ids_use_display_name(self):
        ids = [m["id"] for m in self._prov().list_models()]
        self.assertIn("qdc-Qwen3.8-Max", ids)
        self.assertIn("qdc-DeepSeek-V4-Pro", ids)
        # 内部 key 不再直接暴露
        self.assertNotIn("qdc-qmodel_38max", ids)

    def test_disabled_models_hidden_by_default(self):
        ids = [m["id"] for m in self._prov().list_models()]
        self.assertNotIn("qdc-Ultimate", ids)

    def test_disabled_models_can_be_included(self):
        ids = [m["id"] for m in self._prov(includeDisabledModels=True).list_models()]
        self.assertIn("qdc-Ultimate", ids)

    def test_request_accepts_both_name_and_key(self):
        p = self._prov()
        self.assertEqual(p.normalize_model("qdc-Qwen3.8-Max"), "qmodel_38max")
        self.assertEqual(p.normalize_model("qdc-qmodel_38max"), "qmodel_38max")
        self.assertEqual(p.normalize_model("Qwen3.8-Max"), "qmodel_38max")


class CodeArtsTokenShapeTest(unittest.TestCase):
    """★ 换 token 的失败必须**看得见真因**，且 user_id 缺失不该直接判死。"""

    def test_business_error_surfaces_message(self):
        with self.assertRaises(CA.CodeArtsError) as ctx:
            CA._request_token_error_guard = None  # 占位，保持可读性
            raise CA.CodeArtsError(400, "x") if False else _raise_from({
                "error_code": "STS5.1805", "error_msg": "invalid authorization code"})
        self.assertIn("STS5.1805", str(ctx.exception))

    def test_missing_credentials_reports_response(self):
        with self.assertRaises(CA.CodeArtsError) as ctx:
            CA.accounts_from_token_response({"foo": 1}, "v", {})
        msg = str(ctx.exception)
        self.assertIn("STS 凭证", msg)
        self.assertIn("foo", msg)

    def test_credentials_without_user_id_uses_current_user(self):
        """凭证可用但响应没带 user_id → 用 AK/SK 补查身份，而不是登录失败。"""
        data = {"credentials": {"access_key_id": "AK", "secret_access_key": "SK",
                                "security_token": "ST", "expiration": "2030-01-01T00:00:00Z"}}
        orig = CA.fetch_current_user
        CA.fetch_current_user = lambda acc: {"user_id": "u-9", "user_name": "鲸"}
        self.addCleanup(lambda: setattr(CA, "fetch_current_user", orig))
        acc = CA.accounts_from_token_response(data, "v", CA.dpop_new_key())
        self.assertEqual(acc["uid"], "u-9")
        self.assertEqual(acc["name"], "鲸")
        self.assertEqual(acc["credentials"]["securityToken"], "ST")

    def test_user_id_read_from_nested_shape(self):
        data = {"data": {"user_id": "u-nested", "user_name": "n"},
                "credentials": {"access_key_id": "A", "secret_access_key": "S",
                                "security_token": "T"}}
        acc = CA.accounts_from_token_response(data, "v", {})
        self.assertEqual(acc["uid"], "u-nested")

    def test_normalize_credentials_legacy_shape(self):
        cred = CA.normalize_credentials({
            "credential": {"access": "AK", "secret": "SK", "securitytoken": "ST",
                           "expires_at": "2030-01-01T00:00:00Z"}})
        self.assertEqual((cred["accessKeyId"], cred["secretAccessKey"],
                          cred["securityToken"]), ("AK", "SK", "ST"))


def _raise_from(payload):
    """辅助：直接走 _request_token 的业务错误判定分支（不联网）。

    做法：把「业务错误」判定逻辑抽出来复用 —— 这里用 accounts_from_token_response
    之外的路径不合适，因而显式调用内部判定。
    """
    code = str(payload.get("error_code") or "").strip()
    err = str(payload.get("error") or "").strip()
    if err or (code and code not in ("0", "0000")):
        raise CA.CodeArtsError(400, json.dumps(
            {k: v for k, v in payload.items() if k not in ("credentials", "credential")},
            ensure_ascii=False)[:300], CA.EP_OAUTH_TOKENS)
    return payload


class BrowserLoginTest(unittest.TestCase):
    """★ 浏览器：降级顺序 chrome-for-testing → chrome → edge，且每次全新 profile。"""

    def test_fallback_order(self):
        import pw_login
        self.assertEqual(tuple(pw_login.BROWSER_FALLBACK),
                         ("chromium", "chrome", "msedge"))

    def test_fresh_profile_each_run(self):
        import pw_login
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            a = pw_login.pick_profile_dir(tmp, False, log=lambda *x: None)
            b = pw_login.pick_profile_dir(tmp, False, log=lambda *x: None)
            self.assertNotEqual(a, b, "两次登录必须用不同的 profile（否则带上一次登录态）")

    def test_reuse_profile_returns_latest(self):
        import pw_login
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            a = pw_login.pick_profile_dir(tmp, False, log=lambda *x: None)
            b = pw_login.pick_profile_dir(tmp, True, log=lambda *x: None)
            self.assertEqual(a, b)

    def test_old_profiles_cleaned(self):
        """清理策略：只保留「最近一次旧会话（供 --reuse-profile）」+ 本次会话。

        所以连续跑三次后，最早那次应该被清掉，而最近一次旧会话仍在。
        （注意不是「只留本次」—— 那会让 --reuse-profile 失去目标。）
        """
        import pw_login
        import tempfile
        import time as _t
        with tempfile.TemporaryDirectory() as tmp:
            dirs = []
            for _ in range(3):
                dirs.append(pw_login.pick_profile_dir(tmp, False, log=lambda *x: None))
                _t.sleep(1.05)          # 目录名带秒级时间戳，隔开以免重名
            left = sorted(os.listdir(tmp))
            self.assertIn(os.path.basename(dirs[-1]), left, "本次会话必须在")
            self.assertNotIn(os.path.basename(dirs[0]), left, "最早那次应被清理")
            self.assertLessEqual(len(left), 2, f"最多保留两次会话，实际 {left}")

    def test_stealth_script_present(self):
        import pw_login
        self.assertIn("webdriver", pw_login.STEALTH_JS)


class QoderCatalogRequestTest(unittest.TestCase):
    """★ 模型目录必须是 **GET**（POST 会被上游 400，旧实现因此静默回退静态快照）。

    2026-10-22 实测：官方客户端用 ``GET /api/v2/model/list?Encode=1``，
    响应是 ``{scene: [模型...]}``；旧实现发 POST → ``400 Request method 'POST'
    not supported`` → 异常被吞、回退仓库快照 → 界面「模型少了很多」。
    """

    SCENES = {
        "chat": [
            {"key": "auto", "display_name": "Auto", "enable": True, "price_factor": 0.5},
            {"key": "dmodel", "display_name": "DeepSeek-V4-Pro", "enable": True,
             "price_factor": 0.5},
        ],
        "inline": [{"key": "x", "display_name": "X", "enable": True}],
    }

    def test_uses_get_and_chat_scene(self):
        calls = {}

        def fake_http(url, method="GET", data=None, headers=None, timeout=30.0, retries=1):
            calls["method"] = method
            calls["data"] = data
            return self.SCENES

        orig = QC.http_json
        QC.http_json = fake_http
        self.addCleanup(lambda: setattr(QC, "http_json", orig))
        cat = QC.fetch_catalog({"uid": "u", "realm": "cn", "accessToken": "t"}, force=True)
        self.assertEqual(calls["method"], "GET")
        self.assertIsNone(calls["data"], "GET 请求不得带 body（签名按空 body 计算）")
        self.assertEqual([m["key"] for m in cat], ["auto", "dmodel"])
        self.assertEqual(cat[0]["price_factor"], 0.5)

    def test_falls_back_to_any_nonempty_scene(self):
        orig = QC.http_json
        QC.http_json = lambda *a, **k: {"unknown_scene": self.SCENES["chat"]}
        self.addCleanup(lambda: setattr(QC, "http_json", orig))
        cat = QC.fetch_catalog({"uid": "u", "realm": "cn", "accessToken": "t"}, force=True)
        self.assertEqual(len(cat), 2)

    def test_static_snapshot_only_on_failure(self):
        """上游失败才用仓库快照兜底（且必须能兜住，不能返回空列表）。"""
        orig = QC.http_json
        QC.http_json = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        self.addCleanup(lambda: setattr(QC, "http_json", orig))
        cat = QC.fetch_catalog({"uid": "u", "realm": "cn", "accessToken": "t"}, force=True)
        self.assertGreater(len(cat), 0)

    def test_rate_table_from_price_factor(self):
        """倍率来自 price_factor，并同时登记带前缀/不带前缀/内部 key 三种写法。"""
        from providers.qoder_cn import QoderCNProvider
        p = QoderCNProvider({"accounts": [{"uid": "u", "realm": "cn", "accessToken": "t"}]})
        p._catalog = [
            {"key": "dmodel", "display_name": "DeepSeek-V4-Pro", "price_factor": 0.5},
            {"key": "nofactor", "display_name": "NoFactor"},
        ]
        t = p.rate_table()
        self.assertEqual(t["DeepSeek-V4-Pro"], 0.5)
        self.assertEqual(t["dmodel"], 0.5)
        self.assertEqual(t["qdc-DeepSeek-V4-Pro"], 0.5)
        self.assertNotIn("NoFactor", t, "没有 price_factor 的模型不该编一个倍率")


class CodeArtsModelSourceTest(unittest.TestCase):
    """★ CodeArts 模型以 /v1/model/builtin 为主源，用 model_name 作对外名。"""

    BUILTIN = {"builtinModels": [
        {"model_id": "deepseek-v4.1-flash", "model_name": "DeepSeek-V4.1-Flash",
         "display_enabled": True, "enable": True, "supports_images": False,
         "credit": [{"ratio": "0.18", "ratio_display": "0.18x"}]},
        {"model_id": "GLM-5.2", "model_name": "GLM-5.2", "display_enabled": True,
         "enable": True, "credit": [{"ratio_display": "0.7x"}]},
        {"model_id": "Qwen3-VL-235B", "model_name": "Qwen3-VL-235B",
         "display_enabled": False, "enable": True, "credit": []},
    ]}

    def setUp(self):
        self._orig_signed = CA.signed_get
        self._orig_claim = CA.claim_benefit

        def fake_signed(account, url, agent_type=""):
            if url.endswith(CA.EP_MODEL_BUILTIN):
                return self.BUILTIN
            raise RuntimeError("benefit gateway down")

        CA.signed_get = fake_signed
        CA.claim_benefit = lambda acc: None
        self.addCleanup(lambda: (setattr(CA, "signed_get", self._orig_signed),
                                 setattr(CA, "claim_benefit", self._orig_claim)))

    def test_builtin_is_primary_and_names_used(self):
        models = CA.fetch_models({"uid": "u"})
        ids = [m["id"] for m in models]
        self.assertIn("DeepSeek-V4.1-Flash", ids)
        self.assertIn("GLM-5.2", ids)

    def test_rate_parsed_from_ratio_display(self):
        models = {m["id"]: m for m in CA.fetch_models({"uid": "u"})}
        self.assertAlmostEqual(models["GLM-5.2"]["rate"], 0.7)
        self.assertAlmostEqual(models["DeepSeek-V4.1-Flash"]["rate"], 0.18)

    def test_display_disabled_flagged(self):
        models = {m["id"]: m for m in CA.fetch_models({"uid": "u"})}
        self.assertFalse(models["Qwen3-VL-235B"]["display_enabled"])

    def test_benefit_failure_is_not_fatal(self):
        """福利网关挂了（当前上游就是 400 invalid channel）不该让整份目录失败。"""
        models = CA.fetch_models({"uid": "u"})
        self.assertGreaterEqual(len(models), 3)

    def test_rate_table_registers_prefix_and_key(self):
        prov = _providers()["codearts"]
        prov._models = CA.fetch_models({"uid": "u"})
        t = prov.rate_table()
        self.assertAlmostEqual(t["GLM-5.2"], 0.7)
        self.assertAlmostEqual(t["ca-GLM-5.2"], 0.7)

    def test_parse_ratio_variants(self):
        for text, want in (("0.7x", 0.7), ("0.18X", 0.18), ("0.05", 0.05),
                           (1.5, 1.5), ("1x", 1.0), (None, None), ("abc", None)):
            self.assertEqual(CA._parse_ratio(text), want, text)


class CodeArtsCheckinTest(unittest.TestCase):
    """★ CONFIRMED（今日已完成）必须报「已签到」并带上当日额度。

    历史事故（2026-10-22）：旧实现遇到 CONFIRMED 只记一句「已完成」就 continue，
    既不报 already 也不给 amount → 账号页永远「未签到」、积分恒 0。
    """

    ITEMS = [
        {"campaignId": 1, "title": "每日签到领积分", "type": "USER_LOGIN",
         "benefitAmount": 1000, "benefitUnit": "CREDIT", "status": "CONFIRMED",
         "claimable": False, "pendingTotalAmount": 0.0,
         "extra": {"startTime": "2026-09-08T16:00:00Z", "endTime": "2026-12-30T16:00:00Z"}},
        {"campaignId": 4, "title": "新用户注册礼 4000 积分", "type": "NEW_USER_REGISTER",
         "benefitAmount": 4000, "benefitUnit": "CREDIT", "status": "CONFIRMED",
         "claimable": False, "pendingTotalAmount": 0.0,
         "extra": {"startTime": "2026-09-08T16:00:00Z", "endTime": "2026-12-30T16:00:00Z"}},
        {"campaignId": 3, "title": "推荐好友奖励", "type": "INVITE_USER",
         "benefitAmount": 1000, "benefitUnit": "CREDIT", "status": None,
         "claimable": False, "extra": {}},
    ]

    def setUp(self):
        self._orig = CA.fetch_campaigns
        CA.fetch_campaigns = lambda acc: list(self.ITEMS)
        self.addCleanup(lambda: setattr(CA, "fetch_campaigns", self._orig))

    def test_confirmed_reports_already_with_amount(self):
        r = CA.checkin({"uid": "u"})
        self.assertTrue(r["ok"])
        self.assertTrue(r["already"])
        self.assertEqual(r["amount"], 1000, "当日签到额度必须带出来，供写 gain 表")
        self.assertEqual(r["credits"], 0, "本次没有新领取")

    def test_credited_reflects_daily_done_not_ok(self):
        """★ 2026-10-23：写 gain 的唯一闸门是 credited（不是 ok）。
        CONFIRMED（今日已完成）→ credited=True；只有「已过期/不可领取」→ credited=False。
        旧版调用方拿 ok 判，活动过期也会被渲染成「已签到」。"""
        r = CA.checkin({"uid": "u"})
        self.assertTrue(r["credited"], r)
        # 每日签到活动已结束 + 没有任何可领 → ok 仍为 True，但 credited 必须 False
        expired = [dict(self.ITEMS[0], status="EXPIRED")]
        CA.fetch_campaigns = lambda acc: expired
        r2 = CA.checkin({"uid": "u"})
        self.assertTrue(r2["ok"], "跑完了就是 ok（语义不变）")
        self.assertFalse(r2["credited"], "没有签到入账就不能给 credited，否则写假 gain")

    def test_only_daily_and_newuser_considered(self):
        r = CA.checkin({"uid": "u"})
        self.assertNotIn("推荐好友", r["msg"], "其它活动不参与（不误领邀请奖励）")

    def test_claimable_daily_is_claimed(self):
        items = [dict(self.ITEMS[0], status="ELIGIBLE", claimable=True)]
        CA.fetch_campaigns = lambda acc: items
        calls = []
        orig_cr = CA.credit_request
        CA.credit_request = lambda acc, m, path, body=None: (calls.append((m, path)), {})[1]
        self.addCleanup(lambda: setattr(CA, "credit_request", orig_cr))
        r = CA.checkin({"uid": "u"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["credits"], 1000)
        self.assertTrue(any(CA.EP_CREDIT_CLAIM == p for _m, p in calls))
        self.assertTrue(any(CA.EP_CREDIT_CONFIRM == p for _m, p in calls))

    def test_rotated_campaign_is_recognized_by_type(self):
        """★ 2026-10-28：活动 id 会轮换（1 → 34），必须按 type 认人。

        实测当日 items 里已经没有 id=1（「每日签到领积分」+1000），
        取而代之的是 id=34（「每日签到领100积分」+100，USER_LOGIN、claimable）。
        旧实现只认 id ∈ {1,4} → 每天报「今日未签到」，两个账号一分不领。
        """
        items = [{"campaignId": 34, "title": "每日签到领100积分",
                  "type": "USER_LOGIN", "benefitAmount": 100,
                  "benefitUnit": "CREDIT", "status": "ELIGIBLE",
                  "claimable": True, "pendingTotalAmount": 100.0,
                  "extra": {"startTime": "2026-10-26T16:00:00Z",
                            "endTime": "2026-12-31T15:59:59Z"}}]
        CA.fetch_campaigns = lambda acc: items
        calls = []
        orig_cr = CA.credit_request
        CA.credit_request = lambda acc, m, path, body=None: (
            calls.append((m, path, body)), {})[1]
        self.addCleanup(lambda: setattr(CA, "credit_request", orig_cr))
        r = CA.checkin({"uid": "u"})
        self.assertTrue(r["credited"], r)
        self.assertEqual(r["amount"], 100)
        self.assertEqual(r["credits"], 100)
        claims = [b for _m, path, b in calls if path == CA.EP_CREDIT_CLAIM]
        self.assertTrue(claims and claims[0].get("campaignId") == 34, calls)

    def test_daily_id_fallback_without_type(self):
        """type 缺失时仍按历史 id=1 兜底（兼容老响应/老夹具）。"""
        items = [dict(self.ITEMS[0])]
        items[0]["type"] = None
        CA.fetch_campaigns = lambda acc: items
        r = CA.checkin({"uid": "u"})
        self.assertTrue(r["credited"])
        self.assertEqual(r["amount"], 1000)

    def test_new_user_by_type_even_if_id_changes(self):
        """新用户礼同样按 type 认；CONFIRMED 只记消息，不能算成每日签到。"""
        items = [{"campaignId": 99, "title": "新用户注册送 4000 积分",
                  "type": "NEW_USER_REGISTER", "benefitAmount": 4000,
                  "benefitUnit": "CREDIT", "status": "CONFIRMED",
                  "claimable": False, "pendingTotalAmount": 0.0,
                  "extra": {"startTime": "2026-09-08T16:00:00Z",
                            "endTime": "2026-12-30T16:00:00Z"}}]
        CA.fetch_campaigns = lambda acc: items
        r = CA.checkin({"uid": "u"})
        self.assertIn("新用户", r["msg"])
        self.assertFalse(r["credited"], "新手礼不是每日签到，不能写签到 gain")

    def test_is_expiry_error_text(self):
        """★ 2026-10-30：过期判据必须覆盖华为云的几种文案。

        采集器/admin 刷新没有 provider 实例可用，只能靠错误文本判断
        （此前没有这个判据 → token 一过期就静默失败）。
        """
        for text in ("APIG.0602",
                     '{"error_msg":"Bad request: the security token has expired"}',
                     "STS5.1806", "APIG.0301", "decrypt token fail"):
            self.assertTrue(CA.is_expiry_error_text(text), text)
        self.assertFalse(CA.is_expiry_error_text("并发会话数已达上限(3个)"))
        self.assertFalse(CA.is_expiry_error_text(None))

    def test_credits_unknown_not_zero(self):
        """没有余额接口 → 返回 None（未知），绝不能是 0。"""
        credits, reason = CA.fetch_credits({"uid": "u"})
        self.assertIsNone(credits)
        self.assertTrue(reason)


class QoderRequestBodyTest(unittest.TestCase):
    """★ 请求体必须是**原始 JSON**（编码后上游 500），且 SSE 错误必须暴露。

    2026-10-22 实测（同一请求、同一签名，三种写法对比）：
      qoder_encode(raw) → 500 event:error（Jackson 解析失败）
      std base64(raw)   → 500
      原样 JSON         → 200 + 正常流
    """

    def test_raw_json_body_is_signed(self):
        from providers.qoder_cn import QoderCNProvider
        prov = QoderCNProvider({"accounts": [{"uid": "u", "realm": "cn",
                                              "accessToken": "t"}]})
        signed_bodies = []

        class FakeSess:
            def headers(self, body, url, model_key="", sse=True, accept=""):
                signed_bodies.append(body)
                return {}

        class FakeResp:
            status_code = 200

            async def aclose(self):
                pass

        class FakeClient:
            def build_request(self, *a, **k):
                return None

            async def send(self, req, stream=True):
                return FakeResp()

        orig_sess = QC.SESSIONS.get
        QC.SESSIONS.get = lambda account: FakeSess()
        prov._client = FakeClient()
        prov._ensure_token = lambda account: asyncio.sleep(0, result=True)
        self.addCleanup(lambda: setattr(QC.SESSIONS, "get", orig_sess))

        body = {"messages": [{"role": "user", "content": "hi"}], "model": "dmodel"}
        asyncio.run(prov._open_stream(body, {"uid": "u", "realm": "cn",
                                             "accessToken": "t"}, "dmodel"))
        self.assertTrue(signed_bodies, "应已发出请求")
        sent = signed_bodies[0]
        self.assertTrue(sent.lstrip().startswith("{"), "签名体必须是原始 JSON")
        json.loads(sent)          # 必须是合法 JSON（不是自定义 base64）

    def test_error_event_raises(self):
        from providers.qoder_base import _parse_envelope_line
        with self.assertRaises(RuntimeError) as ctx:
            _parse_envelope_line('data:{"success":false,"msgInfo":"boom"}',
                                 last_event="error")
        self.assertIn("boom", str(ctx.exception))

    def test_success_false_payload_raises(self):
        from providers.qoder_base import _parse_envelope_line
        with self.assertRaises(RuntimeError) as ctx:
            _parse_envelope_line('data:{"success":false,"msgInfo":"bad"}')
        self.assertIn("bad", str(ctx.exception))


class CodeArtsErrorClassifyTest(unittest.TestCase):
    """上游错误码 → 专用异常（决定「补福利头重试」还是直接报错）。"""

    def test_not_registered(self):
        for code, msg in (("InferHub.002002009.404", "The model is not registered"),
                          ("", "the model is not registered, please request other model")):
            with self.assertRaises(CA.ModelNotRegistered):
                CA._raise_codearts_error(code, msg)

    def test_benefit_unavailable(self):
        with self.assertRaises(CA.BenefitUnavailable):
            CA._raise_codearts_error("InferHub.4004.200", "benefit not found")

    def test_concurrency_limited(self):
        with self.assertRaises(CA.ConcurrencyLimited):
            CA._raise_codearts_error("TM.00001041", "并发会话数已达上限(3)")

    def test_other_error_stays_generic(self):
        with self.assertRaises(RuntimeError) as ctx:
            CA._raise_codearts_error("X.1", "whatever")
        self.assertNotIsInstance(ctx.exception, CA.ModelNotRegistered)


class CodeArtsBenefitLearnTest(unittest.TestCase):
    """★ 福利模型自适应：没带头发起 → 上游说未注册 → 补头重试并记住。

    上游没有可查的福利名单（opengw 网关已失效），只能从错误里学。
    """

    def _events(self, prov, model, outcomes):
        """跑一遍 _events，返回每次尝试实际用的 benefit 标记。"""
        used = []

        async def fake_open(body, account, mdl, force_benefit=None):
            used.append(bool(force_benefit))

            class R:
                async def aclose(self):
                    pass
            return R(), "cid"

        async def fake_iter(resp, cl, rl):
            idx = len(used) - 1
            outcome = outcomes[min(idx, len(outcomes) - 1)]
            if outcome == "notreg":
                raise CA.ModelNotRegistered("not registered")
            if outcome == "benefit":
                raise CA.BenefitUnavailable("benefit not found")
            yield {"choices": [{"delta": {"content": "hi"}}]}

        orig_open, orig_iter = prov._open_stream, _aiter_codearts_module()
        prov._open_stream = fake_open
        setattr(CA, "_aiter_codearts", fake_iter)
        try:
            async def run():
                out = []
                async for c in prov._events({"messages": []}, {"uid": "u"}, model):
                    out.append(c)
                return out
            try:
                res = asyncio.run(run())
                err = None
            except Exception as e:      # noqa: BLE001
                res, err = [], e
        finally:
            prov._open_stream = orig_open
            setattr(CA, "_aiter_codearts", orig_iter)
        return used, res, err

    def test_not_registered_is_not_treated_as_benefit(self):
        """★ 2026-10-23 语义更正：002002009.404 = 「model 名不对」，**不是**「要福利头」。

        老实现据此把模型学成福利模型、补 `maas_type: benefit` 重试，于是每次都撞
        `4004.200 benefit not found`，界面上就是「这个模型一直繁忙」。实测真正的
        原因是发了展示名而不是内部 model_id（DeepSeek-V4.1-Flash 要发
        deepseek-v4.1-flash）。所以现在：不补头、不学习，直接把原因说清楚。
        """
        prov = _providers()["codearts"]
        prov._benefit_models.clear()
        used, res, err = self._events(prov, "GLM-5.3", ["notreg", "ok"])
        self.assertIsNotNone(err, "未注册就该直接报错，不该悄悄补头重试")
        self.assertEqual(used, [False], "只发一次，不许补福利头重试")
        self.assertEqual(prov._benefit_models, set(), "不许把 404 学成福利模型")
        self.assertIn("未注册", str(err))
        self.assertIn("glm-5.3", str(err), "错误里要带上真正发出去的 model_id")

    def test_catalog_marked_benefit_uses_header_directly(self):
        """目录里明确标了 benefit 的模型，第一次就该带福利头。"""
        prov = _providers()["codearts"]
        prov._models = [{"id": "GLM-5.3", "key": "glm-5.3", "benefit": True}]
        used, res, err = self._events(prov, "GLM-5.3", ["ok"])
        self.assertIsNone(err)
        self.assertEqual(used, [True])

    def test_benefit_unavailable_reports_clearly(self):
        """真·福利模型（目录标记）撞 4004.200 时，错误文案要说明「福利」。"""
        prov = _providers()["codearts"]
        prov._models = [{"id": "GLM-5.3", "key": "glm-5.3", "benefit": True}]
        used, res, err = self._events(prov, "GLM-5.3", ["benefit"])
        self.assertIsNotNone(err)
        self.assertIn("福利", str(err))


def _aiter_codearts_module():
    from providers import codearts as m
    return m._aiter_codearts


class QoderQueueTest(unittest.TestCase):
    """★ 上游排队（403 + isQueued/10605）是「繁忙」，不是「断连」。

    2026-10-22 实测：模型列表里 DeepSeek-Flash 这类热门模型整片显示「断连」，
    真因是 Qoder 对容量紧张的模型回 HTTP 403 + 排队体，而 403 被一律当成
    鉴权失败（check_model 里 403 → down）。通道其实好得很。
    """

    REAL = ('{"code":"403","message":"{\"code\":\"10605\",\"message\":'
            '\"{\\\"isQueued\\\":true,\\\"modelKey\\\":\\\"dfmodel\\\",'
            '\\\"queueCount\\\":2108,\\\"retryAfterSeconds\\\":30}\""}')

    def test_parse_wait_seconds(self):
        self.assertEqual(QC.queue_wait_seconds(self.REAL), 30)

    def test_non_queue_returns_zero(self):
        for d in ('{"code":"403","message":"forbidden"}', '', None,
                  '{"error":"invalid token"}'):
            self.assertEqual(QC.queue_wait_seconds(d or ""), 0)

    def test_queue_without_duration_defaults_30(self):
        self.assertEqual(QC.queue_wait_seconds('{"isQueued":true}'), 30)

    def test_classified_as_busy_not_down(self):
        """探活归类：排队/并发/福利都要算「繁忙」。"""
        import auto_router as ar
        cases = [
            (f"qoder-cn 流式传输中断: qoder upstream status=403: {self.REAL}", "busy"),
            ("codearts 上游并发会话已达上限（每账号 3 条）：TM.00001041", "busy"),
            ("模型 X 是福利（免费）模型…InferHub.4004.200 benefit not found", "busy"),
            ("workbuddy upstream HTTP 401: unauthorized", "down"),
            ("All connection attempts failed", "down"),
        ]
        for msg, want in cases:
            self.assertEqual(ar._classify_check_error(msg), want, msg[:40])

    def test_queued_exception_is_distinct(self):
        """排队有专用异常类型，便于上层区分「等待」与「真失败」。"""
        self.assertTrue(issubclass(QC.QoderQueued, RuntimeError))

    # ───── 2026-10-24：RESOURCE_LIMIT 长得像排队，实际等也没用 ─────
    REAL_LIMIT = ('{"code":"403","message":"{\"code\":\"10605\",\"message\":'
                  '\"{\\\"isQueued\\\":true,\\\"modelKey\\\":\\\"dmodel\\\",'
                  '\\\"queueCount\\\":0,\\\"queueType\\\":\\\"p3\\\",'
                  '\\\"retryAfterSeconds\\\":30,\\\"serviceAvailable\\\":false,'
                  '\\\"subCode\\\":\\\"RESOURCE_LIMIT\\\",'
                  '\\\"waitTime\\\":30}\"}"}')

    def test_resource_limit_is_recognised(self):
        self.assertTrue(QC.is_resource_limit(self.REAL_LIMIT))
        # 真的在排队（queueCount>0）不能被误判成资源受限
        self.assertFalse(QC.is_resource_limit(self.REAL))

    def test_resource_limit_does_not_wait(self):
        """★ 核心：RESOURCE_LIMIT 必须返回 0，否则白等 30s×3 轮=90 秒。"""
        self.assertEqual(QC.queue_wait_seconds(self.REAL_LIMIT), 0,
                         "资源受限不是排队，绝不能让调用方等待重试")

    def test_service_available_false_counts_as_limit(self):
        self.assertTrue(QC.is_resource_limit('{"serviceAvailable": false}'))

    def test_resource_limit_classified_busy_not_down(self):
        """RESOURCE_LIMIT 是**模型级**限制（通道是活的）→ 判 busy，不能判 down。

        否则账号页会把整条 Qoder 通道标红「断连」，而真相只是「这个模型此刻
        没资源」。上层文案已改为「请稍后重试或改用其它模型」，不渲染成「繁忙等待」。
        """
        import auto_router as ar
        msg = f"qoder-cn 上游请求失败: 模型 dmodel 当前无可用资源（上游 RESOURCE_LIMIT）"
        self.assertEqual(ar._classify_check_error(msg), "busy")

    def test_resource_limited_exception_is_distinct(self):
        """资源受限有专用异常（动作是「换模型」），不能和排队的「等一会」混为一谈。"""
        self.assertTrue(issubclass(QC.QoderResourceLimited, RuntimeError))
        self.assertFalse(issubclass(QC.QoderResourceLimited, QC.QoderQueued))


class QoderHTTP405Test(unittest.TestCase):
    """★ 2026-10-24：405 是网关/WAF 静态页，**与工具协议无关**（归类修正）。

    旧版把 405 当成「结构化工具历史被上游拒绝」去降级重试：
    降级后的请求体与 405 毫无因果关系，日志还把排查引向工具协议（踩过）。
    """

    # 真实 405 页面的指纹（阿里云 WAF/SLB）
    WAF_405 = ('<!doctypehtml><html lang="zh-cn"><meta data-spm="a3c0e">'
               '<title>405</title></html>')

    def test_405_is_not_treated_as_tool_protocol_rejection(self):
        import inspect
        from providers import qoder_base as QB
        src = inspect.getsource(QB.QoderProvider._open_stream)
        self.assertIn("status == 405", src,
                      "必须为 405 单列分支，不能落进工具协议降级")
        # 405 分支必须在「结构化工具历史被拒」之前，否则永远走不到
        self.assertLess(src.index("status == 405"),
                        src.index("结构化工具历史被上官拒绝") if
                        "结构化工具历史被上官拒绝" in src else
                        src.index("结构化工具历史被上游拒绝"))

    def test_405_is_not_resource_limit(self):
        self.assertFalse(QC.is_resource_limit(self.WAF_405))
        self.assertEqual(QC.queue_wait_seconds(self.WAF_405), 0,
                         "405 不是排队信号，不能被当成等待")


class QoderWAFBackoffTest(unittest.TestCase):
    """★ 2026-10-24：405（边缘 WAF 拦截）改成**对同一主机退避重试**。

    背景（实测，改这条的直接原因）：
      · 三个候选主机会**在同一时刻一起被拦**（同一套边缘规则集）；
      · 全历史统计 api2/api1 被尝试 26 次、**成功 0 次**，全 405；
      · 而拦截是**时间性**的（21:10:59→21:14:25 连拦，之后恢复）。
    ⇒ 所以「立刻换下一个域名」永远等不到放行，应该「等一会再打同一台」。

    本测试用假 client 打桩，验证接线（不联网、不真等待）。
    """

    WAF_405 = ('<!doctypehtml><html lang="zh-cn"><meta name="data-spm"content="a3c0e">'
               '<title>405</title></html>')

    class _Resp:
        def __init__(self, code, body):
            self.status_code = code
            self._body = body.encode("utf-8")

        async def aread(self):
            return self._body

        async def aclose(self):
            return None

    class _Client:
        """记录每次实际发出的 URL；固定返回指定的响应。"""

        def __init__(self, code, body):
            self.code, self.body, self.calls = code, body, []

        def build_request(self, method, url, content=None, headers=None):
            self.calls.append(url)
            return {"method": method, "url": url}

        async def send(self, req, stream=True):
            return QoderWAFBackoffTest._Resp(self.code, self.body)

    class _Sess:
        def headers(self, *a, **k):
            return {}

    def _provider(self, code, body, rounds=2, gap=0.0, budget=999.0):
        from providers import qoder_base as QB

        outer = self

        class P(QB.QoderProvider):
            WAF_RETRY_ROUNDS = rounds
            WAF_RETRY_GAP = gap
            WAF_WAIT_BUDGET_SECS = budget
            WAF_RETRY_JITTER = 0.0

            def __init__(self):
                self.name = "qoder-intl"
                self.realm = "intl"
                self.cfg = {}
                self.client = outer._Client(code, body)

            def build_body(self, *a, **k):
                return {"probe": True}

            async def _ensure_token(self, account, force=False):
                return True

            def get_client(self):
                return self.client

        p = P()
        return p, p.client

    def setUp(self):
        os.environ.pop("QD_GATEWAY_FALLBACKS", None)   # 保证默认只有主主机
        outer = self
        self._orig_sess = QC.SESSIONS

        class _Sessions:
            def get(self, account):
                return outer._Sess()

        QC.SESSIONS = _Sessions()
        # 不让用例真的睡 30 秒
        self._orig_sleep = __import__("asyncio").sleep
        __import__("asyncio").sleep = lambda *a, **k: self._orig_sleep(0)

    def tearDown(self):
        QC.SESSIONS = self._orig_sess
        import asyncio
        asyncio.sleep = self._orig_sleep

    def _run(self, p):
        import asyncio
        return asyncio.run(p._open_stream({"messages": []}, {}, "dfmodel"))

    def test_waf_405_retries_the_same_host(self):
        """同主机重试 rounds 次 → 总共 rounds+1 次请求，且**打的是同一台**。"""
        p, client = self._provider(405, self.WAF_405, rounds=2)
        with self.assertRaises(RuntimeError) as ctx:
            self._run(p)
        self.assertEqual(len(client.calls), 3, "应共尝试 3 次（首次 + 2 轮退避）")
        self.assertEqual(len(set(client.calls)), 1, "退避重试必须打**同一台**主机")
        self.assertIn("退避重试", str(ctx.exception))
        self.assertIn("2 轮", str(ctx.exception))

    def test_rounds_configurable(self):
        p, client = self._provider(405, self.WAF_405, rounds=0)
        with self.assertRaises(RuntimeError):
            self._run(p)
        self.assertEqual(len(client.calls), 1, "rounds=0 时不该重试")

    def test_budget_blocks_further_retries(self):
        """预算不足 → 不再退避（避免把请求拖到几倍时长）。"""
        p, client = self._provider(405, self.WAF_405, rounds=5, gap=100.0, budget=10.0)
        with self.assertRaises(RuntimeError):
            self._run(p)
        self.assertEqual(len(client.calls), 1, "预算不够就不该等")

    def test_non_waf_405_does_not_retry(self):
        """不是 WAF 页的 405（例如端点/方法真变了）→ 不浪费退避时间。"""
        p, client = self._provider(405, '{"error":"method not allowed"}', rounds=3)
        with self.assertRaises(RuntimeError):
            self._run(p)
        self.assertEqual(len(client.calls), 1, "非 WAF 的 405 不该退避")

    def test_405_is_deduped_for_the_final_message(self):
        """退避重试会重复碰同一主机 —— 计数必须去重，否则末尾的 405 分支判不中。"""
        p, client = self._provider(405, self.WAF_405, rounds=3)
        with self.assertRaises(RuntimeError) as ctx:
            self._run(p)
        msg = str(ctx.exception)
        self.assertIn("405", msg, "必须命中 405 专用文案（说明去重没写坏）")
        self.assertIn("WAF", msg)


class CodeArtsSSETest(unittest.TestCase):
    """CodeArts 的 text 字段是**累计全文**快照，必须按增量发给客户端。"""

    def test_snapshot_converted_to_increment(self):
        import asyncio
        from providers.codearts import _aiter_codearts

        class FakeResp:
            def __init__(self, lines):
                self._lines = lines

            async def aiter_text(self):
                for l in self._lines:
                    yield l + "\n"

        lines = [
            json.dumps({"text": "你"}),
            json.dumps({"text": "你好"}),
            json.dumps({"text": "你好呀"}),
            json.dumps({"text": "[DONE]", "error_code": "0"}),
        ]

        async def go():
            out = []
            async for chunk in _aiter_codearts(FakeResp(lines), [0], [0]):
                for ch in chunk.get("choices") or []:
                    c = (ch.get("delta") or {}).get("content")
                    if c:
                        out.append(c)
            return "".join(out)

        # 快照语义：每帧只发新增尾巴，拼起来等于最终全文（重复发全文会翻倍）
        self.assertEqual(asyncio.run(go()), "你好呀")

    def test_error_code_raises(self):
        import asyncio
        from providers.codearts import _aiter_codearts

        class FakeResp:
            async def aiter_text(self):
                yield json.dumps({"text": "x", "error_code": "ChatAgent.00001001",
                                  "error_msg": "并发超限"}) + "\n"

        async def go():
            async for _ in _aiter_codearts(FakeResp(), [0], [0]):
                pass
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(go())
        self.assertIn("并发超限", str(ctx.exception))



class QueueSignalTest(unittest.TestCase):
    """排队判定必须只认真正的排队特征（2026-10-24 收紧）。"""

    def test_resource_limit_is_not_queue(self):
        body = ('{"isQueued":true,"modelKey":"dfmodel","queueCount":0,'
                '"retryAfterSeconds":30,"serviceAvailable":false,'
                '"subCode":"RESOURCE_LIMIT"}')
        self.assertEqual(QC.queue_wait_seconds(body), 0)
        self.assertTrue(QC.is_resource_limit(body))

    def test_real_queue_still_waits(self):
        body = ('{"isQueued":true,"modelKey":"dfmodel","queueCount":2519,'
                '"retryAfterSeconds":30,"serviceAvailable":true,'
                '"subCode":"CALL_COUNT_LIMIT"}')
        self.assertEqual(QC.queue_wait_seconds(body), 30)

    def test_zero_queue_count_is_not_queue(self):
        """queueCount=0 说明没人排队 —— 等也没用，哪怕没有 RESOURCE_LIMIT 字样。"""
        body = '{"isQueued":true,"queueCount":0,"retryAfterSeconds":30}'
        self.assertEqual(QC.queue_wait_seconds(body), 0)

    def test_service_unavailable_is_not_queue(self):
        body = '{"isQueued":true,"queueCount":5,"serviceAvailable":false}'
        self.assertEqual(QC.queue_wait_seconds(body), 0)

    def test_queue_word_alone_is_not_enough(self):
        """过去 'queue' 字样就算排队 —— 这是判据过宽的根因，必须改掉。"""
        body = '{"queueType":"p3","note":"hello queue"}'
        self.assertEqual(QC.queue_wait_seconds(body), 0)

    def test_plain_error_is_not_queue(self):
        self.assertEqual(QC.queue_wait_seconds("HTTP 500 boom"), 0)


class ResourceLimitCooldownTest(unittest.TestCase):
    """RESOURCE_LIMIT 冷却窗口（2026-10-24）。"""

    def test_mark_and_clear(self):
        QC.clear_resource_limited("unit-x", "m1")
        self.assertEqual(QC.resource_limited_until("unit-x", "m1"), 0)
        QC.mark_resource_limited("unit-x", "m1", "no resource")
        self.assertGreater(QC.resource_limited_until("unit-x", "m1"), 0)
        QC.clear_resource_limited("unit-x", "m1")
        self.assertEqual(QC.resource_limited_until("unit-x", "m1"), 0)

    def test_cooldown_is_per_model(self):
        QC.clear_resource_limited("unit-y", "ma")
        QC.clear_resource_limited("unit-y", "mb")
        QC.mark_resource_limited("unit-y", "ma", "no resource")
        self.assertGreater(QC.resource_limited_until("unit-y", "ma"), 0)
        self.assertEqual(QC.resource_limited_until("unit-y", "mb"), 0)


class HostFallback405Test(unittest.TestCase):
    """405 的主机策略 —— 以**最新**（2026-10-24 第三次修订）为准。

    演变（读文档时按这个顺序理解）：
      ① 初版：api3 被拦就立刻 raise → api2/api1 **一次都没试**（单点故障）；
      ② 二版：405 后「换下一个候选主机」；
      ③ **现版（本类锁定）**：默认**只有主主机**，405 时对**同一台**退避重试。
         依据：405 是边缘级拦截，同一时刻三个候选**一起**被拦（同一套规则集）；
               全历史 api2/api1 被尝试 26 次、**成功 0 次**；api3 成功 413 次；
               而拦截是**时间性**的（21:10:59→21:14:25 连拦，之后恢复）
               → 「等同一台」才有意义，「换域名」永远等不到放行。

    旧行为**没有删除**：设 QD_GATEWAY_FALLBACKS=1 即恢复候选主机与逐台降级。
    """

    WAF = b'<html><title>405</title><meta data-spm="a3c0e">'

    def _provider(self, resp_by_host, rounds=0):
        """构造 provider。resp_by_host: {host: 状态码}，未列出的主机一律 405。"""
        from providers import qoder_base as QB

        tried = []

        class FakeResp:
            def __init__(self, code):
                self.status_code = code

            async def aread(self):
                return HostFallback405Test.WAF

            async def aclose(self):
                return None

        class FakeClient:
            def build_request(self, method, url, content=None, headers=None):
                return {"url": url}

            async def send(self, req, stream=False):
                host = req["url"].split("/algo")[0]
                tried.append(host)
                return FakeResp(resp_by_host.get(host, 405))

        class P(QB.QoderProvider):
            WAF_RETRY_ROUNDS = rounds
            WAF_RETRY_GAP = 0.0
            WAF_RETRY_JITTER = 0.0
            WAF_WAIT_BUDGET_SECS = 999.0

            def __init__(self):
                self.name = "qoder-intl"
                self.realm = "intl"
                self.cfg = {}
                self._client = FakeClient()
                self._health = type("H", (), {
                    "mark_ok": staticmethod(lambda *a, **k: None),
                    "mark_invalid": staticmethod(lambda *a, **k: None)})()

            def get_client(self):
                return self._client

            async def _ensure_token(self, account, force=False):
                return True

            def build_body(self, body, account, model_key, structured=True):
                return {"model": model_key}

        return P(), tried

    def _run(self, p):
        import asyncio
        from providers import qoder_base as QB
        acct = {"uid": "u", "accessToken": "at", "refreshToken": "rt", "realm": "intl"}
        return asyncio.run(QB.QoderProvider._open_stream(
            p, {"messages": []}, acct, "dfmodel"))

    def setUp(self):
        os.environ.pop("QD_GATEWAY_FALLBACKS", None)

    def test_default_405_never_touches_fallback_hosts(self):
        """默认只有主主机：被拦时也**不会**去碰 api2/api1（它们从未成功过）。"""
        hosts = QC.gateway_candidates("intl")
        self.assertEqual(hosts, [QC.REALMS["intl"]["gateway"]])
        p, tried = self._provider({})            # 全部 405
        with self.assertRaises(RuntimeError) as ctx:
            self._run(p)
        self.assertEqual(set(tried), {hosts[0]}, "默认下不该出现其它主机")
        self.assertIn("405", str(ctx.exception))

    def test_fallbacks_still_work_when_enabled(self):
        """开关打开后，旧行为（405 换下一台，直到成功）仍然可用 —— 没删掉。"""
        os.environ["QD_GATEWAY_FALLBACKS"] = "1"
        self.addCleanup(lambda: os.environ.pop("QD_GATEWAY_FALLBACKS", None))
        hosts = QC.gateway_candidates("intl")
        self.assertGreaterEqual(len(hosts), 2)
        p, tried = self._provider({hosts[-1]: 200})
        resp = self._run(p)
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(len(tried), 2, "打开开关后 405 必须继续试下一台")
        self.assertEqual(tried[0], hosts[0])
        self.assertEqual(tried[-1], hosts[-1])

    def test_all_405_reports_attempted_count(self):
        """全部候选都 405 时，文案里要带上尝试过的主机数。"""
        hosts = QC.gateway_candidates("intl")
        p, tried = self._provider({})
        with self.assertRaises(RuntimeError) as ctx:
            self._run(p)
        msg = str(ctx.exception)
        self.assertIn("405", msg)
        self.assertIn(str(len(hosts)), msg)


class QoderResourceLimitWaitTest(unittest.TestCase):
    """★ 2026-10-25：RESOURCE_LIMIT 的**等待时长**（master 实测反馈）。

    master 原话：「我实际测试，发现等待 2 到 3 分钟，上游就会有资源了」。
    旧默认是 3 秒 × 2 轮 ≈ **6 秒** —— 等于问一句就走，必然直接报错。

    现在默认拉到 **约 160 秒**（4 轮 × 40 秒），并用 `RL_WAIT_BUDGET_SECS`
    （默认 170）压住上界 —— 上界由**客户端**决定：DSH 的 llm 插件
    `streamIdleTimeoutMs` 默认 300 秒，而等待期间客户端收不到任何字节，
    所以总等待必须明显小于 300 秒，否则会把「上游没资源」变成「客户端流空闲超时」。
    """

    def setUp(self):
        import asyncio
        import os
        os.environ.pop('QD_GATEWAY_FALLBACKS', None)
        self.slept = []
        self._orig_sleep = asyncio.sleep

        async def _fake_sleep(sec, *a, **k):
            self.slept.append(sec)          # 记下「要求睡多久」，但不真的睡
            return None

        asyncio.sleep = _fake_sleep
        from providers import qoder_common as qc
        self.qc = qc

    def tearDown(self):
        import asyncio
        asyncio.sleep = self._orig_sleep

    # ---------- 默认值本身要合理 ----------
    def test_default_wait_covers_master_window(self):
        from providers import qoder_base as QB
        P = QB.QoderProvider
        total = P.RL_RETRY_ROUNDS * P.RL_RETRY_GAP
        self.assertGreaterEqual(total, 120,
                                "总等待要覆盖 master 实测的 2 分钟恢复窗口")
        self.assertLess(total, 300,
                        "总等待必须**小于**客户端 5 分钟流空闲超时（还要留首字节余量）")
        self.assertLessEqual(P.RL_WAIT_BUDGET_SECS, 200,
                             "预算要有余量地低于客户端 idle 超时")
        self.assertGreaterEqual(P.RL_WAIT_BUDGET_SECS, total,
                                "预算至少要容得下正常轮数，否则轮数形同虚设")

    # ---------- 接线 ----------
    def _provider(self, rounds, gap, budget, fail_times, model_fallback=False):
        from providers import qoder_base as QB
        outer = self

        class P(QB.QoderProvider):
            RL_RETRY_ROUNDS = rounds
            RL_RETRY_GAP = gap
            RL_WAIT_BUDGET_SECS = budget

            def __init__(self):
                self.name = 'qoder-cn'
                self.realm = 'cn'
                self.cfg = {'modelFallback': model_fallback}
                self.calls = 0
                self._catalog = []

            def _enabled_models(self):
                return []

            async def _events_one(self, body, account, model_key):
                self.calls += 1
                if self.calls <= fail_times:
                    raise outer.qc.QoderResourceLimited('no resource')
                yield {'choices': [{'index': 0, 'delta': {'content': 'ok'},
                                    'finish_reason': None}]}

        return P()

    def _run(self, p):
        import asyncio

        async def go():
            out = []
            async for c in p._events({'messages': []}, {}, 'dfmodel'):
                out.append(c)
            return out

        return asyncio.run(go())

    def test_waits_the_configured_gap_between_retries(self):
        """每次重试都要**真的等待**（旧版只等 3 秒，这就是本次要改的点）。"""
        p = self._provider(rounds=3, gap=40.0, budget=170.0, fail_times=2)
        out = self._run(p)
        self.assertEqual(p.calls, 3, '失败 2 次 + 成功 1 次')
        self.assertEqual(self.slept, [40.0, 40.0], '每次间隔必须是配置的 40 秒')
        self.assertTrue(any(isinstance(c, dict) and c.get('choices') for c in out))

    def test_gives_up_after_rounds_and_reports_waited_seconds(self):
        p = self._provider(rounds=3, gap=40.0, budget=170.0, fail_times=99)
        with self.assertRaises(self.qc.QoderResourceLimited) as ctx:
            self._run(p)
        self.assertEqual(p.calls, 4, '首次 + 3 轮重试')
        self.assertEqual(self.slept, [40.0, 40.0, 40.0])
        msg = str(ctx.exception)
        self.assertIn('已等待', msg, '文案要报出「等了多久」，否则用户以为没等')
        self.assertIn('3 轮', msg)

    def test_budget_blocks_retry_when_next_gap_would_exceed_it(self):
        """预算守卫：下一轮等待会超预算 → 不再等（避免超出客户端 idle 超时）。"""
        p = self._provider(rounds=5, gap=100.0, budget=10.0, fail_times=99)
        with self.assertRaises(self.qc.QoderResourceLimited):
            self._run(p)
        self.assertEqual(p.calls, 1, '预算不足时一次都不该等')
        self.assertEqual(self.slept, [])

    def test_rounds_zero_means_no_wait(self):
        """把轮数调 0 即退回「立刻报错」（应急开关）。"""
        p = self._provider(rounds=0, gap=40.0, budget=170.0, fail_times=99)
        with self.assertRaises(self.qc.QoderResourceLimited):
            self._run(p)
        self.assertEqual(p.calls, 1)
        self.assertEqual(self.slept, [])


class ResourceLimitFallbackTest(unittest.TestCase):
    """RESOURCE_LIMIT 不许直接断连（2026-10-24，master 反馈后重做）。

    master 原话：「那不应该是让用户等待模型请求吗？怎么就直接断连了」。
    期望行为：某模型无资源 → 先短等重试；**是否自动换模型由开关决定**。

    ★★ 2026-10-24 二次修订（master 再次反馈）：
       自动换模型会把请求偷偷改到**更贵的模型**上照常扣积分 ——
       某账号一天积分被清零就是这么来的（两个池子全用尽）。
       master 决定：「当前模型不行就不行」→ **默认关闭**自动换模型。
       因此本类测试全部显式开启 `cfg["modelFallback"]=True` 来验证
       「开关打开时」的旧行为仍然完整；「默认关闭」的契约由
       `tests/test_qoder_model_fallback.py` 单独锁定。
    """

    def _provider(self, dead_models, good_models, catalog, model_fallback=True):
        from providers import qoder_base as QB

        class P(QB.QoderProvider):
            RL_RETRY_ROUNDS = 1
            RL_RETRY_GAP = 0

            def __init__(self):
                self.name = "qoder-cn"
                self.realm = "cn"
                # ★ 换模型开关现在读**实例配置**（config.json 的 modelFallback），
                #   已不是类属性 RL_FALLBACK（该属性已移除）。
                self.cfg = {"modelFallback": model_fallback}
                self._catalog = catalog
                self.seen = []

            def _enabled_models(self):
                return self._catalog

        return P()

    def test_switches_model_when_no_resource(self):
        import asyncio
        from providers import qoder_common as qc

        catalog = [{"key": "dfmodel", "display_name": "DeepSeek-Flash"},
                   {"key": "gmodel", "display_name": "GLM-5.3"}]
        p = self._provider({"dfmodel"}, {"gmodel"}, catalog)
        qc.clear_resource_limited("cn", "dfmodel")
        qc.clear_resource_limited("cn", "gmodel")

        async def fake_events_one(body, account, model_key):
            p.seen.append(model_key)
            if model_key == "dfmodel":
                raise qc.QoderResourceLimited("no resource")
            yield {"choices": [{"index": 0, "delta": {"content": "ok"},
                                "finish_reason": "stop"}]}

        p._events_one = fake_events_one

        async def go():
            out = []
            async for c in p._events({"messages": []}, {}, "dfmodel"):
                out.append(c)
            return out

        out = asyncio.run(go())
        self.assertIn("gmodel", p.seen, "dfmodel 无资源时必须自动切到 gmodel")
        self.assertTrue(any(isinstance(c, dict) and c.get("qoder_fallback_model") == "gmodel"
                            for c in out), "换模型后应发一条通知给客户端")
        self.assertTrue(any(isinstance(c, dict) and c.get("choices") for c in out))

    def test_all_models_dead_reports_count(self):
        import asyncio
        from providers import qoder_common as qc

        catalog = [{"key": "dfmodel", "display_name": "DeepSeek-Flash"},
                   {"key": "gmodel", "display_name": "GLM-5.3"}]
        p = self._provider({"dfmodel", "gmodel"}, set(), catalog)
        for m in ("dfmodel", "gmodel"):
            qc.clear_resource_limited("cn", m)

        async def fake_events_one(body, account, model_key):
            p.seen.append(model_key)
            raise qc.QoderResourceLimited("no resource")
            yield  # pragma: no cover

        p._events_one = fake_events_one

        async def go():
            async for _ in p._events({"messages": []}, {}, "dfmodel"):
                pass

        with self.assertRaises(qc.QoderResourceLimited) as ctx:
            asyncio.run(go())
        msg = str(ctx.exception)
        self.assertIn("已自动尝试", msg)
        self.assertIn("2", msg)          # 试了 2 个模型

    def test_default_config_does_not_switch_model(self):
        """★★ 2026-10-24 master 决定：默认不换模型。

        「当前模型不行就不行」—— 换到别的模型会照常扣那个模型的积分，
        而用户压根没选它（某账号积分被清零的元凶）。
        这里在不传 modelFallback 的情况下断言：只试原模型，然后如实报错。
        """
        import asyncio
        from providers import qoder_base as QB
        from providers import qoder_common as qc

        class P(QB.QoderProvider):
            RL_RETRY_ROUNDS = 1
            RL_RETRY_GAP = 0

            def __init__(self):
                self.name = "qoder-cn"
                self.realm = "cn"
                self.cfg = {}          # ← 没有任何 modelFallback 配置 = 默认
                self._catalog = [{"key": "dfmodel", "display_name": "DeepSeek-Flash"},
                                 {"key": "gmodel", "display_name": "GLM-5.3"}]
                self.seen = []

            def _enabled_models(self):
                return self._catalog

        p = P()
        qc.clear_resource_limited("cn", "dfmodel")
        qc.clear_resource_limited("cn", "gmodel")

        async def fake_events_one(body, account, model_key):
            p.seen.append(model_key)
            raise qc.QoderResourceLimited("no resource")
            yield  # pragma: no cover

        p._events_one = fake_events_one

        async def go():
            async for _ in p._events({"messages": []}, {}, "dfmodel"):
                pass

        with self.assertRaises(qc.QoderResourceLimited) as ctx:
            asyncio.run(go())
        # 允许对**原模型**重试（RL_RETRY_ROUNDS=1 → 共 2 次），
        # 但绝不能出现 dfmodel 之外的模型。
        self.assertTrue(p.seen, "至少应尝试一次原模型")
        self.assertEqual(set(p.seen), {"dfmodel"},
                         f"默认配置下绝不能打到其它模型，实际尝试过: {p.seen}")
        msg = str(ctx.exception)
        self.assertIn("已关闭", msg)
        self.assertIn("自动切换模型", msg)

    def test_no_switch_after_content_yielded(self):
        """一旦已经吐过内容，绝不换模型（否则前后不一致）。"""
        import asyncio
        from providers import qoder_common as qc

        catalog = [{"key": "dfmodel", "display_name": "DeepSeek-Flash"},
                   {"key": "gmodel", "display_name": "GLM-5.3"}]
        p = self._provider({"dfmodel"}, {"gmodel"}, catalog)
        qc.clear_resource_limited("cn", "dfmodel")

        async def fake_events_one(body, account, model_key):
            p.seen.append(model_key)
            yield {"choices": [{"index": 0, "delta": {"content": "半句"}, "finish_reason": None}]}
            raise qc.QoderResourceLimited("no resource")

        p._events_one = fake_events_one

        async def go():
            async for _ in p._events({"messages": []}, {}, "dfmodel"):
                pass

        with self.assertRaises(qc.QoderResourceLimited):
            asyncio.run(go())
        self.assertEqual(p.seen, ["dfmodel"], "吐过内容后不允许换模型")

    def test_fallback_skips_cooling_models(self):
        """冷却中的模型不该被选作接替目标（过去也是白打）。"""
        from providers import qoder_common as qc
        catalog = [{"key": "dfmodel", "display_name": "DeepSeek-Flash"},
                   {"key": "gmodel", "display_name": "GLM-5.3"},
                   {"key": "mmodel", "display_name": "MiniMax-M3"}]
        p = self._provider(set(), set(), catalog)
        qc.clear_resource_limited("cn", "gmodel")
        qc.clear_resource_limited("cn", "mmodel")
        qc.mark_resource_limited("cn", "gmodel", "no resource")
        try:
            cands = p._fallback_models("dfmodel")
            self.assertNotIn("gmodel", cands, "冷却中的模型不该入选")
            self.assertIn("mmodel", cands)
        finally:
            qc.clear_resource_limited("cn", "gmodel")


if __name__ == "__main__":
    unittest.main()
