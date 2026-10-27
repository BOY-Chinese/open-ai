#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Qoder 登录助手（国内版 / 国际版通用）
=====================================
用法::

    python scripts/login_qoder.py --realm cn     # Qoder 国内版 (qoder.com.cn)
    python scripts/login_qoder.py --realm intl   # Qoder 国际版 (qoder.com)

    --pat pt-xxxx        直接导入个人访问令牌（不开浏览器）
    --timeout 300        等待授权的秒数（默认 300）
    --browser chrome     指定浏览器（默认自动：chrome-for-testing → chrome → edge）
    --reuse-profile      复用上次的浏览器会话（没走完的登录接着来）

链路（逆向自官方 qodercli / 桌面端，见 providers/qoder_common.py）::

  1. 生成 PKCE（verifier + S256 challenge）与 nonce，拼设备授权链接
     ``{website}/device/selectAccounts?challenge=…&nonce=…&client_id=…``
  2. 用 Playwright 拉起**全新 profile** 的浏览器打开该链接（无历史 cookie，
     所以可以反复添加不同账号），用户在页面里登录并「选择账号」授权
  3. 脚本轮询 ``{openapi}/api/v1/deviceToken/poll``（404/202 = 尚未授权）
  4. 拿到 token 后拉 ``/api/v1/userinfo`` 补昵称/套餐，写入 config.json

写回位置：``providers.qoder_cn.accounts[]`` 或 ``providers.qoder_intl.accounts[]``，
同一 uid 覆盖更新（重新登录不产生重复行）。

★ 双区差异全部由 qoder_common.REALMS 决定，本脚本不判区域 —
  国内版带 redirect_uri、nonce 带横线；国际版不带 redirect_uri、nonce 是 32 位 hex。

★ 浏览器方案（2026-10 按 master 要求对齐既有登录脚本）：
  顺序 chrome-for-testing → chrome → edge，**每次都全新 profile**（不带上次的
  cookie，便于连续添加多个不同账号）；细节见 scripts/pw_login.py 模块文档。
"""
import json
import os
import sys
import time

# ★ 打包态 (open-ai-task.exe) 里 __file__ 指向 _MEI 临时目录, 不能拿它当根;
#   优先用 app_paths(安装根)。只有源码态直接 `python scripts/login_*.py` 时
#   才需要先用 __file__ 把仓库根补进 sys.path —— 该用法放在 except 里,
#   满足 check_frozen_paths.py 的门禁。
try:
    import app_paths as _ap
except Exception:  # 源码态
    _HERE = os.path.dirname(os.path.abspath(__file__))
    _ROOT = os.path.dirname(_HERE)
    for _p in (_ROOT, _HERE):
        if _p not in sys.path:
            sys.path.insert(0, _p)
    import app_paths as _ap

_ROOT = _ap.ROOT
_HERE = _ap.SCRIPTS_DIR
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
CONFIG_PATH = _ap.CONFIG_PATH
LOGS_DIR = _ap.LOGS_DIR
DATA_DIR = _ap.DATA_DIR

import pw_login  # noqa: E402

LOG_PATH = os.path.join(LOGS_DIR, "login_qoder.log")
REALM_TO_SEG = {"cn": "qoder_cn", "intl": "qoder_intl"}


def log(msg: str) -> None:
    line = f'[{time.strftime("%H:%M:%S")}] {msg}'
    print(line, flush=True)
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def parse_args(argv: list[str]) -> dict:
    opts = {"realm": "cn", "pat": "", "timeout": 300.0, "browser": "",
            "reuse_profile": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--realm", "-r") and i + 1 < len(argv):
            opts["realm"] = "intl" if argv[i + 1].lower().startswith("i") else "cn"
            i += 2
        elif a == "--pat" and i + 1 < len(argv):
            opts["pat"] = argv[i + 1].strip()
            i += 2
        elif a == "--timeout" and i + 1 < len(argv):
            try:
                opts["timeout"] = max(30.0, float(argv[i + 1]))
            except ValueError:
                pass
            i += 2
        elif a == "--browser" and i + 1 < len(argv):
            opts["browser"] = argv[i + 1].strip().lower()
            i += 2
        elif a == "--reuse-profile":
            opts["reuse_profile"] = True
            i += 1
        else:
            i += 1
    return opts


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: dict) -> None:
    """原子写入：先写临时文件再替换（半截文件会让网关启动即丢配置）。"""
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def upsert_account(seg_key: str, account: dict) -> tuple[int, bool]:
    """把账号写进 config.json。返回 (账号总数, 是否新增)。

    覆盖更新时保留用户改过的 enabled 与 includeDisabledModels ——
    重新登录不该把「关掉的账号」悄悄打开，也不该重置模型过滤偏好。
    """
    cfg = load_config()
    prov = cfg.setdefault("providers", {})
    seg = prov.setdefault(seg_key, {"enabled": True, "timeout": 600, "switchEvery": 5})
    seg.setdefault("enabled", True)
    accs = seg.setdefault("accounts", [])
    uid = str(account.get("uid") or "")
    for i, old in enumerate(accs):
        if str(old.get("uid") or "") == uid:
            merged = dict(account)
            if "enabled" in old:
                merged["enabled"] = old["enabled"]
            for k in ("remark",):
                if k in old:
                    merged[k] = old[k]
            accs[i] = merged
            save_config(cfg)
            return len(accs), False
    accs.append(account)
    save_config(cfg)
    return len(accs), True


def _start_browser(opts: dict, realm: str):
    """启动 Playwright 与全新 profile 浏览器。返回 (playwright, ctx, browser_name)。"""
    pw_login.ensure_browsers_path()
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    ctx, name = pw_login.launch_browser(
        pw, opts["browser"], opts["reuse_profile"],
        root=pw_login.profile_root(f"qoder-{realm}", DATA_DIR), log=log)
    return pw, ctx, name


def _stop_browser(pw, ctx) -> None:
    if ctx is not None:
        pw_login.close_quietly(ctx)
    if pw is not None:
        try:
            pw.stop()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    opts = parse_args(sys.argv[1:])
    realm = opts["realm"]
    seg_key = REALM_TO_SEG[realm]

    from providers import qoder_common as qc  # type: ignore

    log("=" * 60)
    log(f"  Qoder {qc.realm_cfg(realm)['name']} 登录助手")
    log(f"  写回位置: config.json → providers.{seg_key}.accounts[]")
    log("=" * 60)

    # ── 分支一：PAT 导入（无浏览器） ──
    if opts["pat"]:
        log("[PAT] 正在用个人访问令牌交换 jobToken …")
        try:
            account = qc.import_pat(opts["pat"], realm)
        except Exception as e:  # noqa: BLE001
            log(f"[失败] PAT 导入失败: {e}")
            return 1
        total, added = upsert_account(seg_key, account)
        log(f"[成功] 账号 {account.get('uid')}（{account.get('nickname')}）已"
            f"{'新增' if added else '更新'}，当前共 {total} 个账号")
        log("[提示] 回到 open-ai 账号管理页点「刷新账号」，新账号即可用于对话")
        return 0

    # ── 分支二：设备授权（浏览器） ──
    login = qc.start_device_login(realm)
    pw = ctx = None
    try:
        pw, ctx, _name = _start_browser(opts, realm)
    except Exception as e:  # noqa: BLE001
        log(f"[失败] 浏览器无法启动: {e}")
        log(pw_login.browsers_hint())
        _stop_browser(pw, ctx)
        return 1

    log("[授权] 已在浏览器打开 Qoder 授权页：请完成登录并「选择账号」授权")
    log(f"       若浏览器没自动打开，手工复制此链接: {login['authUrl']}")
    page = pw_login.first_page(ctx, login["authUrl"], log=log)
    if page is None:
        log("[失败] 打不开授权页，判定为网络问题")
        log(pw_login.browsers_hint())
        _stop_browser(pw, ctx)
        return 1

    log("[等待] 正在轮询授权结果 …")
    deadline = time.time() + opts["timeout"]
    last_note = ""
    account = None
    closed_by_user = False
    while time.time() < deadline:
        r = qc.poll_device_login(realm, login["verifier"], login["nonce"])
        if r.get("status") == "ok":
            account = r.get("account") or {}
            break
        if r.get("status") == "error":
            log(f"[错误] {r.get('message')}")
            break
        note = r.get("message") or ""
        if note and note != last_note:
            log(f"  [进度] {note}")
            last_note = note
        if not pw_login.tick(page, 1200):
            closed_by_user = True
            log("[提示] 浏览器窗口已被关闭，做最后一次授权确认 …")
            r = qc.poll_device_login(realm, login["verifier"], login["nonce"])
            if r.get("status") == "ok":
                account = r.get("account") or {}
            break

    _stop_browser(pw, ctx)

    if not account:
        log(f"[失败] {int(opts['timeout'])} 秒内未完成授权。自查：")
        log("  1) 浏览器里是否走到了「选择账号 → 授权」的最后一步？")
        log("  2) 页面是否报错（如登录后一直转圈）？")
        log("  3) 国内版/国际版是否选对？国内账号请用 --realm cn")
        log(f"  4) 浏览器是否被提前关闭？{'是' if closed_by_user else '否'}")
        return 1

    log(f"[成功] 已获取凭据: uid={account.get('uid')}"
        f" nickname={account.get('nickname') or '-'} realm={realm}"
        f" refreshToken={'有' if account.get('refreshToken') else '无'}")

    try:
        info = qc.fetch_userinfo(account)
        if info.get("id") or info.get("name"):
            log(f"[校验] userinfo OK: id={info.get('id')} name={info.get('name')}")
        credits, err = qc.fetch_credits(account)
        log(f"[校验] 可用积分: {credits if not err else '查询失败 ' + err}")
    except Exception as e:  # noqa: BLE001
        log(f"[警告] 凭据校验未通过（仍写入，便于排障）: {e}")

    total, added = upsert_account(seg_key, account)
    log(f"[完成] 账号已{'新增' if added else '更新'}，{seg_key} 通道共 {total} 个账号")
    log("[提示] 回到 open-ai 账号管理页点「刷新账号」，新账号即可用于对话")
    return 0


if __name__ == "__main__":
    sys.exit(main())
