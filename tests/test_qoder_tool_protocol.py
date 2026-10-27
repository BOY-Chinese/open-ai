# -*- coding: utf-8 -*-
"""
测试: Qoder 通道的工具协议（tools 透传 / 结构化历史 / 回声回读）
==================================================================
对应事故（2026-10-23，master 反馈）：
    「使用 qoder cn 的 qwen-3.8-flash 时，输出 [TOOL CALLS] [{...}]，
      似乎是格式不对，而且它也没法使用 bash」

根因链（三条，缺一条就会重现）：
  1. **模板自带工具集被泄漏**：请求体沿用官方 baseprompt 的 tools
     （Bash / Edit / TodoWrite …），客户端声明的工具根本没发给上游 ——
     模型照着 CLI 工具集调用，客户端没有这些工具，调用只能当正文显示。
  2. **历史工具调用被文本化**：``flatten_messages`` 把 assistant.tool_calls
     序列化成 ``[TOOL CALLS] + JSON`` 写进 content（上游其实原生认 tool_calls /
     tool 角色），长会话里模型**照格式复述**成正文 → 客户端把 JSON 当正文，
     本轮工具调用静默不执行（参考项目 issue #8/#37 同款）。
  3. **文本化形态没有回读**：复述出来的回声既没被还原成结构化调用，也没留告警 ——
     「协议失败」被伪装成「语义成功」。

本文件对三条修复逐条设断言。不联网、不写 config。
运行: python -m unittest tests.test_qoder_tool_protocol -v
"""
import json
import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from providers import qoder_base as QB  # noqa: E402
from providers.qoder_cn import QoderCNProvider  # noqa: E402

CLIENT_TOOLS = [
    {"type": "function", "function": {
        "name": "bash", "description": "run a shell command",
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "string"}},
                       "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "todo_write", "description": "write todos",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
]


def echo_payload():
    """模型复述出来的回声正文（形态 = flatten_messages 写出去的样子）。"""
    calls = [{"id": "call_216c42952a054d40bb3945f2", "type": "function",
              "function": {"name": "todo_write",
                           "arguments": json.dumps(
                               {"todos": [{"content": "定位安装目录",
                                           "status": "in_progress"}]},
                               ensure_ascii=False)}}]
    return QB.TOOL_CALLS_MARKER + " " + json.dumps(calls, ensure_ascii=False)


class ClientToolsTest(unittest.TestCase):
    """① 只发客户端声明的工具，绝不泄漏模板自带的 CLI 工具集。"""

    def test_no_client_tools_means_empty(self):
        self.assertEqual(QB.client_tools({}), [])
        self.assertEqual(QB.client_tools({"tools": []}), [])
        self.assertEqual(QB.client_tools({"tools": None}), [])

    def test_flat_form_is_wrapped(self):
        out = QB.client_tools({"tools": [
            {"type": "function", "name": "bash", "description": "d",
             "parameters": {"type": "object"}}]})
        self.assertEqual(out[0]["type"], "function")
        self.assertEqual(out[0]["function"]["name"], "bash")

    def test_unsupported_kind_dropped(self):
        out = QB.client_tools({"tools": [
            {"type": "namespace", "name": "mcp__x"},
            {"type": "function", "function": {"name": "bash"}}]})
        self.assertEqual([t["function"]["name"] for t in out], ["bash"])

    def test_declared_names(self):
        self.assertEqual(QB.declared_tool_names(CLIENT_TOOLS), ["bash", "todo_write"])


class ToolChoiceTest(unittest.TestCase):
    """tool_choice=none 必须连 tools 一起丢弃（上游不认 none，实测照样调用）。"""

    def test_none_drops_tools(self):
        value, drop = QB.client_tool_choice({"tool_choice": "none"})
        self.assertIsNone(value)
        self.assertTrue(drop)
        value, drop = QB.client_tool_choice({"tool_choice": {"type": "none"}})
        self.assertTrue(drop)

    def test_auto_and_required_pass_through(self):
        self.assertEqual(QB.client_tool_choice({"tool_choice": "auto"}), ("auto", False))
        self.assertEqual(QB.client_tool_choice({"tool_choice": "required"}),
                         ("required", False))

    def test_object_form(self):
        value, drop = QB.client_tool_choice(
            {"tool_choice": {"type": "function", "function": {"name": "bash"}}})
        self.assertFalse(drop)
        self.assertEqual(value["function"]["name"], "bash")

    def test_unknown_forms_ignored(self):
        self.assertEqual(QB.client_tool_choice({"tool_choice": "weird"}), (None, False))


class FlattenTest(unittest.TestCase):
    """② 结构化 vs 文本降级两种历史形态。"""

    MSGS = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "看下目录"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "bash", "arguments": {"command": "ls"}}}]},
        {"role": "tool", "tool_call_id": "call_1", "name": "bash", "content": "a.txt"},
        {"role": "user", "content": "继续"},
    ]

    def test_structured_keeps_native_shapes(self):
        _sys, flat, _img = QB.flatten_messages(self.MSGS, structured=True)
        self.assertEqual([m["role"] for m in flat],
                         ["user", "assistant", "tool", "user"])
        asst = flat[1]
        self.assertEqual(asst["tool_calls"][0]["id"], "call_1")
        # dict 形态的 arguments 必须统一成 JSON 字符串
        self.assertEqual(json.loads(asst["tool_calls"][0]["function"]["arguments"]),
                         {"command": "ls"})
        self.assertEqual(flat[2]["tool_call_id"], "call_1")
        self.assertEqual(flat[2]["content"], "a.txt")

    def test_structured_needs_tool_call_id_else_text(self):
        msgs = [*self.MSGS[:3], {"role": "tool", "content": "a.txt"},
                self.MSGS[4]]
        _sys, flat, _img = QB.flatten_messages(msgs, structured=True)
        self.assertTrue(flat[2]["role"] == "user")
        self.assertTrue(flat[2]["content"].startswith(QB.TOOL_RESULT_MARKER))

    def test_text_mode_writes_marker(self):
        _sys, flat, _img = QB.flatten_messages(self.MSGS, structured=False)
        self.assertIn(QB.TOOL_CALLS_MARKER, flat[1]["content"])
        self.assertNotIn("tool_calls", flat[1])
        self.assertTrue(flat[2]["content"].startswith(QB.TOOL_RESULT_MARKER))


class StructuredDecisionTest(unittest.TestCase):
    """结构化开关：默认开，id 不齐备自动退回文本，配置/环境可强制。"""

    MSGS = FlattenTest.MSGS

    def tearDown(self):
        os.environ.pop("OPENAI_QODER_STRUCTURED_TOOLS", None)

    def test_auto_on_when_ids_ok(self):
        self.assertTrue(QB._structured_tool_history({}, self.MSGS))

    def test_auto_off_when_id_missing(self):
        msgs = [dict(self.MSGS[2], tool_calls=[{"function": {"name": "bash"}}])]
        self.assertFalse(QB._structured_tool_history({}, msgs))
        msgs = [{"role": "tool", "content": "x"}]
        self.assertFalse(QB._structured_tool_history({}, msgs))

    def test_cfg_and_env_override(self):
        self.assertFalse(QB._structured_tool_history(
            {"structuredToolHistory": False}, self.MSGS))
        self.assertTrue(QB._structured_tool_history(
            {"structuredToolHistory": True}, [{"role": "tool", "content": "x"}]))
        os.environ["OPENAI_QODER_STRUCTURED_TOOLS"] = "off"
        self.assertFalse(QB._structured_tool_history({}, self.MSGS))
        os.environ["OPENAI_QODER_STRUCTURED_TOOLS"] = "on"
        self.assertTrue(QB._structured_tool_history({}, []))

    def test_forced_wins(self):
        self.assertFalse(QB._structured_tool_history({}, self.MSGS, forced=False))
        self.assertTrue(QB._structured_tool_history({}, [], forced=True))

    def test_native_shapes_detector(self):
        self.assertTrue(QB._native_tool_shapes(self.MSGS))
        self.assertFalse(QB._native_tool_shapes([{"role": "user", "content": "hi"}]))


class ParseEchoTest(unittest.TestCase):
    """③ 回声回读：严格守卫 + fail-open。"""

    ALLOWED = ["bash", "todo_write"]

    def test_recovers_openai_shape(self):
        text = "我先列个计划。\n" + echo_payload()
        calls, clean = QB.parse_leaked_tool_calls(text, self.ALLOWED)
        self.assertIsNotNone(calls)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "todo_write")
        self.assertEqual(calls[0]["id"], "call_216c42952a054d40bb3945f2")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"])["todos"][0]["content"],
                         "定位安装目录")
        self.assertEqual(clean, "我先列个计划。")

    def test_recovers_flat_shape_and_fence(self):
        text = QB.TOOL_CALLS_MARKER + "\n```json\n" + json.dumps(
            [{"name": "bash", "arguments": {"command": "ls"}}]) + "\n```"
        calls, clean = QB.parse_leaked_tool_calls(text, self.ALLOWED)
        self.assertIsNotNone(calls)
        self.assertEqual(calls[0]["function"]["name"], "bash")
        self.assertEqual(clean, "")

    def test_bracket_and_case_variants(self):
        for head in ("<TOOL CALLS>", "[tool calls]", "[Tool_Calls]"):
            text = head + " " + json.dumps([{"name": "bash", "arguments": "{}"}])
            calls, _clean = QB.parse_leaked_tool_calls(text, self.ALLOWED)
            self.assertIsNotNone(calls, head)

    def test_refuses_undeclared_name(self):
        """客户端没声明的工具（如 CLI 的 TodoWrite）不许伪造成调用。"""
        text = echo_payload()
        calls, clean = QB.parse_leaked_tool_calls(text, ["bash"])
        self.assertIsNone(calls)
        self.assertEqual(clean, text)

    def test_refuses_no_declared_tools(self):
        calls, clean = QB.parse_leaked_tool_calls(echo_payload(), [])
        self.assertIsNone(calls)
        self.assertEqual(clean, echo_payload())

    def test_refuses_bad_arguments(self):
        text = QB.TOOL_CALLS_MARKER + " " + json.dumps(
            [{"name": "bash", "arguments": "{not json"}])
        calls, _clean = QB.parse_leaked_tool_calls(text, self.ALLOWED)
        self.assertIsNone(calls)

    def test_fail_open_without_array(self):
        for text in ("我支持 [TOOL CALLS] 这种写法。",
                     "讨论 tool calls 的用法 [1, 2]",
                     QB.TOOL_CALLS_MARKER + " 后面没有数组"):
            calls, clean = QB.parse_leaked_tool_calls(text, self.ALLOWED)
            self.assertIsNone(calls)
            self.assertEqual(clean, text)

    def test_ordinary_tool_calls_prose_untouched(self):
        """行文里出现 'tool calls' 但没有开括号 → 一个字都不许动。"""
        text = "In this API, tool calls are expressed as JSON arrays."
        calls, clean = QB.parse_leaked_tool_calls(text, self.ALLOWED)
        self.assertIsNone(calls)
        self.assertEqual(clean, text)


class MarkerPrefixTest(unittest.TestCase):
    """跨帧补全窗口：只按住「可能是 marker 前半截」的尾巴。"""

    def test_holds_real_prefixes(self):
        for text in ("[", "[<", "[TOOL", "[tool cal", "看这个[TOOL"):
            self.assertGreater(QB._marker_prefix_len(text), 0, text)

    def test_does_not_hold_normal_text(self):
        for text in ("", "hello world", "数组结尾[1, 2]", "tool calls",
                     "递归就是自调用。", "见上文见下文"):
            self.assertEqual(QB._marker_prefix_len(text), 0, repr(text))

    def test_holds_bracket_only_tail(self):
        self.assertEqual(QB._marker_prefix_len("abc["), 1)
        self.assertEqual(QB._marker_prefix_len("见上文<"), 1)


class EchoReaderTest(unittest.TestCase):
    """流式回声回读：跨帧缓冲、fail-open、结构化调用优先。"""

    ALLOWED = ["bash", "todo_write"]

    def test_normal_text_passes_through(self):
        r = QB._ToolCallEchoReader(self.ALLOWED)
        self.assertEqual(r.feed("你好，"), "你好，")
        self.assertEqual(r.feed("递归就是自调用。"), "递归就是自调用。")
        calls, tail = r.finish()
        self.assertIsNone(calls)
        self.assertEqual(tail, "")

    def test_partial_marker_flushed_when_disproved(self):
        r = QB._ToolCallEchoReader(self.ALLOWED)
        out = r.feed("数组结尾[")
        self.assertEqual(out, "数组结尾")      # 只按住那个 '['
        self.assertEqual(r.feed("1, 2]"), "[1, 2]")   # 按住的 '[' 原样补回
        calls, tail = r.finish()
        self.assertIsNone(calls)
        self.assertEqual(tail, "")

    def test_partial_marker_flushed_at_stream_end(self):
        r = QB._ToolCallEchoReader(self.ALLOWED)
        self.assertEqual(r.feed("待续[TOOL"), "待续")
        calls, tail = r.finish()
        self.assertIsNone(calls)
        self.assertEqual(tail, "[TOOL")

    def test_echo_recovered_across_frames(self):
        r = QB._ToolCallEchoReader(self.ALLOWED)
        payload = echo_payload()
        emitted = r.feed("我先列个计划。\n" + payload[:20])
        self.assertEqual(emitted, "我先列个计划。\n")   # marker 之前的散文照发
        self.assertEqual(r.feed(payload[20:]), "")
        calls, tail = r.finish()
        self.assertIsNotNone(calls)
        self.assertEqual(calls[0]["function"]["name"], "todo_write")
        self.assertEqual(tail, "")

    def test_marker_exactly_at_frame_end_is_held(self):
        """★ 实测踩到的形态：marker 与数组被上游分在相邻两帧。"""
        r = QB._ToolCallEchoReader(self.ALLOWED)
        payload = echo_payload()
        cut = payload.index(" [")            # marker 之后的第一处空格
        emitted = r.feed(payload[:cut])      # 本帧正好以 marker 结尾
        self.assertEqual(emitted, "")        # 一个字都不许放出去
        self.assertEqual(r.feed(payload[cut:]), "")
        calls, tail = r.finish()
        self.assertIsNotNone(calls)
        self.assertEqual(calls[0]["function"]["name"], "todo_write")
        self.assertEqual(tail, "")

    def test_marker_at_frame_end_then_prose_is_flushed(self):
        r = QB._ToolCallEchoReader(self.ALLOWED)
        self.assertEqual(r.feed("[TOOL CALLS]"), "")
        self.assertEqual(r.feed(" 这只是行文里提到的标记。"), "[TOOL CALLS] 这只是行文里提到的标记。")
        calls, tail = r.finish()
        self.assertIsNone(calls)
        self.assertEqual(tail, "")

    def test_echo_unrecoverable_is_flushed(self):
        r = QB._ToolCallEchoReader(["bash"])          # todo_write 未声明
        payload = echo_payload()
        r.feed(payload)
        calls, tail = r.finish()
        self.assertIsNone(calls)
        self.assertEqual(tail, payload)

    def test_abort_returns_held_text(self):
        r = QB._ToolCallEchoReader(self.ALLOWED)
        r.feed("说明\n" + echo_payload())
        self.assertTrue(r.capturing)
        self.assertEqual(r.abort(), "[" + echo_payload()[1:])
        calls, tail = r.finish()
        self.assertIsNone(calls)
        self.assertEqual(tail, "")


class HistoryEchoCleanupTest(unittest.TestCase):
    """历史里残留的协议回声必须被摘掉（否则模型每轮都在学它）。"""

    def test_history_echo_stripped(self):
        msgs = [{"role": "user", "content": "帮我规划"},
                {"role": "assistant", "content": echo_payload()},
                {"role": "user", "content": "继续"}]
        _sys, flat, _img = QB.flatten_messages(msgs, structured=True)
        self.assertEqual(flat[1]["content"], "")

    def test_prose_prefix_kept(self):
        msgs = [{"role": "assistant", "content": "我先列个计划。\n" + echo_payload()}]
        _sys, flat, _img = QB.flatten_messages(msgs, structured=True)
        self.assertEqual(flat[0]["content"], "我先列个计划。")

    def test_ordinary_reply_untouched(self):
        for text in ("这是普通回复。", "关于 [TOOL CALLS] 这个标记，见文档。",
                     "示例：\n" + QB.TOOL_CALLS_MARKER + " 后面没有数组"):
            msgs = [{"role": "assistant", "content": text}]
            _sys, flat, _img = QB.flatten_messages(msgs, structured=True)
            self.assertEqual(flat[0]["content"], text, text)


class BuildBodyTest(unittest.TestCase):
    """请求体：客户端工具进、CLI 工具集不出。"""

    ACCOUNT = {"uid": "test-uid", "realm": "cn", "accessToken": "t",
               "userType": "personal_standard"}

    def provider(self, **cfg):
        return QoderCNProvider({"accounts": [self.ACCOUNT], **cfg})

    def body(self, **extra):
        return {"model": "qdc-Qwen3.8-Flash",
                "messages": [{"role": "user", "content": "hi"}], **extra}

    def test_client_tools_win(self):
        req = self.provider().build_body(self.body(tools=CLIENT_TOOLS),
                                         self.ACCOUNT, "qfmodel")
        self.assertEqual([t["function"]["name"] for t in req["tools"]],
                         ["bash", "todo_write"])

    def test_no_client_tools_means_no_tools(self):
        req = self.provider().build_body(self.body(), self.ACCOUNT, "qfmodel")
        self.assertEqual(req["tools"], [])
        cli_names = {t["function"]["name"] for t in (QB.BASEPROMPT.get("tools") or [])}
        self.assertTrue(cli_names)                       # 模板里确实有 CLI 工具集
        self.assertFalse(cli_names & {"bash", "todo_write"})   # 但一个都不许出去

    def test_tool_choice_none_drops_tools(self):
        req = self.provider().build_body(
            self.body(tools=CLIENT_TOOLS, tool_choice="none"), self.ACCOUNT, "qfmodel")
        self.assertEqual(req["tools"], [])
        self.assertNotIn("tool_choice", req)

    def test_tool_choice_forwarded_when_tools_present(self):
        req = self.provider().build_body(
            self.body(tools=CLIENT_TOOLS, tool_choice="required"),
            self.ACCOUNT, "qfmodel")
        self.assertEqual(req["tool_choice"], "required")

    def test_structured_history_in_body(self):
        msgs = FlattenTest.MSGS
        req = self.provider().build_body(
            {"model": "qdc-Qwen3.8-Flash", "messages": msgs}, self.ACCOUNT, "qfmodel")
        roles = [m["role"] for m in req["messages"]]
        self.assertIn("tool", roles)
        tool_msg = next(m for m in req["messages"] if m["role"] == "tool")
        self.assertEqual(tool_msg["tool_call_id"], "call_1")

    def test_text_fallback_forced(self):
        msgs = FlattenTest.MSGS
        req = self.provider().build_body(
            {"model": "qdc-Qwen3.8-Flash", "messages": msgs}, self.ACCOUNT,
            "qfmodel", structured=False)
        roles = [m["role"] for m in req["messages"]]
        self.assertNotIn("tool", roles)
        asst = next(m for m in req["messages"]
                    if m["role"] == "assistant" and m.get("content"))
        self.assertIn(QB.TOOL_CALLS_MARKER, asst["content"])
        self.assertNotIn("tool_calls", asst)


if __name__ == "__main__":
    unittest.main()
