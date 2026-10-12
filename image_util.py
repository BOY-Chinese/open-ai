# -*- coding: utf-8 -*-
"""图片归一化 —— 把客户端给的「本地路径 / http(s) URL / 缺 media_type 的 data URI」
统一转成标准 base64 data URI 后再转发下游。

背景 (见《Trae视觉通道实现文档》§4/§5.3):
  Trae 上游【只认】`data:<mime>;base64,<b64>` 形式, 以下一律回 param invalid:
    * data:;base64,xxxx      (缺 media_type)
    * http(s)://host/a.png
    * C:\\path\\a.png          (本地路径)
    * file:///C:/path/a.png
  所以网关侧兜一道归一化, 让「给路径」也能用上视觉。

⚠️ 本模块含【阻塞 IO】(读盘 / 下载)。宿主是异步框架 (FastAPI),
   调用方必须用 `await asyncio.to_thread(...)` 包住, 否则会冻结事件循环
   → HTTP 无响应 → 前端误报「后端未启动」。
"""
import base64
import logging
import mimetypes
import os
import re
import urllib.request

logger = logging.getLogger("openapi.image")

# data URI: 允许 media_type 缺省 (data:;base64,...) —— 缺省时补 image/png
_DATA_URI_RE = re.compile(r"^data:([^;,]*)?;base64,(.*)$", re.DOTALL)

MAX_BYTES = 20 * 1024 * 1024   # 单图上限 20MB
FETCH_TIMEOUT = 8              # 下载超时 (秒)

# 文本里内嵌的本地图片路径 (§5.4)
# 负向后顾 (?<![A-Za-z0-9]) 很关键: 否则 https://x/a.png 里的 "s:/" 会被误判成
# Windows 盘符 "s:" 而匹配。只认本地路径, 【不碰 http(s) URL】—— 文档/搜索结果里
# 到处是图片链接, 全量下载会注入大量无关图片, 上游报 11135 (请开新会话换图)。
_IMG_PATH_RE = re.compile(
    r"(?P<path>"
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'<>|*?]+?\.(?:png|jpe?g|gif|webp|bmp)"
    r"|/(?:home|Users|tmp|mnt|var|opt|root|media|Volumes)/[^\s\"'<>|*?]+?\.(?:png|jpe?g|gif|webp|bmp)"
    r")", re.IGNORECASE)


def sniff_image_mime(raw: bytes):
    """按文件头确认真是图片, 返回 MIME 或 None。

    只看扩展名不够 —— URL 以 .png 结尾不代表内容是图片
    (例如 GitHub /blob/ 链接返回的是 HTML 页面)。
    """
    if not raw or len(raw) < 12:
        return None
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:2] == b"BM":
        return "image/bmp"
    return None


def to_data_uri(ref: str):
    """本地路径 / file:// / http(s) URL / 残缺 data URI → 标准 base64 data URI。

    无法识别为图片时返回 None (由调用方决定是保留原值还是剔除)。
    """
    if not ref or not isinstance(ref, str):
        return None
    m = _DATA_URI_RE.match(ref)
    if m:
        # 已是 data URI: 只补全缺失的 media_type, 内容不重新编码 (零开销)
        mt = (m.group(1) or "image/png").strip() or "image/png"
        return "data:%s;base64,%s" % (mt, m.group(2))
    low = ref.lower()
    try:
        if low.startswith(("http://", "https://")):
            req = urllib.request.Request(ref, headers={"User-Agent": "open-ai/1.0"})
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
                raw = r.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                logger.warning("图片超限 (>%dMB), 跳过: %s", MAX_BYTES // 1024 // 1024, ref[:120])
                return None
        else:
            p = ref[7:] if low.startswith("file://") else ref
            # Windows 路径: 去掉前导斜杠 (/C:/x.png) 以便在 WSL/跨平台下也尽量能读
            if re.match(r"^/[A-Za-z]:[\\/]", p):
                p = p[1:]
            if not os.path.isfile(p) or os.path.getsize(p) > MAX_BYTES:
                return None
            with open(p, "rb") as f:
                raw = f.read()
    except Exception as e:  # noqa: BLE001 — 网络/权限/编码问题一律降级为「转不了」
        logger.warning("图片读取失败 (%s): %s", type(e).__name__, ref[:120])
        return None
    mime = sniff_image_mime(raw)
    if mime is None:
        # 魔数不认: 退回扩展名猜测, 仍不行就放弃
        guess = mimetypes.guess_type(ref.split("?")[0])[0] or ""
        if not guess.startswith("image/"):
            logger.warning("内容不是图片, 跳过: %s", ref[:120])
            return None
        mime = guess
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode())


def normalize_messages(messages) -> int:
    """就地改写 messages 里的图片引用为标准 data URI。返回改动条数。"""
    n = 0
    for msg in messages or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for seg in content:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") == "image_url":
                iu = seg.get("image_url")
                url = iu.get("url") if isinstance(iu, dict) else iu
                new = to_data_uri(url)
                if new and new != url:
                    seg["image_url"] = {"url": new}
                    n += 1
            elif seg.get("type") == "image":
                # Anthropic 风格块 (source.base64 / source.url) 兜底
                src = seg.get("source")
                if isinstance(src, dict) and src.get("type") == "base64" and src.get("data"):
                    if not src.get("media_type"):
                        src["media_type"] = "image/png"
                    n += 1
    return n


def _normalize_roles(messages) -> int:
    """OpenAI 新版 developer 角色 → system (Trae 上游只认 system)。

    与 trae/server.js 的 convertMessages 同源 (§5.2), 这里在网关入口再兜一道,
    对所有 provider 生效。
    """
    n = 0
    for m in messages or []:
        if isinstance(m, dict) and m.get("role") == "developer":
            m["role"] = "system"
            n += 1
    return n


def normalize_request(body: dict) -> dict:
    """请求入口统一归一化: developer 角色映射 + 图片引用 → base64 data URI。

    ⚠️ 含阻塞 IO, 调用方须 `await asyncio.to_thread(normalize_request, body)`。
    """
    msgs = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(msgs, list):
        return {"roles": 0, "images": 0}
    roles = _normalize_roles(msgs)
    images = normalize_messages(msgs)
    if roles or images:
        logger.info("请求归一化: developer→system %d 条, 图片转 data URI %d 张", roles, images)
    return {"roles": roles, "images": images}


def upgrade_text_image_paths(messages) -> int:
    """把「当纯文本发进来的本地图片路径」升级成真实图片块 (§5.4, 可选增强)。

    只在文本块内查找, 且只认【本地路径】, 不碰 http(s) URL。
    返回升级的图片数量。
    """
    n = 0
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            segs = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            segs = content
        else:
            continue
        # 已有 image_url 块的消息不再扫描 (客户端已正确发图)
        if any(isinstance(s, dict) and s.get("type") == "image_url" for s in segs):
            continue
        rebuilt = []
        changed = False
        for seg in segs:
            if not isinstance(seg, dict) or seg.get("type") != "text" or not seg.get("text"):
                rebuilt.append(seg)
                continue
            text = seg["text"]
            uris = []
            for m in _IMG_PATH_RE.finditer(text):
                uri = to_data_uri(m.group("path"))
                if uri:
                    uris.append(uri)
            if not uris:
                rebuilt.append(seg)
                continue
            changed = True
            # 图片块排在文本前 (与 TRAE IDE 校验流量同序); 文本保留原路径不动
            for uri in uris:
                rebuilt.append({"type": "image_url", "image_url": {"url": uri}})
            rebuilt.append(seg)
        if changed:
            msg["content"] = rebuilt
            n += sum(1 for s in rebuilt
                     if isinstance(s, dict) and s.get("type") == "image_url")
    return n
