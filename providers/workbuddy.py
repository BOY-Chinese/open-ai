# -*- coding: utf-8 -*-
"""WorkBuddy Provider — 腾讯 copilot.tencent.com 内置 DeepSeek (逆向)

移植自 workbuddy内置deepseek代理/proxy.py (WorkBuddy 5.3.12 抓包逆向)。
与 open-api 网关的 Provider 接口对齐, 使用 httpx 异步实现。

模型命名: workbuddy-<上游模型名>, 例如:
  workbuddy-deepseek-v4-flash / workbuddy-deepseek-v4-pro

特性:
  * 上游仅支持流式(400: Non-stream not supported), 代理内部永远 stream=true,
    非流式请求由本 provider 聚合 SSE 后返回。
  * token 过期自动刷新(accessToken -> refreshToken), 请求遇 401 自动重试一次。
  * 支持 reasoning_content(思维链) / tools / tool_calls。
"""
import asyncio
import base64
import itertools
import json
import logging
import time
import uuid

import httpx

from providers.base import Provider, make_chunk_id

logger = logging.getLogger("openapi.workbuddy")

UPSTREAM_HOST = "copilot.tencent.com"
UPSTREAM_CHAT_PATH = "/v2/chat/completions"
UPSTREAM_REFRESH_PATH = "/v2/plugin/auth/token/refresh"
USER_AGENT = "WorkBuddy/5.3.12 WorkBuddy/5.3.12 CLI/2.115.0"

# 客户端叫法(workbuddy- 前缀) -> 上游模型名 (与 workbuddy内置deepseek代理/proxy.py 同步)
MODEL_ALIASES = {
    "deepseek-chat": "deepseek-v4-flash",
    "deepseek-reasoner": "deepseek-v4-flash",
    "deepseek-v4-flash": "deepseek-v4-flash",
    "deepseek-v4-pro": "deepseek-v4-pro",
    "auto": "deepseek-v4-flash",
    # 腾讯网关其他内置模型（/v3/config 抓包确认可用）
    "hy3": "hy3",
    "glm-5.2": "glm-5.2",
    "glm-5.1": "glm-5.1",
    "glm-5v-turbo": "glm-5v-turbo",
    "minimax-m3": "minimax-m3",
    "kimi-k3-1": "kimi-k3-1",
    "kimi-k2.7": "kimi-k2.7",
    "kimi-k2.6": "kimi-k2.6",
    # WorkBuddy 自定义模型 id（models.json 里注册的名字）
    "wb-deepseek-v4-flash": "deepseek-v4-flash",
    "wb-deepseek-v4-pro": "deepseek-v4-pro",
    "wb-hy3": "hy3",
    "custom-local:wb-deepseek-v4-flash": "deepseek-v4-flash",
    "custom-local:wb-deepseek-v4-pro": "deepseek-v4-pro",
    "custom-local:wb-hy3": "hy3",
    "custom-local:deepseek-v4-flash": "deepseek-v4-flash",
    "custom-local:deepseek-v4-pro": "deepseek-v4-pro",
    "custom-local:hy3": "hy3",
}


def _strip_prefix(model: str) -> str:
    """去掉模型名里的 workbuddy- / custom-local: 前缀, 得到上游模型名。"""
    m = (model or "").strip()
    for prefix in ("workbuddy-", "custom-local:"):
        if m.startswith(prefix):
            m = m[len(prefix):]
    return m


def normalize_model_name(model: str, default_model: str,
                         aliases: dict | None = None) -> str:
    """把请求里的模型名归一化为上游认识的名字。

    aliases: 实例级模型映射 (config.json providers.<name>.models 合并进
    MODEL_ALIASES 后的结果), 便于在配置层增删模型而不改代码。
    """
    table = aliases if aliases is not None else MODEL_ALIASES
    raw = _strip_prefix(model)
    if raw in table:
        return table[raw]
    if raw.startswith("wb-"):
        stripped = raw[3:]
        if stripped in table:
            return table[stripped]
    return raw or default_model


def _content_to_text(content) -> str:
    if isinstance(content, list):
        parts = []
        for seg in content:
            if isinstance(seg, dict) and seg.get("type") == "text":
                parts.append(seg.get("text", ""))
        return "\n".join(p for p in parts if p)
    return str(content or "")


def _jwt_exp(token: str) -> int:
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return int(payload.get("exp", 0))
    except Exception:
        return 0


# 上游需要透传的固定头
def _upstream_headers(account: dict, domain: str, product: str,
                      body: bytes) -> dict:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {account.get('accessToken', '')}",
        "X-User-Id": account.get("userId", ""),
        "X-Domain": domain,
        "X-Product": product,
        "User-Agent": USER_AGENT,
        "X-Request-ID": uuid.uuid4().hex,
        "X-Conversation-ID": str(uuid.uuid4()),
        "X-Conversation-Request-ID": uuid.uuid4().hex,
        "X-Conversation-Message-ID": uuid.uuid4().hex,
        "X-Root-Request-ID": uuid.uuid4().hex,
        "X-Agent-Intent": "craft",
        "X-Agent-Purpose": "conversation",
        "X-IDE-Type": "WorkBuddy",
        "X-IDE-Name": "WorkBuddy",
        "X-IDE-Version": "5.3.12",
        "X-Private-Data": "true",
        "x-codebuddy-request": "1",
    }


def _to_upstream_body(body: dict, default_model: str,
                      aliases: dict | None = None) -> dict:
    """把 OpenAI 请求体标准化为上游能识别的格式。"""
    model = body.get("model", default_model) or default_model
    out = dict(body)
    out["model"] = normalize_model_name(model, default_model, aliases)
    # 上游只支持流式
    out["stream"] = True
    out["stream_options"] = {"include_usage": True}
    for k in ("user", "n", "seed", "response_format"):
        out.pop(k, None)
    return out


def _parse_sse_line(line: str):
    """解析单条 SSE data 行, 返回 (obj | None)。"""
    line = line.strip()
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if data == "[DONE]":
        return None
    try:
        return json.loads(data)
    except Exception:
        return None


class WorkBuddyProvider(Provider):
    name = "workbuddy"

    def __init__(self, cfg: dict):
        # 多账号: accounts 数组, 每项 {accessToken, refreshToken, userId}
        # 兼容旧式单账号字段(accessToken/refreshToken/userId)
        accounts = cfg.get("accounts") or []
        if not accounts and cfg.get("accessToken"):
            accounts = [{
                "accessToken": cfg.get("accessToken", ""),
                "refreshToken": cfg.get("refreshToken", ""),
                "userId": cfg.get("userId", ""),
            }]
        self.accounts = accounts
        self.domain = cfg.get("domain", "www.codebuddy.cn")
        self.product = cfg.get("product", "SaaS")
        # 账号轮换: 每 switchEvery 次请求切换到下一个账号(循环)
        self.switch_every = max(1, int(cfg.get("switchEvery", 10)))
        self._req_count = 0
        self._cur_idx = 0
        self._cycle = itertools.cycle(range(len(self.accounts))) if self.accounts else iter(())
        self.default_model = cfg.get("defaultModel", "deepseek-v4-flash")
        self.timeout = float(cfg.get("timeout", 300.0))
        # 实例级模型映射: config.json providers.workbuddy.models 合并进内建别名,
        # 便于在配置层增删模型 (与 trae provider 对齐)。
        self.aliases = dict(MODEL_ALIASES)
        self.aliases.update({k: v for k, v in (cfg.get("models") or {}).items()})
        self._client: httpx.AsyncClient | None = None
        self._refresh_lock = asyncio.Lock()

    def _pick_account(self) -> dict:
        """按次数批量轮换: 返回当前账号 dict。无账号时返回空 dict。"""
        if not self.accounts:
            return {}
        self._req_count += 1
        if self._req_count % self.switch_every == 1:
            self._cur_idx = next(self._cycle)
        return self.accounts[self._cur_idx]

    def get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    def list_models(self) -> list[dict]:
        # 对外统一暴露 workbuddy-<上游模型名>
        upstream_models = [
            "deepseek-v4-flash", "deepseek-v4-pro",
            "hy3", "glm-5.2", "glm-5.1", "glm-5v-turbo",
            "minimax-m3", "kimi-k3-1", "kimi-k2.7", "kimi-k2.6",
        ]
        return [{"id": f"workbuddy-{m}", "object": "model", "owned_by": "tencent"}
                for m in upstream_models]

    def normalize_model(self, model: str) -> str:
        return normalize_model_name(model, self.default_model, self.aliases)

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------- token 刷新 ----------
    def _is_expired(self, account: dict) -> bool:
        exp = _jwt_exp(account.get("accessToken", ""))
        return bool(exp and exp - 600 < time.time())

    async def _refresh_token(self, account: dict) -> bool:
        if not account.get("refreshToken"):
            return False
        async with self._refresh_lock:
            if not self._is_expired(account):
                return True
            try:
                client = self.get_client()
                headers = {
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                    "X-Domain": self.domain,
                    "X-Refresh-Token": account["refreshToken"],
                    "X-Auth-Refresh-Source": "plugin",
                    "X-Product": self.product,
                    "User-Agent": USER_AGENT,
                    "X-Trace-ID": "0" * 32,
                    "X-Request-ID": "0" * 32,
                }
                resp = await client.post(
                    f"https://{UPSTREAM_HOST}{UPSTREAM_REFRESH_PATH}",
                    json={}, headers=headers)
                if resp.status_code != 200:
                    logger.warning("workbuddy refresh HTTP %s: %s",
                                   resp.status_code, resp.text[:200])
                    return False
                data = resp.json().get("data", {})
                if data.get("accessToken"):
                    account["accessToken"] = data["accessToken"]
                    if data.get("refreshToken"):
                        account["refreshToken"] = data["refreshToken"]
                    logger.info("workbuddy accessToken 已自动刷新")
                    return True
                return False
            except Exception as e:  # noqa: BLE001
                logger.error("workbuddy refresh 异常: %s", e)
                return False

    # ---------- 上游请求 ----------
    async def _request_upstream(self, up_body: dict, account: dict,
                                retried: bool = False):
        """转发到上游, 返回 (status, streaming_raw)。"""
        client = self.get_client()
        body_bytes = json.dumps(up_body, ensure_ascii=False).encode("utf-8")
        headers = _upstream_headers(account, self.domain, self.product, body_bytes)
        req = client.build_request(
            "POST", f"https://{UPSTREAM_HOST}{UPSTREAM_CHAT_PATH}",
            content=body_bytes, headers=headers)
        resp = await client.send(req, stream=True)

        if resp.status_code in (401, 403) and not retried:
            try:
                await resp.aclose()
            except Exception:
                pass
            if await self._refresh_token(account):
                return await self._request_upstream(up_body, account, retried=True)

        return resp

    # ---------- 会话 ----------
    async def _chat_events(self, body: dict, account: dict):
        """发起上游请求, 产出 SSE 解析后的 dict 事件流。"""
        up_body = _to_upstream_body(body, self.default_model, self.aliases)
        resp = await self._request_upstream(up_body, account)
        if resp is None:
            raise RuntimeError("workbuddy 上游请求失败")
        if resp.status_code != 200:
            err_text = (await resp.aread()).decode("utf-8", errors="replace")
            await resp.aclose()
            raise RuntimeError(f"workbuddy upstream HTTP {resp.status_code}: {err_text[:300]}")
        try:
            async for line in resp.aiter_lines():
                obj = _parse_sse_line(line)
                if obj:
                    yield obj
        finally:
            await resp.aclose()

    def _aggregate(self, events) -> tuple:
        """聚合 SSE 事件流 -> (content, reasoning, tool_calls, usage, finish_reason)。"""
        content_parts, reasoning_parts = [], []
        tool_call_map: dict[int, dict] = {}
        usage = None
        finish_reason = None
        for obj in events:
            if obj.get("usage"):
                usage = obj["usage"]
            for choice in obj.get("choices", []):
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    content_parts.append(delta["content"])
                if delta.get("reasoning_content"):
                    reasoning_parts.append(delta["reasoning_content"])
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    entry = tool_call_map.setdefault(idx, {
                        "id": tc.get("id") or "", "type": tc.get("type") or "function",
                        "function": {"name": "", "arguments": ""},
                    })
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
        account = self._pick_account()
        events = []
        async for obj in self._chat_events(body, account):
            events.append(obj)
        content, reasoning, tool_calls, usage, finish_reason = self._aggregate(events)
        model = normalize_model_name(body.get("model", self.default_model),
                                     self.default_model, self.aliases)
        message = {"role": "assistant", "content": content}
        if reasoning:
            message["reasoning_content"] = reasoning
        if tool_calls:
            message["tool_calls"] = tool_calls
        return {
            "id": make_chunk_id("workbuddy"),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", model),
            "choices": [{"index": 0, "message": message,
                         "finish_reason": finish_reason or
                         ("tool_calls" if tool_calls else "stop")}],
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    async def stream(self, body: dict):
        account = self._pick_account()
        cid = make_chunk_id("workbuddy")
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
        async for obj in self._chat_events(body, account):
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
            yield chunk({"content": ""}, finish="stop")