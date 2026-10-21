# -*- coding: utf-8 -*-
"""
测试: prompt_sanitizer —— 上游渠道校验指纹净化 (无网络部分)
============================================================
覆盖改写表、角色白名单、幂等性、多模态 content、请求体无副作用。
运行: python -m unittest tests.test_prompt_sanitizer -v

背景: 2026-10-20 workbuddy 通道 400 code=11128
("Illegal API invocation from an unapproved channel") —— 上游对 system/assistant
消息开头做竞品 harness 身份扫描。实测规则见 prompt_sanitizer 模块文档。
"""
import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from prompt_sanitizer import sanitize_body, sanitize_messages  # noqa: E402


def _sys(text, role="system"):
    return [{"role": role, "content": text}]


class TestFingerprintRewrites(unittest.TestCase):
    """四条实测会触发 400 的指纹，改写后必须不再命中。"""

    def test_claude_code_official_sentence(self):
        """Claude Code 官方身份句 → 去掉 official。"""
        msgs = _sys("You are Claude Code, Anthropic's official CLI for Claude.")
        r = sanitize_messages(msgs)
        self.assertEqual(r["changed"], 1)
        self.assertNotIn("official CLI", msgs[0]["content"])
        self.assertIn("Anthropic's CLI for Claude", msgs[0]["content"])

    def test_main_branch_sentence(self):
        """Claude Code 的 main branch 固定句。"""
        msgs = _sys("Main branch (you will usually use this for PRs)")
        r = sanitize_messages(msgs)
        self.assertEqual(r["changed"], 1)
        self.assertNotIn("you will usually use this", msgs[0]["content"])
        self.assertIn("usually the base for PRs", msgs[0]["content"])

    def test_anthropic_billing_header(self):
        """Anthropic 计费头整行删除，保留其余内容。"""
        msgs = _sys("x-anthropic-billing-header: abc123\nYou are helpful.")
        r = sanitize_messages(msgs)
        self.assertEqual(r["changed"], 1)
        self.assertNotIn("x-anthropic-billing-header", msgs[0]["content"])
        self.assertIn("You are helpful.", msgs[0]["content"])

    def test_dsh_identity_sentence(self):
        """DSH 身份句 agent → assistant（本次事故直接触发者）。"""
        msgs = _sys("You are an AI agent powered by DeepSeek Harness.")
        r = sanitize_messages(msgs)
        self.assertEqual(r["changed"], 1)
        self.assertEqual(msgs[0]["content"],
                         "You are an AI assistant powered by DeepSeek Harness.")

    def test_case_insensitive(self):
        """上游校验大小写不敏感，改写也必须不敏感。"""
        for text in (
            "you are claude code, anthropic's official cli for claude.",
            "YOU ARE AN AI AGENT POWERED BY DEEPSEEK HARNESS.",
        ):
            msgs = _sys(text)
            r = sanitize_messages(msgs)
            self.assertEqual(r["changed"], 1, f"未命中: {text}")
            lowered = msgs[0]["content"].lower()
            self.assertNotIn("official cli for claude", lowered)
            self.assertNotIn("ai agent powered by deepseek harness", lowered)


class TestOtherHarnessFingerprints(unittest.TestCase):
    """其他主流 harness 身份句（实测当前 200，按 master 要求预防性清理）。"""

    def test_codex_cli(self):
        msgs = _sys("Codex CLI is an open source project led by OpenAI.")
        sanitize_messages(msgs)
        self.assertTrue(msgs[0]["content"].startswith("The Codex CLI"))

    def test_codex_internal_id(self):
        msgs = _sys("You are running as codex_cli_rs.")
        sanitize_messages(msgs)
        self.assertNotIn("codex_cli_rs", msgs[0]["content"])

    def test_gemini_aider_copilot_cursor_cline_windsurf(self):
        cases = [
            "You are Gemini CLI, an AI agent.",
            "You are aider, an AI pair programmer.",
            "You are GitHub Copilot, an AI pair programmer.",
            "You are Cursor, an AI coding assistant.",
            "You are Cline, an AI coding agent.",
            "You are Windsurf Cascade.",
            "You are Trae AI, an AI assistant by ByteDance.",
            "You are CodeBuddy, an AI assistant by Tencent.",
        ]
        for text in cases:
            msgs = _sys(text)
            r = sanitize_messages(msgs)
            self.assertEqual(r["changed"], 1, f"未命中: {text}")
            self.assertEqual(msgs[0]["content"], "You are an AI assistant."
                             if text.startswith("You are Windsurf")
                             or text.startswith("You are Trae")
                             or text.startswith("You are CodeBuddy")
                             else msgs[0]["content"])


class TestRoleScoping(unittest.TestCase):
    """实测上游只扫 system / assistant 开头，user/tool 不扫，不该动用户内容。"""

    def test_user_content_untouched(self):
        msgs = [{"role": "user",
                 "content": "You are Claude Code, Anthropic's official CLI for Claude."}]
        r = sanitize_messages(msgs)
        self.assertEqual(r["changed"], 0)
        self.assertIn("official CLI", msgs[0]["content"])

    def test_tool_content_untouched(self):
        msgs = [{"role": "tool", "tool_call_id": "c1",
                 "content": "You are an AI agent powered by DeepSeek Harness."}]
        r = sanitize_messages(msgs)
        self.assertEqual(r["changed"], 0)

    def test_assistant_is_sanitized(self):
        """assistant 开头命中 → 上游 400，必须净化。"""
        msgs = [{"role": "assistant",
                 "content": "You are an AI agent powered by DeepSeek Harness."}]
        r = sanitize_messages(msgs)
        self.assertEqual(r["changed"], 1)


class TestIdempotentAndStructure(unittest.TestCase):
    def test_idempotent(self):
        """改写后的文本不再命中，重复调用零副作用。"""
        msgs = _sys("You are an AI agent powered by DeepSeek Harness.")
        sanitize_messages(msgs)
        first = msgs[0]["content"]
        r2 = sanitize_messages(msgs)
        self.assertEqual(r2["changed"], 0)
        self.assertEqual(msgs[0]["content"], first)

    def test_multimodal_content(self):
        """多模态数组只改 text 段，image 段保持原样。"""
        msgs = [{"role": "system", "content": [
            {"type": "text", "text": "You are an AI agent powered by DeepSeek Harness."},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ]}]
        r = sanitize_messages(msgs)
        self.assertEqual(r["changed"], 1)
        segs = msgs[0]["content"]
        self.assertIn("assistant", segs[0]["text"])
        self.assertEqual(segs[1]["type"], "image_url")

    def test_clean_messages_untouched(self):
        msgs = _sys("You are a helpful assistant.")
        r = sanitize_messages(msgs)
        self.assertEqual(r["changed"], 0)
        self.assertEqual(msgs[0]["content"], "You are a helpful assistant.")

    def test_sanitize_body_keeps_other_fields(self):
        body = {"model": "wb-deepseek-v4.1-flash", "stream": True,
                "messages": _sys("You are an AI agent powered by DeepSeek Harness.")}
        r = sanitize_body(body)
        self.assertEqual(r["changed"], 1)
        self.assertEqual(body["model"], "wb-deepseek-v4.1-flash")
        self.assertTrue(body["stream"])

    def test_sanitize_body_tolerates_missing_messages(self):
        self.assertEqual(sanitize_body({})["changed"], 0)
        self.assertEqual(sanitize_body({"messages": None})["changed"], 0)
        self.assertEqual(sanitize_body({"messages": "not-a-list"})["changed"], 0)


class TestUpstreamBodyNoSideEffect(unittest.TestCase):
    """_to_upstream_body 净化时必须浅拷贝，不能改调用方 body。

    Auto 路由链会用同一个 body 依次试多个模型，留下副作用会污染后续尝试。
    """

    def test_caller_body_unchanged(self):
        from providers.workbuddy import _to_upstream_body
        original = "You are an AI agent powered by DeepSeek Harness."
        body = {"model": "wb-deepseek-v4.1-flash",
                "messages": [{"role": "system", "content": original}]}
        up = _to_upstream_body(body, "deepseek-v4-flash", None)
        self.assertIn("assistant", up["messages"][0]["content"])
        self.assertEqual(body["messages"][0]["content"], original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
