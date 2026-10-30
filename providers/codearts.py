# -*- coding: utf-8 -*-
"""codearts.py —— 华为云 CodeArts Agent（码道）Provider —— 对外模型前缀 ``ca-``

移植自开源项目 codearts2api（MIT，https://github.com/HITZY2002/codearts2api）的
``internal/upstream``（Go）中的**必要部分**，按 open-ai 的 provider 形态用纯标准库
Python 重写：AK/SK 签名、DSOoP、OAuth2 换 token、SSE 解析、模型发现、签到领取。

上游链路（逆向自 CodeArts Agent 26.7.0 / vscode-codebot 扩展）：

    登录    华为云 CodeArts OAuth2（PKCE）→ snap-manager/sts 换 STS 临时凭证
    推理    POST {snap}/api/v2/chat/completions
            header x-auth-token = security_token + 华为云 SDK-HMAC-SHA256 签名
            （签名头 SignedHeaders = 请求全部头，含 maas_type；CanonicalURI 末尾补 /）
    模型    三路并集：agent-center detail / v1/model/builtin / 福利网关
    签到    GET  /v1/ops/delivery?channel=IDE  → POST /v1/ops/claim → /v1/ops/confirm
            （AK/SK 签名 + X-Security-Token + Agent-Type: PromptCenter）

关键实测结论（照搬自参考项目 docs/reverse-engineering.md，不得凭直觉改）：

  * 签名**不是** x-auth-token 单独鉴权 —— APIG 会拒（APIG.0301 decrypt token fail），
    必须走 AK/SK SDK-HMAC-SHA256，且 SignedHeaders 覆盖全部请求头；
  * chat_id 必须 32 位十六进制（uuid 去连字符），否则「请求参数错误: chat_id」；
  * 限时福利模型必须带 ``maas_type: benefit`` 头，且该头必须在**签名前**设置
    （计入 SignedHeaders），否则 InferHub.002002009.404 model is not registered；
  * refresh_token 与 client_id + DPoP 公钥三者绑定，且一次性轮换 ——
    三者必须随账号一起落盘，刷新时原样复用并把新 refresh_token 写回。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import itertools
import json
import logging
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import httpx

from .base import (
    AccountHealth, Provider, describe_net_exc, make_chunk_id,
    make_usage_chunk, normalize_usage, wants_usage_in_stream,
)
from . import cred_store
from . import tool_echo

logger = logging.getLogger("openapi.providers.codearts")

# ── 常量（逆向自 vscode-codebot / product.json） ──
SNAP_HOST = "https://snap-access.cn-north-4.myhuaweicloud.com"
SNAP_MANAGER = SNAP_HOST + "/snap-manager"
STSHOST = "https://sts.cn-north-4.myhuaweicloud.com"
PORTAL_HOST = "https://codearts.huaweicloud.com/portal"
BENEFIT_HOST = "https://opengw.developer.huaweicloud.com"
CLIENT_ID = "codearts-agent"
PLUGIN_NAME = "snap_AIIDE"
PLUGIN_VERSION = "5.2.0"

EP_CHAT_V2 = "/api/v2/chat/completions"
EP_CHAT_V1 = "/v1/chat/chat"
EP_MODEL_BUILTIN = "/v1/model/builtin"
EP_AGENT_LIST = "/v1/agent-center/agents/useragents"
EP_AGENT_DETAIL = "/v1/agent-center/agents/detail"
EP_OAUTH_TOKENS = "/v1/oauth2/tokens"
EP_CURRENT_USER = "/v1/current/user"
EP_BENEFIT_CONFIG = "/api/v1/gateway/config"
EP_BENEFIT_CLAIM = "/api/v1/benefit/claim"
# ★ 2026-10-23 从官方桌面端 app.asar（PackageInfoService / ModelService）挖到的真端点：
#   statistics/plugin → 套餐 + 积分（唯一可信的余额来源，见 fetch_credits）
#   benefit-gateway-config → 福利渠道是否启用（false 时福利模型根本不存在）
#   package/overview → 按需计费明细（实测 404 APIG.0101，保留但不用）
EP_STAT_PLUGIN = "/snap-manager/v1/statistics/plugin"
EP_PACKAGE_OVERVIEW = "/snap-manager/v1/package/overview"
EP_BENEFIT_GATEWAY = "/v1/benefit-gateway-config"
EP_EDUCATION_GATEWAY = "/v1/education-gateway-config"
# 「凭据过期」类错误的文本特征（APIG.0602 / STS5.1806 / …）。
# ★ 2026-10-30：抽成**模块级**函数 —— 采集器与 admin 积分刷新都没有 provider
#   实例可用来判断，只能看错误文本；过去它们各自没有这个判据，
#   于是 token 一过期就静默失败（见 usage_collector._codearts_used 注释）。
_EXPIRY_ERROR_TOKENS = ("apig.0602", "security token has expired",
                        "sts5.1806", "apig.0301", "decrypt token fail")


def is_expiry_error_text(text) -> bool:
    """错误文本/响应体是否属于「凭据过期」类。"""
    blob = str(text or "").lower()
    return any(t in blob for t in _EXPIRY_ERROR_TOKENS)


EP_CREDIT_DELIVERY = "/v1/ops/delivery"
EP_CREDIT_CLAIM = "/v1/ops/claim"
EP_CREDIT_CONFIRM = "/v1/ops/confirm"

# 并发会话上限的重试预算（env 可调，便于线上加压验证）
# 实测：滚动窗口 ≈3 条 / 60 秒，30 秒间隔可稳定通过（2026-10-23）
CONCURRENCY_WAIT_SECS = float(os.environ.get("CA_CONCURRENCY_WAIT") or 25)
CONCURRENCY_MAX_ROUNDS = int(os.environ.get("CA_CONCURRENCY_ROUNDS") or 3)
CONCURRENCY_BUDGET_SECS = float(os.environ.get("CA_CONCURRENCY_BUDGET") or 180)

# 模型检测的探测节流（见 ProbeGate）：探活是**极短请求**，最容易把名额打满
PROBE_GAP_SECS = float(os.environ.get("CA_PROBE_GAP") or 22)
PROBE_CACHE_TTL = float(os.environ.get("CA_PROBE_CACHE_TTL") or 300)

PREFIX = "ca-"
SIGN_ALG = "SDK-HMAC-SHA256"
HEADER_MAAS_TYPE = "maas_type"
MAAS_BENEFIT = "benefit"

# 冷启动种子：**默认没有任何福利模型**。
#
# ★ 2026-10-23 事故（master 反馈「模型状态一直繁忙」）：这里原来写死了
#   ("deepseek-v4-flash-0731", "deepseek-v4-pro-0813", "glm-5.3-flash")，于是任何
#   名字撞上的模型都会被加上 `maas_type: benefit` 头。而上游目录早已换代
#   （deepseek-v4.1-flash / glm-5.3 / kimi-k3 都是**普通内置模型**，倍率 0.18x /
#   0.79x / 2.38x），带着福利头打过去只会得到 `InferHub.4004.200 benefit not found`
#   —— 再被归类成「繁忙」，看起来像通道挂了，实际是**我们自己发错了头**。
#   福利模型只能来自上游网关配置（enabled 时才有），不许再猜。
SEED_BENEFIT_MODELS: tuple[str, ...] = ()
# 已知内置模型（大小写归一 + 断网兜底展示）—— 与 /v1/model/builtin 实测目录同步
SEED_KNOWN_MODELS = (
    "GLM-5.2", "GLM-5.2-ArkTS-SPARK", "GLM-5.3",
    "OpenPangu-2.0-Pro", "OpenPangu-2.0-Flash",
    "DeepSeek-V4.1-Flash", "Kimi-K2.6-VL", "Kimi-K3", "Qwen3-VL-235B",
)
# 展示名 → 上游内部名（model_id）。上游只认 model_id：直接发明名会得到
# `InferHub.002002009.404 The model is not registered`（2026-10-23 实测）。
SEED_MODEL_KEYS = {
    "glm-5.2": "GLM-5.2",
    "glm-5.2-arkts-spark": "glm-5.2-sft-harmony",
    "glm-5.3": "glm-5.3",
    "openpangu-2.0-pro": "openpangu-2.0-pro",
    "openpangu-2.0-flash": "openpangu-2.0-flash",
    "deepseek-v4.1-flash": "deepseek-v4.1-flash",
    "kimi-k2.6-vl": "kimi-k2.6-vl",
    "kimi-k3": "kimi-k3",
    "qwen3-vl-235b": "Qwen3-VL-235B",
}


# ═══════════════════════════ 华为云 AK/SK 签名 ═══════════════════════════

def _sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _hmac_hex(key: str, msg: str) -> str:
    return hmac.new(key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def _canonical_uri(path: str) -> str:
    """每段 percent-encode 且末尾补 "/"（对齐 JS CanonicalURI）。"""
    segs = [urllib.parse.quote(s, safe="~-_.!*'()") for s in (path or "/").split("/")]
    out = "/".join(segs)
    if not out.endswith("/"):
        out += "/"
    return out


def _canonical_query(raw: str) -> str:
    if not raw:
        return ""
    try:
        vals = urllib.parse.parse_qs(raw, keep_blank_values=True)
    except Exception:  # noqa: BLE001
        return raw
    parts = []
    for k in sorted(vals):
        for v in sorted(vals[k]):
            parts.append(f"{urllib.parse.quote(k, safe='~-_.')}"
                         f"={urllib.parse.quote(v, safe='~-_.')}")
    return "&".join(parts)


def sign_headers(method: str, url: str, headers: dict, body: bytes,
                 ak: str, sk: str, security_token: str = "") -> dict:
    """给请求加 X-Sdk-Date / X-Security-Token / X-Sdk-Content-Sha256 / Authorization。"""
    u = urllib.parse.urlsplit(url)
    h = {k: v for k, v in (headers or {}).items() if v is not None}
    x_date = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    h["X-Sdk-Date"] = x_date
    h["Host"] = u.netloc
    if security_token:
        h["X-Security-Token"] = security_token
    payload_hash = _sha256_hex(body or b"")
    h["X-Sdk-Content-Sha256"] = payload_hash

    keys = sorted(k.lower() for k in h)
    canonical_headers = "".join(f"{k}:{str(h[nk]).strip()}\n"
                                for k, nk in ((k.lower(), k) for k in h))
    # 上面按 h 的插入顺序枚举，必须显式按 key 排序后取值
    by_lower = {k.lower(): k for k in h}
    canonical_headers = "".join(
        f"{k}:{str(h[by_lower[k]]).strip()}\n" for k in keys)
    signed = ";".join(keys)
    canonical_request = "\n".join([
        method.upper(), _canonical_uri(u.path), _canonical_query(u.query),
        canonical_headers, signed, payload_hash,
    ])
    string_to_sign = "\n".join(
        [SIGN_ALG, x_date, _sha256_hex(canonical_request.encode("utf-8"))])
    signature = _hmac_hex(sk, string_to_sign)
    h["Authorization"] = (f"{SIGN_ALG} Access={ak}, SignedHeaders={signed}, "
                          f"Signature={signature}")
    return h


# ═══════════════════════════ DPoP（RFC 9449，ES256 / P-256） ═══════════════════════════
# 纯标准库实现椭圆曲线：华为 STS 把 refresh_token 绑到首次换 token 时的 DPoP 公钥，
# 刷新时必须复用同一把私钥，否则 400 InvalidDPoPHeader。项目不带 cryptography 依赖，
# 故这里自实现 P-256 点运算 + ECDSA 签名（确定性 k 由 RFC 6979 式 HMAC 派生，够用且安全）。

_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_A = _P - 3
_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
_GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5


def _inv(a: int, m: int = _P) -> int:
    return pow(a, m - 2, m)


def _pt_add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    if p1 == p2:
        lam = (3 * x1 * x1 + _A) * _inv(2 * y1) % _P
    else:
        lam = (y2 - y1) * _inv(x2 - x1) % _P
    x3 = (lam * lam - x1 - x2) % _P
    return (x3, (lam * (x1 - x3) - y1) % _P)


def _pt_mul(k: int, pt):
    out = None
    add = pt
    while k:
        if k & 1:
            out = _pt_add(out, add)
        add = _pt_add(add, add)
        k >>= 1
    return out


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _i2b(v: int, size: int = 32) -> bytes:
    return v.to_bytes(size, "big")


def dpop_new_key() -> dict:
    """生成可持久化的 P-256 私钥 JWK。"""
    d = int.from_bytes(os.urandom(32), "big") % (_N - 1) + 1
    x, y = _pt_mul(d, (_GX, _GY))
    return {"kty": "EC", "crv": "P-256",
            "x": _b64url(_i2b(x)), "y": _b64url(_i2b(y)), "d": _b64url(_i2b(d))}


def _dpop_sign(key: dict, htu: str) -> str:
    """构造 DPoP 证明 JWT（htm=POST）。"""
    d = int.from_bytes(base64.urlsafe_b64decode(key["d"] + "=="), "big")
    x = int.from_bytes(base64.urlsafe_b64decode(key["x"] + "=="), "big")
    y = int.from_bytes(base64.urlsafe_b64decode(key["y"] + "=="), "big")
    header = {"alg": "ES256", "typ": "dpop+jwt",
              "jwk": {"kty": "EC", "crv": "P-256",
                      "x": _b64url(_i2b(x)), "y": _b64url(_i2b(y))}}
    payload = {"htm": "POST", "htu": htu, "iat": int(time.time()),
               "jti": uuid.uuid4().hex}
    signing_input = (_b64url(json.dumps(header, separators=(",", ":")).encode())
                     + "." + _b64url(json.dumps(payload, separators=(",", ":")).encode()))
    digest = hashlib.sha256(signing_input.encode("ascii")).digest()
    z = int.from_bytes(digest, "big")
    # RFC 6979 简化版：HMAC 派生确定性 k（避免 os.urandom 依赖并保证不重复）
    v = b"\x01" * 32
    k_ = b"\x00" * 32
    k_ = hmac.new(k_, v + b"\x00" + _i2b(d) + z.to_bytes(32, "big"), hashlib.sha256).digest()
    v = hmac.new(k_, v, hashlib.sha256).digest()
    k_ = hmac.new(k_, v + b"\x01" + _i2b(d) + z.to_bytes(32, "big"), hashlib.sha256).digest()
    v = hmac.new(k_, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k_, v, hashlib.sha256).digest()
        cand = int.from_bytes(v, "big")
        if 1 <= cand < _N:
            break
        k_ = hmac.new(k_, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k_, v, hashlib.sha256).digest()
    r_pt = _pt_mul(cand, (_GX, _GY))
    r = r_pt[0] % _N
    s = (_inv(cand, _N) * (z + r * d)) % _N
    if s > _N // 2:
        s = _N - s
    sig = _i2b(r) + _i2b(s)
    return signing_input + "." + _b64url(sig)


# ═══════════════════════════ HTTP 与业务调用 ═══════════════════════════

class ProbeGate:
    """模型探活的**节流 + 结果缓存**（CodeArts 专用，见 is_benefit_model 上方说明）。

    为什么要它：CodeArts 的并发会话上限是**滚动窗口**（≈3 条/60 秒），而「检查模型」
    发的是极短请求 —— 连续检查 4 个模型就把名额占满，之后每个都回
    `TM.00001041`，界面上就是「明明能用，却整列显示繁忙」（2026-10-23 master 反馈）。

    做法：探活串行 + 相邻两条至少间隔 ``PROBE_GAP_SECS``；成功结果缓存
    ``PROBE_CACHE_TTL`` 秒，重复检查直接返回缓存，一个名额都不占。
    失败结果**不缓存**（上游恢复后应立刻能看到）。
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._cache: dict[str, tuple[float, dict]] = {}

    def cached(self, model: str):
        hit = self._cache.get(str(model))
        if not hit:
            return None
        ts, res = hit
        if time.time() - ts > PROBE_CACHE_TTL:
            self._cache.pop(str(model), None)
            return None
        return {**res, "cached": True}

    async def acquire(self) -> None:
        """排队并补齐间隔（间隔在**计时开始前**完成，不占用探测超时）。"""
        await self._lock.acquire()
        gap = PROBE_GAP_SECS - (time.time() - self._last)
        if gap > 0:
            await asyncio.sleep(gap)

    def release(self, model: str = "", result: dict | None = None) -> None:
        self._last = time.time()
        if model and result and result.get("status") == "ok":
            self._cache[str(model)] = (time.time(), dict(result))
        if self._lock.locked():
            self._lock.release()

    def invalidate(self) -> None:
        self._cache.clear()
        self._last = 0.0


class ModelNotRegistered(RuntimeError):
    """上游回 InferHub.002002009.404 —— 该 model 名上游不认识。

    ★ 2026-10-23 更正（此前注释是错的）：这条错误**不代表**「需要福利头」。
    实测它只表示「名字不对」—— 上游只认 model_id（`deepseek-v4.1-flash`），
    发展示名（`DeepSeek-V4.1-Flash`）就会得到它。福利模型由网关配置决定，
    见 fetch_models / is_benefit_model。据此「补福利头重试」的老逻辑已删除。
    """


class BenefitUnavailable(RuntimeError):
    """带了福利头仍失败（benefit not found）—— 福利没领到。

    官方领取接口是 opengw 的 /api/v1/benefit/claim，但该网关现已
    回 `400 4000 invalid channel, group not found`（实测），
    所以这类模型当前**无法使用**，只能给出可读原因而不是笼统报错。
    """


class ConcurrencyLimited(RuntimeError):
    """上游并发会话数已达上限（TM.00001041，实测每账号 3 条）。"""


class CodeArtsError(RuntimeError):
    def __init__(self, status: int, body: str, path: str = "", code: int | None = None):
        super().__init__(f"codearts api code={code} http={status} path={path} msg={body[:200]}")
        self.status = status
        self.body = body or ""
        self.path = path
        self.code = code


def _truncate(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + "…"


def chat_headers(security_token: str, trace_id: str = "") -> dict:
    return {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "x-auth-token": security_token,
        "x-snap-traceid": trace_id or uuid.uuid4().hex,
        "X-Language": "zh-cn",
        "app-id": "CodeAgent3.0",
        "is_confidential": "false",
    }


def api_request(account: dict, method: str, url: str, body: bytes | None = None,
                extra_headers: dict | None = None, timeout: float = 60.0,
                expect_json: bool = True):
    """带 AK/SK 签名的同步请求（线程池里调用）。"""
    cred = account.get("credentials") or {}
    ak = str(cred.get("accessKeyId") or account.get("accessKeyId") or "")
    sk = str(cred.get("secretAccessKey") or account.get("secretAccessKey") or "")
    st = str(cred.get("securityToken") or account.get("securityToken") or "")
    if not ak or not sk:
        raise CodeArtsError(401, "账号缺少 AK/SK（请重新登录 CodeArts）")
    h = {"Content-Type": "application/json", "X-Language": "zh-cn"}
    h.update(extra_headers or {})
    signed = sign_headers(method, url, h, body or b"", ak, sk, st)
    req = urllib_request(url, method, signed, body)
    try:
        return req
    except CodeArtsError:
        raise


def urllib_request(url: str, method: str, headers: dict, body: bytes | None,
                   timeout: float = 60.0) -> dict:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            raw = ""
        raise CodeArtsError(exc.code, raw, urllib.parse.urlsplit(url).path) from None
    except Exception as exc:  # noqa: BLE001
        raise CodeArtsError(0, str(exc), urllib.parse.urlsplit(url).path) from None
    if not raw.strip():
        return {}
    try:
        d = json.loads(raw)
    except Exception:  # noqa: BLE001
        return {"_raw": raw, "_status": status}
    return d


def credit_request(account: dict, method: str, path: str, body: dict | None = None):
    """活动积分接口（snap 根路径，Agent-Type: PromptCenter）。"""
    raw = json.dumps(body).encode() if body is not None else b""
    h = {"Content-Type": "application/json", "Agent-Type": "PromptCenter",
         "X-Language": "zh-cn"}
    cred = account.get("credentials") or {}
    signed = sign_headers(method, SNAP_HOST + path, h, raw,
                          str(cred.get("accessKeyId") or ""),
                          str(cred.get("secretAccessKey") or ""),
                          str(cred.get("securityToken") or ""))
    env = urllib_request(SNAP_HOST + path, method, signed, raw if body is not None else None)
    code = env.get("code")
    if code is None:
        raise CodeArtsError(400, json.dumps(env, ensure_ascii=False),
                            path, code=None)
    if int(code) != 0:
        raise CodeArtsError(400, json.dumps(env, ensure_ascii=False), path, int(code))
    return env.get("data")


def signed_get(account: dict, url: str, agent_type: str = "") -> dict:
    h = {"Content-Type": "application/json", "X-Language": "zh-cn"}
    if agent_type:
        h["Agent-Type"] = agent_type
    cred = account.get("credentials") or {}
    signed = sign_headers("GET", url, h, b"",
                          str(cred.get("accessKeyId") or ""),
                          str(cred.get("secretAccessKey") or ""),
                          str(cred.get("securityToken") or ""))
    return urllib_request(url, "GET", signed, None)


# ═══════════════════════════ 登录（OAuth2 PKCE + DPoP） ═══════════════════════════

def _pkce() -> tuple[str, str]:
    verifier = _b64url(os.urandom(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def start_login(port: int) -> dict:
    """构造华为云授权 URL（浏览器打开；回调可由本地 127.0.0.1:port 或 ticket 轮询完成）。"""
    ticket_id = os.urandom(16).hex()
    secret = os.urandom(16).hex()
    verifier, challenge = _pkce()
    q = urllib.parse.urlencode({
        "theme": "2", "locale": "zh-cn", "uri_scheme": CLIENT_ID,
        "client_id": CLIENT_ID, "port": str(port),
        "code_challenge": challenge, "code_challenge_method": "SHA-256",
        "ticket_id": ticket_id, "plugin-name": PLUGIN_NAME,
        "plugin-version": PLUGIN_VERSION,
    })
    return {"ticketId": ticket_id, "secret": secret, "verifier": verifier,
            "port": port, "authUrl": PORTAL_HOST + "/authorize?" + q,
            "dpopKey": dpop_new_key()}


def exchange_code(code: str, verifier: str, port: int, dpop_key: dict) -> dict:
    """用授权码换 token（authorization_code）。"""
    form = urllib.parse.urlencode({
        "client_id": CLIENT_ID, "code": code, "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": f"http://127.0.0.1:{port}/oauth/callback",
    }).encode()
    return _request_token(form, dpop_key)


def refresh_token(account: dict) -> dict:
    """用 refresh_token 换新凭证（必须复用签发时的 DPoP 私钥与 client_id）。"""
    form = urllib.parse.urlencode({
        "client_id": str(account.get("clientId") or CLIENT_ID),
        "code_verifier": str(account.get("codeVerifier") or ""),
        "grant_type": "refresh_token",
        "refresh_token": str(account.get("refreshToken") or ""),
    }).encode()
    key = account.get("dpopPrivateKey") or {}
    if not key:
        raise CodeArtsError(400, "账号缺少 DPoP 私钥，无法续期（请重新登录）")
    return _request_token(form, key)


def _request_token(form: bytes, dpop_key: dict) -> dict:
    url = STSHOST + EP_OAUTH_TOKENS
    proof = _dpop_sign(dpop_key, url)
    req = urllib.request.Request(url, data=form, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "DPoP": proof})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            raw = ""
        try:
            d = json.loads(raw)
        except Exception:  # noqa: BLE001
            d = {}
        raise CodeArtsError(exc.code,
                            json.dumps(d, ensure_ascii=False)[:300] if d else raw[:300],
                            EP_OAUTH_TOKENS) from None
    except Exception as exc:  # noqa: BLE001
        raise CodeArtsError(0, str(exc), EP_OAUTH_TOKENS) from None
    _ = status
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        raise CodeArtsError(400, f"token 响应非 JSON: {raw[:200]}", EP_OAUTH_TOKENS) from None
    if not isinstance(data, dict):
        raise CodeArtsError(400, f"token 响应形状异常: {str(data)[:200]}", EP_OAUTH_TOKENS)
    # ★ 200 也可能是业务错误（华为云部分接口会这样返回）：
    #   不判这一层，调用方只会看到「缺少 user_id」这种**掩盖真因**的二次错误。
    #   2026-10-22 实测踩到：真实原因被吞掉，白白多花一轮排查。
    code = str(data.get("error_code") or data.get("errorCode") or "").strip()
    err = str(data.get("error") or "").strip()
    if err or (code and code not in ("0", "0000")):
        raise CodeArtsError(
            400,
            json.dumps({k: v for k, v in data.items()
                        if k not in ("credentials", "credential")},
                       ensure_ascii=False)[:300],
            EP_OAUTH_TOKENS)
    return data


def normalize_credentials(data: dict) -> dict:
    """从 token 响应里取出 STS 三件套（兼容新式 credentials / 旧式 credential）。

    输入非 dict（上游偶尔回字符串/数组）时返回空凭据，由调用方按「凭证缺失」处理 ——
    本函数是纯粹的形状归一化，不该在这里抛类型异常。
    """
    if not isinstance(data, dict):
        return {"accessKeyId": "", "secretAccessKey": "", "securityToken": "",
                "expiration": ""}
    cred = data.get("credentials") or {}
    legacy = data.get("credential") or {}
    if not str(cred.get("security_token") or "").strip() and legacy:
        cred = {"access_key_id": legacy.get("access"),
                "secret_access_key": legacy.get("secret"),
                "security_token": legacy.get("securitytoken"),
                "expiration": legacy.get("expires_at")}
    return {
        "accessKeyId": str(cred.get("access_key_id") or cred.get("accessKeyId") or ""),
        "secretAccessKey": str(cred.get("secret_access_key") or cred.get("secretAccessKey") or ""),
        "securityToken": str(cred.get("security_token") or cred.get("securityToken") or ""),
        "expiration": str(cred.get("expiration") or ""),
    }


def fetch_current_user(account: dict) -> dict:
    """用 AK/SK 查当前用户身份：GET {snap-manager}/v1/current/user。

    用途：换 token 的响应**偶尔不带 user_id**（实测 2026-10-22），但凭证本身是好的。
    此时身份可以另外查出来，没必要因为缺一个字段就让整次登录失败。
    返回 {user_id, user_name, domain_id}（失败返回空 dict，不抛）。
    """
    try:
        d = signed_get(account, SNAP_MANAGER + EP_CURRENT_USER) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("codearts current/user 查询失败: %s", e)
        return {}
    if not isinstance(d, dict):
        return {}
    node = d.get("data") if isinstance(d.get("data"), dict) else d
    return {
        "user_id": str(node.get("user_id") or node.get("userId") or ""),
        "user_name": str(node.get("user_name") or node.get("userName") or ""),
        "domain_id": str(node.get("domain_id") or node.get("domainId") or ""),
    }


def _pick(data: dict, *names):
    """从（可能嵌套的）响应里按多个候选字段名取第一个非空值。"""
    if not isinstance(data, dict):
        return ""          # 上游偶尔回数组/字符串，不能让调用方在这里炸
    nodes = [data]
    for key in ("data", "result", "user"):
        v = data.get(key)
        if isinstance(v, dict):
            nodes.append(v)
    for name in names:
        for node in nodes:
            v = node.get(name)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, (int, float)) and v:
                return str(v)
    return ""


def accounts_from_token_response(data: dict, verifier: str, dpop_key: dict,
                                 client_id: str = CLIENT_ID) -> dict:
    """把 oauth2/tokens 响应归一化成 config 里的账号 dict。

    ★ 2026-10-22 修正两处**掩盖真因**的实现：
      1) 凭证缺失时原先只报「缺少 user_id」，把真实业务错误吞掉 ——
         现在先判凭证，并把响应（去密）写进错误信息；
      2) user_id 缺失不再直接失败：改从多个候选字段找，仍找不到就用刚拿到的
         AK/SK 查 /snap-manager/v1/current/user 把身份补出来。
         望文生义的字段名假设（user_id 一定在顶层）是这次失败的根因之一。
    """
    if not isinstance(data, dict):
        raise CodeArtsError(400, f"token 响应不是 JSON 对象: {str(data)[:200]}",
                            EP_OAUTH_TOKENS)
    cred = normalize_credentials(data)
    if not cred["securityToken"] or not cred["accessKeyId"]:
        safe = {k: v for k, v in (data or {}).items()
                if k not in ("credentials", "credential", "refresh_token")}
        raise CodeArtsError(
            400, "token 响应里没有可用的 STS 凭证："
                 + json.dumps(safe, ensure_ascii=False)[:300], EP_OAUTH_TOKENS)
    uid = _pick(data, "user_id", "userId", "userid")
    uname = _pick(data, "user_name", "userName", "username")
    domain = _pick(data, "domain_id", "domainId")
    if not uid or not uname:
        info = fetch_current_user({"credentials": cred})
        uid = uid or info.get("user_id") or ""
        uname = uname or info.get("user_name") or ""
        domain = domain or info.get("domain_id") or ""
        if uid:
            logger.info("codearts 身份来自 current/user: %s", uid)
    if not uid:
        raise CodeArtsError(
            400, "无法确定账号身份（token 响应与 current/user 都没有 user_id）",
            EP_OAUTH_TOKENS)
    return {
        "uid": uid,
        "name": uname or uid[:12],
        "userName": uname,
        "domainId": domain,
        "refreshToken": _pick(data, "refresh_token", "refreshToken"),
        "codeVerifier": verifier,
        "clientId": client_id,
        "dpopPrivateKey": dpop_key,
        "credentials": cred,
        "expiresAt": _parse_expiry(cred.get("expiration")),
        "enabled": True,
        "source": "oauth",
    }


def _parse_expiry(text) -> int:
    s = str(text or "").strip()
    if not s:
        return int(time.time()) + 3600
    s = s.replace("Z", "+00:00")
    try:
        import datetime as _dt
        return int(_dt.datetime.fromisoformat(s).timestamp())
    except Exception:  # noqa: BLE001
        return int(time.time()) + 3600


def poll_ticket(ticket_id: str, secret: str) -> dict:
    """ticket 轮询（无浏览器回调时的兜底通道）。"""
    path = f"{EP_OAUTH_TOKENS.rsplit('/v1/', 1)[0]}/v1/login/ticket"
    url = SNAP_MANAGER + "/v1/login/ticket?" + urllib.parse.urlencode(
        {"ticket_id": ticket_id, "secret": secret})
    _ = path
    req = urllib.request.Request(url, method="GET", headers={
        "plugin-name": PLUGIN_NAME, "plugin-version": PLUGIN_VERSION,
        "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 202):
            return {"status": "pending"}
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        # TM.00001001「无效 ticketId」= 门户还没登记这张票（用户尚未在授权页完成
        # 登录）。这是**等待中**的正常状态，不是错误 —— 当成错误会让日志刷满
        # 假故障（2026-10-22 实测），也会误导「登录失败」的判断。
        if "TM.00001001" in body or "ticketId" in body:
            return {"status": "pending"}
        return {"status": "error", "message": f"ticket http {exc.code} {body[:160]}"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "pending", "message": str(exc)}
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return {"status": "pending"}
    return {"status": "ok", "data": data}


# ═══════════════════════════ 模型目录 ═══════════════════════════

def canonical_model(model: str) -> str:
    """模型**展示名**大小写归一（上游目录与前端都用展示名）。"""
    s = str(model or "").strip()
    if not s:
        return s
    for known in SEED_KNOWN_MODELS + SEED_BENEFIT_MODELS:
        if s.lower() == known.lower():
            return known
    return s


def seed_model_key(model: str) -> str:
    """展示名 → 上游内部名（model_id）的**内置**映射（目录还没拉到时兜底）。

    上游只认 model_id：发 `DeepSeek-V4.1-Flash` 得到
    ``InferHub.002002009.404 The model is not registered``，
    发 `deepseek-v4.1-flash` 才通（2026-10-23 逐个实测）。
    """
    s = str(model or "").strip()
    if not s:
        return s
    return SEED_MODEL_KEYS.get(s.lower(), canonical_model(s))


def _parse_ratio(text) -> float | None:
    """把上游倍率文本解析成数字：'0.7x' → 0.7、'0.05' → 0.05。"""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    t = str(text).strip().lower().replace("x", "").replace("×", "").strip()
    try:
        return float(t)
    except (TypeError, ValueError):
        return None


def fetch_models(account: dict) -> list[dict]:
    """模型目录。**以 /v1/model/builtin 为主源**（2026-10-22 重写）。

    为什么换主源：
      * builtin 实测可用且信息最全 —— 每条带 `model_id`（上游内部名）、
        `model_name`（展示名）、`display_enabled`（官方 UI 是否展示）、
        `credit[].ratio_display`（**积分倍率**，见 rate_table）；
      * agent-center 的 useragents 对 `Agent-Type: PromptCenter` 直接报
        「未找到合法的路径api」，本轮实测确认不可用；
      * 福利网关（opengw.developer）现在对任何路径都回
        `400 4000 invalid channel, group not found` —— 该来源已失效，
        保留但设为「非致命」，不能再让它决定模型列表的成败。

    返回 [{id, key, name, benefit, display_enabled, rate, desc, supports_images}]
      * id   = 展示名（官方 UI 与聊天接口都用它，例如 DeepSeek-V4.1-Flash）
      * key  = 上游内部名（glm-5.2-sft-harmony 这类，仅排障用）
    """
    out: list[dict] = []
    errs: list[str] = []

    # ── 主源：内置模型 ──
    try:
        d = signed_get(account, SNAP_HOST + EP_MODEL_BUILTIN,
                       agent_type="PromptCenter") or {}
        for m in d.get("builtinModels") or []:
            if not isinstance(m, dict):
                continue
            mid = str(m.get("model_id") or "").strip()
            name = str(m.get("model_name") or "").strip() or mid
            if not name:
                continue
            rate = None
            for c in (m.get("credit") or []):
                if isinstance(c, dict):
                    rate = _parse_ratio(c.get("ratio_display")) \
                        or _parse_ratio(c.get("ratio"))
                    if rate is not None:
                        break
            out.append({
                "id": name, "key": mid, "name": name, "benefit": False,
                "display_enabled": m.get("display_enabled") is not False,
                "rate": rate,
                "desc": str(m.get("model_desc") or ""),
                "supports_images": bool(m.get("supports_images")),
            })
    except Exception as e:  # noqa: BLE001
        errs.append(f"builtin: {e}")

    # ── 次源：福利/教育渠道（**先问网关配置**，enabled=false 时根本没有这些模型）──
    #
    # ★ 2026-10-23 按官方客户端 ModelService.getFreeBenefitModels 的真实逻辑重写：
    #   1) 先 GET /v1/benefit-gateway-config（policyCenter 域，签名不带 Agent-Type）
    #   2) 只有 `enabled: true` 才去 {BENEFIT_HOST}/api/v1/gateway/config 拉模型
    #   3) 拉到的模型统一标记 `maas_type: benefit`（这才是真正的福利模型）
    #   老实现无条件 try claim_benefit + gateway/config —— 该账号 gateway 是 disabled，
    #   于是每次都刷一条 "invalid channel, group not found" 告警，纯噪音。
    benefit_ok = False
    for channel, cfg_path in (("benefit", EP_BENEFIT_GATEWAY),
                              ("education", EP_EDUCATION_GATEWAY)):
        try:
            gate = signed_get(account, SNAP_HOST + cfg_path) or {}
            if not gate.get("enabled"):
                logger.debug("codearts %s 渠道未启用，跳过福利模型", channel)
                continue
            url = BENEFIT_HOST + EP_BENEFIT_CONFIG
            if channel != "benefit":
                url += "?channel=" + channel
            d = signed_get(account, url) or {}
            if str(d.get("error_code") or "0000") != "0000":
                raise RuntimeError(str(d.get("error_msg") or d)[:120])
            for m in (d.get("result") or {}).get("models") or []:
                mid = str(m.get("model_id") or m.get("model_name") or "").strip()
                name = str(m.get("model_name") or mid).strip() or mid
                if not mid:
                    continue
                out.append({"id": name, "key": mid, "name": name, "benefit": True,
                            "display_enabled": True, "rate": None,
                            "desc": str(m.get("model_desc") or m.get("description") or ""),
                            "supports_images": False})
            benefit_ok = True
        except Exception as e:  # noqa: BLE001
            errs.append(f"{channel}: {e}")

    # 去重（按展示名，大小写不敏感），builtin 优先
    merged: dict[str, dict] = {}
    for m in out:
        key = str(m["id"]).lower()
        prev = merged.get(key)
        if prev is None:
            merged[key] = m
        elif prev.get("benefit") and not m.get("benefit"):
            merged[key] = m
    result = list(merged.values())
    if not result:
        raise RuntimeError("CodeArts 模型目录为空: " + "; ".join(errs[:3]))
    if errs:
        logger.info("codearts 模型目录部分来源失败（不影响主源）: %s",
                    "; ".join(errs[:2]))
    _ = benefit_ok
    return result


def claim_benefit(account: dict) -> None:
    """领取限时福利模型（幂等；未领取时福利模型一律 4004.200 benefit not found）。"""
    raw = b"{}"
    h = {"Content-Type": "application/json", "X-Language": "zh-cn"}
    cred = account.get("credentials") or {}
    signed = sign_headers("POST", BENEFIT_HOST + EP_BENEFIT_CLAIM, h, raw,
                          str(cred.get("accessKeyId") or ""),
                          str(cred.get("secretAccessKey") or ""),
                          str(cred.get("securityToken") or ""))
    d = urllib_request(BENEFIT_HOST + EP_BENEFIT_CLAIM, "POST", signed, raw)
    code = str(d.get("error_code") or "0000")
    if code not in ("0000", "0"):
        raise CodeArtsError(400, json.dumps(d, ensure_ascii=False)[:200])


# ═══════════════════════════ 签到（每日福利 + 新用户礼） ═══════════════════════════

def fetch_campaigns(account: dict) -> list:
    d = credit_request(account, "GET", EP_CREDIT_DELIVERY + "?channel=IDE")
    items = (d or {}).get("items")
    if items is None:
        raise RuntimeError("活动接口缺少 data.items")
    return items if isinstance(items, list) else []


# 每日签到活动的 campaignId（实测 2026-10-22：id=1 为「每日签到领积分」）
# ★ 2026-10-28 事故：活动轮换了 —— 旧的 id=1「每日签到领积分」(+1000) 从 items
#   里消失，新的 id=34「每日签到领100积分」(+100) 出现且 claimable=True；
#   而 checkin 只认 id ∈ {1,4}，于是每天报「今日未签到」、一分不领（两个账号全挂）。
#   活动方随时会换 id、换面额（1000 → 100），按 id 写死必然复发，
#   所以改成按 **type** 识别（USER_LOGIN / NEW_USER_REGISTER），id 仅作兜底。
DAILY_CAMPAIGN_ID = 1
DAILY_CAMPAIGN_TYPE = "USER_LOGIN"
# 新用户注册礼（一次性）
NEW_USER_CAMPAIGN_ID = 4
NEW_USER_CAMPAIGN_TYPE = "NEW_USER_REGISTER"


def _is_daily_campaign(c: dict) -> bool:
    """是否「每日签到」活动（按 type 识别，id 兜底）。"""
    if str(c.get("type") or "").strip().upper() == DAILY_CAMPAIGN_TYPE:
        return True
    try:
        return int(c.get("campaignId")) == DAILY_CAMPAIGN_ID
    except (TypeError, ValueError):
        return False


def _is_new_user_campaign(c: dict) -> bool:
    """是否「新用户注册礼」（一次性；按 type 识别，id 兜底）。"""
    if str(c.get("type") or "").strip().upper() == NEW_USER_CAMPAIGN_TYPE:
        return True
    try:
        return int(c.get("campaignId")) == NEW_USER_CAMPAIGN_ID
    except (TypeError, ValueError):
        return False


def _campaign_amount(c: dict) -> float:
    for key in ("benefitAmount", "pendingTotalAmount"):
        try:
            v = c.get(key)
            if v not in (None, ""):
                return float(v)
        except (TypeError, ValueError):
            continue
    return 0.0


def checkin(account: dict) -> dict:
    """领取每日签到（type=USER_LOGIN）与新用户礼（type=NEW_USER_REGISTER）。

    返回 {ok, credited, credits, amount, already?, msg}
      * ok       = 本次调用是否正常跑完（**不代表签到成功**，见下）
      * credited = **今日是否确实已完成签到入账**（本次领到 / CONFIRMED / CLAIMED）
                   —— 调用方**只能**据此写 gain 表
      * credits  = **本次新领取**到的积分
      * amount   = **当日该活动的入账额**（「今天已领过」时也给）

    ★★ 2026-10-23 统一口径（与 qoder_common.checkin 同步）：原先调用方拿
      `ok` 当签到成功的判据，而 `ok` 只表示「没抛异常」（活动已结束/不可领取
      时同样是 True），与 Qoder 一样会把「没签到」渲染成「已签到」。
      现在单独给出 credited，调用方据此判定，ok 维持原语义不变。

    ★ 2026-10-22 修正：CONFIRMED 原先只记一句「已完成」就 continue，
      既不报 already 也不给 amount → 账号页永远显示「未签到」、积分恒 0。
      实测该账号今日状态就是 CONFIRMED（每日签到已于当天完成），
      于是「明明签了却显示未签」。现在按「今日已完成」处理并带上额度。
    """
    try:
        items = fetch_campaigns(account)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "credited": False, "credits": 0, "amount": 0,
                "msg": str(e)[:200]}
    now = int(time.time())
    total, msgs, ok = 0.0, [], True
    daily_amount, daily_done = 0.0, False
    for c in items:
        if not isinstance(c, dict):
            continue
        try:
            cid_int = int(c.get("campaignId"))
        except (TypeError, ValueError):
            continue
        is_daily = _is_daily_campaign(c)
        is_new = _is_new_user_campaign(c)
        if not (is_daily or is_new):
            continue
        unit = str(c.get("benefitUnit") or "")
        if unit and unit != "CREDIT":
            continue
        title = str(c.get("title") or f"积分活动 {cid_int}")
        status = str(c.get("status") or "").strip().upper()
        amount = _campaign_amount(c)
        extra = c.get("extra") or {}
        end = _parse_expiry(extra.get("endTime")) if extra.get("endTime") else 0
        start = _parse_expiry(extra.get("startTime")) if extra.get("startTime") else 0
        if end and now >= end:
            msgs.append(f"{title}: 活动已结束")
            continue
        if start and now < start:
            msgs.append(f"{title}: 活动未开始")
            continue
        if status in ("CONFIRMED", "CONSUMED"):
            # 今日已完成（后端按天重置，见活动接口的 serverTime 语义）
            msgs.append(f"{title}: 今日已完成" + (f" +{int(amount)}" if amount else ""))
            if is_daily:
                daily_done = True
                daily_amount += amount
            continue
        if status == "EXPIRED":
            msgs.append(f"{title}: 已过期")
            continue
        if status == "CLAIMED":
            # 已领未确认 → 只补确认（积分已入账，amount 就是当日额度）
            try:
                credit_request(account, "POST", EP_CREDIT_CONFIRM, {"campaignId": cid_int})
                msgs.append(f"{title}: 已补确认" + (f" +{int(amount)}" if amount else ""))
            except Exception as e:  # noqa: BLE001
                ok = False
                msgs.append(f"{title}: 确认失败 {str(e)[:80]}")
            if is_daily:
                daily_done = True
                daily_amount += amount
            continue
        if not c.get("claimable"):
            msgs.append(f"{title}: 不可领取({status or 'Unknown'})")
            continue
        key = f"claim_{cid_int}_{int(time.time() * 1000)}"
        try:
            res = credit_request(account, "POST", EP_CREDIT_CLAIM,
                                 {"campaignId": cid_int, "channel": "IDE",
                                  "idempotentKey": key}) or {}
        except Exception as e:  # noqa: BLE001
            ok = False
            msgs.append(f"{title}: 领取失败 {str(e)[:80]}")
            continue
        try:
            credit_request(account, "POST", EP_CREDIT_CONFIRM, {"campaignId": cid_int})
        except Exception:  # noqa: BLE001 — 确认失败不影响领取本身
            pass
        got = 0.0
        for k in ("benefitAmount", "pendingTotalAmount", "amount"):
            try:
                got = float(res.get(k) or 0)
            except (TypeError, ValueError):
                got = 0.0
            if got:
                break
        got = got or amount
        total += got
        msgs.append(f"{title}: +{int(got)}" if got else f"{title}: 已领取")
        if is_daily:
            daily_done = True
            daily_amount += got
    return {"ok": ok, "credited": bool(daily_done), "credits": int(total),
            "amount": int(daily_amount), "already": bool(daily_done and not total),
            "msg": "; ".join(msgs) or "无可领取的签到或新手活动"}


def fetch_credits(account: dict) -> tuple:
    """查账号剩余**积分**。返回 (credits|None, err)。

    ★ 2026-10-23：从官方桌面端 app.asar（PackageInfoService.doRequest）挖到真端点::

        GET {snapEngine}/snap-manager/v1/statistics/plugin   （AK/SK 签名，**不带 Agent-Type**）

    实测响应（本机账号，2026-10-23）::

        {
          "package": {"spec_code": "codearts.agent.individual.trial",
                      "package_name_cn": "体验版", "is_credit_package": true,
                      "status": "normal", "start_date": "2026-10-22", ...},
          "metrics": [
            {"name": "usageTotalPackageCredit",
             "package_credit_amount": 6500.0, "package_credit_used": 4.69,
             "package_credit_remain": 6495.31, "show": true},
            {"name": "usageBasicPackageCredit",  ... 500  ...},
            {"name": "usageBonusPackageCredit",  ... 6000 ...}
          ],
          "show": {"metrics": true, "package": true}
        }

    取值口径：**优先 `usageTotalPackageCredit.package_credit_remain`**
    （= 基础 500 + 赠送 6000 − 已用 4.69 = 6495.31，与网页门户「积分」一致）；
    没有该条时退化为「基础 + 赠送」两条相加，再没有就用 total − used。

    ⚠️ 此前这里直接返回 None 并写「CodeArts 未提供余额接口」——那是**错的**：
    真正的余额接口一直存在，只是不在 opengw（那个网关对任何路径都回
    `invalid channel, group not found`），而在 snap-manager 的 statistics 上。
    """
    try:
        d = signed_get(account, SNAP_HOST + EP_STAT_PLUGIN) or {}
    except Exception as e:  # noqa: BLE001
        return None, f"statistics/plugin 请求失败: {str(e)[:160]}"

    metrics = [m for m in (d.get("metrics") or []) if isinstance(m, dict)]
    by_name = {str(m.get("name") or ""): m for m in metrics}

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    total = by_name.get("usageTotalPackageCredit") or {}
    remain = _num(total.get("package_credit_remain"))
    if remain is not None:
        pkg = d.get("package") or {}
        logger.info("codearts 积分: 剩余 %.2f（套餐 %s / 总量 %s / 已用 %s）",
                    remain, pkg.get("package_name_cn") or pkg.get("spec_code") or "-",
                    total.get("package_credit_amount"), total.get("package_credit_used"))
        return remain, ""

    # 退化路径 1：基础 + 赠送
    parts = [by_name.get("usageBasicPackageCredit"),
             by_name.get("usageBonusPackageCredit"),
             by_name.get("usageOnDemandPackageCredit")]
    vals = [_num((p or {}).get("package_credit_remain")) for p in parts]
    vals = [v for v in vals if v is not None]
    if vals:
        return sum(vals), ""

    # 退化路径 2：total − used
    amount = _num(total.get("package_credit_amount"))
    used = _num(total.get("package_credit_used"))
    if amount is not None and used is not None:
        return amount - used, ""

    return None, ("statistics/plugin 里没有积分字段（套餐 %s；metrics=%s）"
                  % ((d.get("package") or {}).get("spec_code"),
                     [m.get("name") for m in metrics][:8]))


def fetch_credit_stats(account: dict) -> tuple:
    """取 CodeArts 的**归一化积分读数**（给流水采集算「当日差值」用）。

    返回 ``({"used":…, "remain":…, "total":…, "package":…}, err)``。

    ★ 为什么另开一个函数，而不改 `fetch_credits()`：
      两者打的是**同一个端点**（`snap-manager/v1/statistics/plugin`），但口径不同 ——
        `fetch_credits()`     → 只要「剩余」，账号页拿它显示余额；
        `fetch_credit_stats()` → 还要「**已用**」，采集器用它算当天消耗。
      改 `fetch_credits()` 的返回值会牵连既有调用方与测试
      （tests/test_codearts_catalog.py 直接断言它的返回），所以这里**不动它**。

    实测响应形状见 `fetch_credits()` 的注释（metrics 里
    `usageTotalPackageCredit.package_credit_used` 即累计已用）。
    取不到时返回 ``({}, 原因)`` —— 调用方必须能容忍空 dict。
    """
    try:
        d = signed_get(account, SNAP_HOST + EP_STAT_PLUGIN) or {}
    except Exception as e:  # noqa: BLE001
        return {}, f"statistics/plugin 请求失败: {str(e)[:160]}"

    metrics = [m for m in (d.get("metrics") or []) if isinstance(m, dict)]
    by_name = {str(m.get("name") or ""): m for m in metrics}

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    total_node = by_name.get("usageTotalPackageCredit") or {}
    used = _num(total_node.get("package_credit_used"))
    if used is None:
        # 退化：基础 + 赠送 + 按量，三条各自的 used 相加
        parts = [by_name.get("usageBasicPackageCredit"),
                 by_name.get("usageBonusPackageCredit"),
                 by_name.get("usageOnDemandPackageCredit")]
        vals = [_num((p or {}).get("package_credit_used")) for p in parts]
        vals = [v for v in vals if v is not None]
        if vals:
            used = sum(vals)
    if used is None:
        return {}, ("statistics/plugin 里没有「已用」字段（套餐 %s；metrics=%s）"
                    % ((d.get("package") or {}).get("spec_code"),
                       [m.get("name") for m in metrics][:8]))

    out = {"used": used}
    remain = _num(total_node.get("package_credit_remain"))
    amount = _num(total_node.get("package_credit_amount"))
    if remain is not None:
        out["remain"] = remain
    if amount is not None:
        out["total"] = amount
    pkg = (d.get("package") or {}).get("spec_code")
    if pkg:
        out["package"] = str(pkg)
    return out, ""


# ═══════════════════════════ SSE 解析 ═══════════════════════════

def _raise_codearts_error(code: str, msg: str):
    """把上游错误码翻译成专用异常（便于上层做「补福利头重试」等决策）。"""
    low = f"{code} {msg}".lower()
    if "002002009" in code or "not registered" in low:
        raise ModelNotRegistered(f"{code} {msg}".strip())
    if "4004.200" in code or "benefit not found" in low:
        raise BenefitUnavailable(f"{code} {msg}".strip())
    if "tm.00001041" in low or "并发" in msg or "concurrent" in low:
        raise ConcurrencyLimited(f"{code} {msg}".strip())
    raise RuntimeError(f"codearts upstream error_code={code} msg={msg}")


def _scan_line(line: str, pending_event: str) -> tuple[str, str, bool]:
    """解析一行 SSE，返回 (event, data, ok)；ok=False 表示这行不构成事件（继续读）。"""
    s = (line or "").rstrip("\r\n")
    if not s.strip():
        # 空行 = 事件分隔：CodeArts 实测无空行分隔，这里保持宽松（不发事件）
        return pending_event, "", False
    if s.startswith("event:"):
        return s[6:].strip(), "", False
    if s.startswith("data:"):
        return pending_event, s[5:].strip(), True
    if s.startswith(":") or s.startswith("id:") or s.startswith("retry:"):
        return pending_event, "", False
    # 部分帧直接是裸 JSON（实测存在）
    if s.lstrip().startswith("{") or s == "[DONE]":
        return pending_event, s.strip(), True
    return pending_event, "", False


def _delta_from(payload: dict) -> tuple[dict | None, str]:
    if isinstance(payload.get("delta"), dict):
        return payload["delta"], ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        c0 = choices[0] if isinstance(choices[0], dict) else {}
        return (c0.get("delta") if isinstance(c0.get("delta"), dict) else None,
                str(c0.get("finish_reason") or ""))
    return None, ""


class CodeArtsProvider(Provider):
    name = "codearts"
    PREFIX = PREFIX

    CATALOG_TTL = 3600

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.accounts = list(self.cfg.get("accounts") or [])
        self.timeout = float(self.cfg.get("timeout", 600.0))
        self.switch_every = max(1, int(self.cfg.get("switchEvery", 5)))
        self.default_model = self.cfg.get("defaultModel") or "GLM-5.2"
        self.benefit_auto_claim = self.cfg.get("benefit_auto_claim", True) is not False
        self._req_count = 0
        self._client: httpx.AsyncClient | None = None
        self._health = AccountHealth()
        self._models: list[dict] = [
            {"id": m, "key": seed_model_key(m), "benefit": False}
            for m in SEED_KNOWN_MODELS
        ] + [{"id": m, "key": seed_model_key(m), "benefit": True}
             for m in SEED_BENEFIT_MODELS]
        self._models_at = 0.0
        self._models_lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()
        # 运行期学会的「该模型需要福利头」集合（现在只由目录里的 benefit 标记填充；
        # 不再从 002002009.404 反推 —— 那其实是 model 名不对，见 ModelNotRegistered）。
        self._benefit_models: set[str] = set()
        # 探活节流（滚动窗口限流，见 ProbeGate）
        self.probe_gate = ProbeGate()
        # 探活允许的更长时间：撞上并发上限时要先等一轮（默认 25s）再重试，
        # 全局 30s 的探测上限不够用 —— 否则「其实是排到队了」会被显示成超时繁忙。
        self.probe_timeout = float(os.environ.get("CA_PROBE_TIMEOUT") or 90)

    # ---------- 账号 ----------
    def cfg_key(self) -> str:
        return self.cfg.get("_cfg_key") or "codearts"

    def _load_accounts(self) -> list:
        """从 config.json 实时读账号；**内存里已续期的凭据优先**。

        ★ 2026-10-23 事故修正：老实现无条件 ``self.accounts = fresh`` —— 于是
          刚续期成功的新凭据在下一次请求就被磁盘旧值覆盖。华为云 STS 的
          refresh token 是**单次使用**的，被覆盖的新 token 立刻失联，旧 token
          又已作废 → 账号只能重新登录（`ca-` 全通道 401，现场见 cred_store 文档）。
          现在按 uid 合并：内存凭据更新（credentials_newer）就保留内存那份。
        """
        try:
            with open(_config_path(), encoding="utf-8") as f:
                seg = (json.load(f).get("providers") or {}).get(self.cfg_key()) or {}
            fresh = seg.get("accounts") or []
            if fresh:
                by_uid = {str(a.get("uid") or ""): a for a in self.accounts
                          if a.get("uid")}
                for acc in fresh:
                    old = by_uid.get(str(acc.get("uid") or ""))
                    if old and cred_store.credentials_newer(old, acc):
                        for k in ("refreshToken", "credentials", "expiresAt",
                                  "refreshedAt"):
                            if old.get(k) not in (None, ""):
                                acc[k] = old[k]
                self.accounts = fresh
        except Exception:  # noqa: BLE001
            pass
        for a in self.accounts:
            cred = a.get("credentials") or {}
            self._health.observe_token(a.get("uid"), cred.get("securityToken"))
        return self.accounts

    def _pick_account(self, model: str = "") -> dict:
        want_benefit = self.is_benefit_model(model)
        accs = [a for a in self._load_accounts()
                if a.get("enabled", True) is not False
                and not self._health.is_invalid(a.get("uid"))]
        if not accs:
            return {}
        if want_benefit:
            # 福利模型按账号授予：优先挑本账号目录里确有该模型的账号，
            # 否则混合池里没这模型的账号会先吃一次 404 再换号
            capable = [a for a in accs
                       if any(str(m.get("id")).lower() == str(model).replace(PREFIX, "").lower()
                              for m in (a.get("models") or []))]
            if capable:
                accs = capable
        self._req_count += 1
        return accs[(self._req_count - 1) // self.switch_every % len(accs)]

    def get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=30.0),
                transport=httpx.AsyncHTTPTransport(retries=2))
        return self._client

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------- 模型 ----------
    def is_benefit_model(self, model: str) -> bool:
        """该模型是否需要 `maas_type: benefit` 头。

        判定顺序：
          1) 运行期学到的集合（最可靠 —— 来自上游 002002009.404 的实证）；
          2) builtin 目录里的 benefit 标记（builtin 现在都标 false，基本用不上）；
          3) 仓库里的冷启动种子。
        """
        key = str(model or "").strip().lower()
        if key.startswith(PREFIX):
            key = key[len(PREFIX):]
        for learned in self._benefit_models:
            lk = learned.lower()
            if lk == key or lk == f"{PREFIX}{key}".lower():
                return True
        for m in self._models:
            if str(m.get("id")).lower() == key:
                return bool(m.get("benefit"))
        # ★ 2026-10-23：**不再**回落到 SEED_BENEFIT_MODELS。
        #   种子写死的名字会和后来的普通模型撞名（deepseek-v4-flash-0731 撞
        #   DeepSeek-V4.1-Flash 这一族），一旦误判就会给普通模型加福利头 →
        #   `4004.200 benefit not found` → 被归类成「繁忙」。目录里没标 benefit
        #   就是普通模型，这是唯一可信来源。
        return False

    def model_key(self, model: str) -> str:
        """展示名 → 上游 model_id（发请求**必须**用这个）。

        2026-10-23 实测：`DeepSeek-V4.1-Flash` 直接发会得到
        `InferHub.002002009.404 The model is not registered`，换成目录里的
        `deepseek-v4.1-flash` 立刻正常。老代码把展示名直接发出去，再把 404
        误判成「需要福利头」，于是整条通道看起来全是繁忙。
        """
        key = str(model or "").strip()
        if key.lower().startswith(PREFIX):
            key = key[len(PREFIX):]
        low = key.lower()
        for m in self._models:
            if str(m.get("id") or "").lower() == low:
                return str(m.get("key") or m.get("id") or key)
        return seed_model_key(key)

    def list_models(self) -> list[dict]:
        ids = []
        for m in self._models:
            mid = str(m.get("id") or "")
            if mid and f"{PREFIX}{mid}" not in ids:
                ids.append(f"{PREFIX}{mid}")
        return [{"id": i, "object": "model", "created": 0, "owned_by": "huaweicloud"}
                for i in ids]

    def rate_table(self) -> dict:
        """{模型名/键: 积分倍率} —— 来自内置模型目录的 `credit[].ratio_display`。

        2026-10-22 实测：builtin 每条带 credit 数组，形如
        ``{"ratio":"0.05","ratio_display":"0.7x","input_price":"8",...}``；
        官方 UI 展示的正是 ``ratio_display``（GLM-5.2 0.7x、OpenPangu-2.0-Flash 0.32x、
        DeepSeek-V4.1-Flash 0.18x…）。这里已由 fetch_models 解析成数字存进 self._models。
        """
        out: dict[str, float] = {}
        for m in self._models:
            rate = m.get("rate")
            if rate is None:
                continue
            try:
                value = float(rate)
            except (TypeError, ValueError):
                continue
            name = str(m.get("id") or "")
            key = str(m.get("key") or "")
            for k in (name, key, f"{PREFIX}{name}", f"{PREFIX}{key}"):
                if k:
                    out[k] = value
        return out

    def normalize_model(self, model: str) -> str:
        raw = str(model or "").strip()
        if raw.lower().startswith(PREFIX.lower()):
            raw = raw[len(PREFIX):]
        return canonical_model(raw) or self.default_model

    async def sync_models(self, force: bool = False) -> bool:
        async with self._models_lock:
            if not force and self._models_at and time.time() - self._models_at < self.CATALOG_TTL:
                return True
            acc = self._pick_account()
            if not acc:
                logger.info("codearts 模型目录同步跳过：暂无可用账号")
                return False
            try:
                # ★ 2026-10-24：模型目录此前直接用旧 token 打，token 一过期就
                #   APIG.0602，目录同步失败并被静默“保留现有目录”。
                #   走 _call_with_token 后：先续期，遇过期错误再强制续期重试一次。
                models = await self._call_with_token(acc, fetch_models)
            except Exception as e:  # noqa: BLE001
                logger.warning("codearts 模型目录同步失败（保留现有目录）: %s", e)
                return False
            if not models:
                return False
            self._models = models
            self._models_at = time.time()
            # 目录按账号存放（福利按账号授予，不能全局共享）
            for a in self._load_accounts():
                a["models"] = [{"id": m["id"], "benefit": bool(m.get("benefit"))}
                               for m in models]
            logger.info("codearts 模型目录已更新: %d 个", len(models))
            return True

    # ---------- token ----------
    async def _ensure_token(self, account: dict, force: bool = False) -> bool:
        exp = int(account.get("expiresAt") or 0)
        if not force and exp and exp - 300 > time.time():
            return True
        async with self._refresh_lock:
            exp = int(account.get("expiresAt") or 0)
            if not force and exp and exp - 300 > time.time():
                return True
            return await asyncio.to_thread(self._refresh_locked, account)

    # ★★ 2026-10-24：签到 / 积分 / 模型目录过去**全都绕过** _ensure_token。
    #
    #   `checkin` / `fetch_credits` / `fetch_models` 是模块级纯函数，直接拿
    #   ``account["credentials"]["securityToken"]`` 签名；而 `_ensure_token()`
    #   此前只被**聊天路径**（`_open_stream`）和 `sync_models` 的失败分支调用。
    #   于是出现一种很隐蔽的分裂：
    #     · 聊天会续期 → 账号“看起来还能用”
    #     · 签到/积分永远拿旧 token → 一律 `APIG.0602 the security token has expired`
    #   实锤：`GT-2503_91855357` 的 expiresAt=10-24 04:33，但 refreshedAt 一直
    #   停在 02:33（13:55 仍是），期间聊天正常、签到连续 6 轮全失败、
    #   积分接口整条 key 从字典里消失（见 admin_api._new_channel_credits）。
    #
    #   现在统一走 `_call_with_token()`：先确保 token 有效（必要时续期），
    #   遇到 APIG.0602/STS5.1806 这类“凭据过期”错误再强制续期重试一次。
    @staticmethod
    def _is_expired_error(exc: Exception) -> bool:
        """判断异常是否为「凭据过期」类。"""
        body = getattr(exc, "body", "") or ""
        return is_expiry_error_text(f"{exc} {body}")

    async def _call_with_token(self, account: dict, fn, *args, **kwargs):
        """在**有效 token** 的前提下执行 `fn(account, *args)`。

        这是签到 / 积分 / 目录这类「非聊天」调用的唯一入口 —— 它们过去都不管
        token 到期，导致账号过期后静默失败（详见上方注释）。
        """
        await self._ensure_token(account)
        try:
            return await asyncio.to_thread(fn, account, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            if not self._is_expired_error(exc):
                raise
            logger.info("codearts 账号 %s 遇到凭据过期，强制续期后重试",
                        str(account.get("uid") or "")[:8])
            if not await self._ensure_token(account, force=True):
                raise
            return await asyncio.to_thread(fn, account, *args, **kwargs)

    def _refresh_locked(self, account: dict) -> bool:
        """续期（跨进程互斥 + 锁内重读磁盘 + 成功后立刻落盘）。

        ★ 2026-10-23：华为云 STS 的 refresh token **单次使用**，因此
          ① 两个进程/线程同时拿它续期 = 判「复用」= 整条凭据链作废 → 必须互斥；
          ② 锁内先重读磁盘：别的进程刚续期过就直接采用它的新凭据，
             不要拿同一个 token 去撞第二次；
          ③ 续期成功后**立刻回写 config.json** —— 内存里的新 token 一旦
             进程重启就没了，而磁盘上的旧 token 已经作废（事故根因）。

        ⚠️ 「磁盘更新」的判据拿**本 provider 自己那份账号**（``self.accounts``）来比，
        不能拿调用方传进来的 dict —— 强制续期路径过去传的是
        ``{**account, "expiresAt": 0}`` 的副本，有效期被人为清零后磁盘那份
        「永远更新」，于是续期被静默跳过、请求拿着旧凭据再吃一次 401
        （2026-10-23 实测就是这条把真因藏了一轮）。
        """
        uid = str(account.get("uid") or "")
        seg = self.cfg_key()
        canonical = next((a for a in self.accounts
                          if str(a.get("uid") or "") == uid), account)
        with cred_store.account_lock(seg, uid):
            disk = cred_store.read_account(seg, uid)
            if disk and cred_store.credentials_newer(disk, canonical):
                account.update(disk)
                self._health.observe_token(
                    uid, (account.get("credentials") or {}).get("securityToken"))
                logger.info("codearts 账号 %s 采用磁盘上更新的凭据（另一进程刚续期）",
                            uid[:8])
                return True
            try:
                data = refresh_token(account)
            except Exception as e:  # noqa: BLE001
                logger.warning("codearts 账号 %s 续期失败: %s", uid[:8], e)
                return False
            fresh = accounts_from_token_response(
                data, str(account.get("codeVerifier") or ""),
                account.get("dpopPrivateKey") or {},
                str(account.get("clientId") or CLIENT_ID))
            for k in ("uid", "name", "userName", "domainId", "enabled", "models"):
                if account.get(k) not in (None, "", []):
                    fresh[k] = account[k]
            fresh["refreshedAt"] = int(time.time())
            account.update(fresh)
            self._health.observe_token(account.get("uid"),
                                       (account.get("credentials") or {}).get("securityToken"))
            logger.info("codearts 账号 %s 凭据已续期", uid[:8])
            cred_store.persist_account(seg, uid, account)
            return True

    # ---------- 上游请求 ----------
    def _build_request(self, body: dict, model: str, benefit: bool) -> tuple[str, bytes, dict]:
        chat_id = uuid.uuid4().hex            # 必须 32 位 hex
        payload = {
            "model": self.model_key(model),
            "stream": True,
            "messages": self._sanitize_history(body.get("messages")),
            "prompt_cache_key": chat_id,
            "tool_stream": True,
            "chat_id": chat_id,
        }
        for k in ("max_tokens", "temperature", "top_p", "reasoning_effort",
                  "tools", "tool_choice"):
            if body.get(k) not in (None, "", []):
                payload[k] = body[k]
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return chat_id, raw, payload

    async def _open_stream(self, body: dict, account: dict, model: str,
                           force_benefit: bool | None = None) -> tuple[object, str]:
        await self._ensure_token(account)
        benefit = (self.benefit_auto_claim and self.is_benefit_model(model)) \
            if force_benefit is None else bool(force_benefit)
        chat_id, raw, _payload = self._build_request(body, model, benefit)
        cred = account.get("credentials") or {}
        headers = chat_headers(str(cred.get("securityToken") or ""))
        if benefit:
            headers[HEADER_MAAS_TYPE] = MAAS_BENEFIT
        signed = sign_headers("POST", SNAP_HOST + EP_CHAT_V2, headers, raw,
                              str(cred.get("accessKeyId") or ""),
                              str(cred.get("secretAccessKey") or ""),
                              str(cred.get("securityToken") or ""))
        signed["Chat-Id"] = chat_id
        # Session-Id 由 chat_id 派生（prompt cache 亲和）
        signed["Session-Id"] = hashlib.sha256(
            ("codearts-session:" + chat_id).encode()).hexdigest()[:32]
        client = self.get_client()
        resp = None
        # 并发上限预算按**请求**计（跨「鉴权续期重试」共用），别让两层重试叠乘
        conc_round = 0
        conc_start = time.time()
        auth_retried = False
        while True:
            try:
                resp = await client.send(
                    client.build_request("POST", SNAP_HOST + EP_CHAT_V2,
                                         content=raw, headers=signed), stream=True)
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(describe_net_exc(e, "codearts 连接上游")) from e

            status = resp.status_code
            if status < 400:
                self._health.mark_ok(account.get("uid"))
                return resp, chat_id

            # 先读 body 再关闭（httpx 的流一旦 aclose，aread 会抛，拒绝原因就看不见了）
            try:
                detail = (await resp.aread()).decode("utf-8", "replace")[:300]
            except Exception as e:  # noqa: BLE001
                detail = f"(读取错误响应失败: {type(e).__name__})"
            await resp.aclose()

            # ── 1) 鉴权被拒：强制续期一次后重试同一账号 ──
            if status in (401, 403) and not auth_retried:
                auth_retried = True
                if await self._ensure_token(account, force=True):
                    cred = account.get("credentials") or {}
                    headers = chat_headers(str(cred.get("securityToken") or ""))
                    if benefit:
                        headers[HEADER_MAAS_TYPE] = MAAS_BENEFIT
                    signed = sign_headers("POST", SNAP_HOST + EP_CHAT_V2, headers, raw,
                                          str(cred.get("accessKeyId") or ""),
                                          str(cred.get("secretAccessKey") or ""),
                                          str(cred.get("securityToken") or ""))
                    signed["Chat-Id"] = chat_id
                    signed["Session-Id"] = hashlib.sha256(
                        ("codearts-session:" + chat_id).encode()).hexdigest()[:32]
                    continue
                # 续期也失败：凭据确已判死，带上首次拒绝详情抛出
                self._health.mark_invalid(account.get("uid"),
                                          reason=f"chat 被拒 HTTP {status}")
                raise RuntimeError(
                    f"codearts 上游拒绝鉴权 HTTP {status}（续期失败）: {detail}")

            # ── 2) 并发会话上限（TM.00001041）→ 等一会儿重试同一账号 ──
            #
            # ★ 2026-10-23 实测口径：上限是**滚动窗口内的会话数**（≈3 条/60 秒），
            #   不是「同时开着的连接数」。同 chat_id、同 Session-Id 连发 4 条照样挂；
            #   改成 30 秒间隔后 6/6 全通。上游每个请求会占住一个名额约 40~60 秒。
            #   所以真实用户连发几轮、或模型检测批量探测都会撞上它 ——
            #   此前一律直接抛「繁忙」，用户侧看到的就是「明明能用却全红」。
            if ("TM.00001041" in detail or "并发会话数已达上限" in detail) \
                    and conc_round < CONCURRENCY_MAX_ROUNDS:
                wait = CONCURRENCY_WAIT_SECS + random.uniform(0, 3)
                if (time.time() - conc_start) + wait <= CONCURRENCY_BUDGET_SECS:
                    conc_round += 1
                    logger.info("codearts 并发会话已达上限，%.0f 秒后重试同账号"
                                "（第 %d/%d 轮）", wait, conc_round,
                                CONCURRENCY_MAX_ROUNDS)
                    await asyncio.sleep(wait)
                    continue
                logger.info("codearts 并发上限重试预算用尽（已等 %.0f 秒）",
                            time.time() - conc_start)

            if status in (401, 403):
                self._health.mark_invalid(account.get("uid"),
                                          reason=f"chat 被拒 HTTP {status}")
            raise RuntimeError(f"codearts 上游 HTTP {status}: {detail}")

    async def _events(self, body: dict, account: dict, model: str):
        """取上游事件流。

        福利头**只在目录明确标记 benefit 时才带**（见 is_benefit_model）；
        不再按上游 404 反推（那其实是「model 名不对」，2026-10-23 实测更正）。
        """
        for attempt in (1,):
            benefit = self.benefit_auto_claim and self.is_benefit_model(model)
            resp, _chat_id = await self._open_stream(body, account, model,
                                                    force_benefit=benefit)
            content_len = [0]
            reason_len = [0]
            try:
                async for chunk in _aiter_codearts(resp, content_len, reason_len):
                    yield chunk
                return
            except ModelNotRegistered as e:
                try:
                    await resp.aclose()
                except Exception:  # noqa: BLE001
                    pass
                # ★ 2026-10-23：**不再**把这条错误当成「需要福利头」。
                #   实测它真正表示「这个 model 名上游不认」—— 常见原因是发了展示名
                #   而不是内部 model_id（DeepSeek-V4.1-Flash → deepseek-v4.1-flash）。
                #   老实现据此把模型学成福利模型、补头重试，于是每次都被 4004.200
                #   挡住，用户侧看到的就是「这个模型一直繁忙」。
                raise RuntimeError(
                    f"模型 {model} 未注册（上游内部名 {self.model_key(model)}）：{e}。"
                    f"该模型可能已下线/改名，请在账号管理页点「刷新」重新拉模型目录") from e
            except BenefitUnavailable as e:
                try:
                    await resp.aclose()
                except Exception:  # noqa: BLE001
                    pass
                raise RuntimeError(
                    f"模型 {model} 是福利（免费）模型，但当前账号未领取到该福利"
                    f"（上游领取网关现已不可用）：{e}") from e
            except ConcurrencyLimited as e:
                try:
                    await resp.aclose()
                except Exception:  # noqa: BLE001
                    pass
                raise RuntimeError(
                    f"上游并发会话已达上限（每账号 3 条，稍后重试或换账号）：{e}") from e
            except Exception as e:  # noqa: BLE001
                try:
                    await resp.aclose()
                except Exception:  # noqa: BLE001
                    pass
                raise RuntimeError(describe_net_exc(e, "codearts 流式传输中断")) from e
            finally:
                try:
                    await resp.aclose()
                except Exception:  # noqa: BLE001
                    pass

    async def _prepare(self, body: dict) -> tuple:
        model_raw = str(body.get("model") or self.default_model)
        model = self.normalize_model(model_raw)
        account = self._pick_account(model)
        if not account:
            raise RuntimeError("codearts 通道没有可用账号（请先在账号管理页添加并登录 CodeArts）")
        return account, model

    # ---------- ### 输出 ----------
    @staticmethod
    def _sanitize_history(messages) -> list:
        """摘掉历史里残留的工具协议回声（见 tool_echo.strip_history_echo）。

        CodeArts 把 ``messages`` 原样透传给上游，所以这里按需**复制**（只有真要改
        的那几条才换对象），避免无谓地整份重建会话。理由：正文里的 marker 必然是
        「客户端从没执行过」的那一次，留在历史里只会教模型继续照抄。
        """
        out = None
        for i, m in enumerate(messages or []):
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            content = m.get("content")
            if not isinstance(content, str) or m.get("tool_calls"):
                continue
            clean = tool_echo.strip_history_echo(content)
            if clean == content:
                continue
            if out is None:
                out = list(messages)
            out[i] = {**m, "content": clean}
        return out if out is not None else list(messages or [])

    def _recover_leaked_calls(self, content: str, body: dict):
        """非流式路径的回声回读；返回 (calls|None, clean_content)。"""
        if not content or not tool_echo._MARKER_RE.search(content):
            return None, content
        allowed = tool_echo.declared_tool_names(body.get("tools"))
        calls, clean = tool_echo.parse_leaked_tool_calls(content, allowed)
        if calls:
            logger.warning("codearts 模型复述了工具协议回声，已还原为 %d 个结构化调用",
                           len(calls))
            return calls, clean
        tool_echo._log_unrecovered_echo("codearts", content)
        return None, content

    async def chat(self, body: dict) -> dict:
        account, model = await self._prepare(body)
        content, reasoning, tool_calls = [], [], {}
        usage, finish = None, None
        async for obj in self._events(body, account, model):
            if obj.get("usage"):
                usage = obj["usage"]
            for ch in obj.get("choices") or []:
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
                delta = ch.get("delta") or {}
                if delta.get("content"):
                    content.append(delta["content"])
                if delta.get("reasoning_content"):
                    reasoning.append(delta["reasoning_content"])
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    e = tool_calls.setdefault(idx, {"id": tc.get("id") or "",
                                                    "type": tc.get("type") or "function",
                                                    "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        e["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    e["function"]["name"] += fn.get("name") or ""
                    e["function"]["arguments"] += fn.get("arguments") or ""
        calls = [tool_calls[i] for i in sorted(tool_calls)] or None
        text = "".join(content)
        if not calls:
            recovered, text = self._recover_leaked_calls(text, body)
            if recovered:
                calls, finish = recovered, "tool_calls"
        msg = {"role": "assistant", "content": text}
        if reasoning:
            msg["reasoning_content"] = "".join(reasoning)
        if calls:
            msg["tool_calls"] = calls
        return {"id": make_chunk_id("codearts"), "object": "chat.completion",
                "created": int(time.time()), "model": body.get("model") or model,
                "choices": [{"index": 0, "message": msg,
                             "finish_reason": finish or ("tool_calls" if calls else "stop")}],
                "usage": normalize_usage(usage) or {
                    "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}

    async def stream(self, body: dict):
        account, model = await self._prepare(body)
        cid = make_chunk_id("codearts")
        created = int(time.time())
        shown = body.get("model") or model
        role_sent = False
        usage = None
        yielded = False
        # 回声回读：与 Qoder 同款（见 providers/tool_echo.py）。CodeArts 自己不写
        # marker，但**客户端的历史里可能有**（那条会话以前跑过 qdc-/qd-）：实测把
        # 带 marker 的一行丢给 ca-GLM-5.2，它会原样复述成正文 —— 没有回读就会把
        # 这段 JSON 原样透传给客户端（工具不执行、正文一串 JSON）。
        reader = tool_echo._ToolCallEchoReader(
            tool_echo.declared_tool_names(body.get("tools")))
        pending_finish = None

        def chunk(delta: dict, finish=None):
            nonlocal role_sent
            d = dict(delta)
            if not role_sent:
                d = {"role": "assistant", **d}
                role_sent = True
            return {"id": cid, "object": "chat.completion.chunk", "created": created,
                    "model": shown,
                    "choices": [{"index": 0, "delta": d, "finish_reason": finish}]}

        def call_chunks(calls):
            out = []
            for i, call in enumerate(calls):
                out.append(chunk({"tool_calls": [{
                    "index": i, "id": call["id"],
                    "type": call.get("type") or "function",
                    "function": call["function"]}]}))
            return out

        async for obj in self._events(body, account, model):
            if obj.get("usage"):
                usage = normalize_usage(obj["usage"])
            for ch in obj.get("choices") or []:
                delta = ch.get("delta") or {}
                out = {}
                if delta.get("content"):
                    safe = reader.feed(delta["content"])
                    if safe:
                        out["content"] = safe
                        yielded = True
                if delta.get("reasoning_content"):
                    out["reasoning_content"] = delta["reasoning_content"]
                if delta.get("tool_calls"):
                    held = reader.abort()
                    if held:
                        out["content"] = (out.get("content") or "") + held
                        yielded = True
                    out["tool_calls"] = delta["tool_calls"]
                    yielded = True
                if out:
                    yield chunk(out)
                if ch.get("finish_reason"):
                    if reader.capturing:
                        pending_finish = ch["finish_reason"]
                    else:
                        yield chunk({}, finish=ch["finish_reason"])
        recovered, tail = reader.finish("codearts")
        if recovered:
            for c in call_chunks(recovered):
                yield c
            yielded = True
            pending_finish = "tool_calls"
        elif tail:
            yield chunk({"content": tail})
            yielded = True
        if pending_finish:
            yield chunk({}, finish=pending_finish)
        if not yielded:
            yield chunk({"content": ""}, finish="stop")
        if wants_usage_in_stream(body):
            yield make_usage_chunk(cid, created, shown, usage or {
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})


async def _aiter_codearts(resp, content_len, reason_len):
    """CodeArts SSE → 标准 chunk 增量。

    上游形态（实测，无空行分隔）：
        {"text":"<累计全文>"}                    ← 全文快照，按「替换」语义处理
        {"delta":{"content":"...","reasoning_content":"..."}}  ← 增量
        {"choices":[{"delta":{...}}]}            ← 标准 ### chunk
        {"text":"[DONE]","error_code":"0"}       ← 结束 / 错误
    """
    pending_event = ""
    buffer = ""
    emitted = {"content": 0, "reason": 0}
    async for text in resp.aiter_text():
        buffer += text
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            ev, data, ok = _scan_line(line, pending_event)
            if not ok:
                pending_event = ev or pending_event
                continue
            pending_event = ""
            if not data:
                continue
            if data == "[DONE]":
                return
            try:
                payload = json.loads(data)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(payload, dict):
                continue
            code = payload.get("error_code")
            if code not in (None, "", "0", 0):
                _raise_codearts_error(
                    str(code), str(payload.get("error_msg") or payload.get("message") or ""))
            if payload.get("usage"):
                yield {"choices": [], "usage": payload["usage"]}
            delta, finish = _delta_from(payload)
            out = {}
            if delta:
                if delta.get("content"):
                    out["content"] = delta["content"]
                if delta.get("reasoning_content"):
                    out["reasoning_content"] = delta["reasoning_content"]
                if delta.get("tool_calls"):
                    out["tool_calls"] = delta["tool_calls"]
            else:
                snap = payload.get("text")
                if isinstance(snap, str) and snap:
                    if snap == "[DONE]":
                        return
                    # 全文快照：只发「新增的尾巴」——客户端按增量累积，发全文会重复
                    prev = emitted["content"]
                    if len(snap) > prev:
                        out["content"] = snap[prev:]
                        emitted["content"] = len(snap)
                out_arr = payload.get("output")
                if isinstance(out_arr, list) and out_arr and not out.get("content"):
                    joined = "".join(str(i.get("text") or "") for i in out_arr
                                     if isinstance(i, dict) and i.get("type") == "output_text")
                    if joined and len(joined) > emitted["content"]:
                        out["content"] = joined[emitted["content"]:]
                        emitted["content"] = len(joined)
            if out:
                yield {"choices": [{"index": 0, "delta": out, "finish_reason": finish or None}]}
            elif finish:
                yield {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}
            if str(ev or "").lower() in ("done", "end", "finish"):
                return
    if buffer.strip():
        ev, data, ok = _scan_line(buffer, pending_event)
        if ok and data and data != "[DONE]":
            try:
                payload = json.loads(data)
                snap = payload.get("text") if isinstance(payload, dict) else None
                if isinstance(snap, str) and len(snap) > emitted["content"]:
                    yield {"choices": [{"index": 0, "delta": {
                        "content": snap[emitted["content"]:]}, "finish_reason": None}]}
            except Exception:  # noqa: BLE001
                pass


def _config_path() -> str:
    try:
        import app_paths as _ap
        return _ap.CONFIG_PATH
    except Exception:  # noqa: BLE001
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "config.json")


def refresh_account_token(account: dict) -> bool:
    """**模块级**同步续期入口 —— 供签到脚本等「没有 provider 实例」的场景使用。

    ★★ 2026-10-24 新增。背景：`checkin()` / `fetch_credits()` / `fetch_models()`
      都是模块级纯函数，直接用 `credentials.securityToken` 签名，**从不续期**；
      续期逻辑此前只活在 `CodeArtsProvider._ensure_token()` 里，于是签到脚本
      （拿着 config.json 的内存副本直接调 `ca.checkin`）永远用过期 token，
      表现就是「聊天正常、签到天天失败」。

    这里复用 provider 的 `_refresh_locked()` —— 它自带跨进程互斥
    （`cred_store.account_lock`）、锁内重读磁盘、成功后立即落盘，
    这些都是华为 STS「refresh token 单次使用」的硬要求，**不能在这里另写一套**。

    返回 True 表示续期成功（或磁盘上已有更新更的凭据）。
    """
    uid = str(account.get("uid") or "")
    try:
        prov = CodeArtsProvider({"accounts": [account]})
    except Exception:  # noqa: BLE001
        prov = None
    if prov is not None:
        try:
            # _refresh_locked 内部会用 self.accounts 里的那份做“磁盘更新”比对，
            # 所以要把账号塞进去（上面构造时已塞入同一个 dict 对象）。
            return bool(prov._refresh_locked(account))
        finally:
            pass
    # 兜底：直接调（失去互斥保护，但总比不续期强）
    try:
        with cred_store.account_lock("codearts", uid):
            data = refresh_token(account)
        fresh = accounts_from_token_response(
            data, str(account.get("codeVerifier") or ""),
            account.get("dpopPrivateKey") or {},
            str(account.get("clientId") or CLIENT_ID))
        for k in ("uid", "name", "userName", "domainId", "enabled", "models"):
            if account.get(k) not in (None, "", []):
                fresh[k] = account[k]
        fresh["refreshedAt"] = int(time.time())
        account.update(fresh)
        cred_store.persist_account("codearts", uid, account)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("codearts 账号 %s 模块级续期失败: %s", uid[:8], e)
        return False
