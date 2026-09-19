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
import os
import time
import uuid

import httpx

from providers.base import AccountHealth, Provider, make_chunk_id

# 每次 _pick_account 都从 config.json 实时读取启用状态
# ★ 打包态 __file__ 指向解包临时目录, 相对路径会读错 config —— 钉死安装根。
try:
    import app_paths as _ap
    _WB_CONFIG_PATH = _ap.CONFIG_PATH
except Exception:  # 源码态: __file__ 是绝对路径, 兜底安全
    _WB_CONFIG_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'config.json'))

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
    # 腾讯网关其他内置模型（/v3/config 抓包确认可用, 以 cli agent models 为准）
    "hy3": "hy3",
    "hy3-x": "hy3-x",
    "hy4": "hy4-preview",
    "hy4-preview": "hy4-preview",
    "hy4-preview-x": "hy4-preview-x",
    "glm-5.3": "glm-5.3",
    "glm-5.3-flash": "glm-5.3-flash",
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
    "wb-hy3-x": "hy3-x",
    "wb-hy4": "hy4-preview",
    "wb-glm-5.3": "glm-5.3",
    "wb-glm-5.3-flash": "glm-5.3-flash",
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
        # ★ 必须非空: 上游 /v3/config 以该头为「出数据」的开关, 空串会让
        #   data.models 变成 null (模型与倍率全部拿不到)。实测见
        #   scripts/account_manager._wb_family_model_rates 的注释。
        "X-User-Id": account.get("userId") or "open-ai",
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
        # 账号运行态 (2026-09-19): 上游明确拒绝鉴权时标记, 账号管理页展示「真状态」
        self._health = AccountHealth()
        # ---- 动态模型拉取 (启动/每日刷新, 失败回退静态表) ----
        self._upstream_models: list[str] = []       # 最近一次从 /v3/config 拉到的上游模型名
        self._upstream_fetched_at: float = 0.0      # 上次成功拉取时间戳
        self._models_lock = asyncio.Lock()          # 刷新时防并发

    def _pick_account(self) -> dict:
        """按次数批量轮换: 返回当前账号 dict（跳过 enabled=false 的账号）。无可用账号时返回空 dict。"""
        # 每次请求都从 config.json 实时读取启用状态
        try:
            with open(_WB_CONFIG_PATH, encoding='utf-8') as _f:
                _wb_cfg = json.load(_f).get('providers', {}).get('workbuddy', {})
            _fresh = _wb_cfg.get('accounts', [])
            # 合并内存中的 token 刷新状态
            for _old in self.accounts:
                _match = next((_n for _n in _fresh if _n.get('userId') == _old.get('userId') or _n.get('accessToken') == _old.get('accessToken')), None)
                if _match:
                    _match['accessToken'] = _old.get('accessToken', _match.get('accessToken', ''))
                    _match['refreshToken'] = _old.get('refreshToken', _match.get('refreshToken', ''))
            self.accounts = _fresh if _fresh else self.accounts
        except Exception:
            pass  # 读取失败则沿用内存 accounts
        # 凭据观察: token 变化(重新登录/续期)自动解除失效标记 (新凭据给新机会)
        for _a in self.accounts:
            self._health.observe_token(_a.get("userId"), _a.get("accessToken"))
        # 过滤出启用的账号, 并跳过运行态已失效的账号 (上游明确判死, 再打也是白打;
        # 全部失效时返回 {} —— 与 trae 侧「账号池为空」同语义)
        enabled = [a for a in self.accounts if a.get("enabled", True)
                   and not self._health.is_invalid(a.get("userId"))]
        if not enabled:
            return {}
        self._req_count += 1
        # 按次数批量轮换 (与 trae/server.js pickAccount 同口径):
        # 直接按请求序号计算批次索引, 启用账号数变化时也能均匀轮换
        self._cur_idx = (self._req_count - 1) // self.switch_every % len(enabled)
        return enabled[self._cur_idx]

    def get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    # ---------- 动态模型 (启动/每日刷新, 失败回退静态表) ----------
    async def refresh_models(self, force: bool = False) -> bool:
        """从上游 /v3/config 拉取模型列表, 合并进 aliases。

        每天至多刷新一次 (由 main.py 定时调用 force=False);
        force=True 仅用于启动时首次强制拉取或手动调用。
        拉取失败时保留上次结果(或静态表), 不抛异常。
        """
        async with self._models_lock:
            now = time.time()
            if not force and now - self._upstream_fetched_at < 86400:
                return self._upstream_fetched_at > 0
            try:
                models = await self._fetch_upstream_models()
                if models:
                    self._upstream_models = models
                    self._upstream_fetched_at = time.time()
                    for m in models:
                        self.aliases.setdefault(m, m)
                        self.aliases.setdefault(f"wb-{m}", m)
                    logger.info("workbuddy 动态模型更新成功: %d 个 (%s)",
                                len(models), ", ".join(models[:8]) + ("..." if len(models) > 8 else ""))
                    return True
                logger.warning("workbuddy /v3/config 返回空模型列表, 保留现有列表")
            except Exception as e:  # noqa: BLE001
                logger.warning("workbuddy 动态拉取模型失败: %s (回退现有列表)", e)
            return self._upstream_fetched_at > 0

    async def _fetch_upstream_models(self) -> list[str]:
        """GET /v3/config 取 cli agent 的模型列表。遍历账号, 401 走 token 刷新重试。"""
        if not self.accounts:
            raise RuntimeError("workbuddy 无账号, 无法拉取上游模型")
        last_err = None
        for account in self.accounts:
            if await self._refresh_token(account):
                try:
                    headers = _upstream_headers(account, self.domain, self.product, b"")
                    client = self.get_client()
                    resp = await client.get(
                        f"https://{UPSTREAM_HOST}/v3/config", headers=headers, timeout=30.0)
                    if resp.status_code in (401, 403):
                        last_err = f"{resp.status_code}"
                        continue
                    if resp.status_code != 200:
                        last_err = f"HTTP {resp.status_code}"
                        continue
                    data = resp.json()
                    models: list[str] = []
                    for agent in (data.get("data") or {}).get("agents") or []:
                        models.extend(agent.get("models") or [])
                    if models:
                        return models
                    last_err = "empty"
                except Exception as e:  # noqa: BLE001
                    last_err = repr(e)
        raise RuntimeError(f"所有账号均无法拉取上游模型: {last_err}")

    # 列表额外保留的通用兼容别名 (OpenAI 风格叫法, 请求始终可用)
    _COMPAT_ALIASES = ("deepseek-chat", "deepseek-reasoner", "auto")

    def _model_ids(self) -> set[str]:
        """对外列表: 统一 wb- 前缀 (每模型一个名字), 另加 3 个通用兼容别名。

        上游目标名 = 静态别名 values + config 别名 values + 动态上游模型;
        裸名(上游原名)不再出现在列表里, 但 normalize_model 仍接受(向后兼容)。
        """
        targets = set(MODEL_ALIASES.values()) | set(self.aliases.values()) \
            | set(self._upstream_models)
        ids = {f"wb-{m}" for m in targets}
        ids |= set(self._COMPAT_ALIASES)
        return ids

    def list_models(self) -> list[dict]:
        # wb- 前缀统一展示; 裬名仅作为请求别名保留, 不再暴露
        ids = self._model_ids()
        return [{"id": m, "object": "model", "owned_by": "tencent"}
                for m in sorted(ids)]

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
                    if resp.status_code in (401, 403):
                        # 刷新凭据本身被上游拒绝 → refreshToken 已死, 标记失效
                        self._health.mark_invalid(
                            account.get("userId"),
                            reason=f"refresh 被拒 HTTP {resp.status_code}")
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
            if resp.status_code in (401, 403):
                # 走到这里 = 首发被拒且刷新重试仍被拒 (或无 refreshToken 可刷)
                # → 该账号的凭据已被上游判死, 沉淀运行态供账号管理页标红
                self._health.mark_invalid(
                    account.get("userId"),
                    reason=f"chat 被拒 HTTP {resp.status_code} (含刷新重试)")
            raise RuntimeError(f"workbuddy upstream HTTP {resp.status_code}: {err_text[:300]}")
        self._health.mark_ok(account.get("userId"))
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