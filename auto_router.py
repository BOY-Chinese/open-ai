# -*- coding: utf-8 -*-
"""Auto 路由连 — 模型路由链 (虚拟模型)

当请求的 model 为 "Auto路由连" 时, 按 config.json 中 auto_chain 配置的顺序
依次尝试各个模型; 某个模型超时或返回 HTTP 错误时自动切换到下一个, 直到成功。

与各平台内置的 "auto" 模型完全无关 (内置 auto 是上游的模型别名)。
对外名称使用中文「Auto路由连」; 历史别名 "Auto-mode" 仍然兼容。

config.json:
  "auto_chain": {
    "enabled": true,
    "timeout": 120,           # 每个模型的最大等待秒数 (0 表示用 provider 默认)
    "models": ["tr-DeepSeek-V4-Flash-Official", "wb-deepseek-v4.1-flash", "lm-deepseek-v4-flash-0731"]
  }
"""
import asyncio
import json
import logging
import os

logger = logging.getLogger("openapi.auto")

# 对外虚拟模型名（master 指定的正式名称）
AUTO_MODEL = "Auto路由连"
# 历史别名（集成文档初版名称），仍兼容识别
AUTO_MODEL_ALIASES = ("Auto-mode",)

# ★ 打包态 __file__ 是相对路径, abspath 跟着 CWD 走 —— 钉死安装根 (见 app_paths.py)。
try:
    import app_paths as _ap
    CONFIG_PATH = _ap.CONFIG_PATH
except Exception:  # 源码态的兜底
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

def is_auto_model(model: str) -> bool:
    m = (model or "").strip().lower()
    if m == AUTO_MODEL.lower():
        return True
    return any(m == alias.lower() for alias in AUTO_MODEL_ALIASES)

def load_auto_chain() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return {}
    return cfg.get("auto_chain") or {}

class AutoExhausted(Exception):
    """所有链上模型都失败时抛出。"""

def _chain_models() -> list[str]:
    chain = load_auto_chain()
    models = [m for m in (chain.get("models") or []) if m]
    return models

def _per_model_timeout() -> float:
    chain = load_auto_chain()
    try:
        t = float(chain.get("timeout") or 0)
    except (TypeError, ValueError):
        t = 0
    return t

async def _try_chat(route_provider, providers: dict, model: str, body: dict, timeout: float):
    """对单个模型发起非流式请求。返回响应 dict 或抛异常。"""
    provider = route_provider(model, providers)
    if provider is None:
        raise RuntimeError(f"模型 {model} 无法路由")
    sub = dict(body)
    sub["model"] = model
    sub.pop("stream", None)
    coro = provider.chat(sub)
    if timeout > 0:
        return await asyncio.wait_for(coro, timeout=timeout)
    return await coro

async def auto_chat(route_provider, providers: dict, body: dict) -> dict:
    """按链依次尝试非流式请求; 成功即返回, 全部失败抛 AutoExhausted。"""
    models = _chain_models()
    if not models:
        raise AutoExhausted("auto_chain 未配置任何模型")
    timeout = _per_model_timeout()
    errors = []
    for m in models:
        try:
            logger.info("[auto] 尝试模型: %s", m)
            result = await _try_chat(route_provider, providers, m, body, timeout)
            logger.info("[auto] 模型 %s 成功", m)
            result.setdefault("model", m)
            return result
        except asyncio.TimeoutError:
            errors.append(f"{m}: 超时")
            logger.warning("[auto] 模型 %s 超时, 切换下一个", m)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{m}: {str(e)[:120]}")
            logger.warning("[auto] 模型 %s 失败(%s), 切换下一个", m, str(e)[:120])
    raise AutoExhausted("; ".join(errors))

def _chunk_has_payload(chunk: dict) -> bool:
    """判断一个流式 chunk 是否携带真实内容 (content / reasoning / tool_calls)。"""
    for choice in (chunk.get("choices") or []):
        delta = choice.get("delta") or {}
        if delta.get("content") or delta.get("reasoning_content") or delta.get("tool_calls"):
            return True
    return False

async def auto_stream(route_provider, providers: dict, body: dict):
    """按链依次尝试流式请求; 首个成功产出真实内容的模型即采用。

    yield: (kind, payload) — kind ∈ {'chunk','meta'}。
    只有产出真实内容(content/tool_calls)后才算命中; 之前失败可静默切换。
    """
    models = _chain_models()
    if not models:
        raise AutoExhausted("auto_chain 未配置任何模型")
    timeout = _per_model_timeout()
    errors = []
    for m in models:
        provider = route_provider(m, providers)
        if provider is None:
            errors.append(f"{m}: 无法路由")
            continue
        sub = dict(body)
        sub["model"] = m
        sub["stream"] = True

        agen = provider.stream(sub)
        produced = False
        buffered = []
        try:
            logger.info("[auto] 流式尝试模型: %s", m)
            it = agen.__aiter__()
            while True:
                try:
                    if timeout > 0:
                        chunk = await asyncio.wait_for(it.__anext__(), timeout=timeout)
                    else:
                        chunk = await it.__anext__()
                except StopAsyncIteration:
                    break
                if not produced:
                    if not _chunk_has_payload(chunk):
                        # 尚未产出真实内容: 缓冲空/role chunk, 若后续失败则丢弃
                        buffered.append(chunk)
                        continue
                    produced = True
                    yield ("meta", {"model": m})
                    for b in buffered:
                        yield ("chunk", b)
                    buffered = []
                yield ("chunk", chunk)
            if produced:
                logger.info("[auto] 流式模型 %s 完成", m)
                return
            # 无真实内容视为失败
            errors.append(f"{m}: 无输出")
            logger.warning("[auto] 流式模型 %s 无输出, 切换下一个", m)
        except asyncio.TimeoutError:
            errors.append(f"{m}: 超时")
            logger.warning("[auto] 流式模型 %s 超时", m)
            if produced:
                return
        except Exception as e:  # noqa: BLE001
            errors.append(f"{m}: {str(e)[:120]}")
            logger.warning("[auto] 流式模型 %s 失败(%s)", m, str(e)[:120])
            if produced:
                return
        finally:
            try:
                await agen.aclose()
            except Exception:
                pass
    raise AutoExhausted("; ".join(errors))
