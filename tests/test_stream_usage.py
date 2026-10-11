# -*- coding: utf-8 -*-
"""
测试: 流式响应的「用量尾块」(usage chunk)
==========================================
回归 2026-10-10 的 bug: 各 provider 的 stream() 只转发 content/reasoning/
tool_calls 增量, 上游在流尾补发的 usage 事件被整段丢弃 —— 客户端
(deepseek-harness / pi-ai) 读不到 prompt_tokens, 无法计算上下文占用
(DSH 界面里上下文占用恒为 0)。

本套件不发任何网络请求: 用假的 _chat_events / _stream 打桩, 验证
  1) providers.base 的 normalize_usage / make_usage_chunk / wants_usage_in_stream;
  2) workbuddy / loomy 的 stream() 在 include_usage 时补发尾块、且排在
     finish_reason 之后; 未要求时**不补**;
  3) auto_router 的用量块不会被「无真实内容」判定路径吞掉。

运行: python -m unittest tests.test_stream_usage -v
"""
import asyncio
import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from providers.base import (  # noqa: E402
    make_usage_chunk, normalize_usage, wants_usage_in_stream,
)
from providers.loomy import LoomyProvider  # noqa: E402
from providers.workbuddy import WorkBuddyProvider  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------- base helpers

class NormalizeUsageTest(unittest.TestCase):
    """normalize_usage: 补齐三段式, 但不裁掉上游附加字段。"""

    def test_empty_returns_none(self):
        """空/非法入参返回 None —— 调用方据此决定是否发尾块。"""
        for bad in (None, {}, [], "x", 0):
            self.assertIsNone(normalize_usage(bad))

    def test_complete_usage_passthrough(self):
        u = normalize_usage({"prompt_tokens": 6, "completion_tokens": 11,
                             "total_tokens": 17})
        self.assertEqual(u, {"prompt_tokens": 6, "completion_tokens": 11,
                             "total_tokens": 17})

    def test_total_derived_when_missing(self):
        """缺 total_tokens 时用 prompt+completion 补。"""
        u = normalize_usage({"prompt_tokens": 6, "completion_tokens": 11})
        self.assertEqual(u["total_tokens"], 17)

    def test_junk_values_coerced_to_zero(self):
        u = normalize_usage({"prompt_tokens": "x", "completion_tokens": None})
        self.assertEqual((u["prompt_tokens"], u["completion_tokens"],
                          u["total_tokens"]), (0, 0, 0))

    def test_extra_fields_preserved(self):
        """★ 关键: prompt_tokens_details.cached_tokens 等附加字段必须保留 ——
        pi-ai 靠它算缓存命中, 在这里裁剪会让缓存统计永久为 0。"""
        u = normalize_usage({
            "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
            "prompt_tokens_details": {"cached_tokens": 7},
            "points_consumed": 1,
        })
        self.assertEqual(u["prompt_tokens_details"], {"cached_tokens": 7})
        self.assertEqual(u["points_consumed"], 1)


class UsageChunkShapeTest(unittest.TestCase):
    """make_usage_chunk / wants_usage_in_stream。"""

    def test_chunk_has_empty_choices(self):
        """OpenAI 规范: 用量尾块 choices 为空, 只带 usage。"""
        c = make_usage_chunk("id1", 123, "m", {"prompt_tokens": 1,
                                               "completion_tokens": 2,
                                               "total_tokens": 3})
        self.assertEqual(c["choices"], [])
        self.assertEqual(c["object"], "chat.completion.chunk")
        self.assertEqual(c["usage"]["prompt_tokens"], 1)

    def test_gate_reads_stream_options(self):
        self.assertTrue(wants_usage_in_stream(
            {"stream_options": {"include_usage": True}}))
        self.assertFalse(wants_usage_in_stream({}))
        self.assertFalse(wants_usage_in_stream({"stream_options": {}}))
        self.assertFalse(wants_usage_in_stream(
            {"stream_options": {"include_usage": False}}))


# ------------------------------------------------------------ provider stream

class _FakeProviderMixin:
    """把 _chat_events 换成脚本化事件流, 其余逻辑走真实实现。"""

    def _install_events(self, provider, events):
        async def fake_events(*a, **kw):
            for e in events:
                yield e
        provider._chat_events = fake_events


UPSTREAM_EVENTS = [
    {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"},
                  "finish_reason": None}]},
    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    # 上游流尾的用量事件: 无 choices, 只带 usage
    {"usage": {"prompt_tokens": 6, "completion_tokens": 11,
               "total_tokens": 17,
               "prompt_tokens_details": {"cached_tokens": 4}}},
]


class WorkBuddyStreamUsageTest(_FakeProviderMixin, unittest.TestCase):

    def _provider(self):
        p = WorkBuddyProvider({"accounts": [{"userId": "u1",
                                             "accessToken": "t"}]})
        self._install_events(p, UPSTREAM_EVENTS)
        return p

    def _collect(self, include_usage):
        body = {"model": "wb-deepseek-v4-flash", "stream": True,
                "messages": [{"role": "user", "content": "hi"}]}
        if include_usage:
            body["stream_options"] = {"include_usage": True}
        p = self._provider()

        async def go():
            return [c async for c in p.stream(body)]
        return _run(go())

    def test_usage_chunk_emitted_when_requested(self):
        chunks = self._collect(True)
        usage_chunks = [c for c in chunks if c.get("usage")]
        self.assertEqual(len(usage_chunks), 1, "应恰好补发一个用量尾块")
        self.assertEqual(usage_chunks[0]["choices"], [])
        self.assertEqual(usage_chunks[0]["usage"]["prompt_tokens"], 6)
        # 附加字段也要透传 (pi-ai 的缓存统计依赖它)
        self.assertEqual(
            usage_chunks[0]["usage"]["prompt_tokens_details"]["cached_tokens"], 4)

    def test_usage_chunk_ordered_after_finish(self):
        """尾块必须排在 finish_reason 块之后 (OpenAI 规范 + pi-ai 解析顺序)。"""
        chunks = self._collect(True)
        finish_idx = next(i for i, c in enumerate(chunks)
                          if (c.get("choices") or [{}])[0].get("finish_reason"))
        usage_idx = next(i for i, c in enumerate(chunks) if c.get("usage"))
        self.assertGreater(usage_idx, finish_idx)

    def test_no_usage_chunk_without_flag(self):
        """未显式要求 include_usage 时不补发 (严格按 OpenAI 规范)。"""
        chunks = self._collect(False)
        self.assertEqual([c for c in chunks if c.get("usage")], [])


class LoomyStreamUsageTest(_FakeProviderMixin, unittest.TestCase):

    def _collect(self, include_usage):
        p = LoomyProvider({"accounts": [{"userid": "u1", "_token": "t"}]})
        self._install_events(p, UPSTREAM_EVENTS)
        body = {"model": "lm-GLM-5.3-Flash", "stream": True,
                "messages": [{"role": "user", "content": "hi"}]}
        if include_usage:
            body["stream_options"] = {"include_usage": True}

        async def go():
            return [c async for c in p.stream(body)]
        return _run(go())

    def test_usage_chunk_emitted_and_ordered(self):
        chunks = self._collect(True)
        usage_chunks = [c for c in chunks if c.get("usage")]
        self.assertEqual(len(usage_chunks), 1)
        self.assertEqual(usage_chunks[0]["choices"], [])
        finish_idx = next(i for i, c in enumerate(chunks)
                          if (c.get("choices") or [{}])[0].get("finish_reason"))
        self.assertGreater(chunks.index(usage_chunks[0]), finish_idx)

    def test_no_usage_chunk_without_flag(self):
        self.assertEqual([c for c in self._collect(False) if c.get("usage")], [])


# ----------------------------------------------------------------- auto_router

class AutoRouterUsageTest(unittest.TestCase):
    """auto 链必须把用量尾块原样透传给客户端。

    用量块不算「真实内容」(否则只回用量、没出字的模型会被误判成命中),
    但它到达时 produced 已为 True, 走的是直接透传路径 —— 本用例把这条
    端到端路径钉住, 防止以后有人给缓冲逻辑加条件时顺手把它吞掉。
    """

    def test_usage_chunk_not_treated_as_payload(self):
        from auto_router import _chunk_has_payload
        usage_chunk = make_usage_chunk("id", 1, "m", {"prompt_tokens": 1,
                                                      "completion_tokens": 2,
                                                      "total_tokens": 3})
        self.assertFalse(_chunk_has_payload(usage_chunk))

    def test_usage_chunk_passes_through_chain(self):
        from auto_router import auto_stream

        class FakeProvider:
            name = "fake"

            async def stream(self, body):
                yield {"choices": [{"index": 0, "delta": {"content": "hi"},
                                    "finish_reason": None}]}
                yield {"choices": [{"index": 0, "delta": {},
                                    "finish_reason": "stop"}]}
                yield make_usage_chunk("id", 1, "m",
                                       {"prompt_tokens": 9,
                                        "completion_tokens": 1,
                                        "total_tokens": 10})

        def route(m, providers):
            return FakeProvider()

        async def go():
            chain = {"name": "T", "models": [{"model": "fake-1"}]}
            out = []
            async for kind, payload in auto_stream(route, {}, {"stream": True}, chain):
                if kind == "chunk":
                    out.append(payload)
            return out

        chunks = _run(go())
        usage_chunks = [c for c in chunks if c.get("usage")]
        self.assertEqual(len(usage_chunks), 1, "auto 链不得吞掉用量尾块")
        self.assertEqual(usage_chunks[0]["usage"]["prompt_tokens"], 9)
        # 且仍排在 finish 块之后
        finish_idx = next(i for i, c in enumerate(chunks)
                          if (c.get("choices") or [{}])[0].get("finish_reason"))
        self.assertGreater(chunks.index(usage_chunks[0]), finish_idx)


if __name__ == "__main__":
    unittest.main()