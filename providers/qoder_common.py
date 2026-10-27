# -*- coding: utf-8 -*-
"""qoder_common.py —— Qoder 国内版 / 国际版共用的协议层（零第三方依赖）

移植自开源项目 Qoder2API-Hub（MIT，https://github.com/shuishuipingan/qoder2api-hub）
的 qoder_sign.py / qoder_fingerprint.py / qoder_accounts.py 中的**必要部分**，
并按 open-ai 的 provider 形态改写：只保留「签名 + 账号操作 + 签到 + 模型目录」，
不带它那套 Web 看板 / 调度器 / 多账号池。

上游链路（逆向自官方 qodercli / 桌面端）：

    业务接口（签到 / 余额 / 用户信息）
        GET/POST {openapi}{path}          —— 纯 Bearer，无 COSY 签名
    推理接口（对话）
        body   = baseprompt 模板 + 会话
        encode = qoder_encode(json)       —— Qoder 自定义 Base64 变体
        sign   = COSY Bearer（RSA 包 AES 会话密钥 + MD5 签名）
        POST   {gateway}/algo/api/v2/service/pro/sse/agent_chat_generation
        resp   = SSE 信封 {"body":"<内层 chunk>"} —— 需拆两层

双区差异（全部集中在 REALMS，上层代码不判区域）：
    | 项 | 国内版 (qdc) | 国际版 (qd) |
    | openapi | openapi.qoder.com.cn | openapi.qoder.sh |
    | 推理网关 | gateway.qoder.com.cn | api1/api2/api3.qoder.sh（候选逐个降级）|
    | device flow | 带 redirect_uri，nonce 带横线 | 不带 redirect_uri，nonce 32 位 hex |
    | 签到接口 | /sash/api/v1/me/daily-check-in/* | 实测 404，改由 campaign 平台承载 |

安全边界（与上游协议对齐，不得为「加固」单方面偏离，否则请求直接解不开）：
  * cosy 会话密钥 temp_key 必须是 16 个十六进制字符（服务端按同一串字节解包）；
  * RSA 公钥为官方客户端硬编码，换不了更长的。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import hmac
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

logger = logging.getLogger("openapi.providers.qoder")

# ═══════════════════════════ 区域常量 ═══════════════════════════

REALMS = {
    "cn": {
        "name": "国内版",
        "openapi": "https://openapi.qoder.com.cn",
        "gateway": "https://gateway.qoder.com.cn",
        "gateway_fallbacks": (),
        "website": "https://qoder.com.cn",
        "client_id": "1c5e33e1-364d-4ce6-b02c-acaa81274a5c",
        "redirect_uri": "qoder-work-cn://",
        "domain": "qoder.com.cn",
        "ua": "QoderWork/1.1.75",
        "send_redirect_uri": True,
        "nonce_dashed": True,          # device flow 的 nonce 用带横线的 uuid
        "has_checkin": True,           # /sash/api/v1/me/daily-check-in 是否在
        "home_dir": ".qoder-cn",       # 官方客户端本地目录（读 machine_id 用）
    },
    "intl": {
        "name": "国际版",
        "openapi": "https://openapi.qoder.sh",
        "gateway": "https://api3.qoder.sh",
        "gateway_fallbacks": ("https://api2.qoder.sh", "https://api1.qoder.sh"),
        "website": "https://qoder.com",
        "client_id": "e883ade2-e6e3-4d6d-adf7-f92ceff5fdcb",
        "redirect_uri": "qoder://aicoding.aicoding-agent/login-success",
        "domain": "qoder.com",
        "ua": "Qoder/1.1.75",
        "send_redirect_uri": False,
        "nonce_dashed": False,
        # 实测 404：国际版每日领取已搬到 campaign 平台，checkin 能力靠运行时探测
        "has_checkin": False,
        "home_dir": ".qoder",
    },
}

CLIENT_UA = "Go-http-client/2.0"
DEFAULT_USER_TYPE = "personal_professional_trial"

# 业务端点（全部挂 openapi 基址）
PATH_DEVICE_POLL = "/api/v1/deviceToken/poll"
PATH_DEVICE_REFRESH = "/api/v1/deviceToken/refresh"
PATH_JOB_EXCHANGE = "/api/v1/jobToken/exchange"
PATH_JOB_REFRESH = "/api/v1/jobToken/refresh"
PATH_USERINFO = "/api/v1/userinfo"
PATH_QUOTA = "/api/v2/quota/usage"
PATH_CHECKIN_STATUS = "/sash/api/v1/me/daily-check-in/status"
PATH_CHECKIN_CLAIM = "/sash/api/v1/me/daily-check-in/claim"
PATH_CAMPAIGNS = "/sash/api/v1/me/campaigns"
PATH_CAMPAIGN_REWARD = "/sash/api/v1/me/campaigns/%s/reward"
PATH_CAMPAIGN_CLAIM = "/sash/api/v1/me/campaigns/%s/claim"

# 推理端点（挂 gateway 基址，签名覆盖 path，故换主机不影响签名）
CHAT_PATH = "/algo/api/v2/service/pro/sse/agent_chat_generation"
MODELS_PATH = "/algo/api/v2/model/list?Encode=1"

# 桌面端专用请求头（campaign 活动平台必需：client-type 10 = 桌面端）
DESKTOP_CLIENT_TYPE = "10"
DESKTOP_CLIENT_VERSION = "0.4.8"

COSY_VERSION = (os.environ.get("QD_COSY_VERSION") or "1.1.75").strip() or "1.1.75"


def realm_cfg(realm: str) -> dict:
    return REALMS.get(str(realm or "").lower()) or REALMS["cn"]


def gateway_candidates(realm: str) -> list[str]:
    """本区域的推理主机候选列表（**默认只返回主主机**）。

    ★★ 2026-10-24 改：默认**不再附带 fallback 主机**。理由是全历史实测统计
      （`logs/gateway_err.log`，`POST .../agent_chat_generation`）::

        api3.qoder.sh   200 成功 413 次 / 405 23 次   ← 真正在干活的那台
        api2.qoder.sh   200 成功 **0** 次 / 405 15 次  ← 从未成功过
        api1.qoder.sh   200 成功 **0** 次 / 405 11 次  ← 从未成功过

      405 是**边缘 WAF 的静态拦截页**（`data-spm="a3c0e"`）。实测三个候选主机
      会**在同一时刻一起被拦**（同一套规则集），所以「换主机」这条降级路径
      **不可能成功** —— 它只是白花约 1.5 秒，然后照样抛错。
      真正有用的做法是**对同一主机退避重试**（拦截是时间性的，见
      `qoder_base._open_stream` 的 405 分支）。

      要恢复旧的「逐个换主机」行为（排障用）：
      设环境变量 ``QD_GATEWAY_FALLBACKS=1``。
    """
    cfg = realm_cfg(realm)
    out = [cfg["gateway"]]
    raw = (os.environ.get("QD_GATEWAY_FALLBACKS") or "").strip().lower()
    if raw not in ("1", "on", "true", "yes", "enable", "enabled", "force"):
        return out
    for h in cfg.get("gateway_fallbacks") or ():
        if h and h not in out:
            out.append(h)
    return out


# ═══════════════════════════ Qoder 自定义 Base64 ═══════════════════════════

QODER_CUSTOM_ALPHABET = "_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!"
QODER_STD_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
QODER_PAD = "$"

_STD2CUSTOM = {ord(QODER_STD_ALPHABET[i]): QODER_CUSTOM_ALPHABET[i] for i in range(64)}
_STD2CUSTOM[ord("=")] = QODER_PAD
_CUSTOM2STD = {ord(QODER_CUSTOM_ALPHABET[i]): QODER_STD_ALPHABET[i] for i in range(64)}
_CUSTOM2STD[ord(QODER_PAD)] = "="

assert len(QODER_CUSTOM_ALPHABET) == 64 and len(set(QODER_CUSTOM_ALPHABET)) == 64


def qoder_encode(plain: bytes) -> str:
    """明文 → Qoder 自定义 Base64 变体（出站请求体格式）。"""
    std = base64.b64encode(plain).decode("ascii")
    n = len(std)
    a = n // 3
    rearranged = std[n - a:] + std[a:n - a] + std[:a]
    return rearranged.translate(_STD2CUSTOM)


def qoder_decode(encoded: str) -> bytes:
    """qoder_encode 的逆运算（自检 / 排障用）。"""
    std = encoded.translate(_CUSTOM2STD)
    n = len(std)
    a = n // 3
    r1, r2, r3 = std[:a], std[a:n - a], std[n - a:]
    return base64.b64decode((r3 + r2 + r1).encode("ascii"))


# ═══════════════════════════ 纯标准库 AES-128-CBC ═══════════════════════════

def _gmul(a: int, b: int) -> int:
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _rotl8(x: int, k: int) -> int:
    return ((x << k) | (x >> (8 - k))) & 0xFF


def _build_sbox() -> list:
    sbox = [0] * 256
    p = q = 1
    while True:
        p = (p ^ (p << 1) ^ (0x1B if p & 0x80 else 0)) & 0xFF
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ _rotl8(q, 1) ^ _rotl8(q, 2) ^ _rotl8(q, 3) ^ _rotl8(q, 4)
        sbox[p] = (x ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    return sbox


SBOX = _build_sbox()
_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _expand_key(key: bytes) -> list:
    nk = len(key) // 4
    if len(key) != 16:
        raise ValueError("AES-128 key must be 16 bytes")
    words = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (nk + 7)):
        temp = list(words[i - 1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [SBOX[b] for b in temp]
            temp[0] ^= _RCON[i // nk - 1]
        words.append([words[i - nk][j] ^ temp[j] for j in range(4)])
    return [sum(words[4 * r:4 * r + 4], []) for r in range(nk + 7)]


def _encrypt_block(block: bytes, rkeys: list) -> bytes:
    s = [b ^ k for b, k in zip(block, rkeys[0])]
    rounds = len(rkeys)
    for rnd in range(1, rounds):
        s = [SBOX[b] for b in s]
        s = [s[(i + 4 * (i % 4)) % 16] for i in range(16)]     # ShiftRows（列优先排布）
        if rnd != rounds - 1:
            ns = []
            for c in range(4):
                a0, a1, a2, a3 = s[4 * c:4 * c + 4]
                ns += [_gmul(a0, 2) ^ _gmul(a1, 3) ^ a2 ^ a3,
                       a0 ^ _gmul(a1, 2) ^ _gmul(a2, 3) ^ a3,
                       a0 ^ a1 ^ _gmul(a2, 2) ^ _gmul(a3, 3),
                       _gmul(a0, 3) ^ a1 ^ a2 ^ _gmul(a3, 2)]
            s = ns
        s = [b ^ k for b, k in zip(s, rkeys[rnd])]
    return bytes(s)


def aes_cbc_encrypt(plain: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-128-CBC + PKCS7（Qoder 的 info 字段就用它，key == iv）。"""
    pad = 16 - (len(plain) % 16)
    data = plain + bytes([pad]) * pad
    rkeys = _expand_key(key)
    out = bytearray()
    prev = iv
    for off in range(0, len(data), 16):
        blk = bytes(a ^ b for a, b in zip(data[off:off + 16], prev))
        prev = _encrypt_block(blk, rkeys)
        out.extend(prev)
    return bytes(out)


# ═══════════════════════════ RSA PKCS#1 v1.5 ═══════════════════════════

SERVER_PUB_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDA8iMH5c02LilrsERw9t6Pv5Nc
4k6Pz1EaDicBMpdpxKduSZu5OANqUq8er4GM95omAGIOPOh+Nx0spthYA2BqGz+l
6HRkPJ7S236FZz73In/KVuLnwI8JJ2CbuJap8kvheCCZpmAWpb/cPx/3Vr/J6I17
XcW+ML9FoCI6AOvOzwIDAQAB
-----END PUBLIC KEY-----"""


def _parse_rsa_pub(pem: str):
    body = "".join(l for l in pem.splitlines() if "BEGIN" not in l and "END" not in l)
    der = base64.b64decode(body)

    def read_tlv(buf, i):
        ln = buf[i + 1]
        j = i + 2
        if ln & 0x80:
            n = ln & 0x7F
            ln = int.from_bytes(buf[j:j + n], "big")
            j += n
        return buf[j:j + ln], j + ln

    seq, _ = read_tlv(der, 0)
    _, i = read_tlv(seq, 0)            # AlgorithmIdentifier
    bits, _ = read_tlv(seq, i)         # BIT STRING
    rsaseq, _ = read_tlv(bits[1:], 0)
    nbuf, j = read_tlv(rsaseq, 0)
    ebuf, _ = read_tlv(rsaseq, j)
    return int.from_bytes(nbuf, "big"), int.from_bytes(ebuf, "big")


_RSA_N, _RSA_E = _parse_rsa_pub(SERVER_PUB_PEM)


def rsa_pkcs1v15_encrypt(plain: bytes) -> bytes:
    k = (_RSA_N.bit_length() + 7) // 8
    if len(plain) > k - 11:
        raise ValueError("message too long for RSA block")
    ps_len = k - 3 - len(plain)
    ps = bytearray()
    while len(ps) < ps_len:
        ps.extend(x for x in os.urandom(ps_len - len(ps)) if x != 0)
    em = b"\x00\x02" + bytes(ps[:ps_len]) + b"\x00" + plain
    return pow(int.from_bytes(em, "big"), _RSA_E, _RSA_N).to_bytes(k, "big")


# ═══════════════════════════ 设备指纹（按账号稳定派生） ═══════════════════════════

# 官方客户端的 machineId 是**带连字符的 UUIDv4 形状**，请求 id 是每请求随机 hex。
# 但取值必须按账号派生：网关是一个长驻进程冒充多台设备，进程级取值会让所有账号
# 关联到同一台机器上（与「每账号一台设备」的隔离口径自相矛盾）。


def derive_id(uid: str, salt: str) -> str:
    h = hashlib.md5(f"{salt}:{uid or 'anonymous'}".encode("utf-8")).hexdigest()
    h = h[:12] + "4" + h[13:16] + "a" + h[17:]
    return "%s-%s-%s-%s-%s" % (h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])


def derive_machine_type(uid: str) -> str:
    return derive_id(uid, "machinetype").replace("-", "")[:18]


def derive_machine_token(uid: str) -> str:
    raw = hashlib.sha512(f"machinetoken:{uid}".encode("utf-8")).digest()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")[:43]


def generate_request_id() -> str:
    return uuid.uuid4().hex


def _machine_hostname(uid: str) -> str:
    h = hashlib.md5(("hostname:" + (uid or "anonymous")).encode("utf-8")).hexdigest()
    return "DESKTOP-" + h[:7].upper()


def cosy_machine_os() -> str:
    """cosy-machineos：默认 Windows=x86_64_win32 / 其余=x86_64_linux（防指纹漂移）。"""
    raw = (os.environ.get("QD_COSY_MACHINE_OS") or "").strip()
    if raw.lower() == "auto":
        import platform as _p
        arch = (_p.machine() or "").strip().lower()
        arch = "aarch64" if arch in ("arm64", "aarch64") else "x86_64"
        plat = "win32" if sys.platform.startswith("win") else (
            "darwin" if sys.platform == "darwin" else "linux")
        return f"{arch}_{plat}"
    if raw:
        return raw
    return "x86_64_win32" if sys.platform.startswith("win") else "x86_64_linux"


def _cosy_os_headers() -> bool:
    return str(os.environ.get("QD_COSY_OS_HEADERS") or "1").strip().lower() \
        not in ("0", "off", "false", "no", "disable", "disabled")


def json_sorted_compact(mapping: dict) -> bytes:
    """键排序 + 无空白的紧凑 JSON（服务端签名字节与此强绑定）。"""
    parts = []
    for k in sorted(mapping.keys()):
        parts.append(json.dumps(str(k), ensure_ascii=False) + ":" +
                     json.dumps(mapping[k] if mapping[k] is not None else "",
                                ensure_ascii=False))
    return ("{" + ",".join(parts) + "}").encode("utf-8")


# ═══════════════════════════ COSY 会话与签名 ═══════════════════════════

class CosySession:
    """单账号的 COSY 签名会话（access token 轮换后整体重建）。"""

    def __init__(self, uid, nickname="", access_token="", refresh_token="",
                 user_type=DEFAULT_USER_TYPE, org_id="", org_name=""):
        self.uid = uid or ""
        self.machine_id = derive_id(self.uid, "machine")
        self.machine_token = derive_machine_token(self.uid)
        self.machine_type = derive_machine_type(self.uid)
        self.machine_hostname = _machine_hostname(self.uid)
        self.access_token = access_token or ""
        # ★ 必须是 16 个十六进制字符：长度/编码一变，服务端解不开 info
        self.temp_key = uuid.uuid4().hex[:16]
        self.cosy_key = base64.b64encode(
            rsa_pkcs1v15_encrypt(self.temp_key.encode("utf-8"))).decode("ascii")
        identity = {
            "name": nickname or "",
            "aid": self.uid,
            "uid": self.uid,
            "yx_uid": "",
            "organization_id": org_id or "",
            "organization_name": org_name or "",
            "user_type": user_type or DEFAULT_USER_TYPE,
            "security_oauth_token": access_token or "",
            "refresh_token": refresh_token or "",
        }
        self.info = base64.b64encode(aes_cbc_encrypt(
            json_sorted_compact(identity),
            self.temp_key.encode("utf-8"), self.temp_key.encode("utf-8"))).decode("ascii")

    def bearer(self, body: str, raw_url: str):
        path = urllib.parse.urlsplit(raw_url).path or "/"
        if path.startswith("/algo"):
            path = path[len("/algo"):]
        payload = {"cosyVersion": COSY_VERSION, "ideVersion": "", "info": self.info,
                   "requestId": str(uuid.uuid4()), "version": "v1"}
        payload_b64 = base64.b64encode(json_sorted_compact(payload)).decode("ascii")
        date = str(int(time.time()))
        raw = "\n".join([payload_b64, self.cosy_key, date, body, path])
        sig = hashlib.md5(raw.encode("utf-8")).hexdigest()
        return payload_b64, date, "Bearer COSY." + payload_b64 + "." + sig

    def headers(self, body: str, raw_url: str, model_key: str = "",
                sse: bool = True, accept: str = "text/event-stream") -> dict:
        _, date, bearer = self.bearer(body, raw_url)
        h = {
            "cosy-data-policy": "agree",
            "content-type": "application/json",
            "cosy-business-product": "cli",
            "cosy-business-type": "agent",
            "cosy-scene": "assistant",
            "cosy-clienttype": "5",
            "cosy-date": date,
            "cosy-user": self.uid,
            "cosy-key": self.cosy_key,
            "accept": accept,
            "authorization": bearer,
            "accept-encoding": "identity",
            "cosy-version": COSY_VERSION,
            "cosy-machineid": self.machine_id,
            "cosy-machinetoken": self.machine_token,
            "cosy-machinetype": self.machine_type,
            "cosy-clientip": "169.254.198.161",
            "login-version": "v2",
            # 官方 CLI 的推理请求经 undici fetch 发出，由它补默认 UA —— 自造 UA 反而是特征
            "user-agent": "undici",
        }
        if _cosy_os_headers():
            h["cosy-machineos"] = cosy_machine_os()
            if sse:
                h["cosy-machinehostname"] = self.machine_hostname
        if sse:
            h["cache-control"] = "no-cache"
        if model_key:
            h["x-model-key"] = model_key
            h["x-model-source"] = "system"
        return h


class CosySessionCache:
    """uid → CosySession；access token 变化即失效重建。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._entries = {}

    def get(self, account: dict) -> CosySession:
        token = str(account.get("accessToken") or "")
        if not token:
            raise ValueError("qoder: 账号无 accessToken")
        uid = str(account.get("uid") or "")
        key = uid or token[:16]
        with self._lock:
            hit = self._entries.get(key)
            if hit and hit[0] == token:
                return hit[1]
        sess = CosySession(
            uid=uid, nickname=str(account.get("nickname") or ""),
            access_token=token, refresh_token=str(account.get("refreshToken") or ""),
            user_type=str(account.get("userType") or "") or DEFAULT_USER_TYPE,
            org_id=str(account.get("organizationId") or ""),
            org_name=str(account.get("organizationName") or ""))
        with self._lock:
            self._entries[key] = (token, sess)
        return sess

    def invalidate(self, uid):
        with self._lock:
            self._entries.pop(uid, None)


SESSIONS = CosySessionCache()


# ═══════════════════════════ HTTP 工具（标准库） ═══════════════════════════

class QoderHTTPError(RuntimeError):
    def __init__(self, status: int, body: str, url: str = ""):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body or ""
        self.url = url


def http_json(url: str, method: str = "GET", data: bytes | None = None,
              headers: dict | None = None, timeout: float = 30.0,
              retries: int = 1):
    """发请求并解析 JSON。4xx/5xx 抛 QoderHTTPError（调用方按 status 分类）。"""
    h = {"Accept": "application/json", "User-Agent": CLIENT_UA}
    h.update(headers or {})
    if data is not None:
        h.setdefault("Content-Type", "application/json")
    last = None
    for attempt in range(max(1, retries)):
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                body = ""
            last = QoderHTTPError(exc.code, body, url)
            if exc.code < 500 or attempt == retries - 1:
                raise last from None
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt == retries - 1:
                raise
        time.sleep(0.6 * (attempt + 1))
    raise last  # pragma: no cover


def _auth_headers(account: dict, realm: str) -> dict:
    cfg = realm_cfg(realm)
    return {"Accept": "application/json",
            "User-Agent": cfg.get("ua") or CLIENT_UA,
            "Authorization": "Bearer " + str(account.get("accessToken") or "")}


def _umid_component(realm: str) -> str:
    """找本机官方客户端的 UMID 组件 `resources/umid/runtime-info.exe`。

    ★★ 2026-10-23（master 指路 D:\\Lenovo\\Desktop\\temp\\Qoder-Daily 后定位）：
      这就是「Qoder 为什么看不到签到活动」的根因 —— 上游的 growth/campaign
      平台对**没有设备风控身份**的请求直接不下发活动（CN/INTL 都一样）：
        无 UMID → {"showCampaign": false, "claimable": false, "campaigns": []}
        有 UMID → {"showCampaign": true,  "claimable": true,  "campaigns": [CLAIM_BENEFIT/CLAIMABLE]}
      旧实现用 `derive_machine_token()` 自己**伪造**了一套 MT/MC/MT，上游不认，
      于是永远查不到签到期 → 「本区域暂无每日签到活动」。

    查找顺序（2026-10-24 最终版）：
      1. env ``QD_UMID_PATH``（最高优先级）
      2. **独立提取目录** ``%LOCALAPPDATA%\\.openai-umid``
         —— ``scripts/umid_fetch.py`` 的输出位置。**排在客户端之前**，原因见下。
      3. 官方客户端 ``<安装根>/resources/umid/runtime-info.exe``
    找不到返回空串。

    ★ 为什么独立目录优先于客户端：客户端会在后台自动更新
      （``.qoder-versions/`` + ``qoder-update-ready.json``），**更新期间
      ``resources/umid/`` 会被清空**（2026-10-24 实测：master 重装 CN 客户端时
      该目录只剩一个空的 ``PS/``）。此时若只认客户端，签到就会莫名其妙失败。
      独立目录不受更新影响，是更稳的那份。

    ★★ 2026-10-24：Windows 上 UMID **不是单个 exe**，而是
      ``runtime-info.exe`` + ``sgsdk.dll`` **一对**（缺 dll 会直接 panic：
      ``Failed to load sgsdk.dll ... code 126``，实测）。
      因此这里对 Windows 额外校验同目录下 ``sgsdk.dll`` 是否存在 ——
      只有 exe 没有 dll 的目录视为**不可用**，跳过继续找。
    """
    custom = (os.environ.get("QD_UMID_PATH") or "").strip()
    if custom and os.path.isfile(custom):
        return custom
    exe = "runtime-info.exe" if sys.platform.startswith("win") else "runtime-info"
    candidates = [os.path.join(_extracted_umid_dir(), exe)]
    names = ("Qoder", "Qoder CN") if str(realm) == "intl" else ("Qoder CN", "Qoder")
    for name in names:
        for base in _client_roots(name):
            candidates.append(os.path.join(base, "resources", "umid", exe))
    for path in candidates:
        if not os.path.isfile(path):
            continue
        if sys.platform.startswith("win"):
            # 必须成对：没有 dll 的 exe 一运行就 panic，等于没有身份
            if not os.path.isfile(os.path.join(os.path.dirname(path), "sgsdk.dll")):
                logger.debug("UMID 组件缺少 sgsdk.dll，跳过: %s", path)
                continue
        return path
    return ""


def _extracted_umid_dir() -> str:
    """``scripts/umid_fetch.py`` 的默认输出目录（与脚本里的 DEFAULT_ROOT 一致）。"""
    base = (os.environ.get("LOCALAPPDATA") or "").strip() or os.path.expanduser("~")
    return os.path.join(base, ".openai-umid")


def _client_roots(name: str) -> list:
    """官方客户端安装根候选（只用存在性探测，不递归扫盘）。"""
    out = []
    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = (os.environ.get(env_name) or "").strip()
        if base:
            out.append(os.path.join(base, name))
            out.append(os.path.join(base, "Programs", name))
    for drive in ("D:\\", "C:\\", "E:\\"):
        out.append(os.path.join(drive, "app", "agent", name))
    return out


_RISK_CACHE: dict = {}
_RISK_TTL = 40 * 60


def risk_identity(realm: str, uid: str, app_env: str | None = None) -> tuple[dict, str]:
    """设备风控身份三件套 (machineToken/machineCode/machineType)。返回 (values, err)。

    调用官方 UMID 组件现算（它会读本机硬件指纹 + 虚拟化检测）。结果缓存
    40 分钟（组件本身约 200ms，但签到/查活动会连打数次）。
    三件套缺任何一个都算失败 —— **绝不回落到伪造值**：假身份恰恰是上游
    不下发活动的原因，用假值只会把「查不到」伪装成「没有活动」。

    ★★ 2026-10-24 新增 `app_env` 参数（默认 None = 按 realm 推断）：
      它决定 UMID 组件输出**哪一套**桌面端身份 —— `3` = 国际版桌面端、
      `0` = 国内版桌面端。两套身份由**同一个官方组件**按本机硬件指纹现算，
      都是这台机器的真实身份（不是伪造值）。

      用途：上游按「设备」发放每日签到，同一台机器每个区域每天只有一份。
      当某账号用本区域的桌面端身份拿不到 CLAIM_BENEFIT 时，改用另一区域的
      桌面端身份再查一次即可拿到（相当于「本机也装着另一个版本的客户端」）。
      这条兜底路径由 `signin_fallback_app_env()` 按配置开启，见 `checkin()`。
    """
    env = str(app_env) if app_env is not None else ("3" if str(realm) == "intl" else "0")
    key = f"{realm}:{uid}:{env}"      # ★ 身份不同 → 缓存必须分开，否则会串
    hit = _RISK_CACHE.get(key)
    if hit and time.time() - hit[0] < _RISK_TTL:
        return hit[1], ""
    need = ("QD_MACHINE_TOKEN", "QD_MACHINE_CODE", "QD_MACHINE_TYPE")
    env_vals = [(os.environ.get(n) or "").strip() for n in need]
    if all(env_vals):
        vals = {"machineToken": env_vals[0], "machineCode": env_vals[1],
                "machineType": env_vals[2]}
        _RISK_CACHE[key] = (time.time(), vals)
        return vals, ""
    comp = _umid_component(realm)
    if not comp:
        return {}, ("本机没有 Qoder 官方客户端的 UMID 组件"
                    "（可用 QD_UMID_PATH 指定 runtime-info.exe）")
    # env 参数：3 = 国际版桌面端，0 = 国内版桌面端（实测，见 Qoder-Daily）
    # ★ 2026-10-24：改为取上面算好的 `env`（可由调用方指定，见函数说明）。
    try:
        import subprocess
        payload = (json.dumps({"account": str(uid or "")}) + "\n").encode("utf-8")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        proc = subprocess.run(  # noqa: S603
            [comp, env], input=payload, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=25, creationflags=flags)
        line = (proc.stdout or b"").decode("utf-8", "replace").splitlines()
        data = json.loads(line[0].strip()) if line and line[0].strip() else {}
    except Exception as exc:  # noqa: BLE001
        return {}, f"UMID 组件调用失败：{exc}"[:160]
    vals = {}
    for k in ("machineToken", "machineCode", "machineType"):
        v = data.get(k) if isinstance(data, dict) else None
        if not isinstance(v, str) or not v.strip():
            return {}, f"UMID 组件输出缺少 {k}"
        vals[k] = v.strip()
    _RISK_CACHE[key] = (time.time(), vals)
    return vals, ""


def client_version(realm: str) -> str:
    """cosy-clientversion：优先读本机客户端的 build-manifest.json（真实版本）。

    实测 2026-10-23：本机 CN/INTL 都是 **0.4.9**，而旧版写死 0.4.8 —— 版本号
    与真实客户端不符本身就是风控特征。读不到才回落到常量 / env。
    """
    forced = (os.environ.get("QD_DESKTOP_VERSION") or "").strip()
    if forced:
        return forced
    names = ("Qoder", "Qoder CN") if str(realm) == "intl" else ("Qoder CN", "Qoder")
    for name in names:
        for base in _client_roots(name):
            try:
                with open(os.path.join(base, "resources", "build-manifest.json"),
                          encoding="utf-8") as f:
                    ver = str((json.load(f) or {}).get("productVersion") or "").strip()
                if ver:
                    return ver
            except Exception:  # noqa: BLE001
                continue
    return DESKTOP_CLIENT_VERSION


def desktop_headers(account: dict, realm: str, app_env: str | None = None) -> dict:
    """桌面端请求头（campaign 活动平台必需；client-type 10 = 桌面端）。

    ★★ 2026-10-23 修：机器身份改用官方 UMID 组件现算（risk_identity），
      不再用 derive_* 伪造。取不到就不带 —— 宁可「查不到活动」，也不发假身份。
      （实测：带假身份 = showCampaign:false = 永远没有签到期。）

    ★★ 2026-10-24：新增 `app_env` —— 指定用**哪一套**桌面端身份签名/自述
      （`3` 国际版 / `0` 国内版；None = 按 realm 推断）。签到跨区域兜底
      就是靠它把同一台机器的另一套身份带上，见 `checkin()`。
    """
    cfg = realm_cfg(realm)
    uid = str(account.get("uid") or "")
    h = _auth_headers(account, realm)
    h.update({
        "cosy-clienttype": DESKTOP_CLIENT_TYPE,
        "cosy-clientversion": client_version(realm),
        "cosy-user": uid,
        "cosy-machineid": derive_id(uid, "machine"),
        "cosy-machineos": cosy_machine_os(),
        "x-request-id": generate_request_id(),
    })
    risk, _err = risk_identity(realm, uid, app_env)
    if risk:
        h["cosy-machinetoken"] = risk["machineToken"]
        h["cosy-machinecode"] = risk["machineCode"]
        h["cosy-machinetype"] = risk["machineType"]
    else:
        # 完全取不到 UMID 组件时保留旧的派生值，至少让非活动类请求不 400；
        # 签到路径会另行把「缺风控身份」如实报出来（见 checkin 的 no_risk_identity）。
        h["cosy-machinetoken"] = derive_machine_token(uid)
        h["cosy-machinetype"] = derive_machine_type(uid)
    return h


# ═══════════════════════════ 账号操作 ═══════════════════════════

def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(48)).decode("ascii").rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    return verifier, challenge


def local_machine_id(realm: str) -> str:
    """本机官方客户端的 machine_id（有就沿用，保持设备一致），没有则随机。"""
    cfg = realm_cfg(realm)
    p = os.path.join(os.path.expanduser("~"), cfg.get("home_dir") or ".qoder",
                     ".auth", "machine_id")
    try:
        with open(p, encoding="utf-8") as fh:
            mid = fh.read().strip()
        if mid:
            return mid
    except Exception:  # noqa: BLE001
        pass
    return str(uuid.uuid4())


def start_device_login(realm: str) -> dict:
    """构造 PKCE 设备授权 URL（浏览器打开，登录完由网关轮询拿 token）。"""
    cfg = realm_cfg(realm)
    verifier, challenge = _pkce()
    nonce = str(uuid.uuid4()) if cfg.get("nonce_dashed") else uuid.uuid4().hex
    q = {"challenge": challenge, "challenge_method": "S256", "nonce": nonce}
    if cfg.get("send_redirect_uri"):
        q["redirect_uri"] = cfg["redirect_uri"]
    q["client_id"] = cfg["client_id"]
    q["machine_id"] = local_machine_id(realm)
    url = cfg["website"] + "/device/selectAccounts?" + urllib.parse.urlencode(q)
    return {"verifier": verifier, "nonce": nonce, "authUrl": url, "realm": realm}


def poll_device_login(realm: str, verifier: str, nonce: str) -> dict:
    """轮询设备授权结果。返回 {status: pending|ok|error, ...}。"""
    cfg = realm_cfg(realm)
    q = urllib.parse.urlencode({"nonce": nonce, "verifier": verifier,
                                "challenge_method": "S256"})
    url = cfg["openapi"] + PATH_DEVICE_POLL + "?" + q
    req = urllib.request.Request(url, method="GET", headers={
        "Accept": "application/json", "User-Agent": "QoderWork"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 202):
            return {"status": "pending"}
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return {"status": "error", "message": f"poll http {exc.code} {body[:160]}"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "pending", "message": f"poll error: {exc}"}
    if status in (404, 202):
        return {"status": "pending"}
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return {"status": "pending"}
    token = data.get("token") or data.get("device_token") or ""
    if not token:
        return {"status": "pending"}
    uid = str(data.get("user_id") or "")
    nickname, user_type, org_id, org_name = "", DEFAULT_USER_TYPE, "", ""
    try:
        ui = http_json(cfg["openapi"] + PATH_USERINFO, headers={
            "Accept": "application/json",
            "User-Agent": cfg.get("ua") or CLIENT_UA,
            "Authorization": "Bearer " + token}, timeout=15, retries=1)
        uid = str(ui.get("id") or uid)
        nickname = str(ui.get("name") or "")
        user_type = str(ui.get("user_type") or "") or DEFAULT_USER_TYPE
        org_id = str(ui.get("organization_id") or "")
        org_name = str(ui.get("organization_name") or "")
    except Exception:  # noqa: BLE001
        pass
    exp = data.get("expires_at")
    try:
        exp = int(float(exp)) if exp else 0
        if exp > 1e11:
            exp //= 1000
    except (TypeError, ValueError):
        exp = 0
    return {
        "status": "ok",
        "account": {
            "uid": uid or ("q-" + uuid.uuid4().hex[:24]),
            "nickname": nickname or ("u" + (uid[-8:] if uid else "")),
            "realm": realm,
            "domain": cfg["domain"],
            "accessToken": token,
            "refreshToken": data.get("refresh_token") or "",
            "expiresAt": exp or int(time.time()) + 24 * 3600,
            "source": "oauth",
            "enabled": True,
            "userType": user_type,
            "organizationId": org_id,
            "organizationName": org_name,
        },
    }


def import_pat(pat: str, realm: str) -> dict:
    """导入 pt- 个人访问令牌：交换 jobToken 后返回账号 dict。"""
    pat = str(pat or "").strip()
    if not pat.startswith("pt-"):
        raise ValueError("个人访问令牌必须以 pt- 开头")
    cfg = realm_cfg(realm)
    data = http_json(cfg["openapi"] + PATH_JOB_EXCHANGE,
                     data=json.dumps({"personal_token": pat}).encode(),
                     method="POST",
                     headers={"Content-Type": "application/json",
                              "Accept": "application/json",
                              "User-Agent": cfg.get("ua") or CLIENT_UA},
                     timeout=30)
    token = data.get("token") or ""
    if not token:
        raise ValueError("jobToken 交换未返回 token")
    uid = nickname = ""
    user_type, org_id, org_name = DEFAULT_USER_TYPE, "", ""
    try:
        ui = http_json(cfg["openapi"] + PATH_USERINFO, headers={
            "Accept": "application/json",
            "User-Agent": cfg.get("ua") or CLIENT_UA,
            "Authorization": "Bearer " + token}, timeout=15, retries=2)
        uid = str(ui.get("id") or "")
        nickname = str(ui.get("name") or "")
        user_type = str(ui.get("user_type") or "") or DEFAULT_USER_TYPE
        org_id = str(ui.get("organization_id") or "")
        org_name = str(ui.get("organization_name") or "")
    except Exception:  # noqa: BLE001
        pass
    exp = int(time.time() + 24 * 3600)
    if data.get("expires_in"):
        try:
            exp = int(time.time() + int(data["expires_in"]) / 1000)
        except (TypeError, ValueError):
            pass
    return {
        "uid": uid or ("p-" + uuid.uuid4().hex[:24]),
        "nickname": nickname or ("u" + (uid[-8:] if uid else "")),
        "realm": realm,
        "domain": cfg["domain"],
        "accessToken": token,
        "refreshToken": data.get("refresh_token") or "",
        "personalToken": pat,
        "expiresAt": exp,
        "source": "pat",
        "enabled": True,
        "userType": user_type,
        "organizationId": org_id,
        "organizationName": org_name,
    }


def _token_expiry(data: dict, device: bool, fallback: int) -> int:
    """从续期响应里取新的过期时刻。

    实测口径（与参考实现一致）：deviceToken 族返回 ``expires_in``（毫秒）或
    ``expires_at``（RFC3339），都缺省时按 **30 天**（dt- 的官方寿命）；
    jobToken 族只看 ``expires_in``，没有就沿用旧值。

    ★ 老实现直接写死 ``now + 6 小时``：dt- 其实有 30 天，于是每天白续期好几次，
      而每次续期都可能换发新的 refresh token —— 白白增加「旧 token 被复用」的风险。
    """
    raw = data.get("expires_in")
    if raw:
        try:
            return int(time.time() + int(float(raw)) / 1000)
        except (TypeError, ValueError):
            pass
    at = data.get("expires_at")
    if at:
        try:
            return int(float(at) / (1000 if float(at) > 1e11 else 1))
        except (TypeError, ValueError):
            pass
        try:
            import datetime
            dt = datetime.datetime.strptime(str(at)[:19], "%Y-%m-%dT%H:%M:%S")
            return int(dt.timestamp())
        except Exception:  # noqa: BLE001
            pass
    if device:
        return int(time.time()) + 30 * 86400
    return int(fallback or 0)


def refresh_access_token(account: dict) -> tuple:
    """刷新 access token。返回 ``(accessToken, refreshToken, expiresAt)``。

    三条凭证族（**按 refreshToken 的前缀判**，不是按 accessToken）：
      drt-* → deviceToken/refresh（OAuth 设备授权）
      jrt-* → jobToken/refresh，失败再回落 PAT 重新交换
      其它（含只有 PAT 的账号）→ PAT 交换

    ★ 2026-10-23 修正两处：
      1) 设备授权族的两个 token 前缀**不一样** —— ``poll_device_login`` 存下来的
         是 accessToken=``dt-…``、refreshToken=``drt-…``。老实现判的是
         ``token.startswith("drt-")``，于是设备账号两个分支都进不去（job 分支又被
         ``not refresh.startswith("drt-")`` 挡住），直接抛「无可用刷新凭据」
         → **续期从来没成功过**，accessToken 一到期（dt- 官方 30 天，本项目登录
         时也可能只拿到较短有效期）两个 Qoder 通道就整条死掉、只能重新登录。
      2) 设备续期响应里的 token 字段叫 ``device_token``（老实现只认 token /
         access_token，取到空值就静默当作「没有可用凭据」）。
    """
    realm = str(account.get("realm") or "cn")
    cfg = realm_cfg(realm)
    token = str(account.get("accessToken") or "")
    refresh = str(account.get("refreshToken") or "")
    _ = token  # 只用于可读性：判据一律看 refreshToken

    def _post(path, payload):
        return http_json(cfg["openapi"] + path, data=json.dumps(payload).encode(),
                         method="POST", timeout=30,
                         headers={"Content-Type": "application/json",
                                  "Accept": "application/json",
                                  "User-Agent": cfg.get("ua") or CLIENT_UA})

    def _pick(d, device=False):
        t = str(d.get("token") or d.get("access_token")
                or (d.get("device_token") if device else "") or "")
        r = str(d.get("refresh_token") or refresh or "")
        return t, r, _token_expiry(d, device, account.get("expiresAt") or 0)

    errs = []
    if refresh.startswith("drt-"):
        try:
            t, r, exp = _pick(_post(PATH_DEVICE_REFRESH, {"refresh_token": refresh}),
                              device=True)
            if t:
                return t, r, exp
            errs.append("device refresh: 响应里没有 token")
        except Exception as e:  # noqa: BLE001
            errs.append(f"device refresh: {e}")
    if refresh and (refresh.startswith("jrt-") or not refresh.startswith("drt-")):
        try:
            t, r, exp = _pick(_post(PATH_JOB_REFRESH, {"refresh_token": refresh}))
            if t:
                return t, r, exp
            errs.append("job refresh: 响应里没有 token")
        except Exception as e:  # noqa: BLE001
            errs.append(f"job refresh: {e}")
    pat = str(account.get("personalToken") or "")
    if pat:
        try:
            t, r, exp = _pick(_post(PATH_JOB_EXCHANGE, {"personal_token": pat}))
            if t:
                return t, r, exp
        except Exception as e:  # noqa: BLE001
            errs.append(f"pat exchange: {e}")
    raise RuntimeError("Qoder token 刷新失败: " + ("; ".join(errs) or "无可用刷新凭据"))


def fetch_userinfo(account: dict) -> dict:
    realm = str(account.get("realm") or "cn")
    cfg = realm_cfg(realm)
    return http_json(cfg["openapi"] + PATH_USERINFO,
                     headers=_auth_headers(account, realm), timeout=15, retries=2) or {}


# ═══════════════════════════ 余额 ═══════════════════════════

def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def account_quota(account: dict) -> dict:
    """取 `/api/v2/quota/usage` 的**原始**配额对象（含 userType / isQuotaExceeded）。

    ★★ 2026-10-24 新增。起因：`01a16c4e` 这个账号怎么都签不上，而 config.json
      里 userType 明明写着 `personal_professional_trial`（与能签上的那个一样）。
      直接问上游才发现两者真实类型不同：

        账号 A  userType=personal_professional_trial  isQuotaExceeded=false
                     userQuota.remaining=300  addOnQuota.remaining=300
        → campaigns 里有 CLAIM_BENEFIT

        账号 B        userType=personal_standard           isQuotaExceeded=true
                     userQuota.remaining=0    **没有 addOnQuota 字段**
        → campaigns 只有 VIEW_DETAILS

      **结论：免费版（personal_standard）不在每日签到活动的发放范围内。**
      这是上游的产品策略，不是客户端/代码问题。

      `fetch_credits` 只返回一个数字，判断不了这件事；这里把原始对象暴露出来。
      失败返回 {}（调用方必须能容忍空 dict，绝不因此报错）。
    """
    realm = str(account.get("realm") or "cn")
    try:
        d = http_json(realm_cfg(realm)["openapi"] + PATH_QUOTA,
                      headers=_auth_headers(account, realm), timeout=20, retries=1)
    except Exception:  # noqa: BLE001
        return {}
    return d if isinstance(d, dict) else {}


def fetch_credits(account: dict) -> tuple[float, str]:
    """查账号可用积分。返回 (credits, err)。

    ★ 上游真实形状（2026-10-22 实测 /api/v2/quota/usage，两个区域一致）::

        {
          "totalUsagePercentage": 0.01,          ← 这是「已用百分比」，不是积分！
          "userQuota":  {"total": 300.0, "used": 0.0, "remaining": 300.0, "unit": "credits"},
          "addOnQuota": {"total": 100.0, "used": 0.0, "remaining": 100.0}   ← 签到/活动赠送
        }

    可用积分 = userQuota.remaining + addOnQuota.remaining（两个池子都能用）。

    ⚠️ 曾经的错误实现：在顶层字段里按关键字（total/avail/balance…）模糊求和，
    结果抓到 `totalUsagePercentage`（0.01）当积分 —— 界面上就是「积分 0.01」。
    教训：这种「猜字段名」的兜底在**已知形状**面前是有害的，宁可返回 0 也
    不要编一个看起来很真的数字。现在改为按实测形状精确取值，取不到才回退。
    """
    realm = str(account.get("realm") or "cn")
    cfg = realm_cfg(realm)
    try:
        d = http_json(cfg["openapi"] + PATH_QUOTA,
                      headers=_auth_headers(account, realm), timeout=20, retries=2) or {}
    except Exception as e:  # noqa: BLE001
        return 0.0, str(e)[:160]
    if not isinstance(d, dict):
        return 0.0, "配额响应形状异常"

    def _pool(node) -> float | None:
        """取一个配额池的可用量；unlimited 用 -1 表示。取不到返回 None。"""
        if not isinstance(node, dict):
            return None
        if node.get("unlimited"):
            return -1.0
        for key in ("remaining", "available", "balance", "total"):
            if isinstance(node.get(key), (int, float)):
                return float(node[key])
        return None

    total = 0.0
    got = False
    for key in ("userQuota", "addOnQuota", "shared", "credits", "quota"):
        v = _pool(d.get(key))
        if v is None:
            continue
        if v < 0:            # unlimited：不设上限，直接用 -1 透出
            return -1.0, ""
        total += v
        got = True
    if got:
        return total, ""
    # 形状再次变更时：明确说「没认出来」，而不是静默返回 0 ——
    # 静默 0 会让人误以为「这个账号真的没积分」（2026-10-22 的 0.01 也是这么来的）。
    return 0.0, "配额响应形状未知（未匹配到任何积分池字段）"


# ═══════════════════════════ 签到与活动 ═══════════════════════════

def checkin_status(account: dict) -> tuple[bool, dict]:
    """GET daily-check-in/status → (ok, summary|error)。接口不存在返回 unavailable。"""
    realm = str(account.get("realm") or "cn")
    cfg = realm_cfg(realm)
    try:
        q = http_json(cfg["openapi"] + PATH_CHECKIN_STATUS,
                      headers=_auth_headers(account, realm), timeout=15, retries=2) or {}
    except QoderHTTPError as exc:
        if exc.status in (404, 405, 410):
            return False, {"unavailable": True, "reason": "本区域未开放 sash 签到接口",
                          "http": exc.status}
        if exc.status == 403:
            return False, {"unavailable": True, "http": 403,
                           "reason": "上游拒绝访问（HTTP 403）：可能是出口 IP 被拦截"}
        return False, {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return False, {"error": str(exc)}

    last = ""
    if q.get("lastClaimedAt"):
        try:
            last = time.strftime("%Y-%m-%d", time.localtime(int(q["lastClaimedAt"])))
        except (TypeError, ValueError, OSError):
            last = ""
    status = str(q.get("status") or "")
    today = time.strftime("%Y-%m-%d")
    return True, {
        "status": status,
        "active": status in ("CLAIMABLE", "CLAIMED"),
        "today_checked_in": status == "CLAIMED" and last == today,
        "streak_days": int(q.get("currentStreakDays") or 0),
        "reward_credits": int(q.get("rewardCredits") or 0),
        "last_claimed_at": int(q.get("lastClaimedAt") or 0),
    }


def _campaigns(account: dict, app_env: str | None = None) -> tuple[list, str]:
    """活动列表（桌面端头，401/403 回退普通头）。返回 (items, err)。

    `app_env`：指定用哪一套桌面端设备身份去查（None = 账号所在区域那套）。
    """
    realm = str(account.get("realm") or "cn")
    cfg = realm_cfg(realm)
    url = cfg["openapi"] + PATH_CAMPAIGNS
    for headers in (desktop_headers(account, realm, app_env),
                    _auth_headers(account, realm)):
        try:
            d = http_json(url, headers=headers, timeout=15, retries=1) or {}
        except QoderHTTPError as exc:
            if exc.status in (401, 403):
                continue
            if exc.status in (404, 405):
                return [], "本区域无活动平台接口"
            return [], str(exc)[:160]
        except Exception as exc:  # noqa: BLE001
            return [], str(exc)[:160]
        items = d.get("campaigns") if isinstance(d.get("campaigns"), list) else \
            d.get("items") if isinstance(d.get("items"), list) else \
            (d.get("data") or {}).get("items") if isinstance(d.get("data"), dict) else None
        if items is None:
            items = d if isinstance(d, list) else []
        return items, ""
    return [], "活动平台接口拒绝访问（401/403）"


# 「每日签到」活动的识别依据（实测 2026-10-22，两个区域一致）::
#   {"campaignKey":"act-20261020-993","actionType":"CLAIM_BENEFIT",
#    "claimStatus":"CLAIMED","benefit":{"kind":"CREDITS","amount":100},
#    "startAt":1792634400,"endAt":1792720740}
# 每天一个活动实例，窗口 [startAt, endAt] 即「今天这一期」。
# ★ 2026-10-23 实测校正：官方领取窗口是**每天 10:00 (UTC+8) 开、次日 10:00 前关**
#   （见 https://docs.qoder.cn/en/events/100credits 与 Qoder-Daily 的 daily_window_active）。
#   旧注释写「10:00 重置」容易被理解成「00:00 也有活动」—— 实际上 00:00~10:00
#   这段上游**不下发**当日这一期，所以每天 00:00 的自动签到必然扑空。
DAILY_ACTION = "CLAIM_BENEFIT"

# 开窗重试：10:00 开窗后常有几十秒~几分钟的下发延迟（Qoder-Daily 同款策略）
WINDOW_RETRY_MAX = int(os.environ.get("QD_WINDOW_RETRY_MAX") or 12)
WINDOW_RETRY_GAP = float(os.environ.get("QD_WINDOW_RETRY_GAP") or 60.0)
WINDOW_ACTIVE_MINUTES = int(os.environ.get("QD_WINDOW_ACTIVE_MINUTES") or 40)


def window_active(now: float | None = None) -> bool:
    """当前是否处在「开窗补给」时段：本地时间 10:00 ~ 10:40。"""
    t = time.localtime(now if now is not None else time.time())
    return t.tm_hour == 10 and t.tm_min < WINDOW_ACTIVE_MINUTES


def in_daily_window(now: float | None = None) -> bool:
    """当前是否落在官方领取窗口内（10:00 开 → 次日 10:00 前）。

    用本地小时判断即可：窗口每天 10:00 切换，所以
    `hour >= 10` 属于「今天的这一期」，`hour < 10` 属于「昨天那一期」
    （昨天的reward 已过期不补，故算不可领）。
    """
    t = time.localtime(now if now is not None else time.time())
    return t.tm_hour >= 10


def daily_campaign(items: list) -> dict | None:
    """从活动列表里挑出「每日签到领积分」那一期（今天这一期）。

    判定：actionType == CLAIM_BENEFIT 且 benefit.kind == CREDITS，
    再要求当前时间落在窗口内（每天一期，窗口就是这一期的有效期）。
    找不到返回 None（该区域没这个活动）。
    """
    now = int(time.time())
    best = None
    for it in items or []:
        if not isinstance(it, dict):
            continue
        if str(it.get("actionType") or "").upper() != DAILY_ACTION:
            continue
        benefit = it.get("benefit") or {}
        if str(benefit.get("kind") or "").upper() != "CREDITS":
            continue
        start = int(it.get("startAt") or 0)
        end = int(it.get("endAt") or 0)
        if start and now < start:
            continue
        if end and now > end:
            continue
        best = it
        break
    return best


def _campaign_benefit_amount(campaign: dict) -> int:
    try:
        return int((campaign.get("benefit") or {}).get("amount") or 0)
    except (TypeError, ValueError):
        return 0


def claim_campaign(account: dict, campaign: dict, app_env: str | None = None) -> dict:
    """领取一个活动。返回 {ok, credited, credits, status, already, msg}。

    ★ 字段名必须用上游真实的 **claimStatus**（不是 status）——
      2026-10-22 实测：写成 status 会读到空串，于是「已领取」被当成「可领取」，
      反复发起 claim 并拿到 409/AlreadyExists，最终报成签到失败。

    ★★ 2026-10-23 按实测响应格式收紧判定。真实 claim 响应长这样：
        {"grantId":"01a16775-...","status":"CLAIMED","replayed":false,
         "benefit":{"kind":"CREDITS","amount":100,...},"campaignId":"..."}
      旧版只看 rewardCredits/benefitAmount/amount 三个**顶层**字段（全都拿不到，
      积分藏在 benefit.amount 里），于是 got=0 又要靠 claimStatus（同样是错的
      字段名）兜底 —— 一旦上游回个非 CLAIMED 也照样 `ok=True credits=面额`。
      现在按 Qoder-Daily 的口径归一：
        status∈{CLAIMED,GRANTED,SUCCESS} 且 replayed=false → 真领到（credited）
        replayed=true / ALREADY / 409 → 今日已领过（credited，但本次 0 入账）
        其它 → **失败**（绝不再拿面额冒充成功）
    """
    realm = str(account.get("realm") or "cn")
    cfg = realm_cfg(realm)
    cid = campaign.get("campaignId") or campaign.get("id")
    if not cid:
        return {"ok": False, "credited": False, "credits": 0, "msg": "活动缺少 campaignId"}
    status = str(campaign.get("claimStatus") or "").upper()
    amount = _campaign_benefit_amount(campaign)
    if status == "CLAIMED":
        return {"ok": True, "credited": True, "credits": 0, "status": status,
                "already": True,
                "msg": f"今日已签到（+{amount} 已入账）" if amount else "今日已签到"}
    if status and status not in ("CLAIMABLE", "ELIGIBLE", "ACTIVE", "PENDING"):
        # 例如 ACHIEVEMENT_NOT_COMPLETED：活动条件没达成，不是签到失败也不是成功
        return {"ok": True, "credited": False, "credits": 0, "status": status,
                "msg": f"签到活动当前状态 {status}，本次未入账"}
    for headers in (desktop_headers(account, realm, app_env),
                    _auth_headers(account, realm)):
        try:
            r = http_json(cfg["openapi"] + PATH_CAMPAIGN_CLAIM % cid,
                          data=b"{}", method="POST", timeout=15, retries=1,
                          headers=headers) or {}
        except QoderHTTPError as exc:
            body = str(exc.body or "")
            if "ALREADY" in body.upper() or exc.status == 409:
                return {"ok": True, "credited": True, "credits": 0, "already": True,
                        "msg": "今日已签到"}
            if exc.status in (401, 403):
                continue
            return {"ok": False, "credited": False, "credits": 0, "msg": str(exc)[:160]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "credited": False, "credits": 0, "msg": str(exc)[:160]}
        # 归一响应（与新通道上游一致：可能外面再包一层 data）
        body = r.get("data") if isinstance(r.get("data"), dict) else r
        got_status = str(body.get("status") or "").upper()
        replayed = body.get("replayed") is True
        result = str(body.get("result") or "").upper()
        benefit = body.get("benefit") if isinstance(body.get("benefit"), dict) else {}
        real = _campaign_benefit_amount({"benefit": benefit}) or amount
        if replayed or "ALREADY" in result:
            return {"ok": True, "credited": True, "credits": 0, "status": got_status,
                    "already": True, "msg": "今日已签到"}
        if got_status in ("CLAIMED", "GRANTED", "SUCCESS"):
            return {"ok": True, "credited": True, "credits": real,
                    "status": got_status,
                    "msg": f"签到成功 +{real} 积分" if real else "签到成功"}
        # 上游 2xx 但没给出 CLAIMED → 视为未确认成功（绝不按面额冒充）
        fail = str(body.get("failureCode") or body.get("errorCode") or "")
        return {"ok": False, "credited": False, "credits": 0, "status": got_status,
                "msg": f"领取未确认（status={got_status or '?'}"
                       f"{('，' + fail) if fail else ''}）"}
    return {"ok": False, "credited": False, "credits": 0,
            "msg": "活动领取被拒绝（401/403）"}


def claim_campaigns(account: dict) -> dict:
    """领取活动平台上所有「可领取」的奖励（幂等）。"""
    items, err = _campaigns(account)
    if err:
        return {"ok": False, "credits": 0, "message": err}
    got, msgs = 0, []
    for it in items:
        if not isinstance(it, dict):
            continue
        if str(it.get("claimStatus") or "").upper() not in ("CLAIMABLE", "ELIGIBLE"):
            continue
        r = claim_campaign(account, it)
        if r.get("ok"):
            got += int(r.get("credits") or 0)
            msgs.append(r.get("msg") or "已领取")
        else:
            msgs.append(f"活动领取失败: {r.get('msg')}")
    return {"ok": True, "credits": got, "message": "; ".join(msgs) or "无可领取的活动"}


# ═══════════════ 签到「跨区域设备身份」兜底（2026-10-24）═══════════════
#
# 背景：上游**按「设备」发放**每日签到 —— 同一台机器、每个区域、每天只有一份。
#   实测（2026-10-24）：同一时刻 账号 A 有 CLAIM_BENEFIT(CLAIMED)，
#   而同机同区域的 账号 B 只有 VIEW_DETAILS；把 账号 B 换一套设备身份
#   （UMID env=0）后，CLAIM_BENEFIT 立刻出现且为 CLAIMABLE。
#
# 本兜底：拿不到 CLAIM_BENEFIT 时，换**本机另一套**桌面端身份再查一次。
#   两套身份都由官方 UMID 组件按本机硬件指纹现算，是这台机器的真实身份，
#   不是伪造值 —— 语义上等价于「本机也装了另一个版本的客户端」。
#
# ★ 默认**关闭**，由 config 显式打开（master 2026-10-24 的要求）。
#   开关写在 providers.<qoder_cn|qoder_intl>.signinFallbackIdentity。

_FALLBACK_REALM_ENV = {"cn": "0", "intl": "3"}


def _provider_key(account: dict) -> str:
    """账号所属的 provider 配置键（config.json 的 providers.<key>）。"""
    return "qoder_" + str(account.get("realm") or "cn")


def signin_fallback_app_env(account: dict) -> str | None:
    """签到拿不到活动时，额外用**哪一套**桌面端身份再查一次。返回 app_env 或 None。

    配置（`providers.<qoder_cn|qoder_intl>.signinFallbackIdentity`）::

        "cn"    → 改用国内版桌面端身份（UMID env=0）
        "intl"  → 改用国际版桌面端身份（UMID env=3）
        "auto" / true → 自动用「另一个区域」那套
        false / 缺失  → 关闭（行为与旧版完全一致）

    也可用环境变量 `QD_SIGNIN_FALLBACK_IDENTITY` 兜底（仅当 config 没写时生效）。

    与账号自身区域相同 → 返回 None（换了个寂寞，不做无意义的二次请求）。
    """
    raw = None
    try:
        import app_paths as _ap
        with open(_ap.CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        seg = (cfg.get("providers") or {}).get(_provider_key(account)) or {}
        raw = seg.get("signinFallbackIdentity")
    except Exception:  # noqa: BLE001 — 读不到配置就当没开
        raw = None
    if raw is None:
        raw = (os.environ.get("QD_SIGNIN_FALLBACK_IDENTITY") or "").strip() or None
    if raw is None:
        return None
    if isinstance(raw, bool):
        name = "auto" if raw else ""
    else:
        name = str(raw).strip().lower()
    if name in ("", "0", "off", "false", "no", "none", "disable", "disabled"):
        return None
    own = str(account.get("realm") or "cn")
    if name in ("auto", "other", "cross", "1", "on", "true", "yes"):
        name = "intl" if own == "cn" else "cn"
    if name == own:            # 与自身区域相同 → 无意义
        return None
    return _FALLBACK_REALM_ENV.get(name)


def checkin(account: dict) -> dict:
    """每日签到。返回 {ok, credited, credits, amount, potential, already?, msg}。

    ★ 判定顺序按**实测的可靠性**排（2026-10-22）：
      1) **活动平台**（sash 之外的 growth/activity 平台）—— 这是当前真正在跑的
         每日签到；CN/INTL 都能查到那一期活动，`claimStatus=CLAIMED` 即「今天已签」。
      2) sash 接口（`/sash/api/v1/me/daily-check-in/*`）—— 早期通道。CN 上仍在，
         但状态恒为 DISABLED（活动已搬走），故只能当**兜底**，且不再拿它判失败。
         旧实现把它当主路径，于是「活动平台明明已签」也被报成签到失败。

    ★★ 2026-10-23 修「没有签到却显示已签到」（master 报障，实测复现）：
       旧版兜底段最后一行 **无条件** `return {"ok": True, "credits": 0, ...}`，
       于是「活动列表为空 + sash 无签到活动（DISABLED / 404）」也被报成成功。
       叠加 signin_all 只看 `ok`、`amount` 取的又是**活动面额**（不是本次真入账），
       最终写出了一天假的 gain 行 → 账号页显示「已签到」，而积分一分没到
       （实测两队 addOnQuota.remaining 恒为注册赠的 100，签到积分 0）。

       现在把三个语义**彻底分开**，任何调用方都不可能再把「无事可签」当成功：
         * ok       —— 本次调用**是否正常跑完**（无异常/无上游故障）；
                       它**不代表**签到成功，旧调用方一律改用 credited 判断。
         * credited —— **本次调用是否真的完成了一次入账**（领到了 / 今日已领过）。
         * amount   —— 当日该活动的**面额**（参考值，可能远大于本次真入账）。
         * potential—— 与 amount 同义，供新代码使用（语义更明确）。
       无签到活动时返回 ok=True / credited=False + reason='no_campaign'。
    """
    realm_key = str(account.get("realm") or "cn")
    _risk, risk_err = risk_identity(realm_key, str(account.get("uid") or ""))
    items, err = _campaigns(account)
    # ★ 10:00 开窗补给：开窗后上游下发活动常有数十秒~数分钟延迟，
    #   这一轮拿不到就等下一轮（只在 10:00~10:40 内重试，避免拖住整轮签到）。
    if not err and window_active() and not daily_campaign(items):
        for _i in range(WINDOW_RETRY_MAX):
            time.sleep(WINDOW_RETRY_GAP)
            items, err = _campaigns(account)
            if err or daily_campaign(items) or not window_active():
                break

    # ★★ 2026-10-24（master 决定）：跨区域设备身份兜底。
    #   上游按「设备」发放 —— 同机同区域每天只有一份。本区域的桌面端身份
    #   拿不到 CLAIM_BENEFIT 时，若配置了 signinFallbackIdentity，就换
    #   本机另一套真实桌面端身份再查一次（见 signin_fallback_app_env）。
    #   只有**真的查到签到活动**才采用兜底结果，否则保持原样继续往下走
    #   （绝不给「查不到」编造成功）。
    fb_env = signin_fallback_app_env(account)
    fb_used = False
    if fb_env is not None and not daily_campaign(items):
        items_fb, err_fb = _campaigns(account, app_env=fb_env)
        if daily_campaign(items_fb):
            items, err, fb_used = items_fb, err_fb, True
            logger.info("%s 账号 %s 用本区域设备身份查不到签到活动，"
                        "已改用跨区域设备身份（UMID env=%s）并查到，本次按该身份领取",
                        _provider_key(account),
                        str(account.get("uid") or "")[:8], fb_env)

    if not err:
        daily = daily_campaign(items)
        if daily:
            r = claim_campaign(account, daily,
                               app_env=fb_env if fb_used else None)
            if r.get("credited"):
                # amount/potential = 今日该期活动的**面额**（已签到时也给出）；
                # credited 才是「今日确实已入账」的判据 —— 调用方据此写 gain 表。
                # ★ credits 是**本次真入账**：刚领到时 = benefit.amount；
                #   「今日已领过」(already) 时为 0，但 amount 仍给出当日额度，
                #   供 gain 表记录「今天确实签过、额度多少」。
                _amt = _campaign_benefit_amount(daily)
                return {"ok": True, "credited": True,
                        "credits": int(r.get("credits") or 0),
                        "amount": _amt, "potential": _amt,
                        "reason": "campaign_claimed",
                        "identity": ("fallback_env_" + str(fb_env)) if fb_used else "realm",
                        "already": bool(r.get("already")), "msg": r.get("msg")}
            return {"ok": False, "credited": False, "credits": 0, "amount": 0,
                    "potential": _campaign_benefit_amount(daily),
                    "reason": "claim_failed", "msg": r.get("msg") or "签到失败"}
        # 活动平台在，但没有「每日签到」这一期 → 顺手领其它可领取项
        camp = claim_campaigns(account)
        if camp.get("credits"):
            return {"ok": True, "credited": True, "credits": camp["credits"],
                    "amount": camp["credits"], "potential": camp["credits"],
                    "reason": "campaign_claim", "msg": camp["message"]}
    # 兜底：老 sash 通道（只在活动平台不可用/无该活动时尝试）
    ok, st = checkin_status(account)
    if ok and st.get("today_checked_in"):
        # sash 明确回报「今天已领」—— 这是真的入账凭证（credits 不给，只有面额）
        return {"ok": True, "credited": True, "already": True, "credits": 0,
                "amount": int(st.get("reward_credits") or 0),
                "potential": int(st.get("reward_credits") or 0),
                "reason": "sash_claimed", "msg": "今日已签到"}
    if ok and st.get("active"):
        realm = str(account.get("realm") or "cn")
        cfg = realm_cfg(realm)
        try:
            res = http_json(cfg["openapi"] + PATH_CHECKIN_CLAIM, data=b"{}",
                            method="POST", timeout=15, retries=1,
                            headers=_auth_headers(account, realm)) or {}
        except QoderHTTPError as exc:
            if "ALREADY_CLAIMED" in (exc.body or "") or exc.status == 409:
                return {"ok": True, "credited": True, "already": True,
                        "credits": 0, "reason": "sash_claimed", "msg": "今日已签到"}
            return {"ok": False, "credited": False, "credits": 0,
                    "reason": "sash_error", "msg": str(exc)[:160]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "credited": False, "credits": 0,
                    "reason": "sash_error", "msg": str(exc)[:160]}
        reward = int(res.get("rewardCredits") or 0)
        return {"ok": True, "credited": True, "credits": reward,
                "amount": reward, "potential": reward, "reason": "sash_claim",
                "msg": f"签到成功 +{reward} 积分" if reward else "签到成功"}
    if err:
        # 活动平台不可用 = 本次**无法判定**，按失败上报（绝不谎报成功）
        return {"ok": False, "credited": False, "credits": 0, "amount": 0,
                "potential": 0, "reason": "campaigns_unavailable",
                "msg": f"活动平台不可用：{err}"}
    # ★ 活动平台正常、但今天既没有「每日签到」这一期、sash 也没有已领记录
    #   → 就是「今天无签到可签」，必须如实上报，这是本次修复的核心。
    #   实测 2026-10-23：CN `showCampaign=false` / `campaigns=[]`；INTL 只剩
    #   VIEW_DETAILS 活动。两者都落到这里。
    if risk_err:
        # ★ 缺设备风控身份时上游**根本不下发活动**（实测：showCampaign:false），
        #   这时报「今天没有活动」是误导 —— 必须把真原因说出来。
        return {"ok": False, "credited": False, "credits": 0, "amount": 0,
                "potential": 0, "reason": "no_risk_identity",
                "msg": f"缺少设备风控身份，上游未下发签到活动：{risk_err}"}
    # ★★ 2026-10-24（经 master 纠正后重查）：上游**按「设备」发放**每日签到活动，
    #    而不是按「账号」。同一台机器上，每个区域每天只有一份额度。
    #
    #    实锤（国际版两个账号，同一时刻）：
    #      · 两个账号拿到的 machineCode **完全相同**（e656a6847603…），
    #        因为它们共用本机同一份 UMID 身份 —— `risk_identity()` 的身份
    #        来自**本机硬件**，传进去的 account 参数对它没有任何影响（实测：
    #        account 为空 / 两个不同 uid，machineCode 三次完全一致）。
    #      · 账号 A 用这台设备领了今天的 100 credits（CLAIM_BENEFIT/CLAIMED），
    #        于是上游对同设备的账号 B**不再下发** CLAIM_BENEFIT，只剩 VIEW_DETAILS。
    #
    #    决定性 A/B（同一个账号 B账号，只换设备身份）：
    #        env=3（国际版身份 e656a684…）→ ['VIEW_DETAILS', 'VIEW_DETAILS']
    #        env=0（国内版身份 690fc5ab…）→ ['CLAIM_BENEFIT', 'VIEW_DETAILS', ...]
    #      → 换一台"设备"，活动立刻出现。**资格是设备级的，不是账号级的。**
    #
    #    ⚠️ 顺带纠正一个错误结论（本模块上一版写错、当天已撤）：
    #      「免费版 personal_standard 不发放」是**错的**。官方文档明确写着
    #      「适用用户：国际版新老个人用户，**包括体验版（免费）**、Pro 试用期、
    #      Lite、Pro、Pro+ 和 Ultra 用户」——
    #      https://docs.qoder.com/zh/events/100credits
    #      userType 在 trial / standard 之间不同只是注册渠道/时间差异，不是判据。
    #
    #    结论：`no_campaign` 在这里的真正含义是「这台设备今天已经没有可领的了」。
    # 仅用于日志/排障，**不作为判据** —— 见上方说明（上游按设备发放，不是按套餐）
    quota = account_quota(account) or {}
    utype = str(quota.get("userType") or account.get("userType") or "?")
    exceeded = quota.get("isQuotaExceeded")
    _ = (utype, exceeded)
    #    ★ 2026-10-24 补：若配置了跨区域身份兜底，这里说明「兜底也试过了」——
    #      否则用户会以为只是没查（实际两种身份都查不到，是真的没额度）。
    _fb_note = (f"；已改用跨区域设备身份（UMID env={fb_env}）重查，仍无"
                if fb_env is not None else "")
    return {"ok": True, "credited": False, "credits": 0, "amount": 0, "potential": 0,
            "reason": "no_campaign",
            "msg": ("今日该设备无可领签到活动"
                    "（上游按「设备」发放：同一台机器每个区域每天只有一份；"
                    f"本机已有一账号领过本轮额度。sash status={st.get('status') or '?'}"
                    f"{_fb_note}）")}


# ═══════════════════════════ 模型目录 ═══════════════════════════

_CATALOG_CACHE: dict[str, tuple[float, list]] = {}
_CATALOG_TTL = 6 * 3600
_CATALOG_LOCK = threading.Lock()


def _bundled_catalog(realm: str) -> list:
    name = "qoder_catalog_intl.json" if realm == "intl" else "qoder_catalog_cn.json"
    # frozen: 目录 JSON 在 _MEIPASS\providers; 源码态在 providers/ (见 qoder_base 同款说明)
    try:
        res_dir = os.path.join(sys._MEIPASS, "providers")  # type: ignore[attr-defined]
    except Exception:  # 源码态
        res_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(res_dir, name)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def static_catalog(realm: str) -> list:
    """离线兜底模型目录（随仓库打包，来自开源项目快照）。"""
    return [m for m in _bundled_catalog(realm) if isinstance(m, dict) and m.get("key")]


# 上游模型目录的「场景」概念（2026-10-22 实测）：GET /algo/api/v2/model/list?Encode=1
# 返回的是 {scene: [模型...]} 字典，实测场景有
#   chat / assistant / developer / inline / quest / qwork / experts / qwake / app
# 对话用 chat（与 assistant 的模型键集完全一致，实测 CN/INTL 均如此）。
CATALOG_SCENE = "chat"


def fetch_catalog(account: dict, force: bool = False) -> list:
    """拉上游模型目录 → [{key, display_name, price_factor, ...}]。

    ★ 请求必须是 **GET**（2026-10-22 修正，这是「模型列表变少」的根因）：
      官方客户端（app.asar 里的 qoder-agent-sdk）用的是
      ``GET /api/v2/model/list?Encode=1``（经 /algo 前缀）；
      本模块原先发的是 **POST + JSON body**，上游回
      ``400 Request method 'POST' not supported``，异常被 catch 后
      **静默回退到仓库里那份静态快照** —— 于是界面上显示的是一份过期目录
      （CN 14 个 / INTL 17 个），而且快照里大量条目 enable=false，
      又被进一步过滤掉，看起来就是「模型少了很多」。
      静默回退是最坏的部分：它把「请求失败」伪装成「上游就这些模型」。

    ★ 响应是 {scene: [...]}，这里取 chat 场景。

    失败才回退静态快照（快照仍在仓库里，作为断网兜底）。
    """
    realm = str(account.get("realm") or "cn")
    with _CATALOG_LOCK:
        hit = _CATALOG_CACHE.get(realm)
        if hit and not force and time.time() - hit[0] < _CATALOG_TTL:
            return hit[1]
    static = static_catalog(realm)
    try:
        url = realm_cfg(realm)["gateway"] + MODELS_PATH
        sess = SESSIONS.get(account)
        # GET 无 body：签名的 body 段为空串
        headers = sess.headers("", url, model_key="", sse=False,
                               accept="application/json")
        d = http_json(url, method="GET", headers=headers, timeout=20, retries=1) or {}
        items = None
        if isinstance(d, dict):
            if isinstance(d.get("data"), list):          # 兼容扁平形态
                items = d["data"]
            elif isinstance(d.get(CATALOG_SCENE), list):
                items = d[CATALOG_SCENE]
            if items is None:
                # 场景名变了也要能用：取第一个非空的场景列表
                for k, v in d.items():
                    if isinstance(v, list) and v:
                        logger.warning("%s 模型目录没有 %s 场景，改用 %s",
                                       realm, CATALOG_SCENE, k)
                        items = v
                        break
        if not isinstance(items, list) or not items:
            raise RuntimeError("模型目录响应为空或结构不认识")
        out = []
        for m in items:
            if isinstance(m, str):
                out.append({"key": m, "display_name": m})
            elif isinstance(m, dict) and (m.get("key") or m.get("model_key")):
                out.append({
                    "key": m.get("key") or m.get("model_key"),
                    "display_name": m.get("display_name") or m.get("name") or "",
                    "is_reasoning": bool(m.get("is_reasoning")),
                    "is_vl": bool(m.get("is_vl")),
                    "enable": m.get("enable"),
                    # price_factor = 该模型的积分倍率（倍率页直接用，见 provider.rate_table）
                    "price_factor": m.get("price_factor"),
                    "max_input_tokens": m.get("max_input_tokens") or 180000,
                })
        if not out:
            raise RuntimeError("模型目录解析为空")
        logger.info("%s 模型目录已从上游拉取: %d 个（scene=%s）",
                    realm, len(out), CATALOG_SCENE)
        with _CATALOG_LOCK:
            _CATALOG_CACHE[realm] = (time.time(), out)
        return out
    except Exception as e:  # noqa: BLE001
        if static:
            logger.warning("%s 模型目录上游拉取失败，回退仓库内快照（%d 个）: %s",
                           realm, len(static), str(e)[:160])
            with _CATALOG_LOCK:
                _CATALOG_CACHE[realm] = (time.time(), static)
        return static


def resolve_model_key(model: str, catalog: list, realm: str = "cn") -> str:
    """把对外模型名解析成上游 model key。

    接受：官方 key（gmodel）/ 显示名（GLM-5.3，不分大小写）/
    「key (Name)」展示形式 / 裸名。解析不出时原样返回（上游会给出明确报错）。
    """
    raw = str(model or "").strip()
    if not raw:
        return "auto"
    keys = {str(m.get("key")) for m in (catalog or []) if m.get("key")}
    if raw in keys:
        return raw
    low = raw.lower()
    for m in catalog or []:
        k = str(m.get("key") or "")
        dn = str(m.get("display_name") or "")
        if not k:
            continue
        if low in (k.lower(), dn.lower()):
            return k
        if dn and low == f"{k.lower()} ({dn.lower()})":
            return k
    for m in static_catalog(realm):
        k = str(m.get("key") or "")
        if low in (k.lower(), str(m.get("display_name") or "").lower()):
            return k
    return raw


def catalog_meta(model_key: str, realm: str = "cn") -> dict:
    for m in static_catalog(realm):
        if str(m.get("key")) == str(model_key):
            return m
    with _CATALOG_LOCK:
        hit = _CATALOG_CACHE.get(realm)
    for m in (hit[1] if hit else []):
        if str(m.get("key")) == str(model_key):
            return m
    return {}


# ═══════════════════════════ SSE 信封解包 ═══════════════════════════

# ── 上游排队（2026-10-22 实测）────────────────────────────────────────────
# Qoder 在容量紧张时对同一个模型回 **HTTP 403**，body 形如
#   {"code":"403","message":"{\"code\":\"10605\",\"message\":\"{\\\"isQueued\\\":true,
#     \\\"modelKey\\\":\\\"dfmodel\\\",\\\"retryAfterSeconds\\\":30}"}"}
# 即「已排队，请等 N 秒」。这**不是**鉴权失败 —— 当致命错误直接抛，会被
# 上层判成「断连」（check_model 里 403 → down），于是模型列表上一片红。
# 参考实现的做法（Qoder2API-Hub issue #38）是：等 retryAfterSeconds 后重试同一账号，
# 并把「还在排队」按繁忙上报。这里按同等口径实现。
# 默认值与参考实现（Qoder2API-Hub）对齐：最多等 3 轮、单轮上限 180s、
# 整次请求总预算 600s。实测 CN 的热门模型（DeepSeek-Flash）排队人数可达 2000+，
# 预算给宽一些才有机会等到放行；超预算仍按「繁忙」如实上报，不假装成功。
QUEUE_MAX_ROUNDS = int(os.environ.get("QD_QUEUE_MAX_ROUNDS") or 3)
QUEUE_WAIT_MAX_SECS = int(os.environ.get("QD_QUEUE_WAIT_MAX_SECS") or 180)
QUEUE_WAIT_BUDGET_SECS = int(os.environ.get("QD_QUEUE_WAIT_BUDGET_SECS") or 600)
QUEUE_WAIT_JITTER_SECS = float(os.environ.get("QD_QUEUE_WAIT_JITTER") or 2.0)


def is_resource_limit(detail: str) -> bool:
    """响应体是否为「资源受限」而非「排队」。返回 True 时**不值得等待重试**。

    ★★ 2026-10-24 实测：上游对「当前没有可用资源」的模型返回的是
        {"isQueued":true,"modelKey":"dmodel","queueCount":0,"queueType":"p3",
         "retryAfterSeconds":30,"serviceAvailable":false,
         "subCode":"RESOURCE_LIMIT","waitTime":30}
    它**长得像排队**（isQueued=true + retryAfterSeconds），但
      * `queueCount == 0`  —— 根本没人排队，不是挤不进去；
      * `serviceAvailable == false` + `subCode == "RESOURCE_LIMIT"`
        —— 上游明确说「该模型此刻没有服务」。
    这种状态下等多久都不会放行。旧版只认 retryAfterSeconds，于是
    白等 30s × QUEUE_MAX_ROUNDS(3) = 90 秒，最后还报成「通道繁忙」，
    既费时又给错方向（用户以为等一会就好，其实要换模型/等资源恢复）。
    """
    d = detail or ""
    low = d.lower()
    if "resource_limit" in low:
        return True
    # serviceAvailable:false 单独出现也视为无资源（保守：只在明确的 false 上判）
    if re.search(r'"serviceavailable"\s*:\s*false', low):
        return True
    return False


def queue_wait_seconds(detail: str) -> int:
    """从上游响应体里解析「等多久」；不是排队信号返回 0。

    ★ 2026-10-24：资源受限（RESOURCE_LIMIT）**不算排队**，返回 0 ——
      调用方据此立刻失败并给出准确文案，不再干等 90 秒。

    ★★ 2026-10-24（二次收紧）：判定条件过去含一个兜底的 `"queue" in low`，
      于是**任何带 `queueCount` / `queueType` 字样的响应都被当成排队** ——
      RESOURCE_LIMIT 之所以没白等，纯粹是靠上面那句 `is_resource_limit()`
      提前拦截（"靠豁免"，不是"判定正确"）。任何新形态（`serviceAvailable:true`
      但没给 `retryAfterSeconds`、`subCode` 变了）都会落进"保守等 30 秒 × 3 轮"。
      现在改为**只认真正的排队特征**：
        · 业务码 10605，或
        · 显式 `"isQueued": true`
      且**必须** `queueCount > 0`（有人排队）。`queueCount == 0` 说明队列是空的，
      等多久都没用 —— 直接判非排队。
    """
    d = detail or ""
    low = d.lower()
    if is_resource_limit(d):
        return 0
    queued = ("10605" in d) or bool(re.search(r'"isqueued"\s*:\s*true', low))
    if not queued:
        return 0
    # queueCount 明确为 0 → 没人排队，不是"挤不进去"，等也没意义
    m_count = re.search(r'"queuecount"\s*:\s*(\d+)', low)
    if m_count:
        try:
            if int(m_count.group(1)) <= 0:
                return 0
        except (TypeError, ValueError):
            pass
    # serviceAvailable:false 也说明没有服务（is_resource_limit 已覆盖，
    # 这里再兜一层：避免 "RESOURCE_LIMIT" 字样换了大小写/编码时漏判）
    if re.search(r'"serviceavailable"\s*:\s*false', low):
        return 0
    m = re.search(r"retryAfterSeconds\D+(\d+)", d)
    if not m:
        return 30                     # 确实在排队但没给时长：保守按 30s
    try:
        return max(1, int(m.group(1)))
    except (TypeError, ValueError):
        return 30


class QoderResourceLimited(RuntimeError):
    """上游明确回报「该模型当前没有可用资源」（RESOURCE_LIMIT）。

    与 QoderQueued 分开成类，是因为两者的**用户动作完全不同**：
      * QoderQueued            → 等一会儿再试（真的在排队）；
      * QoderResourceLimited   → 换模型 / 稍后再说（等也没用）。
    上层据此给出不同文案，绝不把后者渲染成「繁忙」。

    ★★ 2026-10-24（master 反馈后重做）：这个异常**不该直接冒到用户面前**。

    master 的原话是「那不应该是让用户等待模型请求吗？怎么就直接断连了」。
    实测支持这个判断：`RESOURCE_LIMIT` 是**瞬时状态**——连续轮询国内版 12 个
    模型两轮，24 次请求全部 200；而历史 20 次失败集中在 13/14/17 点几个短窗。
    也就是说换个模型、或隔几十秒再来，基本都能成功。

    因此 provider 层在抛出它之前会**自己先try**：
      ① 同一模型短暂等待后重试（见 QD_RL_RETRY_*）；
      ② 仍不行 → 在同通道目录里**自动换一个可用模型**（见 QD_RL_FALLBACK）；
      ③ 全部失败才把异常抛给用户，并说明「已尝试 N 个模型」。

    `fallbacks_tried` 记录已经试过的模型，供上层写错误文案。
    """

    def __init__(self, message: str = "", fallbacks_tried: list | None = None):
        super().__init__(message)
        self.fallbacks_tried = list(fallbacks_tried or [])


# ── RESOURCE_LIMIT 冷却窗口（2026-10-24）──────────────────────────────
# 上游说「这个模型没有可用资源」后，**同一个 (realm, model) 在几十秒内
# 基本不会变**（实测 dmodel/gmodel/qmodel_38max 持续数分钟都是 RESOURCE_LIMIT）。
# 而 auto 路由探活 + 用户点击会在同一分钟内反复打同一个模型：每次都白走一趟
# 上游 RTT，还额外增加风控暴露面。这里记一个短冷却窗口，窗口内本地直接
# 抛出同样语义的异常 —— **不消耗上游请求**。
_RL_COOLDOWN: dict = {}
_RL_TTL = float(os.environ.get("QD_RESOURCE_LIMIT_COOLDOWN") or 45.0)
_RL_LOCK = threading.Lock()


def resource_limited_until(realm: str, model_key: str) -> float:
    """该 (realm, model) 的冷却截止时间；已过期或没记录返回 0。"""
    if _RL_TTL <= 0:
        return 0
    key = f"{realm}:{model_key}"
    with _RL_LOCK:
        hit = _RL_COOLDOWN.get(key)
    if not hit:
        return 0
    until, _msg = hit
    return until if until > time.time() else 0


def mark_resource_limited(realm: str, model_key: str, msg: str = "") -> None:
    """记下「该模型此刻无资源」，在 `QD_RESOURCE_LIMIT_COOLDOWN` 秒内不再打上游。"""
    if _RL_TTL <= 0:
        return
    key = f"{realm}:{model_key}"
    with _RL_LOCK:
        _RL_COOLDOWN[key] = (time.time() + _RL_TTL, msg)


def clear_resource_limited(realm: str, model_key: str) -> None:
    key = f"{realm}:{model_key}"
    with _RL_LOCK:
        _RL_COOLDOWN.pop(key, None)


class QoderQueued(RuntimeError):
    """上游排队中（等待预算内仍未放行）。

    单独成类是为了让上层能按「繁忙」而非「断连」上报 —— 通道是活的，
    只是这个模型此刻挤不进去。
    """


def _strip_data_prefix(line: str) -> str:
    s = (line or "").strip()
    if s.startswith("data:"):
        s = s[5:].strip()
    elif s.startswith("event:"):
        return ""
    elif s.startswith(":"):     # 心跳注释行
        return ""
    return s


_NOISE_KEYS = ("reasoning_content", "content", "tool_calls", "function_call")


def clean_chunk(obj: dict) -> str:
    """去掉网关每帧padding的空字段，避免客户端收到一堆空 delta。"""
    if not isinstance(obj, dict):
        return ""
    for choice in obj.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict):
            for k in _NOISE_KEYS:
                if k in delta and not delta.get(k):
                    delta.pop(k, None)
            if not delta and not choice.get("finish_reason"):
                return ""
    return json.dumps(obj, ensure_ascii=False)


def iter_inner_chunks(resp, usage_holder: dict | None = None):
    """把上游 SSE 信封流解包成内层 chunk dict。

    上游帧形态::

        data:{"headers":{...},"body":"<内层 chunk JSON 字符串>","statusCodeValue":200}
        data:{"body":"[DONE]"}
        event:finish{...}        ← 计时元数据，忽略
    """
    for raw in resp.iter_lines():
        if isinstance(raw, bytes):
            try:
                line = raw.decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                continue
        else:
            line = raw
        if line is None:
            continue
        data = _strip_data_prefix(line)
        if not data:
            continue
        try:
            outer = json.loads(data)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(outer, dict):
            continue
        status = outer.get("statusCodeValue")
        body = outer.get("body")
        if status not in (None, 200, "200"):
            raise RuntimeError(
                f"qoder upstream status={status}: "
                f"{(body if isinstance(body, str) else json.dumps(outer, ensure_ascii=False))[:300]}")
        if not isinstance(body, str):
            continue
        if body == "[DONE]":
            return
        try:
            inner = json.loads(body)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(inner, dict):
            continue
        if usage_holder is not None and inner.get("usage"):
            usage_holder["usage"] = inner["usage"]
        # 内层可能是标准 chunk，也可能是上游的业务错误帧
        if inner.get("error_code") or inner.get("code"):
            code = inner.get("error_code") or inner.get("code")
            if str(code) not in ("0", "200", "None", ""):
                raise RuntimeError(
                    f"qoder upstream code={code} msg={inner.get('error_msg') or inner.get('message') or ''}")
        yield inner
