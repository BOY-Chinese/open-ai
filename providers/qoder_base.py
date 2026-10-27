# -*- coding: utf-8 -*-
"""qoder_base.py —— Qoder 通道 Provider 基类（国内版 / 国际版共用）

国内版 (Qoder CN) 与国际版 (Qoder) 上游链路**完全同构**：同一套自定义 Base64、
同一套 COSY 签名、同一套 SSE 信封，差别只在于区域配置（域名 / client_id /
device flow 参数）与模型目录。因此这里把全部请求逻辑放在基类，
``qoder_cn.py`` / ``qoder_intl.py`` 只声明 ``name`` / ``realm`` / ``PREFIX``。

对外模型前缀：
    国内版 qdc-<上游 key 或显示名>      （Qoder CN）
    国际版 qd-<上游 key 或显示名>       （Qoder）

请求形态（逆向 qodercli 1.1.75）：
    body < baseprompt.json 模板（system 提示词 + tools 框架）
         + 会话 messages
         + model_config（按目录取 key / display_name / is_vl / is_reasoning）
    → qoder_encode → COSY 头 → POST {gateway}/algo/api/v2/service/pro/sse/agent_chat_generation
    → 解两层 SSE 信封 → 标准 ### chunk
"""
from __future__ import annotations

import asyncio
import copy
import itertools
import json
import logging
import os
import sys
import random
import re
import time
import uuid

import httpx

from .base import (
    AccountHealth, Provider, describe_net_exc, make_chunk_id,
    make_usage_chunk, normalize_usage, wants_usage_in_stream,
)
from . import qoder_common as qc
from . import cred_store
from .tool_echo import (            # noqa: F401  （同名再导出，兼容既有调用点/测试）
    TOOL_CALLS_MARKER,
    _MARKER_RE,
    _ToolCallEchoReader,
    _log_unrecovered_echo,
    _marker_prefix_len,
    client_tool_choice,
    declared_tool_names,
    parse_leaked_tool_calls,
    strip_history_echo,
)

logger = logging.getLogger("openapi.providers.qoder")

# 模板：官方 baseprompt.json 快照（随 providers/ 一起打包）
# ★ 打包态 (PyInstaller onefile): 数据文件解包在 sys._MEIPASS\providers 下,
#   __file__ 也指到 _MEI, 但构建门禁 check_frozen_paths.py 禁止运行时裸用
#   __file__ 拼路径 —— 故 frozen 分支显式走 _MEIPASS, 源码态才在 except 兜底。
try:
    _PROVIDER_RES_DIR = os.path.join(sys._MEIPASS, "providers")  # type: ignore[attr-defined]
except Exception:  # 源码态
    _PROVIDER_RES_DIR = os.path.dirname(os.path.abspath(__file__))
_BASE_PROMPT_PATH = os.path.join(_PROVIDER_RES_DIR, "qoder_baseprompt.json")

# 工具结果在文本降级形态下的前缀（与官方 CLI 同款标记，便于模型识别）
TOOL_RESULT_MARKER = "[TOOL RESULT"

# 单次请求体上限（阿里云 SLB 30MB 会 RST；提前拦下，避免打爆账号池）
MAX_UPSTREAM_BODY_BYTES = 20 * 1024 * 1024


def _truthy(raw, default: bool = False) -> bool:
    """把配置里的「开关」值归一成 bool。

    config.json 里用户可能写 true / false（JSON 布尔），也可能写 "on"/"off"、
    1/0（字符串或数字）；环境变量则一定是字符串。空值/null 用 default。
    """
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    s = str(raw).strip().lower()
    if not s:
        return default
    return s not in ("0", "off", "false", "no", "disabled", "disable")


def _load_base_prompt() -> dict:
    try:
        with open(_BASE_PROMPT_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001
        logger.error("qoder baseprompt 模板加载失败（请求将退化为最小模板）: %s", e)
        return {}


BASEPROMPT = _load_base_prompt()


def _shrink_tool_result(text: str, limit: int = 6000) -> str:
    """长工具结果首尾保留、中间省略（官方 CLI 同风格，抑制复述且省 token）。"""
    if not text or len(text) <= limit:
        return text or ""
    head = limit // 3
    tail = limit - head
    return (f"{text[:head]}\n…[省略 {len(text) - limit} 字符]…\n{text[-tail:]}")


def _flatten_content(content):
    """把 ### content（str 或 parts 列表）压成纯文本；图片另行收集。"""
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    if isinstance(content, dict):
        content = [content]
    elif not isinstance(content, list):
        return str(content), []
    texts, images = [], []
    for piece in content:
        if isinstance(piece, str):
            texts.append(piece)
            continue
        if not isinstance(piece, dict):
            continue
        ptype = piece.get("type") or ""
        if ptype in ("text", "input_text", "output_text", "summary_text"):
            texts.append(piece.get("text") or "")
        elif ptype in ("image_url", "image", "input_image") or "image_url" in piece:
            url = piece.get("image_url") or piece.get("url")
            if isinstance(url, dict):
                url = url.get("url")
            if not url and piece.get("data"):
                mime = piece.get("mimeType") or piece.get("mime_type") or "image/png"
                url = "data:%s;base64,%s" % (mime, piece["data"])
            if url:
                images.append(url)
    return "\n".join(t for t in texts if t), images


def flatten_messages(messages, structured: bool = False) -> tuple:
    """把客户端会话压平成 Qoder 上游接受的 {role, content} 序列。

    返回 (system_text, flat, images)：
      * 首个 system/developer 抽为 system_text（模板 system 的替换源）
      * tool 结果：``structured=True`` → 原生 ``{role:"tool", tool_call_id, content}``
        （缺 tool_call_id 时退回文本形态）；``False`` → user 文本
        （带 [TOOL RESULT ...] 标记）——官方 CLI 形态
      * assistant.tool_calls：``structured=True`` → 原样透传（id/type/function，
        arguments 统一成 JSON 字符串）；``False`` → 序列化进 content
        （[TOOL CALLS] + JSON 数组），保持有据可查
      * assistant.reasoning_content 仅对推理模型保留（多轮一致性依赖它）

    ★ 2026-10-23 起 ``structured`` 默认走**结构化**（见 _structured_tool_history）：
      文本降级形态正是「模型照着 [TOOL CALLS] 格式复述成正文 → 客户端把这段 JSON
      当正文显示 → 本轮工具调用静默不执行」的根源（参考项目 issue #8/#37 同款）。
      文本形态仅作为上游不认结构化历史时的兜底保留，回读侧见
      parse_leaked_tool_calls。
    """
    system_text = None
    flat, images = [], []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        text, imgs = _flatten_content(m.get("content"))
        images.extend(imgs)
        text = text or ""
        if role in ("system", "developer"):
            if system_text is None and text:
                system_text = text
            continue
        if role == "tool":
            cid = str(m.get("tool_call_id") or "").strip()
            if structured and cid:
                entry = {"role": "tool", "tool_call_id": cid,
                         "content": _shrink_tool_result(text)}
                name = str(m.get("name") or "").strip()
                if name:
                    entry["name"] = name
                flat.append(entry)
                continue
            name = m.get("name") or ""
            flat.append({"role": "user",
                         "content": f"{TOOL_RESULT_MARKER}{(' (%s)' % name) if name else ''}]\n"
                                    + _shrink_tool_result(text)})
            continue
        if role == "assistant" and m.get("tool_calls"):
            calls = []
            for tc in m["tool_calls"]:
                tc = tc if isinstance(tc, dict) else {}
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, (dict, list)):
                    args = json.dumps(args, ensure_ascii=False)
                elif not isinstance(args, str):
                    args = "" if args is None else str(args)
                calls.append({"id": tc.get("id") or "",
                              "type": tc.get("type") or "function",
                              "function": {"name": fn.get("name") or "",
                                           "arguments": args}})
            if structured:
                # 原样透传：上游自己就是 ### 形态（chatcmpl-… + tool_calls delta），
                # 2026-10-23 实测 CN/INTL 六个模型全部接受并按工具结果接续。
                entry = {"role": "assistant", "content": text,
                         "tool_calls": calls}
            else:
                body = text
                if calls:
                    body = (text + "\n" if text else "") + TOOL_CALLS_MARKER + " " + \
                        json.dumps(calls, ensure_ascii=False)
                entry = {"role": "assistant", "content": body}
            rc = m.get("reasoning_content") or m.get("reasoning")
            if rc:
                entry["reasoning_content"] = rc
            flat.append(entry)
            continue
        if role in ("user", "assistant"):
            if role == "assistant":
                # 历史里残留的协议回声只教模型照抄，清掉（见 tool_echo.strip_history_echo）
                text = strip_history_echo(text)
            entry = {"role": role, "content": text}
            rc = m.get("reasoning_content") or m.get("reasoning")
            if role == "assistant" and rc:
                entry["reasoning_content"] = rc
            flat.append(entry)
    return system_text, flat, images


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 工具协议：客户端 tools 透传 / 文本降级 / 回声回读
# ---------------------------------------------------------------------------
# 回读侧（marker 识别、跨帧缓冲、历史回声摘除）已抽到 providers/tool_echo.py ——
# Qoder 与 CodeArts 两个通道共用；这里只保留 Qoder 特有的两件事：
#   1) 客户端 tools / tool_choice 怎么发（上游自带的 CLI 工具集绝不能泄漏出去）；
#   2) 历史工具调用走结构化还是文本降级（_structured_tool_history）。

# 上游 tools[].type 只认 function；namespace / web_search 等原样透传会让上游
# 在反序列化阶段 400（unknown variant）。丢弃并告警（参考实现 issue #63）。
_UNSUPPORTED_TOOL_WARNED: set = set()


def _log_unsupported_tool(kind) -> None:
    kind = str(kind or "")
    if kind in _UNSUPPORTED_TOOL_WARNED:
        return
    _UNSUPPORTED_TOOL_WARNED.add(kind)
    logger.warning("客户端声明了上游无法表达的工具类型 %r，已丢弃（function 不受影响）",
                   kind)

def _log_unsupported_tool(kind) -> None:
    kind = str(kind or "")
    if kind in _UNSUPPORTED_TOOL_WARNED:
        return
    _UNSUPPORTED_TOOL_WARNED.add(kind)
    logger.warning("客户端声明了上游无法表达的工具类型 %r，已丢弃（function 不受影响）",
                   kind)


def client_tools(body: dict) -> list:
    """客户端声明的工具，归一化成上游认的 ``{type:function, function:{...}}``。

    ★ 客户端**没给** tools 时返回空表，绝不回落到模板自带的 Qoder CLI 工具集
      （Bash / Edit / TodoWrite …）。2026-10-23 实测：把 CLI 工具集发出去，模型
      就会按它调用，而客户端根本没有这些工具 —— 调用只能被当成正文显示出来，
      用户看到的就是「模型输出了 [TOOL CALLS] 但没法使用 bash」。
      参考实现同口径（"客户端给了就用客户端的，否则置空"）。
    """
    out = []
    for t in body.get("tools") or []:
        if not isinstance(t, dict):
            continue
        kind = t.get("type")
        if kind not in (None, "function"):
            _log_unsupported_tool(kind)
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else None
        if fn is None:
            # 顶层平铺形态（Responses / ### 风格）→ 包成 function 对象
            fn = {"name": t.get("name") or "",
                  "description": t.get("description") or "",
                  "parameters": t.get("parameters") or {}}
        if not str(fn.get("name") or "").strip():
            continue
        out.append({"type": "function", "function": fn})
    return out


def _tool_ids_ok(messages) -> bool:
    """结构化工具历史的前提：tool 消息有 tool_call_id，assistant 调用有 id。"""
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "tool":
            if not str(m.get("tool_call_id") or "").strip():
                return False
        elif m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"] or []:
                if not isinstance(tc, dict) or not str(tc.get("id") or "").strip():
                    return False
    return True


def _native_tool_shapes(messages) -> bool:
    """会话里是否存在「只有结构化形态才成立」的消息（原生调用 / 工具结果）。"""
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "tool" and str(m.get("tool_call_id") or "").strip():
            return True
        if m.get("role") == "assistant" and m.get("tool_calls") and _tool_ids_ok([m]):
            return True
    return False


def _structured_tool_history(cfg: dict, messages, forced: bool | None = None) -> bool:
    """本次请求的历史工具调用是否走**结构化**形态（原生 tool_calls / tool 角色）。

    :param forced: 显式指定（内部降级重试用），None 时按配置/环境/自动判定
    :return: True=结构化；False=文本降级（marker + JSON 数组）

    三态覆盖入口（优先级从高到低）：
      ``providers.<段>.structuredToolHistory`` = true/false
      环境变量 ``OPENAI_QODER_STRUCTURED_TOOLS`` = on/off（auto 或缺省 = 自动）
    自动判定只剩一道闸：``_tool_ids_ok``（id 不齐备就退回文本化，宁可不发畸形请求）。
    """
    if forced is not None:
        return bool(forced)
    raw = (cfg or {}).get("structuredToolHistory")
    if raw is None:
        raw = os.environ.get("OPENAI_QODER_STRUCTURED_TOOLS")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low in ("off", "0", "false", "no", "disable", "disabled"):
            return False
        if low in ("on", "1", "true", "yes", "enable", "enabled", "force"):
            return True
    return _tool_ids_ok(messages)



def _requested_effort(body: dict) -> str:
    for key in ("reasoning_effort",):
        v = body.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    return ""


class QoderProvider(Provider):
    """Qoder 通道公共实现；子类只需给出 name / realm / PREFIX。"""

    name = "qoder"
    realm = "cn"
    PREFIX = "qd-"

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.accounts = list(self.cfg.get("accounts") or [])
        self.domain = self.cfg.get("domain") or qc.realm_cfg(self.realm)["domain"]
        self.timeout = float(self.cfg.get("timeout", 600.0))
        self.switch_every = max(1, int(self.cfg.get("switchEvery", 5)))
        self.default_model = self.cfg.get("defaultModel") or "auto"
        self._req_count = 0
        self._client: httpx.AsyncClient | None = None
        self._health = AccountHealth()
        self._catalog: list = qc.static_catalog(self.realm)
        self._catalog_at: float = 0.0
        self._models_lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()

    # ---------- 账号轮换 ----------
    def _load_accounts(self) -> list:
        """从 config.json 实时读账号（启用状态变更即时生效，无需重启）。"""
        try:
            with open(qc_config_path(), encoding="utf-8") as f:
                seg = (json.load(f).get("providers") or {}).get(self.cfg_key()) or {}
            fresh = seg.get("accounts") or []
            if fresh:
                # 内存里的新 token（自动续期）比磁盘新时优先 —— 与 workbuddy 同口径；
                # 判定复用 cred_store.credentials_newer（refreshedAt → expiresAt），
                # 与落盘/锁内重读是同一把尺子，避免「各判一套」。
                for old in self.accounts:
                    match = next((n for n in fresh
                                  if n.get("uid") and n.get("uid") == old.get("uid")), None)
                    if (match and old.get("accessToken")
                            and old.get("accessToken") != match.get("accessToken")
                            and cred_store.credentials_newer(old, match)):
                        match["accessToken"] = old["accessToken"]
                        match["refreshToken"] = old.get("refreshToken") or match.get("refreshToken")
                        match["expiresAt"] = old.get("expiresAt") or match.get("expiresAt")
                        match["refreshedAt"] = old.get("refreshedAt") or match.get("refreshedAt")
                self.accounts = fresh
        except Exception:  # noqa: BLE001
            pass
        for a in self.accounts:
            self._health.observe_token(a.get("uid"), a.get("accessToken"))
        return self.accounts

    def cfg_key(self) -> str:
        return self.cfg.get("_cfg_key") or self.name.replace("-", "_")

    def _pick_account(self) -> dict:
        accs = [a for a in self._load_accounts()
                if a.get("enabled", True) is not False
                and str(a.get("realm") or self.realm) == self.realm
                and not self._health.is_invalid(a.get("uid"))]
        if not accs:
            return {}
        self._req_count += 1
        idx = (self._req_count - 1) // self.switch_every % len(accs)
        return accs[idx]

    def _account_of(self, model: str) -> dict:
        """本请求归属账号：优先带该模型 realm 的账号（双区可能同进程并存）。"""
        _ = model
        return self._pick_account()

    def get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=30.0),
                transport=httpx.AsyncHTTPTransport(retries=2))
        return self._client

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------- 模型目录 ----------
    async def sync_models(self, force: bool = False) -> bool:
        """拉上游 COSY 模型目录（启动/每日一次），失败保留静态目录。"""
        async with self._models_lock:
            if not force and self._catalog_at and time.time() - self._catalog_at < 21600:
                return True
            acc = self._pick_account()
            if not acc:
                logger.info("%s 模型目录同步跳过：暂无可用账号", self.name)
                return False
            try:
                catalog = await asyncio.to_thread(qc.fetch_catalog, acc, force)
            except Exception as e:  # noqa: BLE001
                logger.warning("%s 模型目录同步失败（保留现有目录）: %s", self.name, e)
                return False
            if catalog:
                self._catalog = catalog
                self._catalog_at = time.time()
                avail = self._enabled_models()
                logger.info("%s 模型目录已更新: 上游 %d 个，对外暴露 %d 个%s",
                            self.name, len(catalog), len(avail),
                            "" if len(avail) == len(catalog)
                            else "（已剔除上游标记 enable=false 的条目）")
                return True
            return False

    def _model_label(self, model_key: str) -> str:
        """内部 key → 给用户看的名字。

        ★★ 2026-10-24：报错文案过去直接打上游内部 key，用户看到的是
          「模型 dfmodel 当前无可用资源」——`dfmodel` 既不是界面上的模型名，
          也不是任何地方能搜到的东西，纯属把上游实现细节漏给了用户。
          目录里本来就有 `display_name`（dfmodel → DeepSeek-Flash），用它。
          目录里查不到时退回 key 本身（保持可排障）。
        """
        key = str(model_key or "").strip()
        if not key:
            return "?"
        for m in (self._catalog or []):
            if isinstance(m, dict) and str(m.get("key") or "") == key:
                name = str(m.get("display_name") or "").strip()
                if name:
                    return name
        meta = qc.catalog_meta(key, self.realm)
        name = str((meta or {}).get("display_name") or "").strip()
        return name or key

    def _enabled_models(self) -> list:
        """可用模型目录条目（默认剔除上游标了 enable=false 的）。

        ★ 上游每个条目带 `enable` 字段（2026-10-22 实测）：国际版 17 个里只有
          Qwen3.8-Max / Qwen3.8-Flash 是 enable=true，其余 15 个是 false。
          把它们一并列出来，用户选中后必然失败（上游按区域/套餐未开放）——
          模型列表应当只回答「这个账号现在能用什么」。
          想看到全量目录可把 config 的 providers.<段>.includeDisabledModels 设为 true。
        """
        include_disabled = bool(self.cfg.get("includeDisabledModels"))
        out = []
        for m in self._catalog or []:
            if not isinstance(m, dict):
                continue
            if not str(m.get("key") or ""):
                continue
            if not include_disabled and m.get("enable") is False:
                continue
            out.append(m)
        return out

    def _model_ids(self) -> list:
        """对外模型 id 用**上游显示名**（不再是内部 key）。

        ★ 2026-10-22 修正：原先直接暴露上游内部 key（qmodel_38max / qfmodel /
          dmodel…），界面上是一串看不懂的「怪称」。上游目录本来就带
          display_name（Qwen3.8-Max / Qwen3.8-Flash / DeepSeek-V4-Pro…），
          直接用它；请求侧仍同时接受 key 与显示名（见 normalize_model）。
        """
        ids = []
        for m in self._enabled_models():
            name = str(m.get("display_name") or "").strip() or str(m.get("key"))
            mid = f"{self.PREFIX}{name}"
            if mid not in ids:
                ids.append(mid)
        return ids

    def list_models(self) -> list[dict]:
        return [{"id": m, "object": "model", "created": 0,
                 "owned_by": "qoder" if self.realm == "cn" else "qoder-intl"}
                for m in self._model_ids()]

    def rate_table(self) -> dict:
        """{模型名/键: 积分倍率} —— 来自上游目录的 `price_factor`。

        2026-10-22 实测（GET /algo/api/v2/model/list?Encode=1 的 chat 场景）：
        每个模型带 `price_factor`，就是倍率页要显示的数（Auto 0.5、Qwen3.8-Max 0.2、
        Kimi-K3 1.4 …）。同时登记「带前缀 / 不带前缀 / 内部 key / 展示名」四种写法：
        前端可能拿 routeModelId 或裸名来查（见 httpBackend.listModels 的三级回退）。
        """
        out: dict[str, float] = {}
        for m in self._enabled_models():
            pf = m.get("price_factor")
            if pf is None:
                continue
            try:
                rate = float(pf)
            except (TypeError, ValueError):
                continue
            key = str(m.get("key") or "")
            name = str(m.get("display_name") or "").strip() or key
            for k in (name, key, f"{self.PREFIX}{name}", f"{self.PREFIX}{key}"):
                if k:
                    out[k] = rate
        return out

    def normalize_model(self, model: str) -> str:
        raw = str(model or "").strip()
        if raw.lower().startswith(self.PREFIX.lower()):
            raw = raw[len(self.PREFIX):]
        return qc.resolve_model_key(raw, self._catalog, self.realm) or self.default_model

    # ---------- token 刷新 ----------
    async def _ensure_token(self, account: dict, force: bool = False) -> bool:
        """access token 临近过期时主动续期（失败不抛，交给请求路径判定）。

        :param force: 上游明确回 401/403 时强制续期一次（无视 expiresAt）

        ★ 2026-10-23 加了两层保护（与 CodeArts 同款，见 cred_store 文档）：
          * **跨进程互斥 + 锁内重读磁盘**：续期会换发新的 refresh token，
            两个进程同时用同一个去换 = 只能成功一个，另一个把账号玩坏；
          * **成功后立刻落盘**：新凭据只留在内存里的话，一旦重启（本项目
            重启很频繁）磁盘上就只剩已作废的旧 token。
        """
        exp = int(account.get("expiresAt") or 0)
        if not force and exp and exp - 300 > time.time():
            return True
        async with self._refresh_lock:
            exp = int(account.get("expiresAt") or 0)
            if not force and exp and exp - 300 > time.time():
                return True
            return await asyncio.to_thread(self._refresh_locked, account)

    def _refresh_locked(self, account: dict) -> bool:
        """真正的续期动作：锁内重读磁盘 → 续期 → 回写磁盘。

        ⚠️ 「磁盘更新」拿本 provider 自己那份账号（``self.accounts``）来比，
        不拿调用方传进来的 dict —— 强制续期路径若传副本，有效期比对会失真
        （2026-10-23 CodeArts 侧实测踩到，见其 _refresh_locked 注释）。
        """
        uid = str(account.get("uid") or "")
        seg = self.cfg_key()
        canonical = next((a for a in self.accounts
                          if str(a.get("uid") or "") == uid), account)
        with cred_store.account_lock(seg, uid):
            disk = cred_store.read_account(seg, uid)
            if disk and cred_store.credentials_newer(disk, canonical):
                account.update(disk)
                self._health.observe_token(uid, account.get("accessToken"))
                qc.SESSIONS.invalidate(uid)
                logger.info("%s 账号 %s 采用磁盘上更新的凭据（另一进程刚续期）",
                            self.name, uid[:8])
                return True
            try:
                token, refresh, exp = qc.refresh_access_token(account)
            except Exception as e:  # noqa: BLE001
                logger.warning("%s 账号 %s token 续期失败: %s",
                               self.name, uid[:8], e)
                return False
            if not token:
                return False
            account["accessToken"] = token
            if refresh:
                account["refreshToken"] = refresh
            # 过期时刻以上游响应为准（dt- 是 30 天），拿不到才退 6 小时
            account["expiresAt"] = int(exp or 0) or int(time.time()) + 6 * 3600
            account["refreshedAt"] = int(time.time())
            self._health.observe_token(account.get("uid"), token)
            qc.SESSIONS.invalidate(account.get("uid"))
            logger.info("%s 账号 %s accessToken 已自动续期",
                        self.name, uid[:8])
            cred_store.persist_account(seg, uid, account)
            return True

    # ---------- 请求体构造 ----------
    def build_body(self, body: dict, account: dict, model_key: str,
                   structured: bool | None = None) -> dict:
        """按官方 baseprompt 骨架构造 agent_chat_generation 请求体。

        :param structured: 历史工具调用的形态（None = 自动，见 _structured_tool_history）
        """
        req = copy.deepcopy(BASEPROMPT) if BASEPROMPT else {
            "stream": True, "chat_task": "FREE_INPUT", "version": 3,
            "session_type": "qodercli", "agent_id": "agent_common",
            "task_id": "common", "source": 1,
            "chat_context": {"text": {"type": "text", "text": ""}, "extra": {}},
            "parameters": {"max_tokens": 32768},
            "business": {"product": "cli", "version": "0.1.43", "type": "agent",
                         "stage": "start"},
        }
        system_text, flat, images = flatten_messages(
            body.get("messages"),
            structured=_structured_tool_history(self.cfg, body.get("messages"), structured))
        tmpl_system = [m for m in (req.get("messages") or [])
                       if isinstance(m, dict) and m.get("role") == "system"]
        if system_text is not None:
            req["messages"] = [{"role": "system", "content": system_text}]
        else:
            req["messages"] = tmpl_system
        req["messages"].extend(flat)

        # tools / tool_choice：**只发客户端声明的**
        # ★ 绝不回落到模板自带的 Qoder CLI 工具集（Bash/Edit/TodoWrite…）：
        #   模型会照着它调用，而客户端没有这些工具 —— 调用无法执行，用户看到的
        #   就是「模型输出了一段工具调用文本，但什么也没发生」（2026-10-23 实测）。
        tools = client_tools(body)
        choice, drop_tools = client_tool_choice(body)
        if drop_tools:
            tools, choice = [], None
        req["tools"] = tools
        if choice is not None and tools:
            req["tool_choice"] = choice
        else:
            req.pop("tool_choice", None)

        prompt = ""
        for m in reversed(flat):
            if m.get("role") == "user" and m.get("content"):
                prompt = str(m["content"])
                break

        nid = str(uuid.uuid4())
        req["request_id"] = nid
        req["chat_record_id"] = nid
        req["request_set_id"] = str(uuid.uuid4())
        req["session_id"] = str(uuid.uuid4())
        req["stream"] = True
        req["agent_id"] = "agent_common"
        req["aliyun_user_type"] = account.get("userType") or qc.DEFAULT_USER_TYPE

        meta = qc.catalog_meta(model_key, self.realm)
        mc = req.get("model_config") or {}
        mc.update({
            "key": model_key,
            "display_name": meta.get("display_name") or model_key,
            "model": "",
            "format": "###",
            "is_vl": bool(meta.get("is_vl")),
            "is_reasoning": bool(meta.get("is_reasoning")),
            "api_key": "",
            "url": "",
            "source": "system",
            "max_input_tokens": meta.get("max_input_tokens") or 180000,
        })
        req["model_config"] = mc

        cc = req.get("chat_context") or {}
        txt = cc.get("text") or {"type": "text"}
        txt["type"] = "text"
        txt["text"] = prompt
        cc["text"] = txt
        extra = cc.get("extra") or {}
        oc = extra.get("originalContent") or {"type": "text"}
        oc["type"] = "text"
        oc["text"] = prompt
        extra["originalContent"] = oc
        extra["modelConfig"] = dict(mc)
        cc["extra"] = extra
        # 顶层 imageUrls 是纯冗余（上游只读 messages 里的结构化 image parts），
        # 填充它会让出站体积翻倍并撞 30MB 上限 —— 保持 None，与官方客户端一致
        cc.setdefault("imageUrls", None)
        req["chat_context"] = cc
        req.setdefault("image_urls", None)
        _ = images

        params = req.get("parameters") or {}
        max_tokens = body.get("max_tokens") or body.get("max_completion_tokens")
        if max_tokens:
            try:
                params["max_tokens"] = int(max_tokens)
            except (TypeError, ValueError):
                pass
        effort = _requested_effort(body)
        if effort:
            params["reasoning_effort"] = effort
        req["parameters"] = params

        biz = req.get("business") or {}
        biz.update({
            "product": "cli",
            "type": "agent",
            "id": str(uuid.uuid4()),
            "name": (prompt[:48] if prompt else ""),
            "begin_at": int(time.time() * 1000),
            "stage": "start",
        })
        req["business"] = biz
        return req

    # ---------- 上游请求 ----------
    async def _open_stream(self, body: dict, account: dict, model_key: str):
        """发上游请求，返回上游 response（200）。候选网关逐个降级 + 排队重试。

        三类「非 200」的处理口径（都是实测踩出来的）：
          * **401/403 且是鉴权拒绝** → 强制续期一次重试；仍失败才 mark_invalid；
          * **403 且是排队**（体里有 isQueued/10605）→ 等 retryAfterSeconds 后
            **重试同一主机**，并按「繁忙」语义向上报（绝不判成断连）；
          * 其它 4xx → 直接失败（换网关也没用）；5xx → 换下一个候选网关。
          * **4xx 且本次发的是结构化工具历史** → 先降级成文本化形态重试一次
            （结构化是 2026-10-23 起的默认形态；万一某个模型/上游版本不认，
            不能让整条通道挂掉，见 _structured_tool_history）。
        """
        native_shapes = _native_tool_shapes(body.get("messages"))
        structured = _structured_tool_history(self.cfg, body.get("messages"))
        fallback_done = False
        req_obj = self.build_body(body, account, model_key, structured=structured)
        # ★ 请求体必须是**原始 JSON**（2026-10-22 实测修正）。
        #
        # 参考项目（Qoder2API-Hub）对请求体做了「自定义 Base64 变体」编码
        # （qoder_encode），本模块最初照搬了它 —— 但实测该编码**现在会被上游拒**：
        #   qoder_encode(raw)  → 500 event:error（Jackson 解析失败，服务端解出来不是 JSON）
        #   标准 base64(raw)   → 同样 500
        #   原样 JSON          → 200 + 正常 SSE 流
        # 三种写法在同一个请求、同一份 COSY 签名下对比，结论明确。
        # 故此处直接发原始 JSON；qoder_encode/qoder_decode 仍保留（有测试覆盖），
        # 仅作为历史兼容与排障工具，不再参与请求路径。
        payload = json.dumps(req_obj, ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_UPSTREAM_BODY_BYTES:
            raise RuntimeError(
                f"出站请求体过大（{len(payload)} 字节 > {MAX_UPSTREAM_BODY_BYTES}），"
                f"请缩短会话或减少图片")
        await self._ensure_token(account)
        sess = qc.SESSIONS.get(account)
        client = self.get_client()
        body_text = payload.decode("utf-8")
        last_err = None
        queued_seen = False
        gateway_405: list[str] = []      # 被 WAF 拦过的主机（见下方 405 分支）
        # 排队预算按**请求**计（跨候选网关共用），不是按主机 ——
        # 否则 N 个候选域名各等满一轮，单请求会被拖到 N 倍时长。
        queue_start = time.time()
        queue_round = 0
        # ★ 2026-10-24：WAF 405 的退避预算（同样按**请求**计，不按主机）。
        waf_start = time.time()
        waf_round = 0

        for host in qc.gateway_candidates(self.realm):
            url = host + qc.CHAT_PATH
            # 内层循环：只用于「排队 → 等一会 → 重试同一主机」
            while True:
                headers = sess.headers(body_text, url, model_key=model_key, sse=True)
                try:
                    resp = await client.send(
                        client.build_request("POST", url, content=payload,
                                             headers=headers), stream=True)
                except Exception as e:  # noqa: BLE001
                    last_err = describe_net_exc(e, f"{self.name} 连接 {host}")
                    break
                if resp.status_code == 200:
                    self._health.mark_ok(account.get("uid"))
                    return resp

                # 先读 body 再 aclose：httpx 的流一旦关闭，aread() 会抛
                # "Attempted to read or stream content, but the stream has been
                # closed."，上游的拒绝原因就永远看不到了（2026-10 冒烟实测）。
                status = resp.status_code
                try:
                    detail = (await resp.aread()).decode("utf-8", "replace")[:300]
                except Exception as e:  # noqa: BLE001
                    detail = f"(读取错误响应失败: {type(e).__name__})"
                await resp.aclose()

                # —— 资源受限（RESOURCE_LIMIT）：**立即失败**，绝不等待 ——
                # ★ 2026-10-24：上游用 {isQueued:true, queueCount:0,
                #   serviceAvailable:false, subCode:"RESOURCE_LIMIT",
                #   retryAfterSeconds:30} 表达「该模型此刻没有服务」。
                #   它长得像排队，但 queueCount=0 说明没人排队、等也没用。
                #   旧版会白等 30s×3 轮=90 秒再报「繁忙」，既浪费时间又误导用户。
                #   —— 必须排在排队判定**之前**，否则永远走不到这里。
                if qc.is_resource_limit(detail):
                    msg = (f"{self.name} 模型 {self._model_label(model_key)} 当前无可用资源"
                           f"（上游 RESOURCE_LIMIT）：请稍后重试或改用其它模型")
                    # 记冷却：接下来 QD_RESOURCE_LIMIT_COOLDOWN 秒内同模型直接本地失败
                    qc.mark_resource_limited(self.realm, model_key, msg)
                    raise qc.QoderResourceLimited(msg)

                # —— 排队（403 + isQueued/10605）——
                wait = qc.queue_wait_seconds(detail)
                if wait > 0:
                    queued_seen = True
                    if (queue_round < qc.QUEUE_MAX_ROUNDS
                            and wait <= qc.QUEUE_WAIT_MAX_SECS
                            and (time.time() - queue_start) + wait
                            <= qc.QUEUE_WAIT_BUDGET_SECS):
                        queue_round += 1
                        logger.info("%s 上游排队中，%d 秒后重试同一主机（第 %d/%d 轮）",
                                    self.name, wait, queue_round, qc.QUEUE_MAX_ROUNDS)
                        # 抖动 0~2s：多客户端同时被唤醒会一起打同一个队列
                        await asyncio.sleep(
                            wait + 1 + random.uniform(0, qc.QUEUE_WAIT_JITTER_SECS))
                        last_err = f"HTTP 403 排队: {detail}"
                        continue          # 重试同一主机
                    last_err = f"HTTP 403 排队（预算用尽）: {detail}"
                    break

                # —— 鉴权被拒：续期一次再试同一主机 ——
                if status in (401, 403):
                    denied = detail[:200]
                    retried_ok = False
                    if await self._ensure_token(account, force=True):
                        sess = qc.SESSIONS.get(account)
                        headers = sess.headers(body_text, url, model_key=model_key,
                                               sse=True)
                        try:
                            resp2 = await client.send(
                                client.build_request("POST", url, content=payload,
                                                     headers=headers), stream=True)
                        except Exception as e:  # noqa: BLE001
                            last_err = describe_net_exc(e, f"{self.name} 重试 {host}")
                            break
                        if resp2.status_code == 200:
                            self._health.mark_ok(account.get("uid"))
                            return resp2
                        try:
                            denied = (await resp2.aread()).decode(
                                "utf-8", "replace")[:200]
                        except Exception:  # noqa: BLE001
                            pass
                        await resp2.aclose()
                        retried_ok = True
                    self._health.mark_invalid(
                        account.get("uid"), reason=f"chat 被拒 HTTP {status}")
                    raise RuntimeError(
                        f"{self.name} 上游拒绝鉴权 HTTP {status}"
                        f"（{'含' if retried_ok else '无'}续期重试）: {denied}")

                # —— 405：上游**边缘**拒绝（阿里云 WAF/SLB 静态页），与工具协议无关 ——
                # ★ 2026-10-24 归类修正。405 返回的是阿里云 WAF 静态页
                #   （`<title>405</title>` + `data-spm="a3c0e"`），0.5s 内就拒，
                #   根本没进模型。旧版把它当成「结构化工具历史被拒」去降级重试，
                #   方向完全错（降级后的请求体与 405 无因果关系，日志还会把排查
                #   引向工具协议）。
                #
                # ★★ 2026-10-24 三次修正（本次）—— **改成对同一主机退避重试**。
                #   上一版是「立刻换下一个候选主机」，实测这条路径**毫无用处**：
                #     · 三个候选主机**在同一时刻一起被拦**（同一套边缘规则集）；
                #     · 全历史统计：api2/api1 被尝试 26 次、**成功 0 次**，全 405；
                #       api3 成功 413 次 —— 说明拦不拦跟主机无关。
                #   而 405 是**时间性**的：实测同一台机器上 21:10:59→21:14:25
                #   三次尝试全被拦，过了这段就恢复（会话记录里 turn 25-29 连
                #   5 次 405，随后正常）。所以正确的动作是「等一会再打同一台」，
                #   而不是「换个域名再打」——后者永远等不到放行。
                #
                #   证据还显示：405 与同期的 RESOURCE_LIMIT（上游过载）总是
                #   前后脚出现（会话 turn 16-23 全是 RESOURCE_LIMIT，24-29 变
                #   405），且**跨模型**（DeepSeek-Flash 和 Qwen3.8-Flash 都被拦）
                #   → 归因为「上游过载时边缘甩的一张拦截页」，不是请求内容命中。
                if status == 405:
                    if host not in gateway_405:      # 去重：退避重试会重复记同一主机
                        gateway_405.append(host)
                    last_err = f"HTTP 405: {detail}"
                    # 只有确认是 WAF 拦截页才值得等 —— 其它 405（例如端点/方法
                    # 真变了）等也没用，直接走换主机/收尾。
                    waf_page = ("data-spm" in detail) or ("<title>405" in detail.replace(" ", ""))
                    if (waf_page
                            and waf_round < self.WAF_RETRY_ROUNDS
                            and (time.time() - waf_start) + self.WAF_RETRY_GAP
                            <= self.WAF_WAIT_BUDGET_SECS):
                        waf_round += 1
                        nap = self.WAF_RETRY_GAP + random.uniform(0, self.WAF_RETRY_JITTER)
                        logger.warning("%s 主机 %s 返回 405（边缘 WAF 拦截），"
                                       "%d 秒后重试**同一主机**（第 %d/%d 轮）",
                                       self.name, host, int(nap),
                                       waf_round, self.WAF_RETRY_ROUNDS)
                        await asyncio.sleep(nap)
                        continue          # 重试同一主机（内层 while）
                    logger.warning("%s 主机 %s 返回 405（WAF 拦截），退避重试已用尽"
                                   "（已试 %d 轮），改试下一个候选",
                                   self.name, host, waf_round)
                    break              # 跳出排队内层循环，进入下一个候选主机

                # —— 结构化工具历史被拒：降级成文本化形态再试同一主机一次 ——
                # 只在「会话里确实有原生调用/工具结果」时才值得降级：普通对话
                # 的 400（参数/额度问题）重试同一份请求体毫无意义。
                if (status < 500 and structured and native_shapes
                        and not fallback_done):
                    fallback_done = True
                    structured = False
                    req_obj = self.build_body(body, account, model_key,
                                              structured=False)
                    payload = json.dumps(req_obj, ensure_ascii=False).encode("utf-8")
                    body_text = payload.decode("utf-8")
                    logger.warning("%s 结构化工具历史被上游拒绝（HTTP %s），"
                                   "本次降级为文本化重试: %s",
                                   self.name, status, detail[:160])
                    continue

                last_err = f"HTTP {status}: {detail}"
                if status < 500:
                    break              # 4xx 换网关也没用（参数/额度问题）
                break                  # 5xx：换下一个候选网关

        if queued_seen and queue_round >= qc.QUEUE_MAX_ROUNDS:
            raise qc.QoderQueued(
                f"{self.name} 上游持续排队（已重试 {queue_round} 轮）: {last_err}")
        if queued_seen and queue_round > 0:
            raise qc.QoderQueued(
                f"{self.name} 上游排队未放行: {last_err}")
        # 全部候选主机都被 WAF 拦（退避重试也用尽了）
        if gateway_405 and len(gateway_405) == len(qc.gateway_candidates(self.realm)):
            raise RuntimeError(
                f"{self.name} 上游边缘拦截 HTTP 405（阿里云 WAF，"
                f"{len(gateway_405)} 个候选主机全部被拦）：已对主主机退避重试 "
                f"{waf_round} 轮仍未放行。**这不是模型错误、也与本次请求内容无关**"
                f"（实测同一时刻所有候选主机一起被拦，且总伴随上游过载时段；"
                f"封锁一般持续数分钟，过去后会自动恢复）：{str(last_err or '')[:160]}")
        raise RuntimeError(f"{self.name} 上游请求失败: {last_err}")

    # ★★ 2026-10-24（master 反馈「应该是让用户等待模型请求，怎么就直接断连了」）：
    #
    #   把「资源受限」的处理从「立即抛给用户」改为 **先自己扛**：
    #     ① 同一模型短等后重试（RESOURCE_LIMIT 是瞬时的，实测 24 次轮询全 200）
    #     ② 仍不行 → 在同通道目录里自动换一个可用模型
    #     ③ 全试完才抛 QoderResourceLimited，文案里带上「已尝试 N 个模型」
    #
    #   ★ 只在「尚未向用户输出任何内容」时才允许换模型 —— 一旦吐过字就不能换
    #     （换了就是另一个模型接着说话，用户看到的是前后不一致的回复）。
    #
    #   ⚠️ 上面那句「RESOURCE_LIMIT 是瞬时的」**当时是错的**（2026-10-25 更正）：
    #      实测上游过载可以持续**数分钟**，短等几秒根本等不到。等待时长已按
    #      master 实测重设，见下面这段。
    # ★★ 2026-10-25（master 实测反馈）：**把「等资源」的时间拉长**。
    #
    #   master 原话：「我实际测试，发现等待 2 到 3 分钟，上游就会有资源了」。
    #   旧默认是 3 秒 × 2 轮（≈6 秒）—— 对真实的上游过载而言太短，
    #   等于「问一句就走」，用户看到的就是「没资源，请稍后重试」。
    #
    #   ★ 上界不是随手定的，而是由**客户端**决定的：
    #     DSH 的 llm 插件有 `streamIdleTimeoutMs`（默认 **300 秒**，本机 profile
    #     未覆盖），含义是「多久没收到新数据就断开」。而我们等待期间客户端
    #     **收不到任何字节** —— 所以「总等待 + 模型出首字节」必须明显小于 300 秒。
    #     本实现把**总等待上界**压在 170 秒（≈2.8 分钟），留 ~130 秒给出首字节：
    #       · 覆盖 master 实测的 2~3 分钟恢复窗口；
    #       · 又不会被客户端掐断（真被掐断，用户看到的是「流空闲超时」，
    #         比「上游没资源」更难排查 —— 那是把问题从上游挪到了链路上）。
    #
    #   调参（环境变量，改了要重启网关）：
    #     QD_RL_RETRY_ROUNDS      退避轮数（默认 4）
    #     QD_RL_RETRY_GAP         每轮间隔秒（默认 40 → 4×40=160 秒）
    #     QD_RL_WAIT_BUDGET_SECS  总等待上界秒（默认 170，**硬守卫**）
    #
    #   ⚠️ 若要再拉长：先确认客户端的 `streamIdleTimeoutMs`（改 profile 才能提），
    #      否则超过它只会把「上游没资源」变成「客户端流空闲超时」，得不偿失。
    RL_RETRY_ROUNDS = int(os.environ.get("QD_RL_RETRY_ROUNDS") or 4)
    RL_RETRY_GAP = float(os.environ.get("QD_RL_RETRY_GAP") or 40.0)
    RL_WAIT_BUDGET_SECS = float(os.environ.get("QD_RL_WAIT_BUDGET_SECS") or 170.0)

    # ★★ 2026-10-24：**405（边缘 WAF 拦截）的退避重试**参数。
    #
    #   为什么是「等」而不是「换主机」：实测三个候选主机在同一时刻一起被拦
    #   （同一套边缘规则集），全历史统计里 api2/api1 被尝试 26 次、成功 0 次。
    #   而拦截是**时间性**的（21:10:59→21:14:25 连拦三次，之后恢复），
    #   所以对**同一主机**等一会再打才有意义。
    #
    #   预算按**请求**计（不按主机）：默认最多退避 2 轮、每轮约 30 秒（含抖动），
    #   总等待不超过 120 秒。调参用环境变量：
    #     QD_WAF_RETRY_ROUNDS  / QD_WAF_RETRY_GAP / QD_WAF_WAIT_BUDGET_SECS
    #     QD_WAF_RETRY_JITTER（抖动上限，避免多客户端同时醒来再撞同一堵墙）
    WAF_RETRY_ROUNDS = int(os.environ.get("QD_WAF_RETRY_ROUNDS") or 2)
    WAF_RETRY_GAP = float(os.environ.get("QD_WAF_RETRY_GAP") or 30.0)
    WAF_WAIT_BUDGET_SECS = float(os.environ.get("QD_WAF_WAIT_BUDGET_SECS") or 120.0)
    WAF_RETRY_JITTER = float(os.environ.get("QD_WAF_RETRY_JITTER") or 4.0)

    # ★★ 2026-10-24 master 决定：**关闭模型自动降级**（默认关闭）。
    #
    #   背景（实锤）：Qoder 账号积分一天被清零（280/300 + 100/100 全用尽），
    #   而日志里 Resource_Limit 全部集中在 qfmodel / dfmodel / mmodel / gfmodel ——
    #   正是「用户选了便宜模型 → 上游说没资源 → 插件自动改用目录里
    #   下一个模型（往往更贵）→ 照常扣积分」这条路径。
    #   用户根本没要求换模型，却被扣了高价模型的钱。
    #
    #   取舍（master 原话）：「当前模型不行就不行」。
    #   → 默认 **不换模型**：同模型短等重试（RL_RETRY_ROUNDS）后仍不行，
    #     就如实抛 QoderResourceLimited，让用户自己决定要不要换。
    #
    #   开关优先级：config.json 的 providers.<通道>.modelFallback
    #              > 环境变量 QD_RL_FALLBACK > 默认 False
    #   （配置优先，因为 Windows 启动脚本里塞环境变量不可靠）
    RL_FALLBACK_DEFAULT = False

    def _model_fallback_enabled(self) -> bool:
        """本通道是否允许「模型被拒后自动换下一个模型」。

        优先级：config.json 的 `modelFallback` > 环境变量 QD_RL_FALLBACK > 默认关。

        读的是**实例上的真实配置**（含热更新后的值），不是类属性 ——
        这样改 config.json 后无需重启即可生效（与本文件其它开关同口径）。
        """
        raw = (self.cfg or {}).get("modelFallback")
        if raw is not None:
            return _truthy(raw, self.RL_FALLBACK_DEFAULT)
        env = os.environ.get("QD_RL_FALLBACK")
        if env is not None and str(env).strip():
            return _truthy(env, self.RL_FALLBACK_DEFAULT)
        return self.RL_FALLBACK_DEFAULT

    def _fallback_models(self, model_key: str) -> list:
        """同通道里可**自动接替**的模型候选（按目录顺序，排除当前这个）。

        ★ 只在本通道目录里选：跨通道换模型等于偷偷换了账号/套餐，是另一回事。
        ★ `auto` 类不适合做接替目标（它自己还要再调度一轮，容易互相绕圈）。
        ★ 2026-10-24：默认**关闭**（见 RL_FALLBACK_DEFAULT 说明）。
          关掉时本函数返回空列表 —— 调用方会因此如实抛出「当前模型无可用资源」。
        """
        if not self._model_fallback_enabled():
            return []
        out = []
        skip = {"auto", "fast", "ultimate", "performance", "efficient"}
        for m in self._enabled_models():
            key = str(m.get("key") or "")
            if not key or key == model_key or key in skip:
                continue
            # 跳过仍在冷却里的模型（刚被判定没资源，换过去也是白打）
            if qc.resource_limited_until(self.realm, key):
                continue
            out.append(key)
        return out

    async def _events(self, body: dict, account: dict, model_key: str):
        """产出解包后的内层 chunk；对排队与「资源受限」自动等待/换模型重开。

        ★ 为什么还要在这一层再处理一次排队（2026-10-22 实测）：
          Qoder 的排队信号有两种到达方式 ——
            ① HTTP 403 + isQueued（在 _open_stream 里处理）
            ② **HTTP 200，但 SSE 第一帧是 403/10605 排队体**（服务端先把
               HTTP 头发出，队列状态随后作为数据帧下发）
          ② 这条如果只在流里抛错，用户看到的就是「请求失败」，而实际只要等
          几十秒就能用。参考实现（issue #38）同样在这一层做 retry，
          但**一旦已经吐过内容就不能重试** —— 否则会重复输出。
        """
        # ── 资源受限时的自救策略（见上方 RL_* 说明）──────────────────
        # ① 先按原模型**等待**重试；② 再依次试同通道的替代模型；③ 都不行才抛。
        tried: list = []
        rl_round = 0
        rl_start = time.time()      # ★ 等待预算的起点（见 RL_WAIT_BUDGET_SECS）
        current = model_key
        tried.append(current)
        while True:
            yielded_before = False
            try:
                async for chunk in self._events_one(body, account, current):
                    yielded_before = True
                    yield chunk
                return
            except qc.QoderResourceLimited as e:
                # 已经吐过内容 → 绝不换模型（会前后不一致），如实抛出
                if yielded_before:
                    raise
                # ① 同模型**等待**重试（2026-10-25：等待时间按 master 实测拉长）
                waited = time.time() - rl_start
                if (rl_round < self.RL_RETRY_ROUNDS
                        and waited + self.RL_RETRY_GAP <= self.RL_WAIT_BUDGET_SECS):
                    rl_round += 1
                    logger.info("%s 模型 %s 无资源，%g 秒后重试（第 %d/%d 轮，已等 %.0f 秒，"
                                "预算 %.0f 秒）",
                                self.name, self._model_label(current),
                                self.RL_RETRY_GAP, rl_round, self.RL_RETRY_ROUNDS,
                                waited, self.RL_WAIT_BUDGET_SECS)
                    qc.clear_resource_limited(self.realm, current)
                    await asyncio.sleep(self.RL_RETRY_GAP)
                    continue
                # ② 换模型
                #    ★ 2026-10-24 master 决定：默认**不换**。用户选了哪个模型
                #      就用哪个；它没资源就如实报错，绝不偷偷换成（往往更贵的）
                #      别的模型去扣积分。要恢复旧行为在 config.json 里写
                #      providers.<通道>.modelFallback = true（见 RL_FALLBACK_DEFAULT）。
                if not self._model_fallback_enabled():
                    e.fallbacks_tried = tried
                    # ★ 文案要报出「我们已经等了多久」—— 否则用户看到「请稍后重试」
                    #   会以为程序根本没等（2026-10-25 起等待可达 ~2.8 分钟）。
                    raise qc.QoderResourceLimited(
                        f"{self.name} 模型 {self._model_label(model_key)} 当前无可用资源"
                        f"（上游 RESOURCE_LIMIT，已等待 {time.time() - rl_start:.0f} 秒、"
                        f"重试 {rl_round} 轮仍未放行）："
                        f"本通道已关闭「自动切换模型」，请稍后重试或手动改用其它模型"
                    ) from e
                picked = None
                for cand in self._fallback_models(model_key):
                    if cand not in tried:
                        picked = cand
                        break
                if picked is None:
                    e.fallbacks_tried = tried
                    raise qc.QoderResourceLimited(
                        f"{self.name} 模型 {self._model_label(model_key)} 当前无可用资源"
                        f"（上游 RESOURCE_LIMIT）：已自动尝试 "
                        f"{len(tried)} 个模型（{'、'.join(self._model_label(t) for t in tried)}）"
                        f"均无资源，请稍后重试") from e
                tried.append(picked)
                logger.warning("%s 模型 %s 无资源，自动改用 %s（已试 %d 个）",
                               self.name, self._model_label(current),
                               self._model_label(picked), len(tried))
                current = picked
                rl_round = 0          # 换模型后重新给足重试次数
                yield {"qoder_fallback_model": picked}
                continue

    async def _events_one(self, body: dict, account: dict, model_key: str):
        """单个模型的流式产出（排队重试 + 冷却窗口判定都在这里）。"""
        # ── RESOURCE_LIMIT 冷却窗口（2026-10-24）─────────────────────────
        # 上游刚说过「这个模型没资源」→ 窗口内<b>不再打上游</b>，直接本地抛出
        # 同样语义的异常。理由：实测该状态短时间内不变，而 auto 探活 +
        # 用户点击会在同一分钟内反复打同一模型，每次都白花一次 RTT，还多一次
        # 风控暴露。窗口长度见 QD_RESOURCE_LIMIT_COOLDOWN（默认 45s，设 0 关闭）。
        # ★ 只对**请求发起前**生效；一旦拿到 200 就清掉窗口（说明已恢复）。
        # ★ 注意：冷却只影响**这一个模型**；外层 _events 会自动换模型继续。
        _until = qc.resource_limited_until(self.realm, model_key)
        if _until:
            raise qc.QoderResourceLimited(
                f"{self.name} 模型 {self._model_label(model_key)} 当前无可用资源"
                f"（上游 RESOURCE_LIMIT，冷却中约 {int(_until - time.time())} 秒）："
                f"请稍后重试或改用其它模型")

        queue_start = time.time()
        queue_round = 0
        while True:
            resp = await self._open_stream(body, account, model_key)
            # 拿到 200 → 资源已恢复，清掉冷却标记
            qc.clear_resource_limited(self.realm, model_key)
            yielded_any = False
            try:
                async for chunk in _aiter_chunks(resp):
                    yielded_any = True
                    yield chunk
                return
            except Exception as e:  # noqa: BLE001
                # 资源受限（RESOURCE_LIMIT）不是排队：不等待、不再重开，直接如实抛出
                # （与 _open_stream 里的判定同口径，2026-10-24）
                if qc.is_resource_limit(str(e)):
                    msg = (f"{self.name} 模型 {self._model_label(model_key)} 当前无可用资源"
                           f"（上游 RESOURCE_LIMIT）：请稍后重试或改用其它模型")
                    qc.mark_resource_limited(self.realm, model_key, msg)
                    raise qc.QoderResourceLimited(msg) from e
                wait = qc.queue_wait_seconds(str(e))
                if (not yielded_any and wait > 0
                        and queue_round < qc.QUEUE_MAX_ROUNDS
                        and wait <= qc.QUEUE_WAIT_MAX_SECS
                        and (time.time() - queue_start) + wait
                        <= qc.QUEUE_WAIT_BUDGET_SECS):
                    queue_round += 1
                    logger.info("%s 流内排队（尚未输出内容），%d 秒后重开（第 %d/%d 轮）",
                                self.name, wait, queue_round, qc.QUEUE_MAX_ROUNDS)
                    await asyncio.sleep(
                        wait + 1 + random.uniform(0, qc.QUEUE_WAIT_JITTER_SECS))
                    continue
                if wait > 0 and not yielded_any:
                    # 等待预算用尽：明确按「繁忙」抛出（含 isQueued 关键字，
                    # 上层据此判 busy 而不是 down）
                    raise qc.QoderQueued(
                        f"{self.name} 上游持续排队（已重试 {queue_round} 轮）: {e}") from e
                raise RuntimeError(
                    describe_net_exc(e, f"{self.name} 流式传输中断")) from e
            finally:
                try:
                    await resp.aclose()
                except Exception:  # noqa: BLE001
                    pass

    def _aggregate(self, events) -> tuple:
        content, reasoning = [], []
        tool_calls: dict[int, dict] = {}
        usage, finish = None, None
        for obj in events:
            if obj.get("usage"):
                usage = obj["usage"]
            for choice in obj.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    content.append(delta["content"])
                if delta.get("reasoning_content"):
                    reasoning.append(delta["reasoning_content"])
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    entry = tool_calls.setdefault(idx, {
                        "id": tc.get("id") or "", "type": tc.get("type") or "function",
                        "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        entry["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    entry["function"]["name"] += fn.get("name") or ""
                    entry["function"]["arguments"] += fn.get("arguments") or ""
        calls = [tool_calls[i] for i in sorted(tool_calls)]
        for c in calls:
            if not c["id"]:
                c["id"] = "call_" + uuid.uuid4().hex[:24]
        return "".join(content), "".join(reasoning), (calls or None), usage, finish

    async def _prepare(self, body: dict) -> tuple:
        model = str(body.get("model") or self.default_model)
        model_key = self.normalize_model(model)
        account = self._account_of(model)
        if not account:
            raise RuntimeError(
                f"{self.name} 通道没有可用账号（请先在账号管理页添加并登录，"
                f"或检查账号是否被禁用/失效）")
        return account, model_key

    # ---------- ### 输出 ----------
    def _recover_leaked_calls(self, content: str, body: dict):
        """非流式路径的回声回读（守卫见 parse_leaked_tool_calls）。

        返回 (calls|None, clean_content)。
        """
        if not content or not _MARKER_RE.search(content):
            return None, content
        allowed = declared_tool_names(client_tools(body))
        calls, clean = parse_leaked_tool_calls(content, allowed)
        if calls:
            logger.warning("%s 模型复述了工具协议回声，已还原为 %d 个结构化调用",
                           self.name, len(calls))
            return calls, clean
        _log_unrecovered_echo(self.name, content)
        return None, content

    async def chat(self, body: dict) -> dict:
        account, model_key = await self._prepare(body)
        events = []
        async for obj in self._events(body, account, model_key):
            events.append(obj)
        content, reasoning, tool_calls, usage, finish = self._aggregate(events)
        if not tool_calls:
            recovered, content = self._recover_leaked_calls(content, body)
            if recovered:
                tool_calls, finish = recovered, "tool_calls"
        message = {"role": "assistant", "content": content}
        if reasoning:
            message["reasoning_content"] = reasoning
        if tool_calls:
            message["tool_calls"] = tool_calls
        return {
            "id": make_chunk_id("qoder"),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model") or model_key,
            "choices": [{"index": 0, "message": message,
                         "finish_reason": finish or ("tool_calls" if tool_calls else "stop")}],
            "usage": normalize_usage(usage) or {
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    async def stream(self, body: dict):
        account, model_key = await self._prepare(body)
        cid = make_chunk_id("qoder")
        created = int(time.time())
        model = body.get("model") or model_key
        role_sent = False
        usage = None
        yielded = False
        # 回声回读：模型可能把历史里的 [TOOL CALLS] 序列化形态复述成正文，
        # 这里做跨帧缓冲 + 流尾裁决（见 _ToolCallEchoReader）。
        reader = _ToolCallEchoReader(declared_tool_names(client_tools(body)))
        pending_finish = None

        def chunk(delta: dict, finish=None):
            nonlocal role_sent
            d = dict(delta)
            if not role_sent:
                d = {"role": "assistant", **d}
                role_sent = True
            return {"id": cid, "object": "chat.completion.chunk", "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": d, "finish_reason": finish}]}

        def call_chunks(calls):
            """把还原出来的调用按 ### 增量形态发出去（一次给全参数）。"""
            out = []
            for i, call in enumerate(calls):
                out.append(chunk({"tool_calls": [{
                    "index": i, "id": call["id"], "type": call.get("type") or "function",
                    "function": call["function"]}]}))
            return out

        async for obj in self._events(body, account, model_key):
            if obj.get("usage"):
                usage = normalize_usage(obj["usage"])
            for choice in obj.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta") or {}
                out = {}
                if delta.get("content"):
                    safe = reader.feed(delta["content"])
                    if safe:
                        out["content"] = safe
                        yielded = True
                if delta.get("reasoning_content"):
                    out["reasoning_content"] = delta["reasoning_content"]
                if delta.get("tool_calls"):
                    # 上游自己给了结构化调用 → 回声捕获作废，按住的文本原样补发
                    held = reader.abort()
                    if held:
                        out["content"] = (out.get("content") or "") + held
                        yielded = True
                    out["tool_calls"] = delta["tool_calls"]
                    yielded = True
                if out:
                    yield chunk(out)
                if choice.get("finish_reason"):
                    if reader.capturing:
                        pending_finish = choice["finish_reason"]   # 等裁决后再补
                    else:
                        yield chunk({}, finish=choice["finish_reason"])
        # —— 流尾裁决：回声 → 结构化调用；否则原样补发 ——
        recovered, tail = reader.finish(self.name)
        if recovered:
            for c in call_chunks(recovered):
                yield c
            yielded = True
            pending_finish = "tool_calls"
        elif tail:
            yield chunk({"content": tail})
            yielded = True
        if pending_finish:
            yield chunk({}, finish=pending_finish)
        if not yielded:
            yield chunk({"content": ""}, finish="stop")
        if wants_usage_in_stream(body):
            yield make_usage_chunk(cid, created, model, usage or {
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})


async def _aiter_chunks(resp):
    """httpx 流 → 内层 chunk（信封拆包，逐行状态机）。

    维护 last_event：SSE 的 ``event:error`` 与其后的 ``data:`` 是两行，
    必须配对才能识别错误帧（见 _parse_envelope_line 的注释）。
    """
    buffer = ""
    last_event = ""
    async for text in resp.aiter_text():
        buffer += text
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            stripped = line.strip()
            if stripped.startswith("event:"):
                last_event = stripped[6:].strip()
                continue
            chunk = _parse_envelope_line(line, last_event)
            if last_event:
                last_event = ""
            if chunk is _DONE:
                return
            if chunk is not None:
                yield chunk
        await asyncio.sleep(0)      # 让出事件循环（长流不饿死其它请求）
    if buffer.strip():
        chunk = _parse_envelope_line(buffer, last_event)
        if chunk not in (None, _DONE):
            yield chunk


class _Sentinel:
    pass


_DONE = _Sentinel()


def _parse_envelope_line(line: str, last_event: str = ""):
    # ★ event:error 帧必须当成错误抛出去。
    #   2026-10-22 实测：上游出错时发的是
    #     event:error
    #     data:{"stackTrace":[...],"success":false,"msgInfo":"Internal Server Error"}
    #   旧实现只认 "data:" 行、把 event 行当噪音丢掉，再把这个**没有 choices、
    #   也没有 error_code** 的业务错误体当作普通 chunk 跳过 ——
    #   结果是「客户端收到 200 + 空回复」，真正的错误完全看不见。
    data = qc._strip_data_prefix(line)
    if last_event.strip().lower() == "error":
        raise RuntimeError(f"qoder upstream error: {data[:300] if data else ''}")
    if not data:
        return None
    try:
        outer = json.loads(data)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(outer, dict):
        return None
    # 业务失败体可能直接就是外层对象（没有 body 信封）——
    # 不在这里拦，它会因为「body 不是字符串」被静默跳过（见测试）。
    if outer.get("success") is False:
        raise RuntimeError(
            f"qoder upstream success=false "
            f"msg={outer.get('msgInfo') or outer.get('message') or ''}")
    status = outer.get("statusCodeValue")
    body = outer.get("body")
    if status not in (None, 200, "200"):
        raise RuntimeError(
            f"qoder upstream status={status}: "
            f"{(body if isinstance(body, str) else json.dumps(outer, ensure_ascii=False))[:300]}")
    if not isinstance(body, str):
        return None
    if body == "[DONE]":
        return _DONE
    try:
        inner = json.loads(body)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(inner, dict):
        return None
    code = inner.get("error_code") or inner.get("code")
    if code not in (None, "", 0, "0", 200, "200"):
        raise RuntimeError(
            f"qoder upstream code={code} "
            f"msg={inner.get('error_msg') or inner.get('message') or ''}")
    # 业务失败体（success=false）同样要抛：它没有 error_code，光看上面那条会漏
    if inner.get("success") is False:
        raise RuntimeError(
            f"qoder upstream success=false "
            f"msg={inner.get('msgInfo') or inner.get('message') or ''}")
    return inner


def qc_config_path() -> str:
    """安装根下的 config.json（打包态 __file__ 不可信，见 app_paths）。"""
    try:
        import app_paths as _ap
        return _ap.CONFIG_PATH
    except Exception:  # noqa: BLE001
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "config.json")
