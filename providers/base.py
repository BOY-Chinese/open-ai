# -*- coding: utf-8 -*-
"""Provider 抽象基类: 所有逆向模型提供商实现此接口, 注册后即可统一对外服务"""
import threading
from abc import ABC, abstractmethod
from typing import AsyncGenerator


class AccountHealth:
    """进程内账号运行态（invalid 标志）—— 账号管理页「真状态」的数据源。

    背景（2026-09-19 事故）：账号状态原先纯离线读 config，运行时失效
    （token/session 被上游判死）只体现在请求报错里，页面永远绿「启用」。
    本类让 provider 在请求路径上把「上游明确的鉴权拒绝」沉淀成可查状态，
    由 admin_api._provider_runtime_state 读取并合并进账号行。

    约定:
      - key 与 admin_api._account_id 的行 id 裸值一致
        (workbuddy* = userId, loomy = userid, trae = uid —— 后者在 node 侧)；
      - 线程安全: 标记发生在事件循环, 读取发生在 admin 线程；
      - ★ 铁律（trae 误报教训）: 只有上游**明确的鉴权拒绝**(401/403、
        业务码「未登录」、刷新凭据被拒)才允许 mark_invalid;
        网络异常/超时/5xx 一律不标记 —— 网络抖动不是账号死亡。
    """

    def __init__(self) -> None:
        self._invalid: dict[str, bool] = {}
        self._tokens: dict[str, str] = {}
        self._lock = threading.Lock()

    def mark_invalid(self, uid, reason: str = "") -> None:
        uid = str(uid or "")
        if not uid:
            return
        with self._lock:
            self._invalid[uid] = True
        if reason:
            import logging
            logging.getLogger("openapi.providers").warning(
                "账号运行态标记失效: uid=%s 原因=%s", uid, reason)

    def mark_ok(self, uid) -> None:
        uid = str(uid or "")
        if not uid:
            return
        with self._lock:
            self._invalid[uid] = False

    def is_invalid(self, uid) -> bool:
        return bool(self._invalid.get(str(uid or "")))

    def observe_token(self, uid, token) -> None:
        """记录账号当前凭据; 凭据发生变化（重新登录/续期写回）时自动解除失效。

        新凭据 = 新机会: 上一次的「已失效」判决是针对旧凭据的, 不能让
        换了新 token 的账号永远背着红牌 (trae 侧同款语义: 新 token 由
        健康检查重新验证)。
        """
        uid = str(uid or "")
        token = str(token or "")
        if not uid or not token:
            return
        with self._lock:
            old = self._tokens.get(uid)
            self._tokens[uid] = token
            if old is not None and old != token:
                self._invalid[uid] = False

    def state(self) -> dict[str, bool]:
        with self._lock:
            return dict(self._invalid)


class Provider(ABC):
    """统一 OpenAI 兼容 Provider 接口。

    - chat(body): 非流式, 返回 OpenAI chat.completion 响应 dict
    - stream(body): 流式, 产出 OpenAI chat.completion.chunk dict (不含 [DONE])
    - body: 已解析的请求 dict (OpenAI 格式, 含 model/messages/stream/tools 等)
    """

    # 提供商唯一标识, 用于日志/错误信息
    name: str = "base"

    # 模型名特征: resolve 时按 (前缀/关键字) 路由到本 provider
    # 例: [("deepseek", "deepseek"), ("ds-", "deepseek")]
    model_hints: list[tuple[str, str]] = []

    def runtime_account_state(self) -> dict[str, bool]:
        """账号运行态 {uid: invalid}; 无健康跟踪的 provider 返回空表。

        admin_api 据此把「运行时已失效」的账号在账号管理页标红断连。
        """
        health = getattr(self, "_health", None)
        return health.state() if health is not None else {}

    @abstractmethod
    def list_models(self) -> list[dict]:
        """返回 OpenAI 格式模型列表 [{id, object, owned_by}]"""
        ...

    @abstractmethod
    def normalize_model(self, model: str) -> str:
        """把请求中的模型名映射为 provider 内部模型标识"""
        ...

    @abstractmethod
    async def chat(self, body: dict) -> dict:
        """非流式对话, 返回完整 chat.completion dict"""
        ...

    @abstractmethod
    async def stream(self, body: dict) -> AsyncGenerator[dict, None]:
        """流式对话, yield 每个 chat.completion.chunk dict"""
        ...
        yield  # pragma: no cover


def make_chunk_id(prefix: str = "chatcmpl") -> str:
    import uuid
    return f"{prefix}-{uuid.uuid4().hex[:24]}"


def describe_net_exc(e: BaseException, prefix: str = "") -> str:
    """把异常转成「永不落空」的人类可读描述 (任何情况下都返回非空字符串)。

    背景 (2026-10-04 修复): workbuddy 流式传输中途被上游/链路 RST 时, httpx
    抛出的 ReadError/ReadTimeout **不带任何消息** (str(e) == ""), main.py 原本
    直接 str(e)[:300] 透传, 客户端只能看到 {"message":"","type":"upstream_error"}
    —— 完全无从排查。本函数对 httpx 网络层异常给出语义化中文描述, 其余异常
    兜底用异常类名。

    prefix: 可选上下文前缀 (如 "workbuddy 流式传输中断"), 最终形如
            "prefix: 语义说明 [httpx.ReadError: 原始消息]"
    """
    detail = str(e).strip()
    cls = f"{type(e).__module__}.{type(e).__name__}"
    hint = ""
    try:
        import httpx
        if isinstance(e, httpx.ReadTimeout):
            hint = "上游读超时 (迟迟等不到下一个数据包)"
        elif isinstance(e, httpx.ReadError):
            hint = "上游连接在传输中途被重置 (网络抖动/链路 RST)"
        elif isinstance(e, httpx.ConnectError):
            hint = "无法建立到上游的连接"
        elif isinstance(e, httpx.PoolTimeout):
            hint = "连接池获取连接超时"
        elif isinstance(e, httpx.RemoteProtocolError):
            hint = "上游未按 HTTP 协议收尾就断开"
        elif isinstance(e, httpx.WriteError):
            hint = "向上游发送请求数据失败"
    except Exception:  # noqa: BLE001 — httpx 缺席时退化为类名兜底, 不影响调用方
        pass
    if detail and hint:
        body = f"{hint} [{cls}: {detail}]"
    elif hint:
        body = f"{hint} [{cls}]"
    else:
        body = detail or cls
    return f"{prefix}: {body}" if prefix else body
