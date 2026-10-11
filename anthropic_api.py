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


def sanitize_openai_messages(msgs: list) -> list:
    """规范化 OpenAI 消息序列, 保证 tool_calls 与 tool 结果严格配对。

    背景 (2026-10-11): 上游腾讯 copilot 对 tool 配对校验极严, 一旦
    「tool_calls 与 tool 结果对不上」即返回 HTTP 400 / code 11148
    "tool calls and tool results do not match, please start a new conversation"。

    而 Claude Code 在流式异常 (见 openai_stream_to_anthropic 的索引修复) 时,
    会把错误提示作为 **assistant 文本消息** 写回会话历史, 且恰好插在
    tool_use 与 tool_result 之间; 该脏历史每轮都被原样转发, 导致请求
    **永久性失败** —— 用户只能不断新建会话, 但新会话很快又被同样污染。

    本函数把五类畸形形态抹平为合法序列:
      ① assistant 带 tool_calls 却收到悬空(无结果) → 补占位 tool 结果
      ② 悬空 tool_call 之后又来了别的消息 → 先补占位再继续
      ③ tool 结果找不到对应 tool_call (孤儿) → 降级为普通文本, 不破坏序列
      ④ tool_call id 为空 → 就地补合成 id (不丢弃, 保住内容)
      ⑤ 相邻同角色 user 消息 → 合并
    """
    out: list = []
    pending: list = []          # 已发出但还没收到结果的 tool_call_id

    for m in msgs:
        role = m.get("role")

        if role == "assistant":
            # ① 上一批 tool_call 没收到结果 → 补占位, 保证配对
            for cid in pending:
                out.append({"role": "tool", "tool_call_id": cid,
                            "content": "[interrupted: no result returned]"})
            pending = []
            tcs = m.get("tool_calls") or []
            if tcs:
                # ④ 空 id 就地补合成 id (丢弃会让其 tool 结果变孤儿)
                fixed = []
                for i, t in enumerate(tcs):
                    t = dict(t)
                    if not t.get("id"):
                        t["id"] = f"call_synth_{len(out)}_{i}"
                    fixed.append(t)
                m = dict(m, tool_calls=fixed)
                pending = [t["id"] for t in fixed]
            out.append(m)
            continue

        if role == "tool":
            cid = m.get("tool_call_id")
            if not cid and pending:
                # ④ 空 id 的结果: 按顺序认领第一个待配对 id
                cid = pending.pop(0)
                out.append({"role": "tool", "tool_call_id": cid,
                            "content": m.get("content") or ""})
            elif cid and cid in pending:
                pending.remove(cid)
                out.append(m)
            else:
                # ③ 孤儿结果: 降级为文本, 避免破坏序列
                out.append({"role": "user",
                            "content": f"[orphan tool_result dropped: {cid or 'empty'}]"})
            continue

        # ② 其它角色插在 tool_call 与结果之间 → 先补齐工具结果, 再放本条
        if pending:
            for cid in pending:
                out.append({"role": "tool", "tool_call_id": cid,
                            "content": "[interrupted: no result returned]"})
            pending = []
        out.append(m)

    # 收尾: 补最后一个悬空 tool_call
    for cid in pending:
        out.append({"role": "tool", "tool_call_id": cid,
                    "content": "[interrupted: no result returned]"})

    # ⑤ 合并相邻同角色 user 消息 (纯文本才合并, 保持结构简单)
    merged: list = []
    for m in out:
        if (merged and merged[-1].get("role") == "user" and m.get("role") == "user"
                and isinstance(merged[-1].get("content"), str)
                and isinstance(m.get("content"), str)):
            merged[-1]["content"] += "\n" + m["content"]
        else:
            merged.append(m)
    return merged


def anthropic_to_openai(body: dict) -> dict:
    oa: dict = {
        "model": body.get("model") or "",
        "messages": [],
        "stream": bool(body.get("stream")),
    }
    # Anthropic 协议要求 message_delta 必带 usage (input/output_tokens 是必填字段),
    # 而网关在这里扮演「provider 的客户端」—— 必须显式索要用量尾块,
    # 否则 provider.stream() 不会补发 usage, 只能退回按内容块计数的粗估
    # (2026-10-10)。非流式路径不受影响 (provider.chat 恒带 usage)。
    oa["stream_options"] = {"include_usage": True}
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
            # ★ 有 tool_calls 时不再附带空 content (2026-10-11):
            #   上游把「content:"" + tool_calls」判为 tool calls/results 失配
            #   (workbuddy 错误码 11148)。仅在没有 tool_calls 且无文本时补 ""。
            oa_msg: dict = {"role": "assistant"}
            if text:
                oa_msg["content"] = text
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
            elif not text:
                oa_msg["content"] = ""
            msgs.append(oa_msg)
        elif role == "user":
            text = _text_from_blocks(blocks)
            tool_results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
            # ★ tool 结果必须先于同一 user 消息里的文本发出 (2026-10-11):
            #   OpenAI 协议要求 role=tool 紧跟在带 tool_calls 的 assistant 之后。
            #   原实现先发文本再发 tool 结果, 会在两者之间插入一条 user 消息,
            #   上游即判「tool calls and tool results do not match」(11148)。
            for b in tool_results:
                tcontent = b.get("content")
                if isinstance(tcontent, list):
                    tcontent = _text_from_blocks(tcontent)
                msgs.append({
                    "role": "tool",
                    "tool_call_id": b.get("tool_use_id") or "",
                    "content": tcontent or "",
                })
            if text:
                msgs.append({"role": "user", "content": text})
        else:
            msgs.append({"role": role, "content": content})
    oa["messages"] = sanitize_openai_messages(msgs)

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
    tool_offset: int | None = None   # 工具块索引偏移 (文本块占了 0 则为 1), 见下方说明
    output_tokens = 0
    usage: dict | None = None
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

        # 用量尾块 (choices 为空, 只带 usage): 上游真实用量在流尾才到,
        # 记下来交给收尾的 message_delta 上报 (2026-10-10)。
        if chunk.get("usage"):
            usage = chunk["usage"]

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
            raw_idx = tc.get("index", 0)
            # ★ 索引偏移 (2026-10-11): 上游 tool_calls[].index 独立从 0 开始,
            #   与文本块无关; 而文本块固定占用 index 0。若模型「先说一句再调
            #   工具」(Claude Code 系统提示鼓励如此, 故几乎每次都命中), 两者
            #   就会共用 index 0 → 发出两条 index=0 的 content_block_stop →
            #   Claude Code 抛 StreamMalformedEventError("Content block already
            #   closed"), 对外显示 "The response stream was malformed";
            #   同时 input_json_delta 被挂到文本块上, 导致工具参数丢失/串位。
            #   故: 文本块已占用 index 0 时, 工具块整体后移一位。
            if tool_offset is None:
                tool_offset = 1 if text_block_started else 0
            idx = raw_idx + tool_offset
            _merge_tool_calls(tool_states, tc)
            st = tool_states[raw_idx]
            if not tool_started.get(raw_idx):
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
                tool_started[raw_idx] = True
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
    for raw_idx in sorted(tool_started):
        yield _anthropic_sse("content_block_stop", {
            "type": "content_block_stop",
            "index": raw_idx + (tool_offset or 0)})
    yield _anthropic_sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        # 优先用上游真实用量; 拿不到时才退回「按内容块计数」的粗估
        # (旧行为, 每个 content delta 记 1 —— 与真实 token 数差距很大)。
        "usage": {
            "input_tokens": int((usage or {}).get("prompt_tokens") or 0),
            "output_tokens": int((usage or {}).get("completion_tokens") or output_tokens),
        },
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
