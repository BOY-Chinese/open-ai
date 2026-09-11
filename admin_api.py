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

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")

# 通道键（对外） → config.json providers 段键（对内）
CHANNEL_TO_PROVIDER = {
    "Trae": "trae",
    "WorkBuddy": "workbuddy",
    "WorkBuddy_IE": "workbuddy_intl",
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
}

# 归一化：把任何来源的通道写法收敛到对外三值
CHANNEL_ALIASES = {
    "trae": "Trae",
    "workbuddy": "WorkBuddy",
    "workbuddy_ie": "WorkBuddy_IE",
    "workbuddyie": "WorkBuddy_IE",
    "workbuddy_intl": "WorkBuddy_IE",
    "workbuddy-intl": "WorkBuddy_IE",
    "intl": "WorkBuddy_IE",
}


def normalize_channel(value: str | None) -> str:
    """把任意通道写法归一化为 Trae / WorkBuddy / WorkBuddy_IE。

    未知值原样返回（便于排查），但模型端点会把它映射到上述三值之一。
    """
    v = (value or "").strip()
    return CHANNEL_ALIASES.get(v.lower(), v)

# 对外模型名前缀（与各 provider 实现保持一致）
CHANNEL_PREFIX = {
    "Trae": "tr-",
    "WorkBuddy": "wb-",
    "WorkBuddy_IE": "wbie-",
}

# 账号积分余额的内存缓存：{accountId: {credits, workCredits}}
#   GET  只读这里（毫秒级，不联网）
#   POST /accounts/credits/refresh 才真正请求上游并回填
# 进程重启即失效——这是有意的：余额是易变数据，不做跨进程持久化。
_CREDIT_CACHE: dict[str, dict] = {}
_CREDIT_CACHE_TS: float = 0.0


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
    """稳定 id：优先 uid/userId，其次账号名，最后序号。"""
    raw = str(acc.get("uid") or acc.get("userId") or acc.get("name") or idx)
    return f"{channel}:{raw}"


def _account_status(acc: dict) -> str:
    """三态：enabled / disabled / disconnected。

    连接态判定依据（不联网）：有可用于上游调用的凭据即视为已连接。
      trae        → token
      workbuddy*  → accessToken 或 refreshToken（可自动刷新）
    """
    if acc.get("enabled", True) is False:
        return "disabled"
    has_cred = bool(acc.get("token") or acc.get("accessToken")
                    or acc.get("refreshToken") or acc.get("cookie"))
    return "enabled" if has_cred else "disconnected"


def _load_accounts_raw() -> list[dict]:
    """读取三通道账号，产出前端 Account[] 形状（不含联网积分）。"""
    cfg = _read_config()
    prov = cfg.get("providers") or {}
    out: list[dict] = []
    for channel, pkey in CHANNEL_TO_PROVIDER.items():
        seg = prov.get(pkey) or {}
        for i, acc in enumerate(seg.get("accounts") or []):
            uid = str(acc.get("uid") or acc.get("userId") or "")
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
    """启用/关闭账号（写 config.json）。body: {id}"""
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
        for i, acc in enumerate(seg.get("accounts") or []):
            if _account_id(channel, acc, i) == acc_id:
                acc["enabled"] = acc.get("enabled", True) is False
                _write_config(cfg)
                return {"ok": True, "id": acc_id, "enabled": acc["enabled"]}
        raise HTTPException(status_code=404, detail="账号不存在")

    return await _in_thread(_work)


@router.post("/accounts/delete")
async def delete_account(payload: dict = Body(...)):
    """删除账号（写 config.json）。body: {id}"""
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
        keep = [a for i, a in enumerate(accs)
                if _account_id(channel, a, i) != acc_id]
        if len(keep) == len(accs):
            raise HTTPException(status_code=404, detail="账号不存在")
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
        if not os.path.exists(script):
            raise HTTPException(status_code=501,
                                detail=f"{script_name} 不存在（该通道未提供登录脚本）")
        exe = os.path.join(BASE, ".venv", "Scripts", "python.exe")
        if not os.path.exists(exe):
            exe = sys.executable
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if os.name == "nt" else 0
        subprocess.Popen([exe, script], cwd=BASE,  # noqa: S603
                         creationflags=flags)
        return {"ok": True, "launched": script_name, "channel": channel}

    return await _in_thread(_work)


@router.post("/accounts/refresh")
async def refresh_accounts(payload: dict = Body(default={})):
    """刷新账号状态（重读配置；联网拉积分走 /accounts/credits/refresh）。"""
    data = await list_accounts(payload.get("channel"))
    return {**data, "refreshed": True}


@router.post("/accounts/reconnect")
async def reconnect_accounts(payload: dict = Body(default={})):
    """重新连接账号：拉起既有重连脚本（异步、不等待）。"""
    def _work() -> dict:
        import subprocess
        import sys
        script = os.path.join(BASE, "scripts", "signin_all.py")
        if not os.path.exists(script):
            raise HTTPException(status_code=501, detail="signin_all.py 不存在")
        exe = os.path.join(BASE, ".venv", "Scripts", "python.exe")
        if not os.path.exists(exe):
            exe = sys.executable
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen([exe, script], cwd=BASE,
                         creationflags=flags)  # noqa: S603
        return {"ok": True, "started": True}

    return await _in_thread(_work)


# ─────────────────────────── 模型 ───────────────────────────

def _model_rates() -> dict:
    """各通道「模型 → 积分倍率」表：{channel: {模型名: rate}}。

    复用 account_manager 的既有实现（与 GUI 模型列表同源）。这些函数
    **返回 (items, err) 元组**，items 是逐模型的元组列表：

      trae_model_rates → [(config_name, display_name, model_name, rate, err)]
      wb_model_rates   → [(model_id, display_name, credits, err)]
      intl_model_rates → [(model_id, display_name, credits, err)]

    倍率字段位置不同（Trae 在 idx 3、WB 系在 idx 2），故分别解析；
    同时按 routeModelId（带前缀）与裸名双向登记，便于前端任取其一匹配。
    """
    import sys
    if os.path.join(BASE, "scripts") not in sys.path:
        sys.path.insert(0, os.path.join(BASE, "scripts"))
    import account_manager as am  # type: ignore

    def _put(table: dict, names: list[str], rate: float) -> None:
        for n in names:
            if n:
                table[str(n)] = rate

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
            _cfg_name, display_name, model_name, rate = it[0], it[1], it[2], it[3]
            if (it[4] if len(it) > 4 else None):
                continue                       # 单模型失败，跳过
            try:
                r = float(rate or 0)
            except (TypeError, ValueError):
                continue
            _put(table, [model_name, display_name,
                         f"tr-{model_name}", f"tr-{display_name}"], r)
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
                try:
                    r = float(credits or 0)
                except (TypeError, ValueError):
                    continue
                _put(table, [model_id, display_name,
                             f"{prefix}{model_id}", f"{prefix}{display_name}"], r)
            out[channel] = table
        except Exception as e:  # noqa: BLE001
            logger.warning("%s 异常: %s", fn_name, e)

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
                fn = getattr(prov, "refresh_models", None)
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
            daily[day] = {"day": day, "Trae": 0.0, "WorkBuddy": 0.0, "WorkBuddy_IE": 0.0}

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


# ─────────────────────────── API 密钥 ───────────────────────────

def _api_store():
    import sys
    if os.path.join(BASE, "scripts") not in sys.path:
        sys.path.insert(0, os.path.join(BASE, "scripts"))
    import api_store  # type: ignore
    return api_store


@router.get("/api-keys")
async def list_api_keys():
    """API 密钥列表。

    api_store.list_apis() 返回 [(name, key, is_legacy)]，其中 is_legacy 表示
    该密钥来自旧版顶层 api_key 字段（改名走 rename_legacy_api 而非 rename_api）。
    前端用 key 本身作为行标识（key 唯一且不可变）。
    """
    def _work() -> list[dict]:
        store = _api_store()
        cfg = store.load_config()
        items = []
        for i, row in enumerate(store.list_apis(cfg)):
            name, key, legacy = (list(row) + [None, None, False])[:3]
            items.append({
                "id": f"key-{i}",
                "name": name or f"无名{i + 1}",
                "key": key or "",
                "legacy": bool(legacy),
                "createdAt": 0,
            })
        return items

    rows = await _in_thread(_work)
    return {"apiKeys": rows, "total": len(rows)}


@router.post("/api-keys/create")
async def create_api_key(payload: dict = Body(default={})):
    def _work() -> dict:
        store = _api_store()
        cfg = store.load_config()
        name, key = store.create_api(cfg)
        return {"ok": True, "name": name, "key": key}

    return await _in_thread(_work)


@router.post("/api-keys/rename")
async def rename_api_key(payload: dict = Body(...)):
    """body: {key, name}；legacy 密钥走独立改名路径。"""
    def _work() -> dict:
        store = _api_store()
        cfg = store.load_config()
        old = str(payload.get("key") or "")
        name = str(payload.get("name") or "").strip()
        if not old or not name:
            raise HTTPException(status_code=400, detail="key 与 name 必填")
        if old == (cfg.get("api_key") or ""):
            store.rename_legacy_api(cfg, name)
            return {"ok": True, "legacy": True}
        if not store.rename_api(cfg, old, name):
            raise HTTPException(status_code=404, detail="密钥不存在")
        return {"ok": True, "legacy": False}

    return await _in_thread(_work)


@router.post("/api-keys/delete")
async def delete_api_key(payload: dict = Body(...)):
    """body: {key}；旧版顶层 api_key 不允许删除（它是网关的兜底密钥）。"""
    def _work() -> dict:
        store = _api_store()
        cfg = store.load_config()
        key = str(payload.get("key") or "")
        if not key:
            raise HTTPException(status_code=400, detail="key 必填")
        if key == (cfg.get("api_key") or ""):
            raise HTTPException(status_code=400,
                                detail="旧版 api_key 为网关兜底密钥，不可删除")
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
                ps1 = os.path.join(BASE, "start_hidden.ps1")
                vbs = (
                    'Set sh = CreateObject("WScript.Shell")\r\n'
                    'sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass '
                    '-WindowStyle Hidden -File ""' + ps1 + '""", 0, False\r\n'
                )
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
                "https://api.github.com/repos/BOY-Chinese/open-ai/releases/latest",
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
