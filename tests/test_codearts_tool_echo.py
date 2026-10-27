# -*- coding: utf-8 -*-
"""
测试: CodeArts 侧的工具协议回声护栏（2026-10-23 与 Qoder 对齐）
================================================================================
CodeArts **天生没有**「把 tool_calls 写进正文」的缺陷：它原样透传 messages、不写
marker（实测原生 tool_calls + role:"tool" 也被上游接受）。但它仍需要回读，因为：

  * 客户端的历史里可能有 marker —— 那条会话以前跑过 qdc-/qd-（同一个对话换模型
    很常见）；
  * 实测：把带 marker 的一行丢给 ca-GLM-5.2，它会**原样复述成正文**
    （191 字符、`finish_reason=stop`、没有任何结构化调用）。

没有回读时这段 JSON 会被当正文发给客户端（工具不执行）；有了回读则还原成结构化
调用，认不出来也 fail-open + 限流 WARN。本文件钉住四件事：
  1. 共享模块 `providers/tool_echo.py` 的机制对两个通道都可用；
  2. `_sanitize_history` 只在确实要改时复制、只动 assistant 正文、不动 tool_calls；
  3. `_recover_leaked_calls` 的守卫（未声明工具不还原 / 认不出来原样返回）；
  4. 流式 reader 的跨帧裁决（含「marker 正好落在帧尾」这个实测踩过的形态）。

不联网、不动真实 config。
运行: python -m unittest tests.test_codearts_tool_echo -v
"""
import json
import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

from providers import tool_echo as TE  # noqa: E402
from providers.codearts import CodeArtsProvider  # noqa: E402

TOOLS = [
    {"type": "function", "function": {"name": "bash", "description": "run",
                                      "parameters": {"type": "object"}}},
    {"type": "function", "function": {"name": "todo_write", "description": "写待办",
                                      "parameters": {"type": "object"}}},
]
ECHO = ('[TOOL CALLS] ' + json.dumps([{"id": "call_216c42952a054d40bb3945f2",
        "type": "function", "function": {"name": "todo_write",
        "arguments": json.dumps({"todos": [{"content": "x", "status": "in_progress"}]},
                                ensure_ascii=False)}}], ensure_ascii=False))


class Provider(object):
    """CodeArts 的这两个方法都不碰 self，用假实例直接调（避免构造真 provider）。"""

    _sanitize_history = staticmethod(CodeArtsProvider._sanitize_history)

    @staticmethod
    def _recover_leaked_calls(content, body):
        return CodeArtsProvider._recover_leaked_calls(object(), content, body)


class SharedEchoModuleTest(unittest.TestCase):
    """共享模块本身可用（两个通道用同一份实现）。"""

    def test_parse_roundtrip(self):
        calls, clean = TE.parse_leaked_tool_calls("先说明一下。\n" + ECHO,
                                                 ["todo_write"])
        self.assertEqual(calls[0]["function"]["name"], "todo_write")
        self.assertEqual(clean, "先说明一下。")

    def test_qoder_reexports_same_objects(self):
        """qoder_base 的同名再导出必须与共享模块**同一个对象**（防止两套实现）。"""
        from providers import qoder_base as QB
        self.assertIs(QB.parse_leaked_tool_calls, TE.parse_leaked_tool_calls)
        self.assertIs(QB._ToolCallEchoReader, TE._ToolCallEchoReader)
        self.assertIs(QB.TOOL_CALLS_MARKER, TE.TOOL_CALLS_MARKER)
        self.assertIs(QB.strip_history_echo, TE.strip_history_echo)


class SanitizeHistoryTest(unittest.TestCase):
    """历史摘除：只动 assistant 正文，且按需复制。"""

    def test_strips_echo_from_assistant_text(self):
        msgs = [{"role": "user", "content": "帮我规划"},
                {"role": "assistant", "content": ECHO},
                {"role": "user", "content": "继续"}]
        out = Provider._sanitize_history(msgs)
        self.assertEqual(out[1]["content"], "")
        self.assertEqual(out[0]["content"], "帮我规划")      # 其余原样
        self.assertIsNot(out, msgs)                          # 改过就复制

    def test_no_change_returns_equivalent_list(self):
        msgs = [{"role": "assistant", "content": "普通回复"}]
        out = Provider._sanitize_history(msgs)
        self.assertEqual(out, msgs)

    def test_keeps_prose_prefix(self):
        msgs = [{"role": "assistant", "content": "我先列个计划。\n" + ECHO}]
        self.assertEqual(Provider._sanitize_history(msgs)[0]["content"], "我先列个计划。")

    def test_does_not_touch_tool_calls_messages(self):
        msgs = [{"role": "assistant", "content": ECHO, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "bash", "arguments": "{}"}}]}]
        out = Provider._sanitize_history(msgs)
        self.assertEqual(out[0]["content"], ECHO)            # 有结构化调用就不动
        self.assertIn("tool_calls", out[0])

    def test_empty_and_none(self):
        self.assertEqual(Provider._sanitize_history(None), [])
        self.assertEqual(Provider._sanitize_history([]), [])


class RecoverLeakedCallsTest(unittest.TestCase):
    """非流式回读的守卫。"""

    def test_recovers_declared_tool(self):
        calls, clean = Provider._recover_leaked_calls(ECHO, {"tools": TOOLS})
        self.assertEqual(calls[0]["function"]["name"], "todo_write")
        self.assertEqual(clean, "")

    def test_refuses_undeclared_tool(self):
        calls, clean = Provider._recover_leaked_calls(
            ECHO, {"tools": [TOOLS[0]]})                    # 只声明了 bash
        self.assertIsNone(calls)
        self.assertEqual(clean, ECHO)

    def test_refuses_when_client_declares_no_tools(self):
        calls, _clean = Provider._recover_leaked_calls(ECHO, {})
        self.assertIsNone(calls)

    def test_plain_text_untouched(self):
        for text in ("普通回复。", "关于 [TOOL CALLS] 这个标记，见文档。"):
            calls, clean = Provider._recover_leaked_calls(text, {"tools": TOOLS})
            self.assertIsNone(calls)
            self.assertEqual(clean, text)


class CodeArtsReaderTest(unittest.TestCase):
    """流式 reader：CodeArts 走的是同一份实现（跨帧 + fail-open）。"""

    ALLOWED = ["bash", "todo_write"]

    def test_marker_split_at_frame_boundary(self):
        cut = ECHO.index(" [")
        r = TE._ToolCallEchoReader(self.ALLOWED)
        self.assertEqual(r.feed(ECHO[:cut]), "")
        self.assertEqual(r.feed(ECHO[cut:]), "")
        calls, tail = r.finish("codearts")
        self.assertIsNotNone(calls)
        self.assertEqual(calls[0]["function"]["name"], "todo_write")
        self.assertEqual(tail, "")

    def test_unrecoverable_flushed(self):
        r = TE._ToolCallEchoReader(["bash"])
        r.feed(ECHO)
        calls, tail = r.finish("codearts")
        self.assertIsNone(calls)
        self.assertEqual(tail, ECHO)

    def test_normal_text_passes_through(self):
        r = TE._ToolCallEchoReader(self.ALLOWED)
        self.assertEqual(r.feed("你好，"), "你好，")
        self.assertEqual(r.feed("递归就是自调用。"), "递归就是自调用。")

    def test_declared_names_reads_flat_and_nested(self):
        names = TE.declared_tool_names([
            {"type": "function", "function": {"name": "bash"}},
            {"type": "function", "name": "flat_tool"}])
        self.assertEqual(names, ["bash", "flat_tool"])


if __name__ == "__main__":
    unittest.main()
