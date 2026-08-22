# -*- coding: utf-8 -*-
"""
Open-API — 统一 OpenAI 兼容聚合网关 (open-ai, 端口 8000)

聚合多个逆向提供商, 单端口对外提供标准 OpenAI 兼容 API。
当前注册的提供商 (providers/):
  * workbuddy — 腾讯 WorkBuddy 网关 (copilot.tencent.com 内置 DeepSeek 等)
  * trae      — TRAE 内嵌 Node 后端 (trae/server.js, 转发 18787, Work 积分通道, DeepSeek-V4-Flash-Official)

端点:
  GET  /v1/models                 所有提供商模型列表
  POST /v1/chat/completions       对话(非流式 + 流式 + 工具调用)
  GET  /                          健康检查 + 提供商概览

鉴权: Authorization: Bearer <api_key> (config.json 的 api_key, 默认 open-api-key)

模型路由 (providers/__init__.py route_provider):
  模型名含 "trae" 或以 "tr-" 开头  → trae provider (trae-flash-official → DeepSeek-V4-Flash-Official)
  模型名含 "workbuddy" 或以 "wb-" 开头 → workbuddy provider
  其他 → 第一个 provider (workbuddy)

WorkBuddy 自定义模型 (需配 ~/.workbuddy/models.json):
  WorkBuddy 客户端会发送 model = "custom-local:<id>", 各 provider 的
  normalize_model 会剥离该前缀后再做别名映射。
"""
import asyncio
import collections
import json
import logging
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from providers import build_providers, route_provider
from anthropic_api import (anthropic_to_openai, openai_to_anthropic,
                           openai_stream_to_anthropic, aggregate_stream)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("openapi")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


CONFIG = load_config()
API_KEY = CONFIG.get("api_key", "open-api-key")
HOST = CONFIG.get("host", "0.0.0.0")
PORT = int(CONFIG.get("port", 8000))

# 多 API 密钥: 兼容旧顶层 api_key + 新 api_keys 列表 (由 API 管理页维护)
# 每次请求实时读取 config.json —— 在 API 管理页创建/改名/删除后立即生效, 无需重启网关
def _valid_keys() -> set:
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    keys = {cfg.get("api_key") or API_KEY}
    for a in cfg.get("api_keys") or []:
        if a.get("key"):
            keys.add(a["key"])
    return keys

# 限速参数
MAX_REQ_PER_MIN = int(CONFIG.get("max_req_per_min", 30))

PROVIDERS = build_providers(CONFIG)

app = FastAPI(title="Open-API 聚合网关", version="1.0.0")


class SlidingWindowLimiter:
    def __init__(self, max_per_min: int):
        self.max_per_min = max_per_min
        self._stamps: collections.deque = collections.deque()
        self._lock = asyncio.Lock()

    async def allow(self) -> bool:
        if self.max_per_min <= 0:
            return True
        async with self._lock:
            now = time.monotonic()
            while self._stamps and now - self._stamps[0] > 60:
                self._stamps.popleft()
            if len(self._stamps) >= self.max_per_min:
                return False
            self._stamps.append(now)
            return True


limiter = SlidingWindowLimiter(MAX_REQ_PER_MIN)


def _check_auth(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return "缺少 Authorization: Bearer <api_key>"
    if auth[7:].strip() not in _valid_keys():
        return "API Key 无效"
    return None


@app.get("/")
async def root():
    return {
        "service": "Open-API 聚合网关",
        "version": "1.0.0",
        "providers": {name: p.list_models() for name, p in PROVIDERS.items()},
        "models": [m["id"] for p in PROVIDERS.values() for m in p.list_models()],
        "endpoints": ["GET /v1/models", "POST /v1/chat/completions"],
    }


@app.get("/v1/models")
async def list_models(request: Request):
    err = _check_auth(request)
    if err:
        return JSONResponse({"error": {"message": err, "type": "auth_error"}}, status_code=401)
    data = []
    for p in PROVIDERS.values():
        data.extend(p.list_models())
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    err = _check_auth(request)
    if err:
        return JSONResponse({"error": {"message": err, "type": "auth_error"}}, status_code=401)

    if not await limiter.allow():
        return JSONResponse(
            {"error": {"message": "请求过于频繁, 请稍后再试", "type": "rate_limit_error"}},
            status_code=429, headers={"Retry-After": "10"})

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "请求体不是合法 JSON",
                                       "type": "invalid_request_error"}}, status_code=400)

    model = body.get("model", "")
    provider = route_provider(model, PROVIDERS)
    if provider is None:
        return JSONResponse({"error": {"message": "该模型提供商未配置或未注册, 请检查 config.json",
                                       "type": "provider_error"}}, status_code=503)
    if not body.get("messages"):
        return JSONResponse({"error": {"message": "messages 不能为空",
                                       "type": "invalid_request_error"}}, status_code=400)

    stream = bool(body.get("stream", False))

    async def run_non_stream():
        try:
            return JSONResponse(await provider.chat(body))
        except Exception as e:
            logger.exception("provider %s 非流式失败", provider.name)
            msg = str(e)[:300]
            if "401" in msg or "token" in msg.lower():
                return JSONResponse({"error": {"message": msg, "type": "auth_error"}},
                                    status_code=401)
            return JSONResponse({"error": {"message": msg, "type": "upstream_error"}},
                                status_code=502)

    async def run_stream():
        try:
            async for chunk_dict in provider.stream(body):
                yield "data: " + json.dumps(chunk_dict, ensure_ascii=False) + "\n\n"
        except Exception as e:
            logger.exception("provider %s 流式失败", provider.name)
            err = {"error": {"message": str(e)[:300], "type": "upstream_error"}}
            yield "data: " + json.dumps(err, ensure_ascii=False) + "\n\n"
        yield "data: [DONE]\n\n"

    if stream:
        return StreamingResponse(run_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})
    return await run_non_stream()


# ======================= Anthropic Messages API 兼容端点 (/v1/messages) =======================
# 供 Claude Code / CC Switch 等 Claude 协议客户端使用。
# 请求走 x-api-key 或 Authorization: Bearer 鉴权; 模型名沿用 workbuddy-*/trae-* 路由规则。


@app.post("/v1/messages")
@app.post("/v1/v1/messages")  # 别名: 兼容客户端把 /v1 叠写的情况
async def anthropic_messages(request: Request):
    # 鉴权: 兼容 Anthropic 的 x-api-key 与 OpenAI 的 Authorization
    raw_key = request.headers.get("x-api-key") or ""
    if not raw_key:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            raw_key = auth[7:].strip()
    if raw_key not in _valid_keys():
        return JSONResponse(
            {"type": "error",
             "error": {"type": "authentication_error",
                       "message": "invalid x-api-key"}},
            status_code=401)

    if not await limiter.allow():
        return JSONResponse(
            {"type": "error",
             "error": {"type": "rate_limit_error",
                       "message": "请求过于频繁, 请稍后再试"}},
            status_code=429, headers={"Retry-After": "10"})

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"type": "error",
             "error": {"type": "invalid_request_error",
                       "message": "请求体不是合法 JSON"}},            status_code=400)

    model = body.get("model", "")
    # 监控日志: 记录 Claude Code 请求特征 (system 长度/工具数)
    _sys = body.get("system") or ""
    if isinstance(_sys, list):
        _sys = "".join((b.get("text") or "") for b in _sys if isinstance(b, dict))
    logger.info("[anthropic] model=%s system_len=%d tools=%d",
                model, len(_sys), len(body.get("tools") or []))
    provider = route_provider(model, PROVIDERS)
    if provider is None:
        return JSONResponse(
            {"type": "error",
             "error": {"type": "provider_error",
                       "message": "该模型提供商未配置或未注册(注意: 账号需先通过 账号管理.bat 登录)"}},
            status_code=503)

    oa_body = anthropic_to_openai(body)
    stream = bool(body.get("stream"))

    async def run_anthropic_stream():
        try:
            async for sse in openai_stream_to_anthropic(provider.stream(oa_body), model):
                yield sse
        except Exception as e:
            logger.exception("anthropic 流式失败")
            yield "event: error\ndata: " + json.dumps(
                {"type": "error",
                 "error": {"type": "api_error", "message": str(e)[:300]}},
                ensure_ascii=False) + "\n\n"

    if stream:
        return StreamingResponse(run_anthropic_stream(),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # 非流式: 走流式通道聚合 (规避 trae 上游非流式挂起问题)
    try:
        oa_resp = await aggregate_stream(provider.stream(oa_body))
        oa_resp["model"] = model
    except Exception as e:
        logger.exception("anthropic 非流式失败")
        return JSONResponse(
            {"type": "error",
             "error": {"type": "api_error", "message": str(e)[:300]}},
            status_code=502)
    return openai_to_anthropic(oa_resp, model)


if __name__ == "__main__":
    import uvicorn
    print("=" * 60)
    print("  Open-API 聚合网关 (open-ai)")
    print(f"  http://{HOST}:{PORT}/v1/chat/completions")
    print(f"  API Key: {API_KEY}")
    print(f"  Providers: {list(PROVIDERS.keys())}")
    print("=" * 60)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
