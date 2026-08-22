# -*- coding: utf-8 -*-
"""Trae Provider — 转发到内嵌 Node 后端 (127.0.0.1:18787)

open-ai/trae/server.js (Node) 复用 TRAE 内置 aha 网络栈(sscronet.dll)完成 TTNet 加密签名,
调用 llm_utils_chat 并携带会话上下文 → 计费到 **Work 积分池 (TraeWork)**。
本 provider 作为纯转发层, 让 open-api 8000 端口成为唯一对外入口。
Node 后端与 Python 网关同属 open-ai 独立项目, 由 start.bat 统一拉起。

模型命名: trae-<上游模型名>, 例如:
  trae-flash-official  → deepseek-flash-official (DeepSeek-V4-Flash-Official, 火山方舟, 消耗 Work 积分)
  trae-v4-pro          → deepseek-v4-pro
  trae-glm-5.2         → glm-5.2
  trae-kimi-k2.7       → kimi-k2.7
  trae-seed-2.1-pro    → seed-2.1-pro
"""
import json
import logging

import httpx

from providers.base import Provider, make_chunk_id

logger = logging.getLogger("openapi.trae")

# 客户端叫法(trae- 前缀) -> 内嵌 Node 后端模型名
MODEL_ALIASES = {
    "flash-official": "deepseek-flash-official",
    "deepseek-flash-official": "deepseek-flash-official",
    "deepseek-v4-flash-official": "deepseek-flash-official",
    "v4-pro": "deepseek-v4-pro",
    "deepseek-v4-pro": "deepseek-v4-pro",
    "glm-5.2": "glm-5.2",
    "kimi-k2.7": "kimi-k2.7",
    "seed-2.1-pro": "seed-2.1-pro",
}


class TraeProvider(Provider):
    name = "trae"

    def __init__(self, cfg: dict):
        self.base_url = (cfg.get("base_url") or "http://127.0.0.1:18787/v1").rstrip("/")
        self.timeout = float(cfg.get("timeout", 300.0))
        # config.json models: {"trae-flash-official": "deepseek-flash-official", ...}
        self.aliases = dict(MODEL_ALIASES)
        self.aliases.update({k: v for k, v in (cfg.get("models") or {}).items()})

    # ---------------- 基础接口 ----------------
    def list_models(self) -> list[dict]:
        ids = set(self.aliases.keys())
        for alias in MODEL_ALIASES:
            ids.add(alias)
        return [{"id": m, "object": "model", "created": 0, "owned_by": "trae-proxy"}
                for m in sorted(ids)]

    def normalize_model(self, model: str) -> str:
        if not model:
            return "deepseek-flash-official"
        # WorkBuddy 自定义模型会带 custom-local: 前缀, 先剥掉
        if model.startswith("custom-local:"):
            model = model[len("custom-local:"):]
        if model in self.aliases:
            return self.aliases[model]
        # 去前缀: trae-flash-official -> flash-official
        for prefix in ("trae-", "t-", "tr-"):
            if model.startswith(prefix):
                return self.aliases.get(model[len(prefix):], model)
        return self.aliases.get(model, model)

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
