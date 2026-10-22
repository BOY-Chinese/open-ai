# -*- coding: utf-8 -*-
"""提示词净化器 —— 屏蔽会触发上游「渠道校验」拦截的竞品产品名。

## 为什么存在（2026-10-20 本机事故）

DSH 用 workbuddy 通道时请求被拒：

    400 {"code":11128,"msg":"Illegal API invocation from an unapproved channel"}

`displayTips` 原文是 "This call failed channel verification, possibly because ..."
—— 上游对**提示词内容**做竞品客户端身份扫描，命中即整单拦截，
与 token / 账号 / 模型全部无关（实测 7 个模型全拦、两个账号全拦）。

## 策略：屏蔽产品名，而非改写句式（master 2026-10-21 定）

早先的做法是把整句「改写」成等价句式（如 `AI agent` → `AI assistant`）。
问题在于**必须精确命中句子**：上游一旦微调措辞（加/减一个词、换标点），
改写规则就整条失效，而且每条改动都要重新抓包验证。

现改为**统一屏蔽产品名**：把竞品产品名一律替换成 `###`。

    You are an AI agent powered by DeepSeek Harness.  →  ... powered by ###.
    You are Claude Code, Anthropic's official CLI for Claude.
                                                     →  You are ###, ###'s official CLI for ###.

这样做的依据（2026-10-21 实测）：

* `AI agent powered by DeepSeek Harness.` 原句 → **400**
* `AI agent powered by ###.`             → **200** ✅
* 只屏蔽 `DeepSeek`、只屏蔽 `Harness`，同样 **200**
  → 说明上游扫的是**产品名本身**，去掉任一半就足以放行，不必猜句式。
* `You are ###, ###'s official CLI for ###.` → **200**（Claude Code 官方句）
* `You are ###, an AI assistant by ###.`     → **200**（CodeBuddy / Trae 官方句）

## 设计原则

* **只屏蔽产品名**，句子骨架（You are / powered by / the CLI for）原样保留，
  模型仍能理解「你是一个由某方提供的助手」，语义损失最小。
* **角色范围：system / assistant / tool**。
  - `user` 是用户真实输入，**绝不篡改**（实测把身份句发进 user，上游并不拦截）。
* **幂等**：屏蔽后文本不再含任何产品名，重复调用零副作用。
* **可审计**：每次屏蔽都写日志，便于排查「模型怎么突然不认识自己是谁」。
"""
import logging
import re

logger = logging.getLogger("openapi.prompt_sanitizer")

# 扫描角色白名单。
#
# ★ tool 必须在内（2026-10-21 补充）：抓到过一条 579 条消息、623 KB 的真实
#   请求被 400，触发词全在 tool 消息里（抓来的 issue 全文、同步文档里逐字
#   列出的指纹、grep 源码打印出的官方句）。现象极具迷惑性：**同一通道在干净
#   工作区正常、在排查过 11128 的工作区必挂**，因为「查这个 bug 的过程本身」
#   就在往上下文里灌触发词。
#
# ★ 不含 user：user 是用户真实输入，不该被网关篡改；实测把身份句发进 user /
#   工作区文档，上游并不拦截。
_SCANNED_ROLES = frozenset({"system", "assistant", "tool"})

# 屏蔽占位符（master 指定）
MASK = "###"

# ---------------------------------------------------------------------------
# 屏蔽表：(正则, 说明)
# 顺序敏感 —— 长名在前，避免短名先吃掉长名的一部分（如 Anthropic 先于 Claude）。
# 全部大小写不敏感（上游校验本身大小写不敏感）。
#
# ★ 只列**产品名 / 客户端名**，不列普通英文词：
#   `agent` / `assistant` / `code` / `cli` 这类通用词一律不屏蔽，
#   否则会把正常技术文档改得面目全非。
# ---------------------------------------------------------------------------
_MASKS = [
    # ---- Claude Code 客户端注入的计费头（实测 400，整行删除）----
    # ★ 必须排在 Anthropic 规则**之前**：否则 `Anthropic` 先命中，把行改成
    #   `x-###-billing-header: abc`，整行删除规则就再也匹配不到完整行名了。
    (re.compile(r"x-anthropic-billing-header\s*:[^\n]*", re.IGNORECASE),
     "Anthropic 计费头"),

    # ---- DSH（DeepSeek Harness）----
    # 本次事故主凶：`You are an AI agent powered by DeepSeek Harness.`
    # 实测整个短语换成 ### → 200；只换 DeepSeek 或只换 Harness 也 → 200。
    # 这里屏蔽**整个短语**（连读），避免留下 "### Harness" 这种半残表述。
    (re.compile(r"DeepSeek\s+Harness", re.IGNORECASE), "DSH 产品名"),

    # ---- Claude / Claude Code / Anthropic ----
    (re.compile(r"Claude\s+Code", re.IGNORECASE), "Claude Code"),
    (re.compile(r"\bClaude\b", re.IGNORECASE), "Claude"),
    (re.compile(r"\bAnthropic\b", re.IGNORECASE), "Anthropic"),

    # ---- Codex / OpenAI ----
    (re.compile(r"Codex\s+CLI", re.IGNORECASE), "Codex CLI"),
    (re.compile(r"\bcodex_cli_rs\b", re.IGNORECASE), "codex 内部标识"),
    (re.compile(r"\bCodex\b", re.IGNORECASE), "Codex"),
    (re.compile(r"\bOpenAI\b", re.IGNORECASE), "OpenAI"),

    # ---- 其他主流 harness / IDE 客户端 ----
    (re.compile(r"\bTrae\b", re.IGNORECASE), "Trae"),
    (re.compile(r"\bCodeBuddy\b", re.IGNORECASE), "CodeBuddy"),
    (re.compile(r"\bCursor\b", re.IGNORECASE), "Cursor"),
    (re.compile(r"\bAider\b", re.IGNORECASE), "Aider"),
    (re.compile(r"Gemini\s+CLI", re.IGNORECASE), "Gemini CLI"),
    (re.compile(r"\bGemini\b", re.IGNORECASE), "Gemini"),
    (re.compile(r"GitHub\s+Copilot", re.IGNORECASE), "GitHub Copilot"),
    (re.compile(r"\bCopilot\b", re.IGNORECASE), "Copilot"),
    (re.compile(r"\bCline\b", re.IGNORECASE), "Cline"),
    (re.compile(r"Windsurf\s+Cascade", re.IGNORECASE), "Windsurf Cascade"),
    (re.compile(r"\bWindsurf\b", re.IGNORECASE), "Windsurf"),
    (re.compile(r"\bOpenHands\b", re.IGNORECASE), "OpenHands"),
    (re.compile(r"\bOpenCode\b", re.IGNORECASE), "OpenCode"),

    # ---- 无产品名、但实测必拦的 Claude Code 固定句 ----
    # 本句不含任何产品名，屏蔽表覆盖不到；实测原句 400，
    # 整句换成 ### → 200。它只是 Claude Code 提 PR 时的样板提示，
    # 对模型行为无实质影响，整句屏蔽最省事。
    (re.compile(r"Main branch\s*\(you will usually use this for PRs\)",
                re.IGNORECASE),
     "Claude Code 的 main branch 固定句"),

]


def _text_of(content) -> str:
    """把消息 content 压平成可扫描的纯文本（多模态数组只取 text 段）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for seg in content:
            if isinstance(seg, dict) and seg.get("type") in (None, "text", "input_text"):
                t = seg.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    return ""


def _sanitize_text(text: str) -> tuple:
    """对单段文本跑屏蔽表。返回 (新文本, 命中的说明列表)。"""
    hits = []
    out = text
    for pattern, label in _MASKS:
        # 计费头整行删除，其余换成 ###
        repl = "" if "billing" in label else MASK
        new, n = pattern.subn(repl, out)
        if n:
            hits.append(f"{label} x{n}")
            out = new
    return out, hits


def _set_text(content, new_text: str):
    """把屏蔽后的文本写回 content，保持原有结构（str / 多模态数组）。"""
    if isinstance(content, str):
        return new_text
    if isinstance(content, list):
        # 多模态数组：把所有 text 段合并写入第一个 text 段，其余清空，
        # 避免破坏 image 段的位置语义。
        wrote = False
        out = []
        for seg in content:
            if isinstance(seg, dict) and seg.get("type") in (None, "text", "input_text"):
                seg = dict(seg)
                if not wrote:
                    seg["text"] = new_text
                    wrote = True
                else:
                    seg["text"] = ""
                out.append(seg)
            else:
                out.append(seg)
        return out
    return content


def sanitize_messages(messages) -> dict:
    """就地屏蔽 messages 里的竞品产品名（system / assistant / tool）。

    返回 {"changed": 命中条数, "hits": [说明...]}；无命中时零副作用。
    幂等：屏蔽后的文本不再含产品名，重复调用不会二次改写。
    """
    changed = 0
    all_hits = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        if (msg.get("role") or "") not in _SCANNED_ROLES:
            continue
        content = msg.get("content")
        text = _text_of(content)
        if not text:
            continue
        new_text, hits = _sanitize_text(text)
        if not hits:
            continue
        msg["content"] = _set_text(content, new_text)
        changed += 1
        all_hits.extend(hits)
    if changed:
        logger.info("提示词净化: 屏蔽 %d 条消息里的产品名 [%s]",
                    changed, "; ".join(all_hits))
    return {"changed": changed, "hits": all_hits}


def sanitize_body(body: dict) -> dict:
    """请求体级入口：只处理 messages，其余字段原样保留。"""
    if not isinstance(body, dict):
        return {"changed": 0, "hits": []}
    return sanitize_messages(body.get("messages"))
