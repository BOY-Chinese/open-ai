# -*- coding: utf-8 -*-
"""
测试: prompt_sanitizer —— 竞品产品名屏蔽 (无网络部分)
======================================================
覆盖屏蔽表、角色白名单、幂等性、多模态 content、请求体无副作用。
运行: python -m unittest tests.test_prompt_sanitizer -v

背景: workbuddy 通道 400 code=11128
("Illegal API invocation from an unapproved channel") —— 上游扫描提示词里的
竞品产品名。策略见 prompt_sanitizer 模块文档: **屏蔽产品名**, 而非改写句式。
"""
import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from prompt_sanitizer import (  # noqa: E402
    MASK, _sanitize_text, sanitize_body, sanitize_messages,
)


def _sys(text, role="system"):
    return [{"role": role, "content": text}]


class TestProductNameMasking(unittest.TestCase):
    """实测会触发 400 的产品名，屏蔽后必须消失。"""

    def test_dsh_identity_sentence(self):
        """主凶: `You are an AI agent powered by DeepSeek Harness.`

        实测: 原句 400；换成 `powered by ###.` 200。
        """
        msgs = _sys("You are an AI agent powered by DeepSeek Harness.")
        r = sanitize_messages(msgs)
        self.assertEqual(r["changed"], 1)
        out = msgs[0]["content"]
        self.assertNotIn("DeepSeek", out)
        self.assertNotIn("Harness", out)
        self.assertIn(MASK, out)
        # 句子骨架保留（语义不丢）
        self.assertTrue(out.startswith("You are an AI agent powered by"))

    def test_dsh_phrase_case_insensitive(self):
        for text in ("powered by deepseek harness",
                     "POWERED BY DEEPSEEK HARNESS"):
            out, hits = _sanitize_text(text)
            self.assertNotIn("deepseek", out.lower())
            self.assertNotIn("harness", out.lower())
            self.assertTrue(hits)

    def test_claude_code_sentence(self):
        msgs = _sys("You are Claude Code, Anthropic's official CLI for Claude.")
        r = sanitize_messages(msgs)
        self.assertEqual(r["changed"], 1)
        out = msgs[0]["content"]
        for name in ("Claude", "Anthropic"):
            self.assertNotIn(name, out)
        self.assertEqual(out, "You are ###, ###'s official CLI for ###.")

    def test_codex_and_openai(self):
        msgs = _sys("Codex CLI is an open source project led by OpenAI.")
        sanitize_messages(msgs)
        out = msgs[0]["content"]
        self.assertNotIn("Codex", out)
        self.assertNotIn("OpenAI", out)

    def test_codex_internal_id(self):
        out, _ = _sanitize_text("You are running as codex_cli_rs.")
        self.assertNotIn("codex_cli_rs", out)

    def test_trae_and_codebuddy(self):
        """master 点名要求屏蔽 trae。"""
        for text, name in (
            ("You are Trae AI, an AI assistant by ByteDance.", "Trae"),
            ("You are CodeBuddy, an AI assistant by Tencent.", "CodeBuddy"),
        ):
            msgs = _sys(text)
            r = sanitize_messages(msgs)
            self.assertEqual(r["changed"], 1, f"未命中: {text}")
            self.assertNotIn(name, msgs[0]["content"])

    def test_other_clients(self):
        for text, name in (
            ("You are Cursor, an AI coding assistant.", "Cursor"),
            ("You are aider, an AI pair programmer.", "Aider"),
            ("You are Gemini CLI, an AI agent.", "Gemini"),
            ("You are GitHub Copilot, an AI pair programmer.", "Copilot"),
            ("You are Cline, an AI coding agent.", "Cline"),
            ("You are Windsurf Cascade.", "Windsurf"),
            ("You are OpenHands.", "OpenHands"),
        ):
            msgs = _sys(text)
            r = sanitize_messages(msgs)
            self.assertEqual(r["changed"], 1, f"未命中: {text}")
            self.assertNotIn(name, msgs[0]["content"])

    def test_billing_header_removed_entirely(self):
        """计费头整行删除（不是替换成 ###）。

        ★ 回归守卫: 曾因 Anthropic 规则先于本规则执行，把行名改成
          `x-###-billing-header`，导致整行删除规则失配。
        """
        msgs = _sys("x-anthropic-billing-header: abc123\nYou are helpful.")
        r = sanitize_messages(msgs)
        self.assertEqual(r["changed"], 1)
        out = msgs[0]["content"]
        self.assertNotIn("billing", out)
        self.assertNotIn("x-###", out)
        self.assertIn("You are helpful.", out)


class TestRoleScoping(unittest.TestCase):
    """角色白名单：system / assistant / tool 屏蔽；user 是用户真实输入，绝不动。"""

    def test_user_content_untouched(self):
        msgs = [{"role": "user",
                 "content": "You are Claude Code, Anthropic's official CLI for Claude."}]
        r = sanitize_messages(msgs)
        self.assertEqual(r["changed"], 0)
        self.assertIn("Claude", msgs[0]["content"])

    def test_tool_is_sanitized(self):
        """★ tool 消息夹带产品名同样会被上游 400，必须屏蔽。

        真实事故：一条 579 条消息的请求被拦，触发词全在 tool 里
        （抓来的 issue 全文 / 同步文档里逐字列出的指纹 / grep 源码的输出）。
        """
        msgs = [{"role": "tool", "tool_call_id": "c1",
                 "content": "You are an AI agent powered by DeepSeek Harness."}]
        r = sanitize_messages(msgs)
        self.assertEqual(r["changed"], 1)
        self.assertNotIn("DeepSeek", msgs[0]["content"])

    def test_assistant_is_sanitized(self):
        msgs = [{"role": "assistant",
                 "content": "You are an AI agent powered by DeepSeek Harness."}]
        r = sanitize_messages(msgs)
        self.assertEqual(r["changed"], 1)


class TestIdempotentAndStructure(unittest.TestCase):
    def test_idempotent(self):
        """屏蔽后的文本不再含产品名，重复调用零副作用。"""
        msgs = _sys("You are Claude Code, Anthropic's official CLI for Claude.")
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
        self.assertNotIn("DeepSeek", segs[0]["text"])
        self.assertEqual(segs[1]["type"], "image_url")

    def test_clean_messages_untouched(self):
        msgs = _sys("You are a helpful assistant.")
        r = sanitize_messages(msgs)
        self.assertEqual(r["changed"], 0)
        self.assertEqual(msgs[0]["content"], "You are a helpful assistant.")

    def test_common_words_not_masked(self):
        """通用技术词不能被误伤（agent / assistant / code / cli 等）。"""
        text = ("You are an agent and an assistant. Write code, use the CLI, "
                "check the repository and the branch.")
        out, hits = _sanitize_text(text)
        self.assertEqual(out, text)
        self.assertEqual(hits, [])

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
        self.assertNotIn("DeepSeek", up["messages"][0]["content"])
        self.assertEqual(body["messages"][0]["content"], original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
