# -*- coding: utf-8 -*-
"""tool_echo.py —— 工具协议回声（``[TOOL CALLS] + JSON``）的识别与回读

## 这是什么问题

2026-10-23 master 反馈：模型把工具调用当**正文**吐出来，客户端看到一段 JSON、
工具一次都没执行。根因之一是本项目**自己写出去**的协议文本被模型照格式复述：

    历史里的 assistant.tool_calls
      →（文本降级形态）序列化成 ``[TOOL CALLS] + JSON 数组`` 写进 content
      → 长会话里模型照抄这段格式当正文输出
      → 客户端把 JSON 当正文显示，本轮工具调用静默不执行
    （参考项目 issue #8 / #37 同款）

## 这一层提供什么

* ``TOOL_CALLS_MARKER``：写入侧与回读侧**必须共用**的标记（改一处就够）；
* ``parse_leaked_tool_calls()``：把「marker + 合法 JSON 数组」还原成结构化
  tool_calls，守卫从严（认不出来就原样返回，绝不伪造调用）；
* ``_ToolCallEchoReader``：流式路径上的跨帧缓冲 —— 一帧里出现完整 marker 时
  先按住，流尾统一裁决（认得出来变结构化调用，认不出来原样补发，fail-open）；
* ``strip_history_echo()``：把**已经躺在历史里**的回声摘掉，断掉自我强化循环；
* ``_log_unrecovered_echo()``：认不出来时留一条限流 WARN —— 失败路径是
  fail-open，「协议失败」会被伪装成「语义成功」，必须有据可查。

Qoder 与 CodeArts 两个通道共用本模块（CodeArts 侧实测：把带 marker 的一行
丢给 ca-GLM-5.2，它会原样复述成正文 —— 所以它同样需要回读）。
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid

logger = logging.getLogger("openapi.providers.tool_echo")

# 工具协议：客户端 tools 透传 / 文本降级 / 回声回读
# ---------------------------------------------------------------------------
# 写入侧是 flatten_messages（structured=False 时把 assistant.tool_calls 序列化进
# content），回读侧是 parse_leaked_tool_calls —— 两侧必须用同一个 marker。
TOOL_CALLS_MARKER = "[TOOL CALLS]"

# marker 的所有变体：方/尖括号、大小写、空格/下划线（模型复述时常改写这些）。
# 至少要求一个开括号 —— 否则普通行文里的 "tool calls" 也会被当成回声。
_MARKER_RE = re.compile(r"[\[<(]\s*tool[\s_]*calls\s*[\]>)]?", re.I)
_MARKER_CORE = "toolcalls"

# 未还原回声的告警节流（一次长会话能刷出几十条，不节流会把日志淹掉）
_ECHO_WARN_STATE = {"last": 0.0, "suppressed": 0}
_ECHO_WARN_INTERVAL = 60.0

# 上游 tools[].type 只认 function；namespace / web_search 等原样透传会让上游
# 在反序列化阶段 400（unknown variant）。丢弃并告警（参考实现 issue #63）。
_UNSUPPORTED_TOOL_WARNED: set = set()


def declared_tool_names(tools) -> list:
    """已声明工具的名字集合（回声回读的白名单：没声明的工具还原了也没人执行）。"""
    names = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = fn.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def client_tool_choice(body: dict) -> tuple:
    """归一化客户端 tool_choice，返回 (值|None, 是否丢弃 tools)。

    ★ ``none`` 必须**连 tools 一起丢弃**：2026-10-23 实测上游不认 none ——
      带着 tools 发 tool_choice=none，模型照样发起工具调用（违反 OpenAI 语义）。
    对象形态原样透传（实测上游接受 {"type":"function","function":{"name":…}}）。
    """
    tc = body.get("tool_choice")
    if tc is None:
        return None, False
    if isinstance(tc, str):
        low = tc.strip().lower()
        if low == "none":
            return None, True
        return (low, False) if low in ("auto", "required") else (None, False)
    if isinstance(tc, dict):
        typ = str(tc.get("type") or "").strip().lower()
        if typ == "none":
            return None, True
        if typ in ("auto", "required"):
            return typ, False
        if typ == "function":
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
            name = str(fn.get("name") or "").strip()
            if name:
                return {"type": "function", "function": {"name": name}}, False
    return None, False


def _coerce_arguments(args):
    """arguments 归一化成 JSON 字符串；非法返回 None（宁可整条不还原）。"""
    if args is None:
        return ""
    if isinstance(args, (dict, list)):
        return json.dumps(args, ensure_ascii=False)
    if not isinstance(args, str):
        return None
    if not args.strip():
        return ""
    try:
        json.loads(args)
    except Exception:  # noqa: BLE001
        return None
    return args


def _call_from_echo_item(item, allowed_names):
    """回声里的一个条目 → {id,type,function}；非法/未声明 → None。"""
    if not isinstance(item, dict):
        return None
    fn = item.get("function") if isinstance(item.get("function"), dict) else item
    name = fn.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    if allowed_names is not None and name not in allowed_names:
        return None
    args = _coerce_arguments(fn.get("arguments"))
    if args is None:
        return None
    cid = item.get("id")
    if not isinstance(cid, str) or not cid.strip():
        cid = "call_" + uuid.uuid4().hex[:24]
    return {"id": cid.strip(), "type": "function",
            "function": {"name": name, "arguments": args}}


def parse_leaked_tool_calls(text, allowed_names=None) -> tuple:
    """把模型复述的工具协议回声（marker + JSON 数组）还原成结构化 tool_calls。

    为什么需要：文本降级形态下 flatten_messages 会把历史里的 assistant.tool_calls
    序列化成 ``[TOOL CALLS] [...]`` 写进 content；长会话里模型偶发**照格式复述**
    成正文（没有结构化 tool_calls），客户端就会把这段 JSON 当正文显示、本轮工具
    调用完全不执行（参考项目 issue #8/#37 同款）。这里在严格守卫下还原成结构化
    调用，并把正文里的回声块摘掉。

    守卫（宁可不还原，也不伪造调用）：
      1) 取**最后一个** marker —— 它之前的散文作为 clean 返回，由调用方补发；
      2) marker 之后必须是（可带 ``` 围栏的）JSON 数组，数组非空；
      3) 每项有非空 name，arguments 必须是合法 JSON（字符串或对象形态都收）；
      4) 传了 ``allowed_names`` 时**全部** name 必须命中 —— 客户端没声明的工具
         还原出来也没人能执行（客户端只会再报一次未知工具）。

    :return: (calls|None, clean_text)。未识别时原样返回 ``text``（fail-open）。
    """
    raw = text or ""
    last = None
    for m in _MARKER_RE.finditer(raw):
        last = m
    if last is None:
        return None, raw
    head = raw[:last.start()].rstrip()
    rest = raw[last.end():].strip()
    if rest.startswith("```"):          # 模型复述时常带 ```json 围栏
        rest = rest[3:]
        if rest[:4].lower() == "json":
            rest = rest[4:]
        rest = rest.lstrip("\r\n ")
    if rest.endswith("```"):
        rest = rest[:-3]
    rest = rest.strip()
    if not (rest.startswith("[") and rest.endswith("]")):
        return None, raw
    try:
        arr = json.loads(rest)
    except Exception:  # noqa: BLE001
        return None, raw
    if not isinstance(arr, list) or not arr:
        return None, raw
    calls = []
    for item in arr:
        call = _call_from_echo_item(item, allowed_names)
        if call is None:
            return None, raw
        calls.append(call)
    return calls, head


def strip_history_echo(text: str) -> str:
    """历史 assistant 正文里残留的工具协议回声 → 摘掉（只留散文）。

    为什么必须做：回声是**我们自己写出去的协议文本**，客户端从来没执行过对应调用
    （真正执行过的那次走的是结构化 tool_calls，正文里不会有 marker）。把它原样留在
    历史里等于每轮都在教模型「照这个格式复述」——「模型自己学会吐 [TOOL CALLS]」
    就是这么来的，清掉才能断掉这个自我强化循环。

    守卫与回读同一套（marker + 合法 JSON 数组），散文里顺带提到 marker 的正常回复
    不受影响；摘掉动作记一条 INFO（便于解释「模型怎么不记得自己调用过工具」）。
    """
    raw = text or ""
    if not _MARKER_RE.search(raw):
        return raw
    calls, clean = parse_leaked_tool_calls(raw, None)
    if not calls:
        return raw
    logger.info("历史里的工具协议回声已摘除（%d 个调用、%d 字符）",
                len(calls), len(raw) - len(clean))
    return clean


def _marker_prefix_len(text: str) -> int:
    """末尾有多少字符「可能是 marker 的前半截」（跨帧补全窗口，fail-open）。

    只认「开括号 + toolcalls 的真前缀」；普通正文（含行尾的 ``[`` 之外的字符）
    一律返回 0，绝不因为等一个 marker 把正常输出压住。
    """
    tail = (text or "")[-32:]
    for i in range(len(tail)):
        piece = tail[i:]
        stripped = piece.lstrip("[<( \t")
        if stripped == piece:           # 没有开括号 → 不可能是 marker 前缀
            continue
        if not stripped.strip():        # 只有括号/空白
            if not any(ch.isalnum() for ch in piece):
                return len(piece)
            continue
        compact = stripped.replace(" ", "").replace("_", "").lower()
        if len(compact) < len(_MARKER_CORE) and _MARKER_CORE.startswith(compact):
            return len(piece)
    return 0


def _log_unrecovered_echo(name: str, text: str) -> None:
    """识别到疑似工具协议回声、但守卫不许还原时记一条 WARN（60 秒节流）。

    为什么必须看得见：失败路径是 fail-open（原样透传），于是**协议失败**会被
    转换成**语义失真** —— 模型说「已经做完了」，工具其实一次都没执行，客户端看到
    的还是一轮正常结束。日志至少让发生率可观测。
    """
    st = _ECHO_WARN_STATE
    st["suppressed"] += 1
    now = time.time()
    if now - st["last"] < _ECHO_WARN_INTERVAL:
        return
    logger.warning("%s 正文里出现疑似工具协议回声但未能还原（%d 字符）——"
                   "本轮工具调用不会执行；近 60 秒共 %d 次",
                   name, len(text or ""), st["suppressed"])
    st["last"] = now
    st["suppressed"] = 0


class _ToolCallEchoReader:
    """流式路径上的回声回读缓冲。

    只做两件事，且全程 fail-open（绝不吞掉正常正文）：
      1) 正文**末尾**看起来像 marker 前半截时，最多按住那十几个字符；
         下一帧一证伪立刻补发；
      2) 一旦看到「完整 marker + JSON 数组起点」，转入捕获模式（从 marker 处
         按住，marker 之前的散文照发），流尾统一交给 parse_leaked_tool_calls
         裁决：认得出来 → 变成结构化 tool_calls；认不出来 → 原样当正文补发。
    """

    def __init__(self, allowed_names=None):
        self.allowed = allowed_names
        self.buf = ""
        self.capturing = False

    def feed(self, text: str) -> str:
        """吃进一段正文，返回「现在可以安全发出去」的部分。"""
        self.buf += text or ""
        if self.capturing:
            return ""
        last = None
        for m in _MARKER_RE.finditer(self.buf):
            last = m
        if last is None:
            hold = _marker_prefix_len(self.buf)
            if hold:
                out, self.buf = self.buf[:-hold], self.buf[-hold:]
            else:
                out, self.buf = self.buf, ""
            return out
        after = self.buf[last.end():]
        stripped = after.lstrip()
        if not stripped:
            # ★ marker 正好落在帧尾：后面是数组还是普通正文**还不知道**，必须按住
            #   等下一帧（2026-10-23 实测踩到：上游把 `[TOOL CALLS]` 和 ` [{…` 分在
            #   相邻两帧，一看到 marker 就放行的话，回声会被拆成两段正文发出去，
            #   客户端看到的正是「正文里出现 [TOOL CALLS] 而工具没执行」）。
            out, self.buf = self.buf[:last.start()], self.buf[last.start():]
            return out
        if stripped[0] in "[{`":
            head, self.buf = self.buf[:last.start()], self.buf[last.start():]
            self.capturing = True
            return head
        out, self.buf = self.buf, ""
        return out

    def abort(self) -> str:
        """放弃捕获（上游给了结构化调用时用），把按住的文本原样交回。"""
        text, self.buf = self.buf, ""
        self.capturing = False
        return text

    def finish(self, name: str = "qoder") -> tuple:
        """流结束：返回 (calls|None, 仍需补发的正文)。"""
        text, self.buf = self.buf, ""
        if not self.capturing:
            self.capturing = False
            return None, text
        self.capturing = False
        calls, clean = parse_leaked_tool_calls(text, self.allowed)
        if calls:
            logger.warning("%s 模型复述了工具协议回声，已还原为 %d 个结构化调用",
                           name, len(calls))
            return calls, clean
        _log_unrecovered_echo(name, text)
        return None, text
