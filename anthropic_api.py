# -*- coding: utf-8 -*-
"""
Anthropic Messages API 兼容层
=============================
把 Claude Code / CC Switch 的 Anthropic 协议请求 (/v1/messages)
转换为内部 OpenAI chat.completions 格式, 路由到 workbuddy/trae provider,
再把响应/流转换回 Anthropic 格式。

支持: 非流式 + 流式(SSE) + 工具调用(tools/tool_use/tool_result) + system
"""
import json
import re
import time
import uuid

# ======================= Anthropic 请求 -> OpenAI =======================

def _text_from_blocks(blocks) -> str:
    """content blocks -> 纯文本"""
    parts = []
    for b in blocks or []:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text") or "")
    return "".join(parts)


# Claude Code 会在 system 头部塞一行 telemetry/计费头:
#   x-anthropic-billing-header: cc_version=...; cc_entrypoint=sdk-cli;
# 该行会被上游内容安全过滤(如腾讯 WorkBuddy)判定为"header 注入/计费篡改"而整包拦截,
# 对下游模型也毫无价值, 故在此剥离。
_BILLING_HEADER_RE = re.compile(r"(?im)^x-[a-z0-9-]*billing[a-z0-9-]*header\s*:.*$")


def strip_problematic_headers(text: str) -> str:
    """移除 system 里 x-*-billing-header 等可能触发内容过滤的头行"""
    if not text:
        return text
    cleaned = [ln for ln in text.split("\n") if not _BILLING_HEADER_RE.match(ln)]
    return "\n".join(cleaned).strip("\n")


def anthropic_to_openai(body: dict) -> dict:
    oa: dict = {
        "model": body.get("model") or "",
        "messages": [],
        "stream": bool(body.get("stream")),
    }
    if body.get("max_tokens"):
        oa["max_tokens"] = body["max_tokens"]
    for k in ("temperature", "top_p"):
        if body.get(k) is not None:
            oa[k] = body[k]
    if body.get("stop_sequences"):
        oa["stop"] = body["stop_sequences"]

    # system
    sys_text = ""
    sys = body.get("system")
    if isinstance(sys, str):
        sys_text = sys
    elif isinstance(sys, list):
        sys_text = _text_from_blocks(sys)
    # 剥离 billling header 等可能触发上游内容过滤的头行
    sys_text = strip_problematic_headers(sys_text)
    msgs: list = []
    if sys_text:
        msgs.append({"role": "system", "content": sys_text})

    # messages
    for m in body.get("messages") or []:
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
            continue
        blocks = content if isinstance(content, list) else []
        if role == "assistant":
            text = _text_from_blocks(blocks)
            tool_uses = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
            oa_msg: dict = {"role": "assistant", "content": text}
            if tool_uses:
                oa_msg["tool_calls"] = [
                    {
                        "id": b.get("id") or f"call_{i}",
                        "type": "function",
                        "function": {
                            "name": b.get("name") or "",
                            "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False),
                        },
                    }
                    for i, b in enumerate(tool_uses)
                ]
            msgs.append(oa_msg)
        elif role == "user":
            text = _text_from_blocks(blocks)
            tool_results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
            if text:
                msgs.append({"role": "user", "content": text})
            for b in tool_results:
                tcontent = b.get("content")
                if isinstance(tcontent, list):
                    tcontent = _text_from_blocks(tcontent)
                msgs.append({
                    "role": "tool",
                    "tool_call_id": b.get("tool_use_id") or "",
                    "content": tcontent or "",
                })
        else:
            msgs.append({"role": role, "content": content})
    oa["messages"] = msgs

    # tools
    tools = body.get("tools")
    if tools:
        oa["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.get("name") or "",
                    "description": t.get("description") or "",
                    "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
            for t in tools if isinstance(t, dict)
        ]
    # tool_choice
    tc = body.get("tool_choice")
    if isinstance(tc, dict):
        t = tc.get("type")
        if t == "auto":
            oa["tool_choice"] = "auto"
        elif t == "any":
            oa["tool_choice"] = "required"
        elif t == "none":
            oa["tool_choice"] = "none"
        elif t == "tool" and tc.get("name"):
            oa["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
    return oa


# ======================= OpenAI 响应 -> Anthropic =======================

def _new_id(prefix="msg") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def openai_to_anthropic(resp: dict, model: str) -> dict:
    choice = (resp.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    blocks: list = []
    text = message.get("content") or ""
    if text:
        blocks.append({"type": "text", "text": text})
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        blocks.append({
            "type": "tool_use",
            "id": tc.get("id") or _new_id("toolu"),
            "name": fn.get("name") or "",
            "input": args if isinstance(args, dict) else {},
        })
    fr = choice.get("finish_reason")
    stop_reason = "end_turn"
    if fr == "tool_calls":
        stop_reason = "tool_use"
    elif fr == "length":
        stop_reason = "max_tokens"
    usage = resp.get("usage") or {}
    return {
        "id": resp.get("id") or _new_id(),
        "type": "message",
        "role": "assistant",
        "content": blocks,
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        },
    }


# ======================= OpenAI 流 -> Anthropic SSE =======================

def _merge_tool_calls(acc: dict, tc: dict) -> None:
    """把流式 tool_calls delta 合并到 acc (dict keyed by index)"""
    idx = tc.get("index", 0)
    st = acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
    if tc.get("id"):
        st["id"] = tc["id"]
    fn = tc.get("function") or {}
    if fn.get("name"):
        st["name"] = fn["name"]
    if fn.get("arguments"):
        st["arguments"] += fn["arguments"]


def _anthropic_sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def openai_stream_to_anthropic(gen, model: str):
    """把 provider.stream() 的 chunk 流转换为 Anthropic SSE 事件流"""
    first = True
    text_block_started = False
    tool_states: dict = {}
    tool_started: dict = {}
    output_tokens = 0
    stop_reason = "end_turn"

    async for chunk in gen:
        if first:
            yield _anthropic_sse("message_start", {
                "type": "message_start",
                "message": {
                    "id": _new_id(),
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            })
            first = False

        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        content = delta.get("content")
        if content:
            if not text_block_started:
                yield _anthropic_sse("content_block_start", {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                })
                text_block_started = True
            yield _anthropic_sse("content_block_delta", {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": content},
            })
            output_tokens += 1

        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            _merge_tool_calls(tool_states, tc)
            st = tool_states[idx]
            if not tool_started.get(idx):
                yield _anthropic_sse("content_block_start", {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {
                        "type": "tool_use",
                        "id": st["id"] or _new_id("toolu"),
                        "name": st["name"],
                        "input": {},
                    },
                })
                tool_started[idx] = True
            fn = tc.get("function") or {}
            if fn.get("arguments"):
                yield _anthropic_sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]},
                })

        fr = choice.get("finish_reason")
        if fr == "tool_calls":
            stop_reason = "tool_use"
        elif fr == "length":
            stop_reason = "max_tokens"
        elif fr == "stop":
            stop_reason = "end_turn"

    if text_block_started:
        yield _anthropic_sse("content_block_stop", {"type": "content_block_stop", "index": 0})
    for idx in sorted(tool_started):
        yield _anthropic_sse("content_block_stop", {"type": "content_block_stop", "index": idx})
    yield _anthropic_sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    })
    yield _anthropic_sse("message_stop", {"type": "message_stop"})


# ======================= 非流式聚合 (用流式通道避免 trae 非流式挂起) =======================

async def aggregate_stream(gen) -> dict:
    """把 provider.stream() 聚合为非流式 OpenAI completion (规避 trae 非流式 bug)"""
    content_parts: list = []
    tool_states: dict = {}
    finish_reason = "stop"
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    async for chunk in gen:
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        c = delta.get("content")
        if c:
            content_parts.append(c)
        for tc in delta.get("tool_calls") or []:
            _merge_tool_calls(tool_states, tc)
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]
        u = chunk.get("usage")
        if u:
            usage = u
    tool_calls = [
        {
            "id": st["id"] or f"call_{i}",
            "type": "function",
            "function": {"name": st["name"], "arguments": st["arguments"]},
        }
        for i, st in sorted(tool_states.items())
    ] or None
    message: dict = {"role": "assistant", "content": "".join(content_parts)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": _new_id("chatcmpl"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage,
    }
