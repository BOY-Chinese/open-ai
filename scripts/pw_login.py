#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pw_login.py —— 浏览器登录的共享底座（Playwright）
=================================================
2026-10 新增三通道（Qoder CN / Qoder 国际 / CodeArts）的登录助手都走「打开浏览器
→ 用户在浏览器里授权 → 脚本拿到凭据写回 config.json」这一套。本模块把其中
**所有和浏览器打交道的部分**收敛到一处，避免每个登录脚本各抄一份、各抄错一点。

与既有 login_trae / login_workbuddy* 的约定保持一致（这是 master 明确要求的）：

  1. **浏览器降级顺序**: chrome-for-testing → chrome → edge
     Playwright 自带的 Chromium 就是「Chrome for Testing」（版本与 playwright
     包锁死，不受系统浏览器自动更新影响，最稳），因而排在第一位；依次降级到
     系统 Chrome、系统 Edge。任一档启动失败自动试下一档。

  2. **每次都是全新 profile**: 目录 `data/pw_profiles/<通道>/run-<时间戳>/`，
     用完即弃。这样**不会带上一次的登录态**（SSO cookie 常驻会让登录页自动
     登录上一个账号，导致加不了第二个账号）。只保留最近一次目录供
     `--reuse-profile` 续跑，更早的自动清理。

  3. **反自动化检测**: 去掉 `--enable-automation`、加
     `--disable-blink-features=AutomationControlled`、隐藏 `navigator.webdriver`。
     用的是**原生 UA**（不覆盖 user_agent）—— 伪造 UA 会和 client-hints 自相矛盾，
     风控正抓这种不一致。

  4. **持久化 context 不隐藏窗口**: headless=False（要用户真的去点授权）。
"""
from __future__ import annotations

import os
import shutil
import sys
import time
import uuid

# 浏览器降级顺序（master 指定）：Chrome for Testing → Chrome → Edge
BROWSER_FALLBACK = ("chromium", "chrome", "msedge")

# 反自动化检测注入：只隐藏 navigator.webdriver 这一条硬特征
STEALTH_JS = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"

_CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
]


def ensure_browsers_path() -> str:
    """让 Playwright 找到随包内嵌的浏览器（便携/打包态必需）。幂等。"""
    try:
        import app_paths as _ap
        return _ap.ensure_playwright_browsers_path()
    except Exception:  # noqa: BLE001
        return ""


def profile_root(channel_key: str, data_dir: str) -> str:
    """该通道的 profile 根目录：<data>/pw_profiles/<通道>"""
    return os.path.join(data_dir, "pw_profiles", channel_key)


def pick_profile_dir(root: str, reuse: bool = False, log=print) -> str:
    """挑本次登录用的 profile 目录（默认**全新**，见模块文档第 2 条）。"""
    os.makedirs(root, exist_ok=True)
    runs = sorted(d for d in os.listdir(root)
                  if d.startswith("run-") and os.path.isdir(os.path.join(root, d)))
    if reuse and runs:
        udd = os.path.join(root, runs[-1])
        log(f"[浏览器] 复用上次登录会话：{udd}")
        return udd
    udd = os.path.join(root, "run-" + time.strftime("%Y%m%d-%H%M%S"))
    try:
        os.makedirs(udd)                       # 先占住目录，并发重跑不撞车
    except OSError:
        udd += "-" + uuid.uuid4().hex[:8]
        os.makedirs(udd, exist_ok=True)
    log(f"[浏览器] 全新登录会话（干净状态，便于添加不同账号）：{udd}")
    for d in runs[:-1]:                        # 只留最近一次旧会话
        shutil.rmtree(os.path.join(root, d), ignore_errors=True)
    return udd


def launch_browser(p, choice: str = "", reuse_profile: bool = False,
                   root: str = "", log=print):
    """启动浏览器（全新 profile + 反自动化），返回 (context, 实际浏览器名)。

    choice 为空按 BROWSER_FALLBACK 顺序自动尝试；指定则先试 choice 再降级其余。
    """
    order = ([choice] if choice else []) + [b for b in BROWSER_FALLBACK if b != choice]
    errs = []
    for name in order:
        udd = pick_profile_dir(root, reuse_profile, log=log)
        try:
            ctx = p.chromium.launch_persistent_context(
                udd,
                headless=False,
                channel=None if name == "chromium" else name,
                locale="zh-CN",
                no_viewport=True,          # 视口跟随窗口，不留自动化痕迹
                args=list(_CHROME_ARGS),
                ignore_default_args=["--enable-automation"],
            )
            ctx.add_init_script(STEALTH_JS)
            log(f"[浏览器] 已启动 {name}（profile: {udd}）")
            return ctx, name
        except Exception as e:  # noqa: BLE001
            errs.append(f"{name} → {type(e).__name__}: {e}")
            log(f"[降级] {name} 启动失败，尝试下一候选 …")
    raise RuntimeError(
        "所有候选浏览器都无法启动:\n  " + "\n  ".join(errs)
        + "\n  提示: 若报 profile 被占用，先关掉上一次登录残留的浏览器窗口")


def first_page(ctx, url: str, log=print, retries: int = 3, timeout_ms: int = 60000):
    """打开目标页（复用持久化 context 自带的空白页，网络抖动时重试）。"""
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    for attempt in range(1, retries + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            return page
        except Exception as e:  # noqa: BLE001
            log(f"[警告] 第 {attempt}/{retries} 次打开页面失败：{type(e).__name__}: {e}")
            if attempt < retries:
                time.sleep(5)
    return None


def tick(page, ms: int = 800) -> bool:
    """推进一次 Playwright 事件循环；窗口被用户关掉时返回 False。

    ★ 必须用 Playwright 自己的等待（而不是 time.sleep）：否则页面事件不会实时
      投递，用户在浏览器里的操作要等到下一次 goto 才被感知。
    """
    try:
        page.wait_for_timeout(ms)
        return True
    except Exception:  # noqa: BLE001
        return False


def close_quietly(ctx) -> None:
    try:
        ctx.close()
    except Exception:  # noqa: BLE001
        pass


def browsers_hint() -> str:
    return ("  排查: 1) Steam++/Watt「网络加速」是否在拦截（可在其界面关掉再试）\n"
            "        2) 系统代理/加速器是否异常\n"
            "        3) 浏览器能否手工打开登录页")


if __name__ == "__main__":  # 自检：只打印顺序，不启动浏览器
    sys.stdout.write("BROWSER_FALLBACK = %s\n" % (BROWSER_FALLBACK,))
