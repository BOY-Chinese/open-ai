# -*- coding: utf-8 -*-
"""Auto 路由链 — 多条自定义模型路由链 (虚拟模型)

网关把若干真实模型按顺序编成一条「路由链」; 请求链名时按顺序逐个尝试,
某个模型超时或返回 HTTP 错误时自动切换到下一个, 直到成功 (故障转移)。

v3.2 起 (2026-09-18): 由「只能定制一条路由链」升级为**多条自定义路由链**,
每条链可自定义名称, 链上每个模型有独立的超时秒数。

与各平台内置的 "auto" 模型完全无关 (内置 auto 是上游的模型别名)。
对外总名称使用中文「Auto路由链」; 历史别名 "Auto路由连" / "Auto-mode" 仍兼容识别。

config.json 新格式 (多条链):
  "auto_chain": {
    "chains": [
      {"id": "c1726...", "name": "无名1", "enabled": true,
       "models": [{"model": "lm-GLM-5.3-Flash", "timeout": 120}, ...]}
    ]
  }
兼容旧格式 (单链, 读取时自动迁移为一条名为「无名1」的链, 保存后落新格式):
  "auto_chain": {"enabled": true, "timeout": 120, "models": ["a", "b"]}

调用方式:
  model = "Auto路由链"   → 用第一条启用的路由链 (别名 "Auto路由连"/"Auto-mode" 同效)
  model = "<路由链名>"   → 名称精确匹配 (去首尾空白、忽略大小写) 的启用路由链
"""
import asyncio
import json
import logging
import os
import re
import time

logger = logging.getLogger("openapi.auto")

# 对外虚拟模型总名 (master 指定的正式名称; 旧名「Auto路由连」为笔误, 已弃用)
AUTO_MODEL = "Auto路由链"
# 历史别名: 旧客户端仍按旧名请求, 继续兼容识别
AUTO_MODEL_ALIASES = ("Auto路由连", "Auto-mode")

# 「检查」探测请求: 尽可能短, 只要能证明「连通且有回复」即可
CHECK_PROMPT = "回复ok"
# 「检查」单模型等待上限 (秒): 检查是探活, 不按正式请求的长超时等
CHECK_TIMEOUT_CAP = 30.0

# ★ 打包态 __file__ 是相对路径, abspath 跟着 CWD 走 —— 钉死安装根 (见 app_paths.py)。
try:
    import app_paths as _ap
    CONFIG_PATH = _ap.CONFIG_PATH
except Exception:  # 源码态的兜底
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


def is_auto_model(model: str) -> bool:
    """请求模型是否为 Auto 路由链总名 (含历史别名)。"""
    m = (model or "").strip().lower()
    if m == AUTO_MODEL.lower():
        return True
    return any(m == alias.lower() for alias in AUTO_MODEL_ALIASES)


# ─────────────────────────── 配置读写 (归一化 + 旧格式迁移) ───────────────────────────

def _read_cfg() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _norm_timeout(v) -> float:
    """单模型超时秒数, 非法值归 0 (= 用 provider 默认)。"""
    try:
        return max(0.0, float(v))
    except (TypeError, ValueError):
        return 0.0


def _norm_models(raw) -> list[dict]:
    """链上模型归一化: [{"model": str, "timeout": 秒}]; 兼容旧版纯字符串数组。"""
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for m in raw:
        if isinstance(m, str):
            m = {"model": m}
        if not isinstance(m, dict):
            continue
        mid = str(m.get("model") or "").strip()
        if not mid:
            continue
        out.append({"model": mid, "timeout": _norm_timeout(m.get("timeout"))})
    return out


def _gen_chain_id() -> str:
    return f"c{int(time.time() * 1000)}"


def normalize_chain(raw, idx: int = 0) -> dict | None:
    """单条链归一化; 名称为空的链自动补「无名N」而不是丢弃。"""
    if isinstance(raw, str):  # 兼容把链名直接当链配置的极端写法
        raw = {"name": raw}
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip() or f"无名{idx + 1}"
    models = _norm_models(raw.get("models"))
    cid = str(raw.get("id") or "").strip()
    if not cid:
        cid = _gen_chain_id() if models else f"c{int(time.time() * 1000)}-{idx}"
    return {"id": cid, "name": name,
            "enabled": bool(raw.get("enabled", True)), "models": models}


def _migrate_legacy(ch: dict) -> list[dict]:
    """旧单链格式 {enabled, timeout, models[str]} → 一条名为「无名1」的链。"""
    models = [{"model": str(m).strip(), "timeout": _norm_timeout(ch.get("timeout"))}
              for m in (ch.get("models") or []) if str(m or "").strip()]
    return [{"id": _gen_chain_id(), "name": "无名1",
             "enabled": bool(ch.get("enabled", True)), "models": models}]


def load_auto_chains(cfg: dict | None = None) -> list[dict]:
    """读取全部路由链 (归一化; 旧单链格式自动迁移, 不回写配置)。"""
    cfg = cfg if cfg is not None else _read_cfg()
    ch = cfg.get("auto_chain") or {}
    raw_chains = ch.get("chains")
    if not isinstance(raw_chains, list):
        if not ch:
            return []
        raw_chains = _migrate_legacy(ch)
    chains: list[dict] = []
    used_names: set[str] = set()
    for i, raw in enumerate(raw_chains):
        c = normalize_chain(raw, i)
        if c is None:
            continue
        # 链名去重: 重名的链补序号后缀, 保证「按链名请求」无歧义
        name, k = c["name"], 1
        while name.lower() in used_names:
            k += 1
            name = f"{c['name']}{k}"
        used_names.add(name.lower())
        c["name"] = name
        chains.append(c)
    return chains


def new_chain_id() -> str:
    """生成新链的稳定 id（毫秒时间戳）。"""
    return _gen_chain_id()


def sanitize_chains(chains) -> list[dict]:
    """校验并归一化外部提交的多链配置（保存 / 建链前统一走这里）。

    非法输入抛 ValueError（中文信息，由调用方转 HTTP 400）；
    合法输入返回深拷贝式的规范数组，id 缺失自动补齐并去重。
    """
    if not isinstance(chains, list):
        raise ValueError("chains 必须是数组")
    out: list[dict] = []
    seen_ids: set[str] = set()
    for i, raw in enumerate(chains):
        c = normalize_chain(raw, i)
        if c is None:
            raise ValueError(f"chains[{i}] 必须是对象")
        if not str(c.get("name") or "").strip():
            raise ValueError(f"chains[{i}] 名称不能为空")
        cid = str(c.get("id") or "").strip() or _gen_chain_id()
        k = 0
        while cid in seen_ids:  # 同毫秒建多条链时 id 会撞, 追加序号
            k += 1
            cid = f"{c.get('id')}-{k}"
        seen_ids.add(cid)
        c["id"] = cid
        out.append(c)
    return out


def load_auto_chain() -> dict:
    """旧版兼容: 返回第一条启用链的 {enabled, timeout, models} 形状。"""
    ch = first_enabled_chain()
    if ch is None:
        return {}
    timeouts = [m["timeout"] for m in ch["models"] if m.get("timeout")]
    return {"enabled": bool(ch.get("enabled", True)),
            "timeout": int(timeouts[0]) if timeouts else 0,
            "models": [m["model"] for m in ch["models"]]}


# ─────────────────────────── 链选择 ───────────────────────────

def first_enabled_chain(cfg: dict | None = None) -> dict | None:
    """第一条「启用且至少有一个模型」的路由链。"""
    for c in load_auto_chains(cfg):
        if c.get("enabled", True) and c.get("models"):
            return c
    return None


def find_chain_by_name(name: str, cfg: dict | None = None) -> dict | None:
    """按名称精确匹配 (去首尾空白、忽略大小写) 启用中的路由链。"""
    key = (name or "").strip().lower()
    if not key:
        return None
    for c in load_auto_chains(cfg):
        if c.get("enabled", True) and c.get("models") and c["name"].strip().lower() == key:
            return c
    return None


def _registered_model_ids(providers: dict) -> set[str]:
    """所有 provider 已注册的真实模型 id (用于避免链名与真实模型抢路由)。"""
    ids: set[str] = set()
    for p in (providers or {}).values():
        try:
            ids |= {m.get("id") for m in p.list_models() if m.get("id")}
        except Exception:  # noqa: BLE001
            continue
    return ids


def resolve_auto_chain(model: str, providers: dict | None = None) -> dict | None:
    """请求模型名 → 命中的启用路由链; 未命中返回 None。

    规则:
      1. "Auto路由链" (含别名) → 第一条启用链;
      2. 与某条启用链的名称精确相同 → 该链;
      3. 链名与 provider 已注册的真实模型重名时**让位给真实模型** (避免抢路由)。
    """
    if is_auto_model(model):
        return first_enabled_chain()
    chain = find_chain_by_name(model)
    if chain is None:
        return None
    if providers and (model or "").strip() in _registered_model_ids(providers):
        logger.debug("[auto] 链名 %r 与真实模型重名, 让位给真实模型", model)
        return None
    return chain


def chain_model_names(chain: dict) -> list[str]:
    return [m["model"] for m in (chain.get("models") or [])]


class AutoExhausted(Exception):
    """链上所有模型都失败时抛出。"""


# ─────────────────────────── 链式调用 (故障转移) ───────────────────────────

def _per_model_timeout(entry: dict) -> float:
    return _norm_timeout(entry.get("timeout"))


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


async def auto_chat(route_provider, providers: dict, body: dict, chain: dict) -> dict:
    """按链依次尝试非流式请求; 成功即返回, 全部失败抛 AutoExhausted。"""
    entries = chain.get("models") or []
    if not entries:
        raise AutoExhausted(f"路由链「{chain.get('name')}」未配置任何模型")
    errors = []
    for entry in entries:
        m = entry["model"]
        timeout = _per_model_timeout(entry)
        try:
            logger.info("[auto:%s] 尝试模型: %s", chain.get("name"), m)
            result = await _try_chat(route_provider, providers, m, body, timeout)
            logger.info("[auto:%s] 模型 %s 成功", chain.get("name"), m)
            result.setdefault("model", m)
            return result
        except asyncio.TimeoutError:
            errors.append(f"{m}: 超时")
            logger.warning("[auto:%s] 模型 %s 超时, 切换下一个", chain.get("name"), m)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{m}: {str(e)[:120]}")
            logger.warning("[auto:%s] 模型 %s 失败(%s), 切换下一个",
                           chain.get("name"), m, str(e)[:120])
    raise AutoExhausted("; ".join(errors))


def _chunk_has_payload(chunk: dict) -> bool:
    """判断一个流式 chunk 是否携带真实内容 (content / reasoning / tool_calls)。"""
    for choice in (chunk.get("choices") or []):
        delta = choice.get("delta") or {}
        if delta.get("content") or delta.get("reasoning_content") or delta.get("tool_calls"):
            return True
    return False


async def auto_stream(route_provider, providers: dict, body: dict, chain: dict):
    """按链依次尝试流式请求; 首个成功产出真实内容的模型即采用。

    yield: (kind, payload) — kind ∈ {'chunk','meta'}。
    只有产出真实内容(content/tool_calls)后才算命中; 之前失败可静默切换。
    """
    entries = chain.get("models") or []
    if not entries:
        raise AutoExhausted(f"路由链「{chain.get('name')}」未配置任何模型")
    errors = []
    for entry in entries:
        m = entry["model"]
        timeout = _per_model_timeout(entry)
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
            logger.info("[auto:%s] 流式尝试模型: %s", chain.get("name"), m)
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
                logger.info("[auto:%s] 流式模型 %s 完成", chain.get("name"), m)
                return
            # 无真实内容视为失败
            errors.append(f"{m}: 无输出")
            logger.warning("[auto:%s] 流式模型 %s 无输出, 切换下一个", chain.get("name"), m)
        except asyncio.TimeoutError:
            errors.append(f"{m}: 超时")
            logger.warning("[auto:%s] 流式模型 %s 超时", chain.get("name"), m)
            if produced:
                return
        except Exception as e:  # noqa: BLE001
            errors.append(f"{m}: {str(e)[:120]}")
            logger.warning("[auto:%s] 流式模型 %s 失败(%s)", chain.get("name"), m, str(e)[:120])
            if produced:
                return
        finally:
            try:
                await agen.aclose()
            except Exception:
                pass
    raise AutoExhausted("; ".join(errors))


# ─────────────────────────── 连通性检查 (GUI「检查」用) ───────────────────────────

def _classify_check_error(msg: str) -> str:
    """把检查失败的原因归为 'busy'(繁忙) / 'down'(断连) 两类。

    繁忙: 限流 (429)、上游 5xx、超时 —— 通道活着, 只是暂时挤不进去;
    断连: 其余 (401/403/404、连接失败、协议错误等) —— 通道当前不可用。
    """
    low = (msg or "").lower()
    if "429" in low or "rate limit" in low or "繁忙" in msg or "busy" in low:
        return "busy"
    if "超时" in msg or "timeout" in low or "timed out" in low:
        return "busy"
    if re.search(r"\b5\d{2}\b", msg or ""):
        return "busy"
    return "down"


def _reply_snippet(result: dict) -> str:
    """从成功响应里抽一句话摘要 (检查结果展示用)。"""
    try:
        msg = ((result.get("choices") or [{}])[0].get("message") or {})
        text = str(msg.get("content") or "").strip().replace("\n", " ")
        return text[:40]
    except Exception:  # noqa: BLE001
        return ""


async def check_model(route_provider, providers: dict, model: str, timeout: float = 0) -> dict:
    """向上游单个模型发一条极短探测请求, 判定连通情况。

    返回: {"model", "status": 'ok'|'busy'|'down', "latencyMs", "detail"}
      ok   → 正常 (有回复)
      busy → 繁忙 (限流 / 上游 5xx / 超时)
      down → 断连 (认证失败、连接失败、其它异常)
    """
    t = _norm_timeout(timeout)
    if t <= 0:
        t = CHECK_TIMEOUT_CAP
    t = min(t, CHECK_TIMEOUT_CAP)

    sub = {"model": model,
           "messages": [{"role": "user", "content": CHECK_PROMPT}]}
    started = time.monotonic()
    try:
        provider = route_provider(model, providers)
        if provider is None:
            return {"model": model, "status": "down", "latencyMs": 0,
                    "detail": "无法路由: 没有匹配的通道"}
        result = await asyncio.wait_for(provider.chat(sub), timeout=t)
        latency = int((time.monotonic() - started) * 1000)
        snippet = _reply_snippet(result)
        logger.info("[auto] 检查模型 %s: 正常 (%s)", model, snippet or "有回复")
        return {"model": model, "status": "ok", "latencyMs": latency,
                "detail": snippet or "有回复"}
    except asyncio.TimeoutError:
        latency = int((time.monotonic() - started) * 1000)
        logger.info("[auto] 检查模型 %s: 繁忙 (%.0fs 无响应)", model, t)
        return {"model": model, "status": "busy", "latencyMs": latency,
                "detail": f"{int(t)}s 内无响应"}
    except Exception as e:  # noqa: BLE001
        latency = int((time.monotonic() - started) * 1000)
        msg = str(e)[:160]
        status = _classify_check_error(msg)
        logger.info("[auto] 检查模型 %s: %s (%s)", model, status, msg)
        return {"model": model, "status": status, "latencyMs": latency, "detail": msg}
