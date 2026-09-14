# -*- coding: utf-8 -*-
"""Loomy (讯飞 Loomy 办公助手) Provider

来源: C:\\Program Files\\Loomy (Electron 套壳 opencode)
真实模型网关: https://loomyad.xunfei.cn/api/v1  (OpenAI 兼容)

鉴权 (与官方客户端一致, useSessionAuth=true):
  Authorization: Bearer <本账号登录 session>  +  token: <本账号登录 session>
  即用「每个账号自己的登录态」发起请求, 积分从该账号扣。
  官方客户端 bundled-resources.js 里 imodel provider 就是 useSessionAuth: true,
  不保存任何 apiKey —— 每个用户用自己的 session, 不存在共享凭据。

  ⚠️ 历史问题: 早期版本曾把反编译得到的 iModel apiKey (98950ac6...) 当共享凭据
  硬编码, 实测该 key 恒定解析到同一个陌生人的账号 (userid 260913114245735244),
  导致所有人的积分/流水都记在那个人头上。已彻底移除该方式。

外部统一模型名: lm-<上游模型id>, 例如 lm-deepseek-v4-flash-0731
  (loomy- 为 v3.0 历史前缀, 仍兼容请求)
路由: providers/__init__.py 按前缀 "lm-"/"loomy" 路由到本 provider。
"""
import asyncio
import json
import logging
import os
import re
import time

import httpx

from .base import Provider, make_chunk_id

logger = logging.getLogger("openapi.providers.loomy")

UPSTREAM_BASE = "https://loomyad.xunfei.cn/api/v1"
UPSTREAM_CHAT_PATH = "/chat/completions"
USER_AGENT = "Loomy/1.0"

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


def _account_session(acc: dict) -> str:
    """取账号的可用凭据。

    Loomy 有两类账号, 凭据形态不同但都能当 Bearer 打上游:
      * 桌面账号 (讯飞账号服务短信登录) → session
      * Web 账号  (loomy.xunfei.cn cookie 会话) → loomy_web_session cookie
    """
    return str(acc.get("session") or (acc.get("cookies") or {}).get("loomy_web_session")
               or "").strip()


def load_config_accounts() -> list[dict]:
    """实时读取 config.json 中 providers.loomy.accounts 的可用账号 (含 session)。

    每次请求实时读取, 保证 GUI 里新登录/禁用账号后无需重启网关即可生效。
    返回项统一补一个 `_token` 字段 (已解析好的 Bearer 凭据), 调用方无需再关心
    账号是桌面形态还是 Web 形态。
    """
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:  # noqa: BLE001
        logger.warning("loomy 读取 config.json 失败: %s", e)
        return []
    loomy = (cfg.get("providers") or {}).get("loomy") or {}
    accounts = list(loomy.get("accounts") or [])
    legacy = loomy.get("account")
    if legacy and _account_session(legacy) and not any(
            a.get("userid") == legacy.get("userid") for a in accounts):
        accounts.append(legacy)
    out = []
    for a in accounts:
        if not a.get("enabled", True):
            continue
        token = _account_session(a)
        if not token:
            continue
        # Web(cookie) 账号不能鉴权对话（实测 100002），只当钱包/签到用，
        # 不进对话轮询 —— 免得每次轮到它都白打一发请求再切换。
        is_web = a.get("kind") == "web" or \
            (not a.get("session") and a.get("cookies"))
        if is_web and not a.get("session"):
            continue
        item = dict(a)
        item["_token"] = token
        out.append(item)
    return out


def load_any_token() -> str:
    """给「模型目录同步」等只读接口用的任意一个账号凭据 (无账号时返回 "")。"""
    for a in load_config_accounts():
        return a["_token"]
    return ""


# 上游模型 id -> 显示名 (来自 Loomy 本地 opencode /config/providers 的 imodel provider)
DEFAULT_MODELS = {
    "deepseek-v4-flash-0731": "DeepSeek V4 Flash 0731",
    "mimo-v2.5": "MiMo V2.5",
    "MiniMax-M3": "MiniMax M3",
    "Kimi-k2.6": "Kimi k2.6",
    "qwen-3.8-max": "Qwen 3.8 Max",
    "GLM-5.3-Flash": "GLM 5.3 Flash",
    "qwen3.8-flash": "Qwen 3.8 Flash",
    "spark-x": "Spark X2.5",
    "doubao-seed-2.0-mini": "Doubao Seed 2.0 mini",
    "doubao-seedream-5-lite": "doubao-seedream-5-lite",
    "qwen-image-3.0-pro": "qwen-image-3.0-pro",
    "qwen3.5-flash": "Qwen3.5 Flash",
}

# 对外统一前缀: lm-<上游模型id> (如 lm-deepseek-v4-flash-0731)。
# 历史前缀 loomy- 仍兼容请求 (v3.1 更名, 旧客户端不用改)。
PREFIX = "lm-"
LEGACY_PREFIXES = ("loomy-",)


def _strip_prefix(model: str) -> str:
    m = (model or "").strip()
    for prefix in (PREFIX, *LEGACY_PREFIXES, "custom-local:"):
        if m.startswith(prefix):
            m = m[len(prefix):]
    return m


def _parse_sse_line(line: str):
    """把一行 SSE 文本解析为 dict; 非数据行返回 None。"""
    line = (line or "").strip()
    if not line or line.startswith(":") or not line.startswith("data:"):
        return None
    data = line[len("data:"):].strip()
    if not data or data == "[DONE]":
        return None
    try:
        return json.loads(data)
    except Exception:
        return None


class AccountUnavailable(RuntimeError):
    """没有任何可用 Loomy 账号 (未登录/全被禁用)。"""


class LoomyProvider(Provider):
    name = "loomy"

    # 模型目录同步 TTL (秒): 与主网关的每日刷新节奏对齐, 取 4h 折中
    MODELS_TTL = 4 * 3600

    # 鉴权类失效信号: HTTP 状态码 + 上游业务码 (100002 = 未登录/token 失效)
    AUTH_STATUS = (401, 403)
    AUTH_BIZ_CODES = ("100002",)

    def __init__(self, cfg: dict):
        self.base_url = (cfg.get("base_url") or UPSTREAM_BASE).rstrip("/")
        self.timeout = float(cfg.get("timeout", 300.0))
        # 模型表: 静态兜底表 + config 覆盖/追加 (动态同步结果会合并进来)
        self.models = dict(DEFAULT_MODELS)
        self.models.update({k: v for k, v in (cfg.get("models") or {}).items()})
        self.default_model = cfg.get("defaultModel") or "deepseek-v4-flash-0731"
        self._client = httpx.AsyncClient(timeout=self.timeout)
        # 动态模型目录缓存: {上游id: 显示名}; None = 从未同步过
        self._dynamic_models: dict | None = None
        self._models_synced_at: float = 0.0
        # 多账号轮询游标
        self._rr = 0
        self._lock = asyncio.Lock()
        # 最近一次成功使用的账号 (日志/排查用)
        self._last_account: dict | None = None

    def get_client(self) -> httpx.AsyncClient:
        return self._client

    # ---------- 基础接口 ----------
    def list_models(self) -> list[dict]:
        # 静态兜底表在前, 动态同步到的模型补充在后 (同名不覆盖静态名)
        ids = list(self.models.keys())
        if self._dynamic_models:
            for mid in self._dynamic_models:
                if mid not in self.models:
                    ids.append(mid)
        return [{"id": f"{PREFIX}{mid}", "object": "model", "created": 0,
                 "owned_by": "loomy"}
                for mid in ids]

    def normalize_model(self, model: str) -> str:
        raw = _strip_prefix(model)
        return raw or self.default_model

    # ---------- 动态模型目录 ----------
    # 上游 GET /models 用 token 头鉴权 (与 chat 的 Bearer 同为账号凭据),
    # 返回的目录带显示名/上下文长度/能力位, 比静态表新鲜。
    async def sync_models(self, force: bool = False) -> bool:
        """拉取上游模型目录合并进模型表。成功 True, 失败 False (保留静态表)。"""
        if not force and self._dynamic_models is not None \
                and time.time() - self._models_synced_at < self.MODELS_TTL:
            return True
        token = load_any_token()
        if not token:
            logger.warning("loomy 模型目录同步跳过: 无可用账号 (请先在账号管理中登录)")
            return False
        try:
            resp = await self._client.get(
                f"{self.base_url}/models",
                headers={"token": token, "Accept": "application/json",
                         "User-Agent": USER_AGENT})
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("loomy 模型目录同步失败 (保留现有模型表): %s", e)
            return False
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            logger.warning("loomy 模型目录响应形状异常, 保留现有模型表")
            return False
        merged: dict = {}
        for it in items:
            if not isinstance(it, dict):
                continue
            mid = str(it.get("id") or "").strip()
            if not mid:
                continue
            name = str(it.get("name") or "").strip() or mid
            # 显示名里的「（x3.0）」倍率尾巴交给倍率接口, 这里去掉更干净
            name = re.sub(r"（[^）]*）\s*$", "", name).strip()
            merged[mid] = name
        if not merged:
            logger.warning("loomy 模型目录为空, 保留现有模型表")
            return False
        # 合并: 动态结果覆盖静态表同名项; 静态表里仍有而上游没有的
        # 视为已下架, 但保留作兜底 (上游目录临时异常时仍有可用模型)
        self._dynamic_models = merged
        for k, v in merged.items():
            self.models[k] = v
        self._models_synced_at = time.time()
        logger.info("loomy 动态模型同步成功: %d 个", len(merged))
        return True

    # ---------- 账号轮询 ----------
    async def _pick_start(self, n: int) -> int:
        """取本轮轮询起点并推进游标 (多账号轮流消耗, 而非只掉一个)。"""
        async with self._lock:
            start = self._rr % n
            self._rr = (self._rr + 1) % n
        return start

    # ---------- 上游请求 ----------
    def _headers(self, token: str) -> dict:
        """与官方客户端一致: session 同时放 Authorization Bearer 与 token 头。"""
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "token": token,
            "User-Agent": USER_AGENT,
        }

    async def _request_upstream(self, body: dict, token: str):
        body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = self._client.build_request(
            "POST", f"{self.base_url}{UPSTREAM_CHAT_PATH}",
            content=body_bytes, headers=self._headers(token))
        return await self._client.send(req, stream=True)

    def _to_upstream_body(self, body: dict) -> dict:
        """构造上游请求体。

        本 provider 内部统一按 SSE 消费 (`_chat_events` 逐行解析), 所以这里
        一律要求上游 `stream: true` —— 上游流式与流式之外的响应形状不同
        (`delta` vs `message`), 固定成一种能少一层分支。
        `stream_options.include_usage` 让上游在流尾补一个 usage 事件,
        否则流式响应拿不到 token 用量。
        """
        out = dict(body)
        out["model"] = self.normalize_model(body.get("model", self.default_model))
        out["stream"] = True
        opts = dict(out.get("stream_options") or {})
        opts.setdefault("include_usage", True)
        out["stream_options"] = opts
        return out

    async def _chat_events(self, body: dict):
        """发起到 Loomy iModel 的对话请求。

        使用账号自己的 session 鉴权, 积分从该账号扣。
        session 失效(未登录/token 过期)时自动换下一个账号重试。
        """
        up_body = self._to_upstream_body(body)
        accounts = load_config_accounts()
        if not accounts:
            raise AccountUnavailable(
                "loomy 无可用账号, 请先在账号管理中登录 Loomy")

        n = len(accounts)
        start = await self._pick_start(n)

        last_err = None
        for i in range(n):
            account = accounts[(start + i) % n]
            token = account["_token"]
            label = f"{account.get('phone') or account.get('userid') or '?'}" \
                    f"[{account.get('kind') or 'desktop'}]"
            try:
                resp = await self._request_upstream(up_body, token)
            except Exception as e:  # noqa: BLE001
                # 网络层异常同样换下一个账号 (单账号网络抖动不该拖垮整轮)
                last_err = f"loomy 账号 {label} 请求异常: {e!r}"
                logger.warning("%s, 换下一个账号", last_err)
                continue

            if resp.status_code != 200:
                err_text = (await resp.aread()).decode("utf-8", errors="replace")
                await resp.aclose()
                last_err = f"loomy upstream HTTP {resp.status_code}: {err_text[:300]}"
                # 仅鉴权类错误才换账号 (401/403 或业务码未登录/token失效)
                if resp.status_code in self.AUTH_STATUS or \
                        any(c in err_text for c in self.AUTH_BIZ_CODES):
                    logger.warning("loomy 账号 %s session 失效, 换下一个账号", label)
                    continue
                raise RuntimeError(last_err)

            # HTTP 200 也可能是「带内错误」: 上游把业务失败也回成 200,
            # body 是 {"code":"100002","desc":"登录已失效"} 且没有 choices。
            # 流式场景下必须先拿到首个数据行判掉, 否则会把错误 body 当正常流
            # 消费, 前端只看到一个空回复。
            lines = resp.aiter_lines()
            first, inband_err = await self._probe_first(lines)
            if inband_err:
                await resp.aclose()
                code, msg = inband_err
                last_err = f"loomy upstream 业务错误 {code}: {msg}"
                if code in self.AUTH_BIZ_CODES:
                    logger.warning("loomy 账号 %s 登录已失效 (%s), 换下一个账号",
                                   label, code)
                    continue
                raise RuntimeError(last_err)
            try:
                if first is not None:
                    yield first
                async for line in lines:
                    obj = _parse_sse_line(line)
                    if obj:
                        yield obj
            finally:
                await resp.aclose()
            self._last_account = account
            return
        raise RuntimeError(f"loomy 所有账号均不可用: {last_err}")

    async def _probe_first(self, lines):
        """从流迭代器里取首个有效事件, 判定该账号是否可用。

        返回 (首个事件或 None, 错误 (code, msg) 或 None)。
        这里持有的是同一个迭代器对象, 未消费完的行由调用方继续迭代 ——
        httpx 的 aiter_lines() 不能「跳出后再开一个」, 会抛 StreamConsumed。

        注意: 上游对失败的请求会回 **非 SSE 的裸 JSON**
        (`{"code":"100002","desc":"登录已失效"}`), 不带 `data:` 前缀,
        因此不能只认 `_parse_sse_line` 的结果 —— 那样会把错误 body 整段丢掉,
        误判成「该账号可用但内容为空」。
        """
        async for line in lines:
            text = (line or "").strip()
            if not text or text.startswith(":"):
                continue
            obj = _parse_sse_line(text)
            if obj is None:
                # 非 SSE 行: 试一次裸 JSON 解析, 失败就跳过 (SSE 的 event:/id: 行)
                if not text.startswith("{"):
                    continue
                try:
                    obj = json.loads(text)
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(obj, dict):
                    continue
            code = str(obj.get("code") or "")
            # 带内业务错误: 有 code 且非成功, 且不是正常的 chat 响应形状
            if code and code != "000000" and not obj.get("choices") \
                    and not obj.get("usage"):
                msg = str(obj.get("desc") or obj.get("message") or code)
                return None, (code, msg)
            return obj, None
        return None, None

    def _aggregate(self, events) -> tuple:
        """把上游事件合并成一条完整回复。

        上游对**非流式**请求回的是完整 `chat.completion`(字段是 `message`),
        对流式请求才回增量 `chat.completion.chunk`(字段是 `delta`)。
        两种形状都要吃 —— 只认 `delta` 会把非流式回复整段丢掉(空回复)。
        """
        content_parts, reasoning_parts = [], []
        tool_call_map: dict = {}
        usage = None
        finish_reason = None
        for obj in events:
            if obj.get("usage"):
                usage = obj["usage"]
            for choice in obj.get("choices", []):
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                # delta = 流式增量; message = 非流式整段
                delta = choice.get("delta") or choice.get("message") or {}
                if delta.get("content"):
                    content_parts.append(delta["content"])
                if delta.get("reasoning_content"):
                    reasoning_parts.append(delta["reasoning_content"])
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    entry = tool_call_map.setdefault(idx, {
                        "id": tc.get("id") or "", "type": tc.get("type") or "function",
                        "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        entry["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        entry["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        entry["function"]["arguments"] += fn["arguments"]
        tool_calls = [tool_call_map[i] for i in sorted(tool_call_map)] or None
        return ("".join(content_parts), "".join(reasoning_parts),
                tool_calls, usage, finish_reason)

    # ---------- OpenAI 输出 ----------
    async def chat(self, body: dict) -> dict:
        events = []
        async for obj in self._chat_events(body):
            events.append(obj)
        content, reasoning, tool_calls, usage, finish_reason = self._aggregate(events)
        model = self.normalize_model(body.get("model", self.default_model))
        message = {"role": "assistant", "content": content}
        if reasoning:
            message["reasoning_content"] = reasoning
        if tool_calls:
            message["tool_calls"] = tool_calls
        return {
            "id": make_chunk_id("loomy"),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", model),
            "choices": [{"index": 0, "message": message,
                         "finish_reason": finish_reason or
                         ("tool_calls" if tool_calls else "stop")}],
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    async def stream(self, body: dict):
        cid = make_chunk_id("loomy")
        created = int(time.time())
        model = body.get("model", self.default_model)
        role_sent = False

        def chunk(delta: dict, finish=None):
            nonlocal role_sent
            d = dict(delta)
            if not role_sent:
                d = {"role": "assistant", **d}
                role_sent = True
            return {
                "id": cid, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": d, "finish_reason": finish}],
            }

        yielded_content = False
        async for obj in self._chat_events(body):
            for choice in obj.get("choices", []):
                delta = choice.get("delta") or {}
                out = {}
                if delta.get("content"):
                    out["content"] = delta["content"]
                    yielded_content = True
                if delta.get("reasoning_content"):
                    out["reasoning_content"] = delta["reasoning_content"]
                if delta.get("tool_calls"):
                    out["tool_calls"] = delta["tool_calls"]
                if out:
                    yield chunk(out)
                if choice.get("finish_reason"):
                    yield chunk({}, finish=choice["finish_reason"])
        if not yielded_content:
            yield chunk({}, finish="stop")
