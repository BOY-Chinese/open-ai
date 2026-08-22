# -*- coding: utf-8 -*-
"""Provider 抽象基类: 所有逆向模型提供商实现此接口, 注册后即可统一对外服务"""
from abc import ABC, abstractmethod
from typing import AsyncGenerator


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
