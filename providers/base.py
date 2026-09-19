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
