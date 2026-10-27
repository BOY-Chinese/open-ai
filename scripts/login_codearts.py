#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
华为云 CodeArts Agent（码道）登录助手
=====================================
用法::

    python scripts/login_codearts.py                 # 默认：拉起浏览器走授权
    python scripts/login_codearts.py --browser chrome # 指定浏览器
    python scripts/login_codearts.py --print-only     # 只打印授权链接（不拉浏览器）
    python scripts/login_codearts.py --timeout 300
    python scripts/login_codearts.py --callback-url "http://127.0.0.1:PORT/oauth/callback?code=…"
                                                      # 回调页打不开时，手工粘贴地址栏 URL
    python scripts/login_codearts.py --reuse-profile   # 复用上次浏览器会话

链路（逆向自 huaweicloud.authentication 扩展，见 providers/codearts.py）::

  1. 生成 ticket_id / secret / PKCE(verifier, challenge) / DPoP(P-256) 私钥
  2. 本地监听 ``127.0.0.1:{port}/oauth/callback``
  3. 用 Playwright 拉起**全新 profile** 的浏览器打开 ``{portal}/authorize?…``
     （无历史 cookie，便于连续添加多个不同账号）
  4. 双通道拿结果（哪个先到用哪个）：
       a. 回调带 ``code`` → POST ``{sts}/v1/oauth2/tokens``（authorization_code）
       b. 轮询 ``{snap-manager}/v1/login/ticket?ticket_id=&secret=``（ticket 兜底）
  5. 响应含 STS 三件套 + refresh_token，连同 client_id / DPoP 私钥一起落盘

★ 为什么 client_id 与 DPoP 私钥必须随 refresh_token 一起存：
  refresh_token 与这三者**绑定**，刷新时换任何一个都会被 STS 拒
  （invalid client id / InvalidDPoPHeader / the refresh token has been used）。

★ 换 token 的端点是 **STS**（``sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens``），
  不是 snap-manager（那个返回 404 APIG.0101，2026-10-22 实测确认）。

写回位置：``providers.codearts.accounts[]``，同 uid 覆盖更新。
"""
import json
import os
import socket
import sys
import threading
import time
import urllib.parse

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

LOG_PATH = os.path.join(LOGS_DIR, "login_codearts.log")
SEG_KEY = "codearts"
# 待授权状态落盘位置：`--callback-url` 补救通道要靠它拿到**同一次运行**的
# PKCE verifier 与 DPoP 私钥 —— 这两样与授权码是配对的，重新生成一对必然换不出 token。
PENDING_PATH = os.path.join(DATA_DIR, ".codearts_login.json")


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
    opts = {"timeout": 300.0, "print_only": False, "callback": "",
            "browser": "", "reuse_profile": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--timeout" and i + 1 < len(argv):
            try:
                opts["timeout"] = max(30.0, float(argv[i + 1]))
            except ValueError:
                pass
            i += 2
        elif a == "--print-only":
            opts["print_only"] = True
            i += 1
        elif a == "--callback-url" and i + 1 < len(argv):
            opts["callback"] = argv[i + 1].strip()
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
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def upsert_account(account: dict) -> tuple[int, bool]:
    cfg = load_config()
    prov = cfg.setdefault("providers", {})
    seg = prov.setdefault(SEG_KEY, {"enabled": True, "timeout": 600, "switchEvery": 5})
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


def _save_pending(login: dict, port: int) -> None:
    """把本次授权会话的配对信息落盘（供 --callback-url 补救通道复用）。"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(PENDING_PATH, "w", encoding="utf-8") as f:
            json.dump({"verifier": login.get("verifier"), "dpopKey": login.get("dpopKey"),
                       "ticketId": login.get("ticketId"), "secret": login.get("secret"),
                       "port": port, "ts": int(time.time())}, f, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        log(f"[警告] 待授权状态落盘失败（--callback-url 补救将不可用）: {e}")


def _load_pending(max_age: int = 1800) -> dict:
    """读取未过期的待授权状态（默认 30 分钟）。取不到返回空 dict。"""
    try:
        with open(PENDING_PATH, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(d, dict) or not d.get("verifier"):
        return {}
    if int(time.time()) - int(d.get("ts") or 0) > max_age:
        log("[提示] 上次授权会话已超过 30 分钟，请重新运行本脚本发起授权")
        return {}
    return d


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


class _CallbackCatcher:
    """极简 127.0.0.1 回调监听（只服务一次 /oauth/callback）。

    不引第三方 web 框架：这里只需要读一行 HTTP 请求行拿 query，然后回一个
    「可以关掉这个页面了」的 HTML。
    """

    def __init__(self, port: int):
        self.port = port
        self.result: dict = {}
        self._srv = None

    def start(self) -> None:
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", self.port))
            srv.listen(4)
        except OSError as e:
            log(f"[回调] 端口 {self.port} 无法监听（将退回 ticket 轮询）: {e}")
            self._srv = None
            return
        self._srv = srv

        def loop():
            srv.settimeout(1.0)
            while self._srv is srv and not self.result:
                try:
                    conn, _addr = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    conn.settimeout(5.0)
                    raw = conn.recv(8192).decode("utf-8", "replace")
                    first = raw.split("\r\n", 1)[0]
                    parts = first.split(" ")
                    path = parts[1] if len(parts) > 1 else "/"
                    q = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
                    got = {k: (v[0] if v else "") for k, v in q.items()}
                    body = ("<html><meta charset='utf-8'><body style='font-family:sans-serif;"
                            "padding:40px'><h2>授权已完成</h2><p>可以关闭这个页面，"
                            "回到 open-ai 桌面端继续。</p></body></html>")
                    conn.sendall(("HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                                  f"Content-Length: {len(body.encode())}\r\nConnection: close\r\n\r\n"
                                  ).encode() + body.encode())
                    # 只认带 code / secret 的那一次请求（浏览器可能先探 /favicon.ico）
                    if got.get("code") or got.get("secret"):
                        self.result = got
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass

        threading.Thread(target=loop, daemon=True).start()

    def stop(self) -> None:
        srv, self._srv = self._srv, None
        if srv is not None:
            try:
                srv.close()
            except Exception:  # noqa: BLE001
                pass


def _redact(data: dict) -> dict:
    """去掉/掩盖响应里的凭据，只留排障需要的字段。"""
    out = {}
    for k, v in (data or {}).items():
        if k in ("credentials", "credential"):
            c = v if isinstance(v, dict) else {}
            out[k] = {kk: ("***" if "secret" in kk or "token" in kk else vv)
                      for kk, vv in c.items()}
        elif "token" in k.lower() and isinstance(v, str):
            out[k] = f"***({len(v)})"
        else:
            out[k] = v
    return out


def _finish(account: dict) -> int:
    uid = account.get("uid")
    cred = account.get("credentials") or {}
    log(f"[成功] 已获取凭据: uid={uid} name={account.get('name')}"
        f" STS={'有' if cred.get('securityToken') else '无'}"
        f" refreshToken={'有' if account.get('refreshToken') else '无'}")
    try:
        from providers import codearts as ca  # type: ignore
        credits, err = ca.fetch_credits(account)
        log(f"[校验] 积分余额: {credits if not err else '查询失败 ' + err}")
    except Exception as e:  # noqa: BLE001
        log(f"[警告] 凭据校验未通过（仍写入，便于排障）: {e}")
    total, added = upsert_account(account)
    log(f"[完成] 账号已{'新增' if added else '更新'}，codearts 通道共 {total} 个账号")
    log("[提示] 回到 open-ai 账号管理页点「刷新账号」，新账号即可用于对话")
    return 0


def main() -> int:
    opts = parse_args(sys.argv[1:])
    from providers import codearts as ca  # type: ignore

    log("=" * 60)
    log("  华为云 CodeArts Agent（码道）登录助手")
    log(f"  写回位置: config.json → providers.{SEG_KEY}.accounts[]")
    log("=" * 60)

    # ── 分支一：手工粘贴回调 URL（远程/回调页打不开时） ──
    if opts["callback"]:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(opts["callback"]).query)
        code = (q.get("code") or [""])[0]
        if not code:
            log("[失败] 回调 URL 里没有 code 参数")
            return 1
        log("[回调] 已从粘贴的地址里取到 code，开始换取 token …")
        # ★ 必须复用**发起授权那一次运行**的 verifier 与 DPoP 私钥：
        #   授权码与 PKCE verifier 是配对的，DPoP 公钥又和 refresh_token 绑定，
        #   重新生成任何一样都换不出可用凭据（2026-10-22 修正）。
        pending = _load_pending()
        if not pending:
            log("[失败] 找不到本次授权会话的配对信息（data/.codearts_login.json）。")
            log("       请先正常运行一次本脚本（会打印授权链接），再带着 code 回来；")
            log("       或直接在能访问 127.0.0.1 的机器上完成授权。")
            return 1
        verifier = str(pending.get("verifier") or "")
        dpop_key = pending.get("dpopKey") or {}
        port = int(pending.get("port") or 0) or \
            (urllib.parse.urlsplit(opts["callback"]).port or 0)
        try:
            data = ca.exchange_code(code, verifier, port, dpop_key)
        except Exception as e:  # noqa: BLE001
            log(f"[失败] 换 token 失败: {e}")
            return 1
        log("[响应] " + json.dumps(_redact(data), ensure_ascii=False)[:500])
        account = ca.accounts_from_token_response(data, verifier, dpop_key)
        return _finish(account)

    port = _free_port()
    login = ca.start_login(port)
    _save_pending(login, port)
    log(f"[授权] 本地回调端口: {port}")
    log(f"[授权] 授权链接: {login['authUrl']}")

    catcher = _CallbackCatcher(port)
    catcher.start()

    pw = ctx = None
    page = None
    if not opts["print_only"]:
        try:
            pw_login.ensure_browsers_path()
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            ctx, _name = pw_login.launch_browser(
                pw, opts["browser"], opts["reuse_profile"],
                root=pw_login.profile_root("codearts", DATA_DIR), log=log)
            page = pw_login.first_page(ctx, login["authUrl"], log=log)
            if page is None:
                log("[警告] 打不开授权页，改为只等待回调/轮询（可手工复制上面的链接）")
        except Exception as e:  # noqa: BLE001
            log(f"[警告] 浏览器无法启动（改为只等待回调）: {e}")
            log(pw_login.browsers_hint())
            page = None
    else:
        log("[提示] --print-only：请手工在浏览器打开上面的授权链接")

    log("[等待] 等待授权回调 / ticket 下发 …")
    deadline = time.time() + opts["timeout"]
    data = None
    last_note = ""
    try:
        while time.time() < deadline:
            cb = dict(catcher.result)
            if cb.get("code"):
                log("[回调] 收到授权码，正在换取 token …")
                try:
                    data = ca.exchange_code(cb["code"], login["verifier"], port,
                                            login["dpopKey"])
                    log("[响应] " + json.dumps(_redact(data), ensure_ascii=False)[:500])
                    break
                except Exception as e:  # noqa: BLE001
                    log(f"[错误] 用授权码换 token 失败: {e}")
                    return 1
            r = ca.poll_ticket(login["ticketId"], login["secret"])
            if r.get("status") == "ok" and isinstance(r.get("data"), dict):
                d = r["data"]
                if d.get("credentials") or d.get("credential"):
                    log("[ticket] 已收到凭据，正在保存 …")
                    log("[响应] " + json.dumps(_redact(d), ensure_ascii=False)[:500])
                    data = d
                    break
            if r.get("status") == "error":
                note = r.get("message") or ""
                if note and note != last_note:
                    log(f"  [进度] {note}")
                    last_note = note
            if page is not None and not pw_login.tick(page, 1200):
                log("[提示] 浏览器窗口已被关闭，继续等待回调 …")
                page = None
    finally:
        catcher.stop()
        if ctx is not None:
            pw_login.close_quietly(ctx)
        if pw is not None:
            try:
                pw.stop()
            except Exception:  # noqa: BLE001
                pass

    if not data:
        log(f"[失败] {int(opts['timeout'])} 秒内未完成授权。自查：")
        log("  1) 浏览器里是否走到了华为云授权页的最后一步（点「同意授权」）？")
        log("  2) 若浏览器提示「无法访问 127.0.0.1」——把地址栏整条 URL 复制下来，")
        log("     用 --callback-url \"<粘贴的URL>\" 重跑一次（远程/跨机场景）。")
        log("  3) 授权链接是否已过期？重新运行本脚本即可（ticket 有效期约 10 分钟）。")
        return 1

    account = ca.accounts_from_token_response(
        data, login["verifier"], login["dpopKey"], ca.CLIENT_ID)
    return _finish(account)


if __name__ == "__main__":
    sys.exit(main())
