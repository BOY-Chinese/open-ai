# -*- coding: utf-8 -*-
"""WorkBuddy 国际版 Provider — www.workbuddy.ai (逆向)

移植自 "open-ai - 副本" 的 providers/workbuddy_intl.py, 并按本仓库 (v2.4) 的
Provider 接口做了适配:

  * 复用国内版 workbuddy.py 的全部逻辑 (请求体标准化 / SSE 解析 / 聚合 /
    流式输出 / 工具调用), 只替换上游 host 与产品标识。
  * 动态模型: 国际版没有 /v3/config, 改用其真实模型目录接口
    GET /v2/enterprises/personal/models 拉取, 失败自动回退静态表
    (与 scripts/account_manager.py 的 intl_model_rates 同源)。
  * 模型命名与国内版 wb- 前缀对齐, 统一 wbie- 前缀 (另保留 wbie-auto)。
    历史前缀 wbai- 仍作为别名保留, 老客户端无需改动。

与国内版 workbuddy.py 的差异点 (抓包 + 实测确认):
  * 域名: www.workbuddy.ai (国内: copilot.tencent.com)
  * 产品标识: X-Product / X-Product-Code = workbuddy-ai (国内: SaaS)
  * 首条消息必须是 system, 否则 400 code=11128 (first message is not system prompt)
    → 自动为无 system 开头的对话注入一条 system
  * 登录: 网页 OIDC 登录后 POST /console/login/enterprise 返回
    accessToken/refreshToken (Keycloak 签发, 有效期约 1 年)
  * 刷新端点: /v2/plugin/auth/token/refresh (与国内版同款)

模型命名: wbie-<上游模型名>, 例如:
  wbie-deepseek-v4.1-flash → deepseek-v4.1-flash
"""
import json
import logging

from providers.workbuddy import (
    WorkBuddyProvider, _WB_CONFIG_PATH, _parse_sse_line,
    _to_upstream_body, _upstream_headers, normalize_model_name,
    UPSTREAM_CHAT_PATH, UPSTREAM_REFRESH_PATH, USER_AGENT,
)

logger = logging.getLogger("openapi.workbuddy_intl")

# 国际版上游
INTL_HOST = "www.workbuddy.ai"
INTL_DOMAIN = "www.workbuddy.ai"
INTL_PRODUCT = "workbuddy-ai"
# 国际版模型目录 (真实接口, 与 account_manager.intl_model_rates 同源)
# 主源用 /v3/config (客户端实际读取的那份, 模型更全: 22 个 vs 目录 18 个,
# 且含 gpt-6-astra 等目录缺失的模型); 目录接口作回退。
INTL_CONFIG_PATH = "/v3/config"
INTL_MODELS_PATH = "/v2/enterprises/personal/models"

INTL_DEFAULT_MODEL = "deepseek-v4.1-flash"

# 客户端叫法 -> 上游模型名
INTL_MODEL_ALIASES = {
    "deepseek-v4.1-flash": "deepseek-v4.1-flash",
    "deepseek-v4.1": "deepseek-v4.1-flash",
    "deepseek-flash": "deepseek-v4.1-flash",
    "deepseek-chat": "deepseek-v4.1-flash",
    "auto": "deepseek-v4.1-flash",
    # WorkBuddy 自定义模型 id 兼容 (新前缀 wbie-)
    "wbie-deepseek-v4.1-flash": "deepseek-v4.1-flash",
    "custom-local:wbie-deepseek-v4.1-flash": "deepseek-v4.1-flash",
    "custom-local:deepseek-v4.1-flash": "deepseek-v4.1-flash",
    # 历史前缀 wbai- 兼容 (v2.5 前对外暴露的写法, 老客户端继续可用)
    "wbai-deepseek-v4.1-flash": "deepseek-v4.1-flash",
    "custom-local:wbai-deepseek-v4.1-flash": "deepseek-v4.1-flash",
}

# 对外模型列表使用的前缀 (单一真源, 改名只需动这里)
INTL_PREFIX = "wbie-"

# 历史前缀: 仅作别名兼容, 不再出现在模型列表中
INTL_LEGACY_PREFIX = "wbai-"

# 列表额外保留的通用兼容别名 (OpenAI 风格叫法, 请求始终可用)
# 注意: 历史前缀 wbai-auto 不作列表项, 仅作请求别名 (见 __init__ 的 _legacy_only)
INTL_COMPAT_ALIASES = ("wbie-auto",)

# 仅作请求兼容、不进入模型列表的历史别名 (改名前的对外叫法)
INTL_LEGACY_ONLY_ALIASES = {"wbai-auto": "deepseek-v4.1-flash"}

# 标准目录接口不返回、但可直接调用的已知模型 id (缺失时补进列表)
_INTL_KNOWN_EXTRA = ("deepseek-v4.1-flash", "gpt-6-astra")


def _strip_intl_prefix(model: str) -> str:
    """去掉模型名里的 wbie- / wbai- / custom-local: 前缀, 得到上游模型名。

    可叠加前缀 (如 custom-local:wbie-xxx) 需循环剥离到稳定为止 ——
    单遍循环会漏掉后出现的那个前缀。
    """
    m = (model or "").strip()
    changed = True
    while changed:
        changed = False
        for prefix in (INTL_PREFIX, INTL_LEGACY_PREFIX, "custom-local:"):
            if m.startswith(prefix):
                m = m[len(prefix):]
                changed = True
    return m


def _intl_headers(account: dict, body: bytes = b"") -> dict:
    """国际版上游请求头: 国内版同款 + X-Product-Code。"""
    headers = _upstream_headers(account, INTL_DOMAIN, INTL_PRODUCT, body)
    headers["X-Product-Code"] = INTL_PRODUCT
    return headers


class WorkBuddyIntlProvider(WorkBuddyProvider):
    name = "workbuddy-intl"

    def __init__(self, cfg: dict):
        cfg = dict(cfg)
        cfg.setdefault("domain", INTL_DOMAIN)
        cfg.setdefault("product", INTL_PRODUCT)
        cfg.setdefault("defaultModel", INTL_DEFAULT_MODEL)
        cfg.setdefault("switchEvery", 10)
        super().__init__(cfg)
        # config.json providers.workbuddy_intl.models 提供的实例级别名
        # (基类 aliases 混入了国内版 MODEL_ALIASES, 这里只保留国际版表)
        self._cfg_aliases = {k: v for k, v in (cfg.get("models") or {}).items()}
        self.aliases = dict(INTL_MODEL_ALIASES)
        self.aliases.update(self._cfg_aliases)
        # 历史前缀别名: 只让请求能用, 不暴露进模型列表
        self.aliases.update(INTL_LEGACY_ONLY_ALIASES)
        # 基类 _to_upstream_body 内部用的是国内版前缀剥离(不认 wbie-/wbai-),
        # 这里补齐 wbie- / wbai- 前缀键, 保证任何调用路径都能命中而非把
        # wbie-xxx 原样发给上游 (否则上游返回 400 code=11102 model service info not found)。
        extra = {}
        for k, v in list(self.aliases.items()):
            if k.startswith(("custom-local:", INTL_PREFIX, INTL_LEGACY_PREFIX)):
                continue
            extra[f"{INTL_PREFIX}{k}"] = v
            extra[f"{INTL_LEGACY_PREFIX}{k}"] = v
        self.aliases.update(extra)

    # ---------- 账号轮换 ----------
    def _pick_account(self) -> dict:
        """复用国内版逻辑 (按 switchEvery 批量轮换), 但读取 providers.workbuddy_intl 段。"""
        try:
            with open(_WB_CONFIG_PATH, encoding="utf-8") as _f:
                intl_cfg = (json.load(_f).get("providers") or {}).get("workbuddy_intl", {})
            fresh = intl_cfg.get("accounts", [])
            # 合并内存中的 token 刷新状态
            for old in self.accounts:
                match = next((n for n in fresh
                              if n.get("userId") == old.get("userId")
                              or n.get("accessToken") == old.get("accessToken")), None)
                if match:
                    match["accessToken"] = old.get("accessToken", match.get("accessToken", ""))
                    match["refreshToken"] = old.get("refreshToken", match.get("refreshToken", ""))
            self.accounts = fresh if fresh else self.accounts
        except Exception:
            pass  # 读取失败则沿用内存 accounts
        # 凭据观察 + 跳过运行态失效账号 (与国内版 _pick_account 同口径)
        for a in self.accounts:
            self._health.observe_token(a.get("userId"), a.get("accessToken"))
        enabled = [a for a in self.accounts if a.get("enabled", True)
                   and not self._health.is_invalid(a.get("userId"))]
        if not enabled:
            return {}
        self._req_count += 1
        # 与国内版同口径: 按请求序号计算批次索引, 启用账号数变化时也能均匀轮换
        self._cur_idx = (self._req_count - 1) // self.switch_every % len(enabled)
        return enabled[self._cur_idx]

    # ---------- 动态模型 (国际版目录接口) ----------
    async def _fetch_upstream_models(self) -> list[str]:
        """取国际版模型目录 —— 主源 GET /v3/config, 回退 /v2/.../models。

        ★ 2026-09-18 换源 (master 反馈「国际版 gpt-6-astra 倍率为 0」) ★
        旧实现只打 /v2/enterprises/personal/models, 实测它**不是客户端用的
        那份数据**: 国际版只返回 18 个模型, 而客户端展示 22 个 (gpt-6-astra
        等根本不在其中, 只能靠在 _INTL_KNOWN_EXTRA 里硬编码补)。
        客户端真正读的是 GET /v3/config → data.models (与国内版同一接口),
        该接口模型更全、倍率也更新。故改为 config 优先、目录兜底。
        """
        if not self.accounts:
            raise RuntimeError("workbuddy-intl 无账号, 无法拉取上游模型")
        last_err = None
        for account in self.accounts:
            if not await self._refresh_token(account):
                continue
            try:
                headers = _intl_headers(account)
                client = self.get_client()
                models: list[str] = []
                # ── 主源: /v3/config → data.models ──
                resp = await client.get(
                    f"https://{INTL_HOST}{INTL_CONFIG_PATH}",
                    headers=headers, timeout=30.0)
                if resp.status_code == 200:
                    cfg_models = ((resp.json().get("data") or {})
                                  .get("models") or [])
                    for m in cfg_models:
                        if isinstance(m, dict) and m.get("id"):
                            models.append(str(m["id"]))
                elif resp.status_code in (401, 403):
                    last_err = f"{resp.status_code}"
                    continue
                # ── 回退: /v2/enterprises/personal/models ──
                if not models:
                    resp2 = await client.get(
                        f"https://{INTL_HOST}{INTL_MODELS_PATH}",
                        headers=headers, timeout=30.0)
                    if resp2.status_code in (401, 403):
                        last_err = f"{resp2.status_code}"
                        continue
                    if resp2.status_code == 200:
                        for m in ((resp2.json().get("data") or {})
                                  .get("models") or []):
                            if isinstance(m, dict) and m.get("id"):
                                models.append(str(m["id"]))
                            elif isinstance(m, str) and m:
                                models.append(m)
                # 两源都缺失但可直接调用的已知模型
                for mid in _INTL_KNOWN_EXTRA:
                    if mid not in models:
                        models.append(mid)
                if models:
                    return models
                last_err = "empty"
            except Exception as e:  # noqa: BLE001
                last_err = repr(e)
        raise RuntimeError(f"所有账号均无法拉取上游模型: {last_err}")

    async def refresh_models(self, force: bool = False) -> bool:
        """基类刷新逻辑 + 补 wbie- 前缀别名 (基类补的是国内版 wb- 前缀)。"""
        ok = await super().refresh_models(force=force)
        for m in self._upstream_models:
            self.aliases.setdefault(f"{INTL_PREFIX}{m}", m)
            self.aliases.setdefault(f"{INTL_LEGACY_PREFIX}{m}", m)
        return ok

    def _model_ids(self) -> set[str]:
        """对外列表: 统一 wbie- 前缀 (每模型一个名字), 另加通用兼容别名。

        上游目标名 = 国际版静态别名 values + config 别名 values + 动态上游模型。
        基类 _model_ids 会混入国内版 MODEL_ALIASES, 故此处完全重写。
        """
        targets = (set(INTL_MODEL_ALIASES.values())
                   | set(self._cfg_aliases.values())
                   | set(self._upstream_models))
        ids = {f"{INTL_PREFIX}{m}" for m in targets if m}
        ids |= set(INTL_COMPAT_ALIASES)
        return ids

    def list_models(self) -> list[dict]:
        return [{"id": m, "object": "model", "owned_by": "tencent-intl"}
                for m in sorted(self._model_ids())]

    def normalize_model(self, model: str) -> str:
        """先去 wbie-/wbai- 前缀, 再走基类别名表。"""
        return normalize_model_name(_strip_intl_prefix(model),
                                    self.default_model, self.aliases)

    # ---------- token 刷新 ----------
    async def _refresh_token(self, account: dict) -> bool:
        """国际版刷新: Keycloak 离线 token, 走同款插件刷新端点 (host=www.workbuddy.ai)。"""
        if not account.get("refreshToken"):
            return False
        async with self._refresh_lock:
            if not self._is_expired(account):
                return True
            try:
                client = self.get_client()
                headers = {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                    "X-Domain": INTL_DOMAIN,
                    "X-Refresh-Token": account["refreshToken"],
                    "X-Auth-Refresh-Source": "plugin",
                    "X-Product": INTL_PRODUCT,
                    "X-Product-Code": INTL_PRODUCT,
                    "User-Agent": USER_AGENT,
                    "X-Trace-ID": "0" * 32,
                    "X-Request-ID": "0" * 32,
                }
                resp = await client.post(
                    f"https://{INTL_HOST}{UPSTREAM_REFRESH_PATH}",
                    json={}, headers=headers)
                if resp.status_code != 200:
                    logger.warning("workbuddy-intl refresh HTTP %s: %s",
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
                    logger.info("workbuddy-intl accessToken 已自动刷新")
                    return True
                return False
            except Exception as e:  # noqa: BLE001
                logger.error("workbuddy-intl refresh 异常: %s", e)
                return False

    # ---------- 请求体 ----------
    def _inject_system(self, body: dict) -> dict:
        """国际版要求首条消息必须是 system (code=11128), 自动补一条。"""
        messages = body.get("messages") or []
        if messages and (messages[0] or {}).get("role") == "system":
            return body
        out = dict(body)
        out["messages"] = [{"role": "system",
                            "content": "You are a helpful assistant."}, *messages]
        return out

    def _to_body(self, body: dict) -> dict:
        """构造上游请求体 (模型名归一化 + 强制流式 + system 注入)。

        模型名先经本类 normalize_model 归一化: 基类 _to_upstream_body 用的是
        国内版前缀剥离规则, 不认 wbie-/wbai-, 会把 wbie-auto 之类原样发给上游。
        """
        body = dict(body)
        if body.get("model"):
            body["model"] = self.normalize_model(body["model"])
        up = _to_upstream_body(body, self.default_model, self.aliases)
        return self._inject_system(up)

    # ---------- 上游请求 ----------
    async def _request_upstream(self, up_body: dict, account: dict,
                                retried: bool = False):
        """与国内版一致, 仅替换上游 host 为国际版。"""
        client = self.get_client()
        body_bytes = json.dumps(up_body, ensure_ascii=False).encode("utf-8")
        headers = _intl_headers(account, body_bytes)
        req = client.build_request(
            "POST", f"https://{INTL_HOST}{UPSTREAM_CHAT_PATH}",
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

    async def _chat_events(self, body: dict, account: dict):
        """与国内版一致, 使用国际版请求体构造 (含 system 注入)。"""
        up_body = self._to_body(body)
        resp = await self._request_upstream(up_body, account)
        if resp is None:
            raise RuntimeError("workbuddy-intl 上游请求失败")
        if resp.status_code != 200:
            err_text = (await resp.aread()).decode("utf-8", errors="replace")
            await resp.aclose()
            if resp.status_code in (401, 403):
                # 首发被拒且刷新重试仍被拒 → 凭据已被上游判死, 沉淀运行态
                self._health.mark_invalid(
                    account.get("userId"),
                    reason=f"chat 被拒 HTTP {resp.status_code} (含刷新重试)")
            raise RuntimeError(
                f"workbuddy-intl upstream HTTP {resp.status_code}: {err_text[:300]}")
        self._health.mark_ok(account.get("userId"))
        try:
            async for line in resp.aiter_lines():
                obj = _parse_sse_line(line)
                if obj:
                    yield obj
        finally:
            await resp.aclose()
