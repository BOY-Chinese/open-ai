# -*- coding: utf-8 -*-
"""Trae Provider — 转发到内嵌 Node 后端 (127.0.0.1:18787)

open-ai/trae/server.js (Node) 复用 TRAE 内置 aha 网络栈(sscronet.dll)完成 TTNet 加密签名,
调用 llm_utils_chat 并携带会话上下文 → 计费到 **Work 积分池 (TraeWork)**。
本 provider 作为纯转发层, 让 open-api 8000 端口成为唯一对外入口。
Node 后端与 Python 网关同属 open-ai 独立项目, 由 start.bat 统一拉起。

模型命名 (对外统一 tr- 前缀, 标识 Trae 通道):
  tr-DeepSeek-V4-Flash-Official  → 上游动态模型 (每日自动同步, 50 个)
  tr-glm-5.3 / tr-kimi-k3 / ...  → 上游动态模型
  tr-flash-official 等短别名     → config.json providers.trae.models 配置层映射
请求归一化: 剥 tr- / trae- / custom-local: 前缀 → 配置别名 → 动态上游名(大小写不敏感)。
"""
import asyncio
import json
import logging
import time

import httpx

from providers.base import Provider, make_chunk_id

logger = logging.getLogger("openapi.trae")

class TraeProvider(Provider):
    name = "trae"

    def __init__(self, cfg: dict):
        self.base_url = (cfg.get("base_url") or "http://127.0.0.1:18787/v1").rstrip("/")
        self.timeout = float(cfg.get("timeout", 300.0))
        # 别名仅来自 config.json providers.trae.models (配置层, 代码不再写死)
        # 例: {"deepseek-v4-flash": "DeepSeek-V4-Flash", "trae-flash-official": "DeepSeek-V4-Flash-Official"}
        self.aliases = {k: v for k, v in (cfg.get("models") or {}).items()}
        self._default = (cfg.get("default_model") or "DeepSeek-V4-Flash-Official").strip()
        # ---- 动态模型: 转发 Node 后端 /v1/models (Node 侧每日拉上游), 失败回退静态 ----
        self._node_models: list[str] = []   # 最近一次从 Node 后端同步到的上游模型
        self._node_synced_at: float = 0.0
        self._node_sync_lock = asyncio.Lock()

    # ---------------- 动态模型 (转发 Node 后端, 每日自动同步) ----------------
    async def sync_models(self, force: bool = False) -> bool:
        """从 Node 后端 /v1/models 同步上游模型列表到本地缓存。

        Node 后端(server.js)持有 Trae 网络栈, 已实现每日自动拉取 get_detail_param,
        这里只需转发其结果。force=True 忽略每日间隔强制同步。
        同步失败时保留上次缓存(或静态表), 不抛异常。
        """
        async with self._node_sync_lock:
            now = time.time()
            if not force and now - self._node_synced_at < 86400:
                return bool(self._node_models)
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(f"{self.base_url}/models")
                    if resp.status_code != 200:
                        logger.warning("trae Node 后端 /v1/models HTTP %s (回退静态)", resp.status_code)
                        return bool(self._node_models)
                    data = resp.json()
                    models = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
                if models:
                    self._node_models = models
                    self._node_synced_at = time.time()
                    logger.info("trae 动态模型同步成功: %d 个", len(models))
                    return True
                logger.warning("trae Node 后端 /v1/models 返回空, 保留现有列表")
            except Exception as e:  # noqa: BLE001
                logger.warning("trae 动态模型同步失败: %s (回退静态表格)", e)
            return bool(self._node_models)

    # ---------------- 基础接口 ----------------
    def list_models(self) -> list[dict]:
        # 对外统一 tr- 前缀标识 Trae 通道: 动态上游模型 + 配置别名 + 默认模型回退
        names = set(self._node_models) | set(self.aliases.values()) \
            | set(self.aliases.keys())
        names.add(self._default)
        return [{"id": f"tr-{m}", "object": "model", "created": 0, "owned_by": "trae-proxy"}
                for m in sorted(names)]

    def normalize_model(self, model: str) -> str:
        name = (model or "").strip()
        # WorkBuddy 客户端旧格式 custom-local: 前缀, 仅请求兼容(不再在列表暴露)
        if name.startswith("custom-local:"):
            name = name[len("custom-local:"):]
        if not name:
            return self._default
        # 1) 原始名直接查配置别名 (兼容 "trae-flash-official" 这类自带前缀的 key)
        if name in self.aliases:
            return self.aliases[name]
        # 2) 剥通道前缀: tr- 为规范前缀, trae- 兼容旧客户端
        low = name.lower()
        if low.startswith("tr-"):
            name = name[3:]
        elif low.startswith("trae-"):
            name = name[5:]
        if name in self.aliases:
            return self.aliases[name]
        # 3) 动态上游名大小写不敏感匹配 (tr-DeepSeek-V4-Flash-Official → 原名)
        for m in self._node_models:
            if m.lower() == name.lower():
                return m
        return name

    # ---------------- 非流式 ----------------
    async def chat(self, body: dict) -> dict:
        upstream = dict(body)
        upstream["model"] = self.normalize_model(body.get("model", ""))
        upstream["stream"] = False
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(f"{self.base_url}/chat/completions", json=upstream)
            if resp.status_code != 200:
                raise RuntimeError(f"trae 上游 {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
        if data.get("error"):
            raise RuntimeError(str(data["error"]))
        # 确保 id / object 字段符合 OpenAI
        data.setdefault("id", make_chunk_id())
        data["object"] = "chat.completion"
        return data

    # ---------------- 流式 ----------------
    async def stream(self, body: dict):
        upstream = dict(body)
        upstream["model"] = self.normalize_model(body.get("model", ""))
        upstream["stream"] = True
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream("POST", f"{self.base_url}/chat/completions",
                                     json=upstream) as resp:
                if resp.status_code != 200:
                    text = (await resp.aread()).decode("utf-8", "replace")
                    raise RuntimeError(f"trae 上游 {resp.status_code}: {text[:300]}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    chunk.setdefault("id", make_chunk_id())
                    chunk["object"] = "chat.completion.chunk"
                    yield chunk
