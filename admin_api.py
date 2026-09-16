# -*- coding: utf-8 -*-
"""admin_api.py — 管理面 REST 路由（供桌面端 / 第三方工具调用）

设计原则
--------
* **不重复实现业务逻辑**：账号读写复用 ``scripts/account_manager`` 与
  ``scripts/api_store``，积分/流水复用 ``scripts/credits_api``，
  模型列表直接取运行中的 provider 实例。
* **读写分离**：所有 GET 只读本地 ``config.json`` / ``usage_history.db``，
  不发上游网络请求（毫秒级返回）；需要联网的动作（拉积分、重连）一律走
  POST，由前端显式触发并展示 loading。
* **阻塞隔离**：这些模块都是同步实现（文件 IO + sqlite），故统一经
  ``asyncio.to_thread`` 下放到线程池，绝不阻塞 FastAPI 事件循环。
* **鉴权**：复用网关既有的 ``Authorization: Bearer <api_key>`` 校验，
  与 /v1/* 同一套密钥，不额外引入凭据。

挂载方式（main.py）::

    from admin_api import router as admin_router
    app.include_router(admin_router)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

from fastapi import APIRouter, Body, HTTPException

logger = logging.getLogger("openapi.admin")

router = APIRouter(prefix="/v1/admin", tags=["admin"])

from app_paths import ROOT as BASE, CONFIG_PATH  # 安装根: 见 app_paths.py


def _update_repo() -> str:
    """发布仓库（owner/repo），「检查更新」据此查 latest release。

    取值优先级: 环境变量 OPEN_AI_UPDATE_REPO > version.py 的 UPDATE_REPO >
    占位值。**不把某个人的 GitHub 账号写死在业务代码里** —— 那是发布者的
    身份信息, 换了 fork/组织就要改代码。取不到时检查更新会失败但不会崩,
    只报「无更新」(见 check_update 的兜底)。
    """
    env = (os.environ.get("OPEN_AI_UPDATE_REPO") or "").strip()
    if env:
        return env
    try:
        import sys as _sys
        if BASE not in _sys.path:
            _sys.path.insert(0, BASE)
        from version import UPDATE_REPO as _r  # type: ignore
        if _r:
            return str(_r).strip()
    except Exception:  # noqa: BLE001
        pass
    return "owner/open-ai"


UPDATE_REPO = _update_repo()

# 通道键（对外） → config.json providers 段键（对内）
CHANNEL_TO_PROVIDER = {
    "Trae": "trae",
    "WorkBuddy": "workbuddy",
    "WorkBuddy_IE": "workbuddy_intl",
    "Loomy": "loomy",
}

# 通道键（对外） → 流水库 gain/usage 表的 platform 键
#
# ★ 与 CHANNEL_TO_PROVIDER 是**两套不同的映射**，不要合并：
#   前者指向 config.json 的段名（workbuddy_intl 用下划线），
#   后者指向 credits_api.PLATFORMS 的稳定 key（同样是 workbuddy_intl，
#   但 Trae 是小写 'trae' 而非 'Trae'）。历史上把两者混用会让
#   「按通道查流水」静默返回空结果。
#
# Loomy 已接入流水库（usage_collector.collect_loomy / loomy_gains，
# credits_api.PLATFORMS 有 'loomy' 键）。
CHANNEL_TO_PLATFORM = {
    "Trae": "trae",
    "WorkBuddy": "workbuddy",
    "WorkBuddy_IE": "workbuddy_intl",
    "Loomy": "loomy",
}

# provider 注册键 → 对外通道名
#   注意 workbuddy_intl provider 在注册表里的键是 'workbuddy-intl'（连字符），
#   与 config 段键 'workbuddy_intl'（下划线）不同，两种写法都要能映射到
#   WorkBuddy_IE —— 否则前端 CHANNEL_META[undefined] 会直接崩溃。
PROVIDER_TO_CHANNEL = {
    "trae": "Trae",
    "workbuddy": "WorkBuddy",
    "workbuddy_intl": "WorkBuddy_IE",
    "workbuddy-intl": "WorkBuddy_IE",
    "workbuddyintl": "WorkBuddy_IE",
    "loomy": "Loomy",
}

# 归一化：把任何来源的通道写法收敛到对外值
CHANNEL_ALIASES = {
    "trae": "Trae",
    "workbuddy": "WorkBuddy",
    "workbuddy_ie": "WorkBuddy_IE",
    "workbuddyie": "WorkBuddy_IE",
    "workbuddy_intl": "WorkBuddy_IE",
    "workbuddy-intl": "WorkBuddy_IE",
    "intl": "WorkBuddy_IE",
    "loomy": "Loomy",
}


def normalize_channel(value: str | None) -> str:
    """把任意通道写法归一化为 Trae / WorkBuddy / WorkBuddy_IE。

    未知值原样返回（便于排查），但模型端点会把它映射到上述三值之一。
    """
    v = (value or "").strip()
    return CHANNEL_ALIASES.get(v.lower(), v)

# 对外模型名前缀（与各 provider 实现保持一致）
# Loomy v3.1 起对外前缀 lm-；倍率表同时登记 loomy- 历史前缀，两种写法都能匹配
CHANNEL_PREFIX = {
    "Trae": "tr-",
    "WorkBuddy": "wb-",
    "WorkBuddy_IE": "wbie-",
    "Loomy": "lm-",
}

# Loomy 历史前缀（v3.0 对外名），请求与倍率匹配仍兼容
LOOMY_LEGACY_PREFIX = "loomy-"

# 账号积分余额的内存缓存：{accountId: {credits, workCredits}}
#   GET  只读这里（毫秒级，不联网）
#   POST /accounts/credits/refresh 才真正请求上游并回填
# 进程重启即失效——这是有意的：余额是易变数据，不做跨进程持久化。
_CREDIT_CACHE: dict[str, dict] = {}
_CREDIT_CACHE_TS: float = 0.0

# 登录脚本句柄：{channel: Popen}
#
# 为什么要留句柄：登录脚本是 fire-and-forget 拉起的，但桌面端要把它的输出
# **实时**贴进操作日志（v2.3 GUI 的 `_subprocess_stream` 就是这么做的）。
# 判断「脚本还在跑吗」需要一个句柄 —— 否则只能靠猜（比如看日志文件 mtime），
# 那种推断在「用户把浏览器晾在一边十分钟」时必然误判。
# 进程重启会丢掉句柄：此时 /accounts/login/log 会返回 running=False，
# 前端停止跟随但已读到的输出仍在，不影响使用。
_LOGIN_PROCS: dict[str, Any] = {}


async def _in_thread(fn, *args, **kwargs):
    """把同步实现下放线程池，避免阻塞事件循环。"""
    return await asyncio.to_thread(fn, *args, **kwargs)


# ─────────────────────────── config.json 读写 ───────────────────────────

def _read_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001
        logger.error("读取 config.json 失败: %s", e)
        return {}


def _write_config(cfg: dict) -> None:
    """原子写入（先写临时文件再替换），避免半截文件损坏配置。"""
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def _now() -> float:
    return time.time()


# ─────────────────────────── 账号 ───────────────────────────

def _account_id(channel: str, acc: dict, idx: int) -> str:
    """稳定 id：优先 uid/userId，其次账号名，最后序号。

    Loomy 账号的字段是小写 userid（讯飞协议），一并纳入。
    """
    raw = str(acc.get("uid") or acc.get("userId") or acc.get("userid")
              or acc.get("name") or idx)
    return f"{channel}:{raw}"


def _account_status(acc: dict) -> str:
    """三态：enabled / disabled / disconnected。

    连接态判定依据（不联网）：有可用于上游调用的凭据即视为已连接。
      trae        → token
      workbuddy*  → accessToken 或 refreshToken（可自动刷新）
      loomy       → session（桌面账号）或 cookies（Web 账号）
                    —— 鉴权整改后两者都是「本账号自己的登录态」，平等可用
    """
    if acc.get("enabled", True) is False:
        return "disabled"
    has_cred = bool(acc.get("token") or acc.get("accessToken")
                    or acc.get("refreshToken") or acc.get("cookie")
                    or acc.get("session") or acc.get("cookies"))
    return "enabled" if has_cred else "disconnected"


def _load_accounts_raw() -> list[dict]:
    """读取三通道账号，产出前端 Account[] 形状（不含联网积分）。

    Loomy 特例（2026-09-14 整合）：同一手机号的「桌面账号（session）」与
    「Web 账号（cookies）」在 config 里是两条记录，但它们是同一个人，
    界面合并成**一行** —— 行 id/名字用桌面账号的（`Loomy(手机号)`，
    能鉴权对话的那条）；积分/签到的数据源仍走 Web 账号（见
    refresh_account_credits / accounts_signin）。
    """
    cfg = _read_config()
    prov = cfg.get("providers") or {}
    out: list[dict] = []
    for channel, pkey in CHANNEL_TO_PROVIDER.items():
        seg = prov.get(pkey) or {}
        if channel == "Loomy":
            for phone, g in _loomy_groups(seg.get("accounts") or []).items():
                d, w = g.get("desktop"), g.get("web")
                if d:
                    idx, acc = d
                    rid, name = _account_id("Loomy", acc, idx), \
                        acc.get("name") or f"Loomy({phone})"
                else:
                    idx, acc = w
                    rid, name = _account_id("Loomy", acc, idx), \
                        acc.get("name") or f"Loomy Web({phone})"
                status, enabled = _loomy_row_state(g)
                out.append({
                    "id": rid,
                    "channel": channel,
                    "name": name,
                    "enabled": enabled,
                    "status": status,
                    "credits": 0,
                    "workCredits": 0,
                    "refreshedAt": 0,
                })
            continue
        for i, acc in enumerate(seg.get("accounts") or []):
            uid = str(acc.get("uid") or acc.get("userId") or acc.get("userid") or "")
            name = acc.get("name") or (uid or f"#{i + 1}")
            out.append({
                "id": _account_id(channel, acc, i),
                "channel": channel,
                "name": name,
                "enabled": acc.get("enabled", True) is not False,
                "status": _account_status(acc),
                # credits / workCredits 由「刷新积分」接口填充（需联网）
                "credits": 0,
                "workCredits": 0,
                "refreshedAt": 0,
            })
    return out


def _loomy_groups(accs: list[dict]) -> "dict[str, dict]":
    """Loomy 账号按手机号分组：{phone: {"desktop": (i, acc)|None, "web": ...}}。

    桌面账号有 session（能鉴权对话），Web 账号是 cookies（仅 /web/api/*）；
    同手机号的两条是同一个人，界面层合并为一行。
    """
    groups: dict[str, dict] = {}
    for i, acc in enumerate(accs):
        is_web = acc.get("kind") == "web" or \
            (not acc.get("session") and acc.get("cookies"))
        phone = str(acc.get("phone") or acc.get("userid") or f"#{i + 1}")
        g = groups.setdefault(phone, {"desktop": None, "web": None})
        g["web" if is_web else "desktop"] = (i, acc)
    return groups


def _loomy_row_state(g: dict) -> tuple[str, bool]:
    """合并行的 (status, enabled)：任一半边启用即算启用；
    连接态看还有没有可用凭据（桌面 session 或 Web cookies）。"""
    halves = [h for h in (g.get("desktop"), g.get("web")) if h]
    if not halves:
        return "disconnected", False
    enabled = any(acc.get("enabled", True) is not False for _, acc in halves)
    if not enabled:
        return "disabled", False
    for _, acc in halves:
        if _account_status(acc) == "enabled":
            return "enabled", True
    return "disconnected", True


def _loomy_group_credits(lc, g: dict) -> tuple[float | None, str]:
    """查一个 Loomy 合并组的积分总额。返回 (total|None, err)。

    数据源优先 **Web 会话**（master 指定：积分/签到走 Web 通道）——
    `/web/api/auth/points-summary` 返回 {permanent, daily}；
    Web 不可用（无 cookie/会话失效）再回退桌面 session 打
    `/api/v1/points/records`（字段 balance/dailyBalance/availableBalance，
    ⚠️ 与 first-login 的 permanentBalance 不同名，认错会丢一半，踩过）。
    """
    d, w = g.get("desktop"), g.get("web")
    if w and w[1].get("enabled", True) is not False and w[1].get("cookies"):
        ok, r = lc.web_request_json("GET", "/web/api/auth/points-summary",
                                    w[1].get("cookies") or {})
        if ok and isinstance(r, dict):
            data = r.get("data") or {}
            return float(data.get("permanent") or 0) + float(data.get("daily") or 0), ""
        last_web_err = str(r)[:120]
    else:
        last_web_err = "Web 会话不可用"
    if d and d[1].get("enabled", True) is not False:
        token = str(d[1].get("session") or "").strip()
        if token:
            ok, r = lc.query_points(token)
            if ok and isinstance(r, dict) and r.get("code") == "000000":
                data = r.get("data") or {}
                total = float(data.get("availableBalance") or 0)
                if not total:
                    total = float(data.get("balance") or 0) + \
                        float(data.get("dailyBalance") or 0)
                return total, ""
            return None, str(r)[:120]
    return None, last_web_err


def _run_blocking(coro):
    """在线程里安全地执行一个协程（线程池线程内没有事件循环）。

    用途：`_in_thread` 的 worker 里复用另一个 async 端点 —— 直接 await 不行
    （worker 是同步函数），`asyncio.run` 在已有 loop 的线程里会报错，
    线程池线程恰好没有 loop，用 asyncio.run 最干净。
    """
    return asyncio.run(coro)


# ─────────────────── 签到补签（供 POST /accounts/signin/refresh 使用） ───────

def _signin_argv(script_name: str, extra: list[str]) -> list[str]:
    """拼出「跑一个内置脚本」的完整命令行 —— **必须按安装形态分叉**。

    打包态（PyInstaller）里安装包**不含 `scripts\\*.py` 源码**，也没有独立
    解释器，故不能拿 `os.path.exists(脚本)` 当门槛（现存实现踩过这个坑：
    安装后签到/重连静默失效）。正确做法与 `launch_login` / Broker 定时任务
    一致：把脚本路径当「路由标记」传给 task shim，由 `task_main` 按文件名
    路由到内置模块。路径参数本身不要求存在。

    源码态则用项目自带 `.venv`，缺失时回落到当前解释器。
    """
    import sys as _sys
    script = os.path.join(BASE, "scripts", script_name)
    if bool(getattr(_sys, "frozen", False)):
        return [os.path.join(BASE, "open-ai-task.exe"), script] + list(extra)
    if not os.path.exists(script):
        raise HTTPException(status_code=501, detail=f"{script_name} 不存在")
    exe = os.path.join(BASE, ".venv", "Scripts", "python.exe")
    return [exe if os.path.exists(exe) else _sys.executable, script] + list(extra)


def _run_signin_script(extra: list[str], timeout: int = 180) -> tuple[int, str]:
    """同步跑一次 signin_all.py（幂等脚本），返回 (退出码, 尾部输出)。

    退出码语义（见 signin_all.main）：0 = TRAE 已签上/今日已签；1 = TRAE 仍
    繁忙 —— **不是错误**，只是本轮没抢到，故调用方只把它记进 errors 供展示，
    不抛异常。超时按失败处理（脚本内部最坏会等 2 次 9074 重试）。
    """
    import subprocess
    argv = _signin_argv("signin_all.py", extra)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        p = subprocess.run(  # noqa: S603
            argv, cwd=BASE, capture_output=True, text=True, timeout=timeout,
            creationflags=flags, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return -1, f"超时（>{timeout}s）"
    except Exception as e:  # noqa: BLE001
        return -1, repr(e)[:200]
    tail = (p.stdout or "").strip().splitlines()[-3:]
    return p.returncode, " / ".join(tail)[:300]


def _signin_targets(channel: str | None, signin: dict) -> dict:
    """当前配置里「今日还没签到」的账号 → 决定补签要跑哪些脚本。

    返回 {wb, trae, pending}：wb/trae 是「该通道存在未签账号」的布尔量
    （决定要不要拉起 `--wb-only` / `--trae-only`），pending 是这些账号的
    **id 列表**（与 GET /accounts/signin 的 key 同构，供前端复核补签结果）。

    WorkBuddy 国际版恒不计入 —— 官方无签到渠道（2026-09-13 确认），
    补签只会白打一次状态探测。

    判定依据是**本地凭证的缺席**（GET /accounts/signin 的语义），因此
    脚本本身仍需自行判定「已签/未参与」，重复调用无副作用。
    """
    cfg = _read_config()
    prov = cfg.get("providers") or {}
    out = {"wb": False, "trae": False, "pending": []}
    for ch, pkey in CHANNEL_TO_PROVIDER.items():
        if channel and ch != channel:
            continue
        if ch == "WorkBuddy_IE":
            continue  # 无签到渠道
        seg = prov.get(pkey) or {}
        accounts = seg.get("accounts") or []
        if ch == "Loomy":
            # Loomy 的补领挂在 --wb-only 里（见 signin_all.main 的 Loomy 补签段），
            # 按手机号合并行判定，与界面行 id 对齐。
            # ⚠ _loomy_groups 的 value 是 (序号, 账号 dict) 元组，不是裸 dict ——
            #   解包取 [1] 才拿得到账号（同 accounts_signin / refresh_account_credits）。
            for phone, g in _loomy_groups(accounts).items():
                d, w = g.get("desktop"), g.get("web")
                half = d or w
                if not half:
                    continue
                acc = half[1]
                rid = _account_id(ch, acc, half[0])
                # ★ 必须用 `in` 判存在，不能用 `.get(rid)` 判真假 —— 签到表的
                #   value 在调用方裁剪后可能是 `{}`（空 dict 是假值），用真假判定
                #   会把「已签到」误判成「未签到」，于是每次刷新都白跑一遍补签。
                if rid in signin:
                    continue
                out["wb"] = True
                # pending 里放**账号 id**（不是手机号/展示名）：前端拿它去签到表
                # 里复核「补签后是否签上了」，必须与 GET /accounts/signin 的
                # key 同构才能对得上表。
                out["pending"].append(rid)
            continue
        for i, acc in enumerate(accounts):
            if acc.get("enabled", True) is False:
                continue
            rid = _account_id(ch, acc, i)
            if rid in signin:  # 同上：签到表 value 可能是 {}，不能按真假判
                continue
            if ch == "Trae":
                out["trae"] = True
            else:
                out["wb"] = True
            out["pending"].append(rid)
    return out


def _history_accounts():
    """取本地流水库里的账号（uid → 显示名）与最近积分快照。"""
    try:
        import sys
        if os.path.join(BASE, "scripts") not in sys.path:
            sys.path.insert(0, os.path.join(BASE, "scripts"))
        import credits_api  # type: ignore
        return credits_api.accounts(), credits_api
    except Exception as e:  # noqa: BLE001
        logger.warning("credits_api 不可用: %s", e)
        return None, None


@router.get("/accounts")
async def list_accounts(channel: str | None = None):
    """账号列表（只读本地配置，毫秒级）。

    channel: Trae / WorkBuddy / WorkBuddy_IE，缺省返回全部。
    """
    rows = await _in_thread(_load_accounts_raw)
    if channel and channel != "all":
        rows = [r for r in rows if r["channel"] == channel]
    return {"accounts": rows, "total": len(rows), "ts": _now()}


@router.get("/accounts/credits")
async def accounts_credits(channel: str | None = None):
    """账号积分快照（读内存缓存，**不联网**，毫秒级）。

    缓存由 POST /accounts/credits/refresh 填充（那一步才真正请求上游）。
    从未刷新过时返回空对象，前端按 0 展示并提示「点刷新获取」。
    """
    data = dict(_CREDIT_CACHE)
    if channel and channel != "all":
        data = {k: v for k, v in data.items() if k.startswith(f"{channel}:")}
    return {"credits": data, "ts": _now(),
            "cached": bool(_CREDIT_CACHE), "refreshedAt": _CREDIT_CACHE_TS}


@router.get("/accounts/signin")
async def accounts_signin(channel: str | None = None):
    """今日签到结果（**只读本地流水库，不联网**，毫秒级）。

    语义：回答「该账号今天签到成功了吗」，供账号管理页的「每日签到」列展示。
    证据取自 `data/usage_history.db` 的 gain 表 —— 有当日入账记录即签到成功
    （TRAE 写 `checkin`，WorkBuddy 系写官方 checkin-activity-status 的
    today_checked_in 或当日入账的资源包，注册/订阅类礼包已排除）。
    这是**客观入账凭证**，比问上游签到接口可靠。

    注：WorkBuddy 国际版无签到渠道（2026-09-13 官方确认，服务端对国际账号
    恒报 active=false），gain 表天然无记录 → 前端按「无渠道」渲染「—」。

    Loomy：不进流水库（usage_collector 不采集），其凭证来自
    `data/loomy_signin_state.json` —— signin_all 每日领取（Web 账号 = 登录
    校验 + 积分摘要；桌面账号 = first-login 领取接口）成功后写入的按天缓存，
    回答「今天领没领到」同样有本地凭证。领取失败/未运行则该账号缺席，
    前端按「未签到」渲染。amount 取当日每日额度（dailyBalance）。

    返回 {signin: {<accountId>: {checkedIn, amount, kinds, ts}}, day, updatedAt}
    —— key 与 GET /accounts 的 account id 同构（`通道:uid`），前端可直接对表。
    """
    def _work() -> dict:
        _, ca = _history_accounts()
        day = ca.day_str(time.time()) if ca is not None else time.strftime("%Y-%m-%d")
        by_platform = ca.gains_accounts(day) if ca is not None else {}
        out: dict[str, dict] = {}
        # Loomy gain 表的 uid（桌面 userid / web:<phone>）要映射到合并行 id ——
        # 否则同一个人的凭证会以两个 key 出现，界面长出第二行
        loomy_rid_by_uid: dict[str, str] = {}
        if not channel or channel == "all" or channel == "Loomy":
            try:
                accs = ((_read_config().get("providers") or {})
                        .get("loomy") or {}).get("accounts") or []
                for phone, g in _loomy_groups(accs).items():
                    d, w = g.get("desktop"), g.get("web")
                    rid = _account_id("Loomy", (d or w)[1], (d or w)[0])
                    for half in (d, w):
                        if half:
                            loomy_rid_by_uid[str(half[1].get("userid") or "")] = rid
            except Exception as e:  # noqa: BLE001
                logger.warning("loomy 合并行映射失败: %s", e)
        for ch, platform in CHANNEL_TO_PLATFORM.items():
            if channel and channel != "all" and ch != channel:
                continue
            for uid, info in (by_platform.get(platform) or {}).items():
                # Loomy 的映射目标已是完整合并行 id（`Loomy:<userid>`），
                # 不能再套一层 `{ch}:` 前缀（会得到 Loomy:Loomy:...）
                if ch == "Loomy" and str(uid) in loomy_rid_by_uid:
                    key = loomy_rid_by_uid[str(uid)]
                else:
                    key = f"{ch}:{uid}"
                out[key] = {
                    "checkedIn": True,
                    "amount": info["amount"],
                    "kinds": info["kinds"],
                    "ts": info["ts"],
                }

        # Loomy: 读按天状态缓存（loomy_client 写、这里只读，保持本端点零联网）。
        # 缓存按 userid 记（web:<phone> / 桌面 userid），但界面是合并行 ——
        # 把同手机号两条记录的凭证都挂到合并行的 id 上（master 指定：
        # 签到/积分走 Web 通道），Web 缺席时用桌面自己的凭证兜底。
        if not channel or channel == "all" or channel == "Loomy":
            try:
                lc = _loomy_client()
                state = lc.load_daily_state(day) or {}
                cfg = _read_config()
                accs = ((cfg.get("providers") or {}).get("loomy") or {}) \
                    .get("accounts") or []
                for phone, g in _loomy_groups(accs).items():
                    d, w = g.get("desktop"), g.get("web")
                    rid = _account_id("Loomy", (d or w)[1], (d or w)[0])
                    # Web 凭证优先；同组桌面自己的记录作为兜底
                    entries = []
                    for half in (w, d):
                        if not half:
                            continue
                        uid = str(half[1].get("userid") or "")
                        st = state.get(uid)
                        if isinstance(st, dict) and st.get("claimed"):
                            entries.append(st)
                    if not entries:
                        continue  # 该行今天没有任何领取凭证 → 前端按「未签到」
                    best = entries[0]
                    amount = 0.0
                    for k in ("dailyBalance", "currentBalance"):
                        try:
                            v = best.get(k)
                            if v is not None:
                                amount = max(amount, float(v))
                                break
                        except (TypeError, ValueError):
                            continue
                    kinds = ["daily-login"]
                    if best.get("alreadyProcessed"):
                        kinds = ["daily-login(已领过)"]
                    out[rid] = {
                        "checkedIn": True,
                        "amount": amount,
                        "kinds": kinds,
                        "ts": int(best.get("ts") or 0),
                    }
            except Exception as e:  # noqa: BLE001
                logger.warning("loomy 签到状态缓存读取失败: %s", e)

        return {"signin": out, "day": day, "updatedAt": _now()}

    return await _in_thread(_work)


@router.post("/accounts/signin/refresh")
async def refresh_account_signin(payload: dict = Body(default={})):
    """对「今日尚未签到」的账号补一次签到（联网，幂等），再回读签到表。

    为什么需要这个端点
    ------------------
    界面上的「刷新账号」按钮原先只做两件事：拉余额（credits/refresh）+ 重读
    签到表 —— 两件事都是**读**。于是「今天还没签到的账号」在用户点刷新后
    依然是未签到，只能干等 Broker 每 30 分钟一轮的 `signin_all.py --wb-only`
    补签；用户看到的却是「我刚点过刷新了，怎么还是没签到」。

    实现复用既有脚本，不重复实现协议：
      * WorkBuddy 系（含国际版）：`signin_all.py --wb-only`
        —— 内部用官方 checkin-activity-status 判定 today_checked_in/active，
           幂等（已签/未参与直接返回），只在 WB 通道被请求时拉起；
      * TRAE：`signin_all.py --trae-only`
        —— 状态查询 + claim，**不做 token 续期**（force 续期会重写
           config.json 里的 token，正是「重新连接」按钮的职责，刷新不该顺带做）；
      * Loomy：由 `--wb-only` 内的一段补领逻辑覆盖（只挑今日未领取的账号）。

    取舍：默认**只对未签到的账号补齐**（用 GET /accounts/signin 判定），
    force=true 时无条件全量跑一遍（供用户显式要求「重新签到」）。
    已有今日凭证时不联网，界面点刷新不会白等一次上游往返。

    返回 {ok, channel, ran: [脚本标签], pending: [...], signin, day, updatedAt}
    —— 直接带上刷新后的签到表，调用方无需再发一次 GET。
    """
    channel = payload.get("channel")
    force = bool(payload.get("force"))

    if channel and channel != "all":
        ch_norm = normalize_channel(channel)
        if ch_norm not in CHANNEL_TO_PROVIDER:
            raise HTTPException(status_code=400, detail=f"未知通道 {channel}")
    else:
        ch_norm = None

    def _work() -> dict:
        signin = (_run_blocking(accounts_signin(channel)) or {}).get("signin") or {}
        targets = _signin_targets(ch_norm, signin)

        ran: list[str] = []
        # force：无条件重跑本通道的签到（用户显式要求），忽略 pending 判定
        if force or targets["wb"]:
            ran.append("--wb-only")
        if force or targets["trae"]:
            ran.append("--trae-only")

        errors: list[str] = []
        for flag in ran:
            rc, err = _run_signin_script([flag])
            if rc != 0:
                errors.append(f"signin_all.py {flag} 退出码 {rc}{('：' + err) if err else ''}")

        # 重新读一次：签到结果落在流水库 / loomy 状态缓存里，必须回读才有意义
        after = (_run_blocking(accounts_signin(channel)) or {})
        return {
            "ok": True,
            "channel": channel or "all",
            "force": force,
            "ran": ran,
            "pending": targets["pending"],
            "errors": errors[:10],
            "signin": after.get("signin") or {},
            "day": after.get("day") or "",
            "updatedAt": _now(),
        }

    return await _in_thread(_work)


@router.post("/accounts/credits/refresh")
async def refresh_account_credits(payload: dict = Body(default={})):
    """联网拉取各账号真实积分余额（通用 / Work 双池）。

    实现说明：
      * 复用 account_manager 的 trae_credits / wb_credits（与 GUI「刷新积分」
        同源，不重复实现协议）；
      * 逐账号串行 + 单账号超时，失败仅记录不抛出（部分账号失效不应让整
        个刷新失败）；
      * 结果写入内存缓存，随后的 GET /accounts 即可带上真实余额。
    """
    channel = payload.get("channel")

    def _work() -> dict:
        import sys
        if os.path.join(BASE, "scripts") not in sys.path:
            sys.path.insert(0, os.path.join(BASE, "scripts"))
        import account_manager as am  # type: ignore

        cfg = _read_config()
        prov = cfg.get("providers") or {}
        result: dict[str, dict] = {}
        errors: list[str] = []

        for ch, pkey in CHANNEL_TO_PROVIDER.items():
            if channel and channel != "all" and ch != channel:
                continue
            seg = prov.get(pkey) or {}
            accounts = seg.get("accounts") or []
            domain = seg.get("domain") or ""
            product = seg.get("product") or ""
            default_dev = seg.get("device_id") or ""
            # Loomy：按手机号合并成一行后再刷新（同手机号的桌面+Web 是同一个人，
            # 积分只有一份），缓存键 = 界面行的 id，与 GET /accounts 直接对表。
            if ch == "Loomy":
                lc = _loomy_client()
                for phone, g in _loomy_groups(accounts).items():
                    d, w = g.get("desktop"), g.get("web")
                    rid = _account_id(ch, (d or w)[1], (d or w)[0])
                    total, err = _loomy_group_credits(lc, g)
                    if err and total is None:
                        errors.append(f"{rid}: {err}")
                        continue
                    result[rid] = {"credits": float(total or 0), "workCredits": 0.0}
                continue
            for i, acc in enumerate(accounts):
                aid = _account_id(ch, acc, i)
                try:
                    if ch == "Trae":
                        dev = acc.get("device_id") or default_dev
                        g, w, err = am.trae_credits(acc, dev)
                        if err:
                            errors.append(f"{aid}: {err}")
                            continue
                        result[aid] = {"credits": float(g or 0),
                                       "workCredits": float(w or 0)}
                    else:
                        total, err = am.wb_credits(acc, domain, product)
                        if err:
                            errors.append(f"{aid}: {err}")
                            continue
                        result[aid] = {"credits": float(total or 0),
                                       "workCredits": 0.0}
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{aid}: {e!r}")

        # 合并进缓存（只覆盖本次刷新到的账号）
        _CREDIT_CACHE.update(result)
        global _CREDIT_CACHE_TS
        _CREDIT_CACHE_TS = _now()
        return {"ok": True, "refreshed": len(result),
                "errors": errors[:20], "ts": _CREDIT_CACHE_TS}

    return await _in_thread(_work)


@router.post("/accounts/toggle")
async def toggle_account(payload: dict = Body(...)):
    """启用/关闭账号（写 config.json）。body: {id}

    Loomy 合并行：id 对应同手机号的桌面+Web 两条记录，开关同时作用于
    两条（积分走 Web、对话走桌面，只切一边会让行状态永远对不上）。
    """
    acc_id = str(payload.get("id") or "")
    if ":" not in acc_id:
        raise HTTPException(status_code=400, detail="id 格式应为 '<channel>:<uid>'")
    channel, raw = acc_id.split(":", 1)

    def _work() -> dict:
        cfg = _read_config()
        pkey = CHANNEL_TO_PROVIDER.get(channel)
        if not pkey:
            raise HTTPException(status_code=400, detail=f"未知通道 {channel}")
        seg = (cfg.get("providers") or {}).get(pkey) or {}
        accs = seg.get("accounts") or []
        if channel == "Loomy":
            for phone, g in _loomy_groups(accs).items():
                d, w = g.get("desktop"), g.get("web")
                if _account_id(channel, (d or w)[1], (d or w)[0]) != acc_id:
                    continue
                new_enabled = None
                for half in (d, w):
                    if not half:
                        continue
                    idx, acc = half
                    if new_enabled is None:
                        # 以「行当前状态」取反：任一半边启用即视为启用
                        new_enabled = not any(
                            a.get("enabled", True) is not False
                            for _i, a in (d, w) if a)
                    acc["enabled"] = new_enabled
                _write_config(cfg)
                return {"ok": True, "id": acc_id, "enabled": bool(new_enabled)}
            raise HTTPException(status_code=404, detail="账号不存在")
        for i, acc in enumerate(accs):
            if _account_id(channel, acc, i) == acc_id:
                acc["enabled"] = acc.get("enabled", True) is False
                _write_config(cfg)
                return {"ok": True, "id": acc_id, "enabled": acc["enabled"]}
        raise HTTPException(status_code=404, detail="账号不存在")

    return await _in_thread(_work)


@router.post("/accounts/delete")
async def delete_account(payload: dict = Body(...)):
    """删除账号（写 config.json）。body: {id}

    Loomy 合并行：同手机号的桌面+Web 两条记录一并删除
    （它们是同一个人，留半边会在界面重新长出一行孤儿账号）。
    """
    acc_id = str(payload.get("id") or "")
    if ":" not in acc_id:
        raise HTTPException(status_code=400, detail="id 格式应为 '<channel>:<uid>'")
    channel, _raw = acc_id.split(":", 1)

    def _work() -> dict:
        cfg = _read_config()
        pkey = CHANNEL_TO_PROVIDER.get(channel)
        if not pkey:
            raise HTTPException(status_code=400, detail=f"未知通道 {channel}")
        seg = (cfg.get("providers") or {}).get(pkey) or {}
        accs = seg.get("accounts") or []
        drop: set[int] = set()
        if channel == "Loomy":
            for phone, g in _loomy_groups(accs).items():
                d, w = g.get("desktop"), g.get("web")
                if _account_id(channel, (d or w)[1], (d or w)[0]) == acc_id:
                    drop = {d[0] for d in (d,) if d} | {w[0] for w in (w,) if w}
                    break
        else:
            for i, acc in enumerate(accs):
                if _account_id(channel, acc, i) == acc_id:
                    drop = {i}
                    break
        if not drop:
            raise HTTPException(status_code=404, detail="账号不存在")
        keep = [a for i, a in enumerate(accs) if i not in drop]
        seg["accounts"] = keep
        cfg.setdefault("providers", {})[pkey] = seg
        _write_config(cfg)
        return {"ok": True, "removed": acc_id, "remaining": len(keep)}

    return await _in_thread(_work)


@router.post("/accounts/login")
async def launch_login(payload: dict = Body(default={})):
    """拉起交互式登录窗口（扫码 / OIDC）。

    添加账号无法由 HTTP 静默完成——登录必须由用户在浏览器/客户端交互。
    本端点只负责把对应的登录脚本拉起来（无控制台窗口），登录成功后
    config.json 会被脚本写入，前端刷新列表即可看到新账号。
    """
    channel = str(payload.get("channel") or "")
    script_name = str(payload.get("script") or "")
    allowed = {
        "Trae": "login_trae.py",
        "WorkBuddy": "login_workbuddy.py",
        "WorkBuddy_IE": "login_workbuddy_intl.py",
    }
    if channel not in allowed:
        raise HTTPException(status_code=400, detail=f"未知通道 {channel}")
    script_name = script_name or allowed[channel]
    if script_name not in allowed.values():
        raise HTTPException(status_code=400, detail="不允许执行该脚本")

    def _work() -> dict:
        import subprocess
        import sys
        script = os.path.join(BASE, "scripts", script_name)
        frozen = bool(getattr(sys, "frozen", False))
        if not frozen and not os.path.exists(script):
            raise HTTPException(status_code=501,
                                detail=f"{script_name} 不存在（该通道未提供登录脚本）")
        if frozen:
            # ★ 打包态: 安装包不含 scripts\*.py, 也没有独立 python —— 与
            #   signin/usage 同机制, 把脚本路径当「路由标记」传给 task exe,
            #   由 task_main 按文件名路由到内置 login_* 模块 (playwright 驱动
            #   已随包; 浏览器用包内自带的 Chromium (v2.6 起, 见 login_*
            #   文档头 —— 不再拉系统 Edge), 用户机无需另装)。路径参数本身
            #   不要求存在, 不可用 os.path.exists 拦截 (app_paths 的老警告)。
            exe = os.path.join(BASE, "open-ai-task.exe")
            cmd = [exe, script]
        else:
            exe = os.path.join(BASE, ".venv", "Scripts", "python.exe")
            if not os.path.exists(exe):
                exe = sys.executable
            cmd = [exe, script]

        # ★ 必须 CREATE_NO_WINDOW：登录脚本本身只在终端里打印进度，
        #   真正的交互发生在它打开的浏览器里（扫码/密码）。原实现用
        #   CREATE_NEW_CONSOLE，于是「点添加账号就弹出一个黑终端」——
        #   用户实测反馈的正是这个（脚本并不需要那个窗口）。
        #
        #   代价是看不到脚本输出，因此把它重定向到 logs/login_<通道>.log，
        #   否则登录失败时用户和开发者都无从下手。
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        logs_dir = os.path.join(BASE, "logs")
        try:
            os.makedirs(logs_dir, exist_ok=True)
            log_path = os.path.join(logs_dir, f"login_{channel}.log")
            # ★ 先量出当前文件长度再打开：这是本次登录输出的**起始偏移**。
            #   日志文件是追加写的，历次登录的输出都在里面；前端从该偏移开始跟随，
            #   才只会看到本次脚本的输出，而不是把上次登录的旧输出当成新的。
            log_offset = os.path.getsize(log_path) if os.path.exists(log_path) else 0
            logf = open(log_path, "ab", buffering=0)
        except Exception:  # noqa: BLE001
            logf, log_path, log_offset = subprocess.DEVNULL, "", 0

        proc = subprocess.Popen(cmd, cwd=BASE,  # noqa: S603
                                creationflags=flags,
                                stdin=subprocess.DEVNULL,
                                stdout=logf, stderr=subprocess.STDOUT)
        # 留句柄供 /accounts/login/log 判断「还在跑吗」（同通道重复登录则替换旧的）
        _LOGIN_PROCS[channel] = proc
        return {"ok": True, "launched": script_name, "channel": channel,
                "logFile": log_path, "logOffset": log_offset, "pid": proc.pid}

    return await _in_thread(_work)


@router.get("/accounts/login/log")
async def login_log(channel: str, offset: int = 0, maxBytes: int = 131072):
    """增量读取登录脚本输出（供桌面端把脚本输出实时贴进操作日志）。

    参数
    ----
    channel : Trae / WorkBuddy / WorkBuddy_IE
    offset  : 上次读到的字节偏移；首次跟随用 POST /accounts/login 返回的 logOffset
    maxBytes: 单次最多返回多少字节（默认 128KB），防止一次拉回整份历史日志

    返回 ``{text, offset, size, running, exists, exitCode}``：
      * ``text``      —— 自 offset 起的新增内容（UTF-8 解码，坏字节用替换字符兜底）
      * ``offset``    —— 下次该传的偏移（= 本次读到的位置）
      * ``running``   —— 脚本是否仍在运行（前端据此决定继续跟随还是收尾）
      * ``exitCode``  —— 已结束时的退出码（None 表示仍在跑或句柄已丢失）

    注意 ``offset`` 是**字节**偏移，不是字符数：日志文件是 `open(path, "ab")`
    追加写的，按字节定位才不会在中文内容上错位。
    """
    def _work() -> dict:
        path = os.path.join(BASE, "logs", f"login_{channel}.log")
        proc = _LOGIN_PROCS.get(channel)
        running = proc is not None and proc.poll() is None
        exit_code = None if (proc is None or running) else proc.returncode

        if not os.path.exists(path):
            return {"text": "", "offset": 0, "size": 0, "running": running,
                    "exists": False, "exitCode": exit_code}

        size = os.path.getsize(path)
        start = max(0, int(offset))
        # 文件被外部清空/轮转时 offset 会越过文件尾：夹回 0，否则永远读不到新内容
        if start > size:
            start = 0
        cap = max(1, min(int(maxBytes), 4 * 1024 * 1024))
        with open(path, "rb") as f:
            f.seek(start)
            chunk = f.read(cap)
        new_offset = start + len(chunk)
        return {
            "text": chunk.decode("utf-8", errors="replace"),
            "offset": new_offset,
            "size": size,
            # 还有积压时本轮不算读完，让前端立刻再拉一次而不是等下一个轮询周期
            "running": running or new_offset < size,
            "exists": True,
            "exitCode": exit_code,
        }

    return await _in_thread(_work)


@router.post("/accounts/refresh")
async def refresh_accounts(payload: dict = Body(default={})):
    """刷新账号状态（重读配置；联网拉积分走 /accounts/credits/refresh）。"""
    data = await list_accounts(payload.get("channel"))
    return {**data, "refreshed": True}


@router.post("/accounts/reconnect")
async def reconnect_accounts(payload: dict = Body(default={})):
    """重新连接账号：拉起重连脚本 signin_all.py（异步、不等待）。

    ★ 命令行走 {@link _signin_argv} —— **这一条曾经漏改，用户机上表现为
      「重新连接账号」秒失败：`ApiError: signin_all.py 不存在`（耗时 41ms）**。

      打包态（安装包）里没有 scripts 目录下的 .py 源码，也没有独立解释器，所以：
        · 拿 `os.path.exists(scripts/signin_all.py)` 当门槛 → 装机后必 501；
        · 拿 .venv/Scripts/python.exe 当解释器 → 那个目录在装机后也不存在。
      正确做法与签到/采集/登录脚本完全一致：把脚本路径当**路由标记**交给
      task shim（`open-ai-task.exe`），由 `task_main._route` 按文件名路由到
      内置模块；路径参数本身不要求存在。

      源码态仍用项目自带 `.venv`，缺失时回落当前解释器 —— 两种形态同一套
      行为，且不再有「只有装机后才炸」的分支（见 tests/test_frozen_script_launch.py）。
    """
    def _work() -> dict:
        import subprocess
        argv = _signin_argv("signin_all.py", [])
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        # 输出无需重定向：signin_all 自己就往 logs/signin.log 追加（见其 LOG()），
        # 这里只要把它拉起来即可（异步、不等待）。
        proc = subprocess.Popen(argv, cwd=BASE, creationflags=flags,  # noqa: S603
                                stdin=subprocess.DEVNULL)
        return {"ok": True, "started": True, "pid": proc.pid}

    return await _in_thread(_work)


# ─────────────────────────── 模型 ───────────────────────────

# 从「x0.05」「x2.20 credits」「0.8」这类文案里抽出数字
_CREDIT_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _norm_rate_key(name: Any) -> str:
    """倍率表键归一化：去掉上游的 `__dev` 开发环境后缀并转小写。

    上游在不同接口里对同一模型的命名并不一致，实测：
        模型目录  kimi-k3                倍率表  kimi-k3__dev
        模型目录  qwen3.8-flash          倍率表  qwen3.8-flash__dev
        模型目录  glm-5.2                倍率表  GLM-5.2（大小写不同）
    只做精确匹配就会出现「列表里有、倍率表里查不到」的行，前端显示 0。
    """
    s = str(name or "").strip()
    if s.lower().endswith("__dev"):
        s = s[:-5]
    return s.lower()


def _parse_credit_value(raw: Any) -> float | None:
    """把上游返回的倍率转成数字；无法解析返回 None（调用方跳过该条）。

    ★ 为什么不能直接 float()：上游三个通道返回的格式并不统一，实测有
        Trae        0.8                 （已是数字）
        WorkBuddy   'x0.05'             （前缀 x）
        WorkBuddy   'x2.20 credits'     （前缀 x + 单位）
        未提供      None / ''
      `float('x0.05')` 会抛 ValueError，而原实现是 `except: continue` ——
      **整条记录被静默丢弃**，倍率表里于是只剩那些 credits 为 None 的条目
      （全被算成 0），前端「积分倍率」列因此整列为 0（本机实测 bug）。
      改为「抽出字符串里第一个数字」，前缀与单位都不再影响结果。
    """
    if raw is None:
        # 上游没给倍率：按 0 记（与旧行为一致），而不是丢弃整条记录
        return 0.0
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    m = _CREDIT_NUM_RE.search(str(raw).replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group())
    except ValueError:
        return None


def _model_rates() -> dict:
    """各通道「模型 → 积分倍率」表：{channel: {模型名: rate}}。

    复用 account_manager 的既有实现（与 GUI 模型列表同源）。这些函数
    **返回 (items, err) 元组**，items 是逐模型的元组列表：

      trae_model_rates → [(config_name, display_name, model_name, rate, err)]
      wb_model_rates   → [(model_id, display_name, credits, err)]
      intl_model_rates → [(model_id, display_name, credits, err)]

    倍率字段位置不同（Trae 在 idx 3、WB 系在 idx 2），故分别解析；
    同时按 routeModelId（带前缀）与裸名双向登记，便于前端任取其一匹配。

    ★ 关于「倍率整列为 0」的两次踩坑（本机实测）：
      1) Trae 的倍率元组第 0 个字段是**干净的配置名**（`glm-5.2`、`kimi-k2.7-code`），
         第 2 个字段才是带 `__dev` 后缀的上游名（`glm-5.2__dev`、`kimi-k2.6-code__dev`）。
         模型目录用的正是配置名，原先只登记第 1、2 个字段 → 查不到 → 显示 0。
      2) WB/IE 的倍率是**字符串**（`'x0.05'`、`'x2.20 credits'`），直接 float() 会抛异常，
         原实现 except 后 continue，把整条记录丢掉。
    """
    import sys
    if os.path.join(BASE, "scripts") not in sys.path:
        sys.path.insert(0, os.path.join(BASE, "scripts"))
    import account_manager as am  # type: ignore

    def _put(table: dict, names: list[str], rate: float) -> None:
        for n in names:
            if not n:
                continue
            table[str(n)] = rate
            # 归一化别名（小写 + 去 __dev）：上游同一模型在不同接口里
            # 大小写/后缀并不一致（列表 `kimi-k3` vs 倍率表 `kimi-k3__dev`、
            # 列表 `glm-5.2` vs 倍率表 `GLM-5.2`），不兜底就查不到。
            # 用 setdefault 是为了不覆盖上游明确给出的精确键。
            table.setdefault(_norm_rate_key(n), rate)

    out: dict[str, dict] = {}

    # ── Trae：(config_name, display_name, model_name, rate, err) ──
    try:
        items, err = am.trae_model_rates()
        if err:
            logger.warning("trae 倍率失败: %s", err)
        table: dict[str, float] = {}
        for it in (items or []):
            if not isinstance(it, (tuple, list)) or len(it) < 4:
                continue
            cfg_name, display_name, model_name, rate = it[0], it[1], it[2], it[3]
            if (it[4] if len(it) > 4 else None):
                continue                       # 单模型失败，跳过
            r = _parse_credit_value(rate)
            if r is None:
                continue                       # 彻底无法解析才跳过
            _put(table, [cfg_name, model_name, display_name,
                         f"tr-{cfg_name}", f"tr-{model_name}",
                         f"tr-{display_name}"], r)
        out["Trae"] = table
    except Exception as e:  # noqa: BLE001
        logger.warning("trae_model_rates 异常: %s", e)

    # ── WorkBuddy 系：(model_id, display_name, credits, err) ──
    for channel, fn_name, prefix in (("WorkBuddy", "wb_model_rates", "wb-"),
                                     ("WorkBuddy_IE", "intl_model_rates", "wbie-")):
        fn = getattr(am, fn_name, None)
        if fn is None:
            continue
        try:
            items, err = fn()
            if err:
                logger.warning("%s 倍率失败: %s", fn_name, err)
            table = {}
            for it in (items or []):
                if not isinstance(it, (tuple, list)) or len(it) < 3:
                    continue
                model_id, display_name, credits = it[0], it[1], it[2]
                if (it[3] if len(it) > 3 else None):
                    continue
                r = _parse_credit_value(credits)
                if r is None:
                    continue
                _put(table, [model_id, display_name,
                             f"{prefix}{model_id}", f"{prefix}{display_name}"], r)
            out[channel] = table
        except Exception as e:  # noqa: BLE001
            logger.warning("%s 异常: %s", fn_name, e)

    # ── Loomy：静态倍率表 (逆向自 Loomy 客户端, 值来自上游) ──
    # 键同时登记 上游id / lm- 前缀 / loomy- 历史前缀 / 显示名, 前端任取其一可匹配
    loomy_rates = {
        "deepseek-v4-flash-0731": 3.0,
        "mimo-v2.5": 3.3,
        "MiniMax-M3": 4.0,
        "Kimi-k2.6": 6.5,
        "qwen-3.8-max": 12.0,
        "GLM-5.3-Flash": 0.8,
        "qwen3.8-flash": 0.8,
        "spark-x": 0.0,
        "doubao-seed-2.0-mini": 0.8,
        "qwen3.5-flash": 1.0,
    }
    table: dict[str, float] = {}
    for mid, r in loomy_rates.items():
        for k in (mid, f"lm-{mid}", f"loomy-{mid}"):
            table[k] = r
    out["Loomy"] = table

    # ── 配置别名回填 ──
    # config.json 里的别名（flash / pro / trae-flash / deepseek-flash …）本身
    # 不在上游倍率表里，但它们指向的模型有倍率，直接继承即可 ——
    # 否则这些别名行会永远显示 0（它们恰恰是 Trae 列表里最常见的名字）。
    try:
        import main as gateway  # type: ignore
        providers = getattr(gateway, "PROVIDERS", {}) or {}
        for pkey, prov in providers.items():
            channel = normalize_channel(PROVIDER_TO_CHANNEL.get(pkey, pkey))
            table = out.get(channel)
            aliases = getattr(prov, "aliases", None) or {}
            if not table or not aliases:
                continue
            prefix = CHANNEL_PREFIX.get(channel, "")
            for key, target in aliases.items():
                r = table.get(str(target))
                if r is None:
                    r = table.get(_norm_rate_key(target))
                if r is None:
                    continue
                _put(table, [key, f"{prefix}{key}"], r)
    except Exception as e:  # noqa: BLE001
        logger.warning("别名倍率回填失败: %s", e)

    return {k: v for k, v in out.items() if v}


def _models_from_providers() -> list[dict]:
    """直接读取运行中 provider 实例的模型列表（含动态刷新结果）。

    routeModelId 即 provider 对外暴露的 id —— 各 provider 已按通道前缀命名
    （tr- / wb- / wbie-），前端「请求模型名称」列直接展示该值。
    """
    out: list[dict] = []
    try:
        import main as gateway  # type: ignore
        providers = getattr(gateway, "PROVIDERS", {}) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("无法访问 PROVIDERS: %s", e)
        return out

    for pkey, prov in providers.items():
        # 注册键可能是 workbuddy-intl（连字符），必须归一化，
        # 否则前端拿不到 CHANNEL_META 而崩溃
        channel = normalize_channel(PROVIDER_TO_CHANNEL.get(pkey, pkey))
        prefix = CHANNEL_PREFIX.get(channel, "")
        try:
            ids = sorted({m.get("id") for m in prov.list_models() if m.get("id")})
        except Exception as e:  # noqa: BLE001
            logger.warning("provider %s 模型列表失败: %s", pkey, e)
            continue
        for mid in ids:
            out.append({
                "id": f"{channel}:{mid}",
                "channel": channel,
                "name": mid[len(prefix):] if prefix and mid.startswith(prefix) else mid,
                "ratio": 0,          # 倍率需联网，由 /models/rates 单独返回
                "ratioUnit": "credits" if channel == "WorkBuddy" else "×",
                "routeModelId": mid,
                "hidden": False,
                "pinned": False,
            })
    return out


@router.get("/models/rates")
async def model_rates():
    """各通道模型积分倍率（联网，与 GUI 模型列表同源）。

    与 /models 分开的原因：倍率需要逐个通道请求上游，耗时秒级；
    模型列表本身是内存读取（毫秒级）。分开后列表能秒开，倍率异步补上。
    """
    data = await _in_thread(_model_rates)
    return {"rates": data, "ts": _now(),
            "channels": sorted(data.keys())}


@router.get("/models")
async def list_models(channel: str | None = None):
    rows = await _in_thread(_models_from_providers)
    if channel and channel != "all":
        rows = [r for r in rows if r["channel"] == channel]
    return {"models": rows, "total": len(rows), "ts": _now()}


@router.post("/models/refresh")
async def refresh_models(payload: dict = Body(default={})):
    """强制各 provider 重新拉取上游模型目录（联网，可能耗时数十秒）。"""
    def _work() -> dict:
        try:
            import main as gateway  # type: ignore
            providers = getattr(gateway, "PROVIDERS", {}) or {}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"无法访问 PROVIDERS: {e}")

        import asyncio as _aio
        reloaded, errors = [], []

        async def _refresh_all():
            for pkey, prov in providers.items():
                # 各 provider 的「重新拉取上游模型目录」方法名不统一：
                #   workbuddy / workbuddy-intl → refresh_models
                #   trae                       → sync_models（转发 Node 后端）
                # 原先只找 refresh_models，导致 Trae 被静默跳过，
                # 「刷新模型列表」按钮对 Trae 无效。
                fn = (getattr(prov, "refresh_models", None)
                      or getattr(prov, "sync_models", None))
                if fn is None:
                    continue
                try:
                    await fn(force=True)
                    reloaded.append(pkey)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{pkey}: {e!r}")

        # 端点本身已在工作线程中，这里为该协程单独跑一个事件循环
        _aio.run(_refresh_all())
        return {"ok": True, "reloaded": reloaded, "errors": errors[:10],
                "total": len(_models_from_providers())}

    return await _in_thread(_work)


# ─────────────────────────── 积分 / 流水 ───────────────────────────

# ─────────────────────────── Auto 路由连（虚拟模型路由链） ───────────────────────────
# config.json:
#   "auto_chain": {"enabled": true, "timeout": 120, "models": ["tr-...", "wb-...", "loomy-..."]}
# 虚拟模型名与核心逻辑见 auto_router.py；本端点只做配置读写。

def _load_auto_chain_cfg(cfg: dict | None = None) -> dict:
    """读取 Auto 路由连配置（归一化后返回）。"""
    cfg = cfg if cfg is not None else _read_config()
    ch = cfg.get("auto_chain") or {}
    try:
        timeout = int(ch.get("timeout") or 120)
    except (TypeError, ValueError):
        timeout = 120
    return {"enabled": bool(ch.get("enabled", True)),
            "timeout": timeout,
            "models": [m for m in (ch.get("models") or []) if m]}


@router.get("/auto-chain")
async def get_auto_chain():
    """读取 Auto 路由连配置 + 当前全量可选模型（供链编辑界面下拉）。"""
    chain = _load_auto_chain_cfg()
    all_models: list[str] = []
    try:
        import main as gateway  # type: ignore
        providers = getattr(gateway, "PROVIDERS", {}) or {}
        for prov in providers.values():
            try:
                all_models.extend(m.get("id") for m in prov.list_models() if m.get("id"))
            except Exception:  # noqa: BLE001
                continue
    except Exception as e:  # noqa: BLE001
        logger.warning("auto-chain 取模型列表失败: %s", e)
    return {"chain": chain,
            "availableModels": sorted(set(all_models)),
            "ts": _now()}


@router.post("/auto-chain")
async def set_auto_chain(payload: dict = Body(...)):
    """保存 Auto 路由连配置。body: {enabled, timeout, models[]}。"""
    models = payload.get("models")
    if not isinstance(models, list):
        raise HTTPException(status_code=400, detail="models 必须是数组")
    models = [str(m).strip() for m in models if str(m).strip()]
    timeout = payload.get("timeout", 120)
    try:
        timeout = max(0, int(timeout))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="timeout 必须是整数秒")

    def _work() -> dict:
        cfg = _read_config()
        cfg["auto_chain"] = {
            "enabled": bool(payload.get("enabled", True)),
            "timeout": timeout,
            "models": models,
        }
        _write_config(cfg)
        return {"ok": True, "chain": cfg["auto_chain"]}

    return await _in_thread(_work)


@router.get("/credits/today")
async def credits_today(channel: str | None = None, limit: int = 500):
    """今日积分概况 + 逐笔流水（本地库聚合，不联网）。

    数据来源：credits_api.usage_rows() 取明细、overview() 取汇总。
    库为空时返回零值而非报错（前端按空态渲染）。
    """
    def _work() -> dict:
        import datetime as _dt
        import sys
        if os.path.join(BASE, "scripts") not in sys.path:
            sys.path.insert(0, os.path.join(BASE, "scripts"))
        import credits_api  # type: ignore

        plat = _platform_of(channel)
        today = _dt.date.today().isoformat()

        rows: list[dict] = []
        stats = {"gained": 0.0, "used": 0.0}
        try:
            raw = credits_api.usage_rows(start=today, end=today, platform=plat,
                                         limit=max(1, min(int(limit), 2000)))
            for r in raw:
                ch = PROVIDER_TO_CHANNEL.get(r.get("platform") or "",
                                             r.get("platform") or "")
                rows.append({
                    "id": f"{ch}:{r.get('uid')}:{r.get('ts')}",
                    "channel": ch,
                    "account": r.get("account") or str(r.get("uid") or ""),
                    "model": r.get("model") or "",
                    "ts": int(r.get("ts") or 0),
                    "amount": float(r.get("credits") or 0),
                })
            ov = credits_api.overview(start=today, end=today, platform=plat)
            net = ov.get("net") or {}
            stats["gained"] = float(net.get("gained") or 0)
            stats["used"] = float(net.get("spent") or 0)
        except Exception as e:  # noqa: BLE001
            logger.warning("today 聚合失败: %s", e)

        return {"stats": stats, "rows": rows, "updatedAt": _now()}

    return await _in_thread(_work)


def _platform_of(channel: str | None) -> str | None:
    """前端通道名 → credits_api 的平台键（all/None → None 表示不收窄）。"""
    if not channel or channel == "all":
        return None
    return CHANNEL_TO_PROVIDER.get(channel, channel)


@router.post("/credits/refresh")
async def refresh_credits(payload: dict = Body(default={})):
    """催一次流水采集（usage_collector --collect），供积分看板的「刷新」调用。

    为什么需要它
    ------------
    `/credits/today` 与 `/credits/week` 都是**只读本地流水库**的聚合（毫秒级），
    而库由后台每 5 分钟采集一次。前端「刷新」若只重读这两个端点，拿到的还是
    同一份快照 —— 用户看到的就是「点了刷新，数字一点没变」。

    本端点把「采集」这一步显式暴露给前端：先采、再读，刷新才真的有新数据。
    采集是**增量**的（usage_collector 按天/按账号续采），重复调用幂等。

    实现复用 app_runtime 的定时任务脚本，不重写采集逻辑（命令行拼装见
    {@link _signin_argv}，打包态走 task shim 按文件名路由到内置模块）。
    采集失败不抛错：库中仍有上一次的数据，前端刷新照常进行
    （返回 ok=false + error 供展示）。
    """
    def _work() -> dict:
        import subprocess

        argv = _signin_argv("usage_collector.py", ["--collect"])
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            p = subprocess.run(  # noqa: S603
                argv, cwd=BASE, capture_output=True, text=True, timeout=180,
                creationflags=flags, encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "采集超时（>180s）", "ts": _now()}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": repr(e)[:200], "ts": _now()}

        tail = (p.stdout or "").strip().splitlines()[-3:]
        return {
            "ok": p.returncode == 0,
            "exitCode": p.returncode,
            "detail": " / ".join(tail)[:300],
            "ts": _now(),
        }

    return await _in_thread(_work)


@router.get("/credits/week")
async def credits_week(offset: int = 0, channel: str | None = None):
    """周维度：统计卡 + 每日三通道堆叠数据（本地库聚合，不联网）。

    空白天数会被补齐为 0，保证前端 X 轴始终是 7 列。
    """
    def _work() -> dict:
        import datetime as _dt
        import sys
        if os.path.join(BASE, "scripts") not in sys.path:
            sys.path.insert(0, os.path.join(BASE, "scripts"))
        import credits_api  # type: ignore

        today = _dt.date.today()
        monday = today - _dt.timedelta(days=today.weekday()) + _dt.timedelta(weeks=offset)
        sunday = monday + _dt.timedelta(days=6)
        s, e = monday.isoformat(), sunday.isoformat()
        plat = _platform_of(channel)

        # 七天骨架：无论库中有无数据都返回完整 7 列
        daily: dict[str, dict] = {}
        for d in range(7):
            day = (monday + _dt.timedelta(days=d)).isoformat()
            daily[day] = {"day": day, "Trae": 0.0, "WorkBuddy": 0.0,
                          "WorkBuddy_IE": 0.0, "Loomy": 0.0}

        stats = {"gained": 0.0, "used": 0.0}
        try:
            for r in credits_api.usage_daily(start=s, end=e, platform=plat):
                day = r.get("day")
                ch = PROVIDER_TO_CHANNEL.get(r.get("platform") or "",
                                             r.get("platform") or "")
                if day in daily and ch in daily[day]:
                    daily[day][ch] += float(r.get("credits") or 0)
            ov = credits_api.overview(start=s, end=e, platform=plat)
            net = ov.get("net") or {}
            stats["gained"] = float(net.get("gained") or 0)
            stats["used"] = float(net.get("spent") or 0)
        except Exception as e2:  # noqa: BLE001
            logger.warning("week 聚合失败: %s", e2)

        return {
            "stats": stats,
            "daily": [daily[(monday + _dt.timedelta(days=i)).isoformat()]
                      for i in range(7)],
            "startDay": s,
            "endDay": e,
            "offset": offset,
            "updatedAt": _now(),
        }

    return await _in_thread(_work)


# ─────────────────────────── Loomy 登录（图形化） ───────────────────────────
# 两条通道都能出可用账号, 但**只有桌面通道的 session 能鉴权对话**:
#   * 桌面通道 (讯飞账号服务 account.xfinfr.com, HMAC 签名): 短信登录 → session
#     —— 这个 session 同时当 chat 的 Bearer 与 token 头, 积分从本账号扣。
#   * Web 通道 (loomy.xunfei.cn): 短信登录 → cookie 会话, 仅 /web/api/* 可用,
#     打 /api/v1/chat/completions 会回 100002。它的价值是查余额/签到。
#
# 鉴权整改后「一人一号、各扣各的」全靠桌面通道, 因此界面默认走桌面登录;
# Web 通道作为补充保留 (历史账号仍可查积分)。
# 结论来源: LOOMY_鉴权整改文档 + 本机实测 (见 MEMORY.md)。

_PHONE_RE = re.compile(r"^1\d{10}$")


def _loomy_client():
    import sys
    if os.path.join(BASE, "scripts") not in sys.path:
        sys.path.insert(0, os.path.join(BASE, "scripts"))
    import loomy_client  # type: ignore
    return loomy_client


@router.post("/accounts/loomy/desktop-send-code")
async def loomy_desktop_send_code(payload: dict = Body(...)):
    """桌面通道 (讯飞账号服务) 发送短信验证码。body: {phone} → {ok, messageId|error}。"""
    phone = str(payload.get("phone") or "").strip()
    if not _PHONE_RE.match(phone):
        raise HTTPException(status_code=400, detail="手机号格式不正确（应为 1 开头的 11 位号码）")

    def _work() -> dict:
        lc = _loomy_client()
        ok, r = lc.send_sms_code(phone)
        if ok:
            return {"ok": True, "messageId": str(r or "")}
        return {"ok": False, "messageId": "", "error": str(r)[:200]}

    return await _in_thread(_work)


@router.post("/accounts/loomy/desktop-login")
async def loomy_desktop_login(payload: dict = Body(...)):
    """桌面通道短信登录。body: {phone, code, messageId}。

    成功后把本账号的 session 写入 config.json providers.loomy.accounts,
    该 session 即网关对话的 Bearer —— 积分记在该账号自己头上。
    """
    phone = str(payload.get("phone") or "").strip()
    code = str(payload.get("code") or "").strip()
    message_id = str(payload.get("messageId") or "")
    if not _PHONE_RE.match(phone):
        raise HTTPException(status_code=400, detail="手机号格式不正确")
    if not re.match(r"^\d{4,8}$", code):
        raise HTTPException(status_code=400, detail="验证码格式不正确")
    if not message_id:
        raise HTTPException(status_code=400, detail="缺少 messageId（请先获取验证码）")

    def _work() -> dict:
        lc = _loomy_client()
        ok, r = lc.login_by_sms(phone, code, message_id)
        if not ok:
            return {"ok": False, "error": str(r)[:200]}
        acct = lc.save_session(r.get("userid", ""), r.get("session", ""), phone)
        return {"ok": True, "userid": acct.get("userid", ""),
                "name": acct.get("name", "")}

    return await _in_thread(_work)


@router.post("/accounts/loomy/send-code")
async def loomy_send_code(payload: dict = Body(...)):
    """Web 端发送短信验证码。body: {phone} → {ok, messageId|error}。"""
    phone = str(payload.get("phone") or "").strip()
    if not _PHONE_RE.match(phone):
        raise HTTPException(status_code=400, detail="手机号格式不正确（应为 1 开头的 11 位号码）")

    def _work() -> dict:
        lc = _loomy_client()
        ok, r = lc.web_send_sms_code(phone)
        if ok and isinstance(r, dict):
            return {"ok": True, "messageId": r.get("messageId") or ""}
        return {"ok": False, "messageId": "", "error": str(r)[:200]}

    return await _in_thread(_work)


@router.post("/accounts/loomy/web-login")
async def loomy_web_login(payload: dict = Body(...)):
    """Web 端短信验证码登录。body: {phone, code, messageId}。

    成功后 cookie 会话写入 config.json providers.loomy.accounts（kind=web）。
    注意: Web cookie 仅能访问 /web/api/*（查余额/签到）, **不能鉴权对话** ——
    对话请用 desktop-login 拿 session。
    """
    phone = str(payload.get("phone") or "").strip()
    code = str(payload.get("code") or "").strip()
    message_id = str(payload.get("messageId") or "")
    if not _PHONE_RE.match(phone):
        raise HTTPException(status_code=400, detail="手机号格式不正确")
    if not re.match(r"^\d{4,8}$", code):
        raise HTTPException(status_code=400, detail="验证码格式不正确")
    if not message_id:
        raise HTTPException(status_code=400, detail="缺少 messageId（请先获取验证码）")

    def _work() -> dict:
        lc = _loomy_client()
        ok, r = lc.web_login(phone, code, message_id)
        if not ok:
            return {"ok": False, "error": str(r)[:200]}
        acct = lc.save_web_session(phone, r["cookies"], r.get("deviceId", ""), r.get("me"))
        return {"ok": True, "userid": acct.get("userid", ""), "name": acct.get("name", "")}

    return await _in_thread(_work)


@router.get("/accounts/loomy/session")
async def loomy_web_session(phone: str):
    """校验某手机号的 Web 会话是否仍有效（GET /web/api/auth/me 探测）。"""
    if not _PHONE_RE.match(phone):
        raise HTTPException(status_code=400, detail="手机号格式不正确")

    def _work() -> dict:
        lc = _loomy_client()
        for a in lc.load_web_accounts():
            if a.get("phone") == phone:
                ok, me = lc.web_me(a.get("cookies") or {})
                nick = ""
                if ok and isinstance(me, dict):
                    d = me.get("data") or me
                    for k in ("nickname", "nickName", "name", "phone"):
                        if d.get(k):
                            nick = str(d[k])
                            break
                return {"valid": bool(ok), "nickname": nick, "checkedAt": _now()}
        return {"valid": False, "nickname": "", "checkedAt": _now(),
                "error": "未找到该手机号的 Web 会话"}

    return await _in_thread(_work)


# ─────────────────────────── API 密钥 ───────────────────────────

def _api_store():
    import sys
    if os.path.join(BASE, "scripts") not in sys.path:
        sys.path.insert(0, os.path.join(BASE, "scripts"))
    import api_store  # type: ignore
    return api_store


@router.get("/api-keys")
async def list_api_keys():
    """API 密钥列表（只读 config.json 的 api_keys，毫秒级）。

    api_store.list_apis() 返回 [(name, key, created_at)]；created_at 为 0 表示
    老配置里没有该字段（前端显示「—」，不再伪造 1970）。
    前端用 key 本身作为行标识（key 唯一且不可变）。

    读的时候顺手做一次旧结构迁移：把顶层 api_key/api_key_name 并入 api_keys。
    放在这里是为了「即使网关先于配置改动启动，用户点开这一页也能自愈」。
    """
    def _work() -> list[dict]:
        store = _api_store()
        cfg = store.load_config()
        try:
            if store.ensure_api_keys(cfg):
                cfg = store.load_config()      # 迁移已落盘，重新读取
        except Exception as e:  # noqa: BLE001
            logger.warning("API 密钥结构统一失败: %s", e)
        items = []
        for i, row in enumerate(store.list_apis(cfg)):
            name, key, created = (list(row) + [None, None, 0])[:3]
            items.append({
                "id": f"key-{i}",
                "name": name or f"无名{i + 1}",
                "key": key or "",
                "createdAt": int(created or 0),
            })
        return items

    rows = await _in_thread(_work)
    return {"apiKeys": rows, "total": len(rows)}


@router.post("/api-keys/create")
async def create_api_key(payload: dict = Body(default={})):
    def _work() -> dict:
        store = _api_store()
        cfg = store.load_config()
        name, key, created = store.create_api(cfg,
                                              str(payload.get("name") or ""))
        return {"ok": True, "name": name, "key": key, "createdAt": created}

    return await _in_thread(_work)


@router.post("/api-keys/rename")
async def rename_api_key(payload: dict = Body(...)):
    """body: {key, name}。"""
    def _work() -> dict:
        store = _api_store()
        cfg = store.load_config()
        old = str(payload.get("key") or "")
        name = str(payload.get("name") or "").strip()
        if not old or not name:
            raise HTTPException(status_code=400, detail="key 与 name 必填")
        if not store.rename_api(cfg, old, name):
            raise HTTPException(status_code=404, detail="密钥不存在")
        return {"ok": True}

    return await _in_thread(_work)


@router.post("/api-keys/delete")
async def delete_api_key(payload: dict = Body(...)):
    """body: {key}。v3.0 起所有密钥一视同仁，均可删除 —— **但最后一条不许删**。

    ★ 为什么必须拦（2026-09-16 实测事故，用户报「拿不到后端数据」）：
      密钥是管理面**唯一**的凭据。用户在本页面把密钥依次删完的那一刻，
      正在发请求的客户端（桌面端自己！）当场全部 401 且**无法自救** ——
      「创建密钥」本身也要鉴权，界面从此只能显示「无法连接后端网关 /
      界面已就绪，但拿不到后端数据」。
      更麻烦的是恢复路径：网关仅在**下次重启**时才由
      `api_store.ensure_api_keys()` 补一条新密钥，而那条新密钥对「还开着」的
      客户端又是一把没见过的钥匙（旧进程缓存的是已删密钥）→ 继续 401。
      用户唯一的出路是重启桌面端，而他看到的提示是「后端 45 秒内仍未就绪」，
      与真实原因（密钥被自己删空了）完全对不上。
      拦住这一步，整条链就不会发生。
    """
    def _work() -> dict:
        store = _api_store()
        cfg = store.load_config()
        key = str(payload.get("key") or "")
        if not key:
            raise HTTPException(status_code=400, detail="key 必填")

        rows = cfg.get("api_keys") or []
        if key in [a.get("key") for a in rows] and len(rows) <= 1:
            raise HTTPException(
                status_code=400,
                detail="至少需要保留一条 API 密钥：删掉最后一条会让所有客户端"
                       "（包括本界面）立即失去访问权限，只能改 config.json 或重启网关才能恢复",
            )
        if not store.delete_api(cfg, key):
            raise HTTPException(status_code=404, detail="密钥不存在")
        return {"ok": True}

    return await _in_thread(_work)


# ─────────────────────────── 网关信息 / 日志 / 进程 ───────────────────────────

@router.get("/gateway")
async def gateway_info():
    cfg = await _in_thread(_read_config)
    port = int(cfg.get("port", 8000))
    host = str(cfg.get("host", "127.0.0.1"))
    disp = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    return {
        "openaiBase": f"http://{disp}:{port}/v1",
        "chatEndpoint": f"http://{disp}:{port}/v1/chat/completions",
        "anthropicBase": f"http://{disp}:{port}",
    }


@router.get("/logs")
async def list_logs(lines: int = 300):
    """读取网关运行日志尾部（只读，最多 2000 行）。"""
    def _work() -> list[dict]:
        path = os.path.join(BASE, "logs", "gateway_out.log")
        if not os.path.exists(path):
            return []
        n = max(1, min(int(lines), 2000))
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                buf = f.readlines()[-n:]
        except Exception:  # noqa: BLE001
            return []
        out = []
        for i, ln in enumerate(buf):
            t = ln.rstrip("\n")
            low = t.lower()
            if "error" in low or "[err]" in low:
                lvl = "error"
            elif "warn" in low:
                lvl = "warn"
            elif "[ok]" in low or "[done]" in low or "success" in low:
                lvl = "success"
            else:
                lvl = "info"
            out.append({"id": i, "level": lvl, "text": t, "ts": _now()})
        return out

    rows = await _in_thread(_work)
    return {"logs": rows, "total": len(rows)}


@router.get("/version")
async def admin_version():
    def _work() -> dict:
        try:
            import sys
            if BASE not in sys.path:
                sys.path.insert(0, BASE)
            from version import APP_VERSION, UPDATE_CHANNEL  # type: ignore
            return {"current": APP_VERSION, "channel": UPDATE_CHANNEL}
        except Exception:  # noqa: BLE001
            return {"current": "unknown", "channel": "dev"}

    return await _in_thread(_work)


# ─────────────────────────── 设置：开机自启 ───────────────────────────

def _startup_dir() -> str | None:
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return None
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu",
                        "Programs", "Startup")


def _autostart_vbs() -> str:
    r"""生成「开机自启」的 VBS 内容 —— 必须按安装形态分叉。

    ★ 原来两侧都固定指向 `<安装根>\start_hidden.ps1`，那是**源码形态**的脚本:
      它依赖 `.venv\` 与 `runtime\Scripts\`, 而 exe 打包安装里这两样都不存在,
      且 start_hidden.ps1 本身也没有打进安装包 —— 于是用户在界面里打开自启开关,
      VBS 写得漂漂亮亮, 开机时却什么都不发生 (静默失效, 无任何报错)。

    打包形态改为两步, 都以隐藏窗口运行:
      1) `open-ai.exe start`  —— 先把 Broker/网关/Trae/定时任务整棵树拉起来,
         这样即使桌面端哪天又起不来, 后端仍然在跑 (别让界面成为唯一的启动路径);
      2) `desktop\open-ai-desktop.exe --minimized` —— 再开界面并驻留托盘,
         托盘图标必须由桌面端创建 (只拉 Broker 会得到「后端在跑但托盘没图标」)。
    """
    head = 'Set sh = CreateObject("WScript.Shell")\r\n'
    packaged = os.path.isfile(os.path.join(BASE, "open-ai-gateway.exe"))
    if packaged:
        cli = os.path.join(BASE, "open-ai.exe")
        app = os.path.join(BASE, "desktop", "open-ai-desktop.exe")
        lines = []
        if os.path.isfile(cli):
            lines.append('sh.Run """%s"" start", 0, True' % cli)
        if os.path.isfile(app):
            lines.append('sh.Run """%s"" --minimized", 0, False' % app)
        if not lines:
            raise HTTPException(
                status_code=500,
                detail="安装目录里找不到 open-ai.exe 或 desktop\\open-ai-desktop.exe，"
                       "无法配置开机自启（安装包可能不完整）")
        return head + "\r\n".join(lines) + "\r\n"
    # 源码形态：沿用隐藏 PowerShell 引导（它自己会建 shim 并调 bootstrap）
    ps1 = os.path.join(BASE, "start_hidden.ps1")
    return (head + 'sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass '
            '-WindowStyle Hidden -File ""' + ps1 + '""", 0, False\r\n')


def _autostart_target() -> str | None:
    """开机自启的落地点：启动文件夹下的 open-ai-autostart.vbs

    与 GUI「开机自启动」开关指向同一文件 —— 两侧状态天然一致。
    """
    sd = _startup_dir()
    return os.path.join(sd, "open-ai-autostart.vbs") if sd else None


def setup_cors(app, extra_origins: list[str] | None = None) -> None:
    """为管理面开启 CORS。

    为什么需要：**打包后的 Tauri 应用**中前端运行在 WebView2 的
    ``http://tauri.localhost``（或 ``https://tauri.localhost``），而网关在
    ``http://127.0.0.1:8000`` —— 跨 origin，浏览器会拦截未授权响应。
    开发态经 Vite 代理是同源，不触发 CORS；打包态必须显式放行。

    安全边界：只放行本机 Tauri / localhost 来源，不开放 * 与任意公网域名。
    """
    from fastapi.middleware.cors import CORSMiddleware

    origins = [
        "http://tauri.localhost",
        "https://tauri.localhost",
        "http://localhost",
        "http://localhost:1420",
        "http://127.0.0.1:1420",
        # Vite 开发服务器可能在其它端口（port 漂移时的兜底）
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
    if extra_origins:
        origins.extend(extra_origins)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        # 兼容 Tauri 自定义协议与任意本机端口
        allow_origin_regex=r"^(https?://(localhost|127\.0\.0\.1|tauri\.localhost)(:\d+)?|tauri://localhost)$",
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )


@router.get("/settings/autostart")
async def get_autostart():
    def _work() -> dict:
        dst = _autostart_target()
        return {"enabled": bool(dst) and os.path.isfile(dst),
                "path": dst or ""}

    return await _in_thread(_work)


@router.post("/settings/autostart")
async def set_autostart(payload: dict = Body(...)):
    """写入/移除开机自启（与 GUI 同一份 VBS 逻辑，保持两侧语义一致）。"""
    enable = bool(payload.get("enabled"))

    def _work() -> dict:
        dst = _autostart_target()
        if not dst:
            raise HTTPException(status_code=500, detail="无法定位启动文件夹")
        try:
            if enable:
                vbs = _autostart_vbs()
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                # 启动文件夹路径可能含中文 → 用 GBK 写入，WScript 才能正确读取
                with open(dst, "w", encoding="gbk", newline="") as f:
                    f.write(vbs)
                return {"ok": True, "enabled": True, "path": dst}
            if os.path.isfile(dst):
                os.remove(dst)
            return {"ok": True, "enabled": False, "path": dst}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"设置失败: {e}")

    return await _in_thread(_work)


@router.post("/version/check")
async def check_update(payload: dict = Body(default={})):
    """检查 GitHub 最新 release（失败不抛错，返回无更新）。

    只读查询，用于系统设置页的「一键更新」前置判断；真正的下载安装仍由
    既有 one-click 更新流程负责。
    """
    def _work() -> dict:
        import sys
        if BASE not in sys.path:
            sys.path.insert(0, BASE)
        try:
            from version import APP_VERSION  # type: ignore
        except Exception:  # noqa: BLE001
            APP_VERSION = "unknown"

        latest = None
        try:
            import urllib.request
            req_ = urllib.request.Request(
                "https://api.github.com/repos/%s/releases/latest" % UPDATE_REPO,
                headers={"Accept": "application/vnd.github+json",
                         "User-Agent": "open-ai-gateway"})
            with urllib.request.urlopen(req_, timeout=8) as resp:  # noqa: S310
                data = json.loads(resp.read().decode("utf-8", "replace"))
            latest = data.get("tag_name")
        except Exception as e:  # noqa: BLE001
            logger.info("检查更新失败（忽略）: %s", e)

        has = bool(latest and latest != APP_VERSION)
        return {"current": APP_VERSION, "latest": latest or APP_VERSION,
                "hasUpdate": has}

    return await _in_thread(_work)


@router.get("/health")
async def admin_health():
    """管理面自检：各子系统可达性（供前端状态条使用）。"""
    def _work() -> dict:
        cfg = _read_config()
        prov = cfg.get("providers") or {}
        accounts = sum(len((prov.get(k) or {}).get("accounts") or [])
                       for k in CHANNEL_TO_PROVIDER.values())
        return {
            "ok": True,
            "configLoaded": bool(cfg),
            "providers": list(prov.keys()),
            "accounts": accounts,
            "ts": _now(),
        }

    return await _in_thread(_work)
