# -*- coding: utf-8 -*-
"""Trae Provider — 转发到内嵌 Node 后端 (127.0.0.1:18787)

open-ai/trae/server.js (Node) 复用 TRAE 内置 aha 网络栈(sscronet.dll)完成 TTNet 加密签名,
调用 llm_utils_chat 并携带会话上下文 → 计费到 **Work 积分池 (TraeWork)**。
本 provider 作为纯转发层, 让 open-api 8000 端口成为唯一对外入口。
Node 后端与 Python 网关同属 open-ai 独立项目, 由 start.bat 统一拉起。

模型命名 (对外统一 tr- 前缀, 标识 Trae 通道):
  tr-glm-5.3-flash / tr-kimi-k3 / ... → 上游动态模型 (每日自动同步)
  tr-DeepSeek-V4-Flash-Official__dev  → 上游变体 (__dev=开发模式, __max=最大模式)
  tr-flash-official 等短别名          → config.json providers.trae.models 配置层映射
请求归一化: custom-local:/tr-/trae- 前缀剥离 → 配置别名(整名优先) → 动态上游名(大小写不敏感)。
上游 v3 协议: 带 __dev/__max 后缀的变体名由 server.js 拆成 config_name+model_name 分开传,
整串塞 config_name 会被上游拒绝 (the param is invalid)。
"""
import asyncio
import json
import logging
import time

import httpx

from providers.base import Provider, make_chunk_id

logger = logging.getLogger("openapi.trae")

# ======================= 上游「无效模型」屏蔽表 =======================
# 上游 get_detail_param 返回的 config_info_list 里混着一批**不可用**的名字,
# 它们不是真实聊天模型, 列出来只会让用户点了报错:
#
#   1. custom_model_*  —— Trae 客户端「自定义模型」功能的占位/代理配置。
#      实测(2026-09-18, 上游 18787 + 网关 8000 双向核对)共 15 个:
#        custom_model_placeholder / custom_model_1M[_text] / custom_model_200k[_text]
#        custom_model_gemini / custom_model_vercel[_gemini] / custom_model_kimi
#        custom_model_gpt-5 / custom_model_gpt-6 / custom_model_no-fc
#        custom_model_deepseek_v4 / custom_model_deepseek_chat / custom_model_deepseek_reasoner
#      这些是「用户自配模型」的槽位, 真身取决于客户端本地配置, 走网关调用必然失败。
#   2. 内部工具名 —— Trae IDE 自身的辅助功能, 不是给人对话用的模型:
#        summary(上下文摘要) / fast_apply[_new](代码快进应用)
#        title_generation(会话标题生成) / input_optimization(输入优化)
#
# ★ 屏蔽只作用于「对外暴露的模型列表」, 不影响请求侧:
#   normalize_model() 仍然照常解析这些名字, 保证已有 auto_chain / 客户端里
#   残留的旧模型名不会从「列表消失」直接变成「请求 500」。
MODEL_BLOCKLIST_PREFIXES = ("custom_model_",)
MODEL_BLOCKLIST_EXACT = {
    "summary",
    "fast_apply",
    "fast_apply_new",
    "title_generation",
    "input_optimization",
}


def is_usable_model(name: str) -> bool:
    """该上游模型名是否应该对外暴露 (False = 无效/内部模型, 需屏蔽)。

    大小写不敏感 —— 上游 config_name 大小写并不统一 (如 DeepSeek-V4-Flash 与
    custom_model_gpt-5 混排), 只按全小写比对才不会漏。
    """
    n = (name or "").strip().lower()
    if not n:
        return False
    if n in MODEL_BLOCKLIST_EXACT:
        return False
    return not any(n.startswith(p) for p in MODEL_BLOCKLIST_PREFIXES)


class TraeProvider(Provider):
    name = "trae"

    def __init__(self, cfg: dict):
        self.base_url = (cfg.get("base_url") or "http://127.0.0.1:18787/v1").rstrip("/")
        self.timeout = float(cfg.get("timeout", 300.0))
        # 别名仅来自 config.json providers.trae.models (配置层, 代码不再写死)
        # 例: {"deepseek-v4-flash": "DeepSeek-V4-Flash__dev", ...}  ← 值可为 __dev/__max 变体名
        self.aliases = {k: v for k, v in (cfg.get("models") or {}).items()}
        self._default = (cfg.get("default_model") or "DeepSeek-V4-Flash-Official__dev").strip()
        # 账号池: 与 trae/server.js 读的是同一份 config.json providers.trae.accounts。
        # 仅用于「有没有可用账号」的判定 —— 见 list_models()。
        self.accounts = cfg.get("accounts") or []
        # ---- 动态模型: 转发 Node 后端 /v1/models (Node 侧每日拉上游), 失败回退静态 ----
        self._node_models: list[str] = []   # 最近一次从 Node 后端同步到的上游模型
        self._node_synced_at: float = 0.0
        self._node_sync_lock = asyncio.Lock()

    def has_usable_account(self) -> bool:
        """是否存在可用 Trae 账号（未禁用、未标记失效）。

        判定口径与 trae/server.js 的 `!a.invalid && a.enabled !== false` 对齐，
        否则会出现「列表说有、请求说没有」的矛盾。
        """
        return any(a.get("enabled", True) is not False and not a.get("invalid")
                   for a in self.accounts)

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
            # 首次同步: Node 后端可能仍在启动/拉取上游, 重试直到拿到真实列表
            # (否则一次失败就要等 24h, 期间 /v1/models 缺 glm/kimi/qwen 等绝大多数模型)
            attempts = 10 if not self._node_models else 1
            for i in range(attempts):
                try:
                    async with httpx.AsyncClient(timeout=20.0) as client:
                        resp = await client.get(f"{self.base_url}/models")
                        if resp.status_code == 200:
                            data = resp.json()
                            models = [m.get("id") for m in (data.get("data") or [])
                                      if m.get("id")]
                            # 入口就屏蔽无效模型 (custom_model_* / 内部工具名):
                            # 缓存里一旦混进这些名字, 后面每个出口都得再过滤一遍,
                            # 在唯一的入口处拦掉最省心 (见 is_usable_model 注释)。
                            dropped = [m for m in models if not is_usable_model(m)]
                            models = [m for m in models if is_usable_model(m)]
                            if dropped:
                                logger.info("trae 已屏蔽 %d 个无效模型: %s",
                                            len(dropped), ", ".join(sorted(dropped)[:8]))
                            if models:
                                self._node_models = models
                                self._node_synced_at = time.time()
                                logger.info("trae 动态模型同步成功: %d 个", len(models))
                                return True
                            logger.warning("trae Node 后端 /v1/models 返回空 (%d/%d), 重试",
                                           i + 1, attempts)
                        else:
                            logger.warning("trae Node 后端 /v1/models HTTP %s (%d/%d), 重试",
                                           resp.status_code, i + 1, attempts)
                except Exception as e:  # noqa: BLE001
                    logger.warning("trae 动态模型同步失败 (%d/%d): %s, 重试",
                                   i + 1, attempts, e)
                if i < attempts - 1:
                    await asyncio.sleep(10)
            logger.warning("trae 动态模型同步仍未成功, 回退静态表格")
            return bool(self._node_models)

    # ---------------- 基础接口 ----------------
    def list_models(self) -> list[dict]:
        # 没有任何可用账号时**不对外暴露任何模型**。
        #
        # 为什么（本机/虚拟机实测反馈「没账号却显示一堆 Trae 模型」）：
        #   Trae 的模型列表 = 动态上游模型 + config 别名 + 默认模型回退，
        #   后两者全是静态的，因此即使账号池为空也会列出一堆模型；
        #   而这些模型一个都调不通（server.js 账号池为空会直接报错），
        #   只会让用户误以为「有模型可用」。WorkBuddy 侧本来就是
        #   「无账号则不注册 provider」，这里把 Trae 的口径对齐。
        if not self.has_usable_account():
            return []

        # 对外统一 tr- 前缀标识 Trae 通道: 动态上游模型 + 配置别名 + 默认模型回退
        names = set(self._node_models) | set(self.aliases.values()) \
            | set(self.aliases.keys())
        names.add(self._default)
        # 出口二次屏蔽 (纵深防御): sync_models 已在入口过滤, 但若 Node 后端是
        # 未更新的旧版 server.js (其 /v1/models 原样透传上游), 缓存里仍会带进
        # custom_model_* —— 这里再拦一道, 保证「重启了 Python 但没重启 Node」
        # 或「只换 Python 不换 JS」时都不会漏出。别名/默认模型同样过筛。
        names = {m for m in names if is_usable_model(m)}
        return [{"id": f"tr-{m}", "object": "model", "created": 0, "owned_by": "trae-proxy"}
                for m in sorted(names)]

    def normalize_model(self, model: str) -> str:
        name = (model or "").strip()
        # WorkBuddy 客户端旧格式 custom-local: 前缀, 仅请求兼容(不再在列表暴露)
        if name.startswith("custom-local:"):
            name = name[len("custom-local:"):]
        if not name:
            return self._default
        # 1) 完整名先查配置别名 —— 必须在拆 "__dev/__max" 后缀**之前**,
        #    否则 "trae-v4-max" 会被先剥 trae- 再截成 "v4__max" 这种垃圾名。
        if name in self.aliases:
            return self.aliases[name]
        # 2) 剥通道前缀: tr- 为规范前缀, trae- 兼容旧客户端 (循环剥, 兼容 trae-trae-x)
        low = name.lower()
        while low.startswith("tr-") or low.startswith("trae-"):
            name = name[5:] if low.startswith("trae-") else name[3:]
            low = name.lower()
            if name in self.aliases:      # 剥完前缀可能命中别名表
                return self.aliases[name]
        # 3) 动态上游名大小写不敏感匹配 (tr-glm-5.3-flash → glm-5.3-flash)
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
