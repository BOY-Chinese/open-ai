# -*- coding: utf-8 -*-
"""cred_store.py —— 续期后的凭据落盘 + 跨进程互斥

## 为什么存在（2026-10-23 CodeArts 账号被「烧死」事故）

华为云 STS 的 refresh token 是**单次使用**的：每次续期都换发新的，旧的立刻作废。
而本项目的续期结果此前**只留在进程内存里，从不落盘**，于是：

* 网关续期成功 → 内存里是新 token，config.json 里还是旧的（已被消耗）；
* 下一次请求 `_load_accounts()` 又用磁盘旧值**把内存覆盖掉**
  （CodeArts 老实现是直接 ``self.accounts = fresh``，连合并都没有）→ 新 token 当场丢失；
* 之后再有任何一次续期（重启、task / CLI / 桌面端第二个进程）都会拿**旧 token**
  去换 → 华为判「refresh token 已被使用」→ **整条凭据链作废，只能重新登录**。

实测现场（2026-10-23 00:32）：config.json 里的 refreshToken 与 23:09 的备份**逐字节
相同**、`expiresAt` 停在登录时刻 +2 小时；日志里第一次出现

    STS5.1806 invalid refresh token: 'the refresh token has been used'

紧随其后整条通道 401（`APIG.0301 decrypt token fail`），`ca-` 全部不可用。

## 这一层提供什么

1. ``credentials_newer(a, b)``：两份凭据谁更新（先比 `refreshedAt`，再比 `expiresAt`）
   —— 内存/磁盘合并的唯一口径，避免「谁覆盖谁」各写一套；
2. ``read_account`` / ``persist_account``：只按 uid 改**自己那一条**账号的凭据字段，
   写入复用 ``scripts/api_store.save_config``（防缩水守卫 + 滚动备份
   `config.json.bak-auto` + 原子替换 + fsync），绝不整份重建配置；
3. ``account_lock``：跨进程互斥（``data/.cred-refresh-<段>.lock``）。续期前拿锁，
   **锁内重读磁盘**：别的进程刚续期过就直接采用它的新凭据，而不是拿同一个
   refresh token 去撞第二次（撞第二次 = 判复用 = 账号报废）。
   锁等待超时/文件陈旧一律 fail-open —— 宁可退化成旧行为，也不能把请求卡死。
"""
from __future__ import annotations

import contextlib
import logging
import os
import sys
import time

logger = logging.getLogger("openapi.providers.cred_store")

# 凭据字段：续期后需要回写磁盘的那几个（按段给，缺字段就跳过）
_CODEARTS_FIELDS = ("refreshToken", "credentials", "expiresAt", "refreshedAt")
_QODER_FIELDS = ("accessToken", "refreshToken", "expiresAt", "refreshedAt")
_WORKBUDDY_FIELDS = ("accessToken", "refreshToken", "expiresAt", "refreshedAt")

# 段名 → 回写字段（未登记的段默认按 Qoder 那套，多写的字段不会伤到别人）
FIELDS_BY_SEGMENT = {
    "codearts": _CODEARTS_FIELDS,
    "qoder_cn": _QODER_FIELDS,
    "qoder_intl": _QODER_FIELDS,
    "workbuddy": _WORKBUDDY_FIELDS,
    "workbuddy_intl": _WORKBUDDY_FIELDS,
}

# 段名 → 账号主键字段名（workbuddy 族用 userId，其余用 uid）
UID_KEY_BY_SEGMENT = {
    "workbuddy": "userId",
    "workbuddy_intl": "userId",
}

_LOCK_TIMEOUT = 30.0     # 等锁上限（超过就 fail-open）
_LOCK_STALE = 120.0      # 锁文件超过这个岁数视为残留（持锁进程已死）


def _api_store():
    """取 scripts/api_store（它带了 config.json 的三道写盘防线）。

    scripts/ 不在 sys.path 时自己补上：网关在 startup 里补过一次，但
    task / CLI 进程不一定，而「续期落盘」随时可能被它们触发。
    """
    try:
        import api_store  # type: ignore
        return api_store
    except Exception:  # noqa: BLE001
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        scripts = os.path.join(root, "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import api_store  # type: ignore
        return api_store


def config_path() -> str:
    try:
        import app_paths as _ap  # type: ignore
        return _ap.CONFIG_PATH
    except Exception:  # noqa: BLE001
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")


def _lock_dir() -> str:
    try:
        import app_paths as _ap  # type: ignore
        base = _ap.DATA_DIR
    except Exception:  # noqa: BLE001
        base = os.path.join(os.path.dirname(config_path()), "data")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        base = os.path.dirname(config_path())
    return base


def credentials_newer(a: dict, b: dict) -> bool:
    """a 的凭据是否比 b 新（缺失一律当 0）。

    先比 ``refreshedAt``（续期时刻，最可靠），再退到 ``expiresAt``
    —— 老配置没有 refreshedAt，而续期一定会把有效期往后推。

    ★ ``refreshedAt`` 只在**两边都有值**时才说话：一边为 0 说明那份是旧版本
      写下的（从来没记过续期时刻），拿它跟真值比会得出「没记过的那份更新」
      这种反直觉结论（实测：磁盘上有效期更晚的凭据被判成更旧）。
    """
    if not isinstance(a, dict) or not a:
        return False
    if not isinstance(b, dict) or not b:
        return True
    ra, rb = int(a.get("refreshedAt") or 0), int(b.get("refreshedAt") or 0)
    if ra and rb and ra != rb:
        return ra > rb
    return int(a.get("expiresAt") or 0) > int(b.get("expiresAt") or 0)


def read_account(seg_key: str, uid: str, key: str = "") -> dict:
    """从 config.json 读**当前磁盘上**的那条账号；没有就返回 {}。

    :param key: 账号主键字段名（缺省按段自动选：workbuddy 族是 ``userId``）
    """
    uid = str(uid or "").strip()
    if not uid:
        return {}
    key = key or UID_KEY_BY_SEGMENT.get(seg_key, "uid")
    try:
        cfg = _api_store().load_config()
    except Exception as e:  # noqa: BLE001
        logger.warning("读 config.json 失败（按无新凭据处理）: %s", e)
        return {}
    seg = ((cfg.get("providers") or {}).get(seg_key) or {})
    for acc in seg.get("accounts") or []:
        if str(acc.get(key) or "").strip() == uid:
            return dict(acc)
    return {}


def persist_account(seg_key: str, uid: str, fields: dict, key: str = "") -> bool:
    """把续期后的凭据字段回写到 config.json 里对应的那条账号。

    * 只动 ``providers.<seg_key>.accounts[uid]`` 的几个字段，其余原样保留
      （绝不整份重建配置 —— 2026-09-16「配置被覆灭」事故的教训）；
    * 写入走 ``api_store.save_config``：防缩水守卫 + 滚动备份 + 原子替换；
    * 任何失败都只记日志、返回 False，**绝不让续期因为写盘问题失败**
      （内存里已经拿到新凭据了，本次请求照常可用）。

    :param key: 账号主键字段名（缺省按段自动选）
    """
    uid = str(uid or "").strip()
    if not uid:
        logger.warning("续期凭据未落盘：账号没有 uid（无法定位要改哪一条）")
        return False
    key = key or UID_KEY_BY_SEGMENT.get(seg_key, "uid")
    keys = FIELDS_BY_SEGMENT.get(seg_key, _QODER_FIELDS)
    patch = {k: fields[k] for k in keys if k in fields and fields[k] not in (None, "")}
    if not patch:
        return False
    try:
        store = _api_store()
        cfg = store.load_config()
        seg = ((cfg.get("providers") or {}).get(seg_key) or {})
        accs = seg.get("accounts") or []
        target = None
        for acc in accs:
            if str(acc.get(key) or "").strip() == uid:
                target = acc
                break
        if target is None:
            logger.warning("续期凭据未落盘：%s 段里找不到账号 %s", seg_key, uid[:8])
            return False
        before = {k: str(target.get(k))[:16] for k in patch}
        target.update(patch)
        if not store.save_config(cfg):
            logger.error("续期凭据落盘被拒绝（%s 段 uid=%s）", seg_key, uid[:8])
            return False
        logger.info("续期凭据已落盘: %s uid=%s 字段=%s（写前 %s）",
                    seg_key, uid[:8], ",".join(sorted(patch)), before)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("续期凭据落盘失败（不影响本次请求）: %s", e)
        return False


def _lock_path(seg_key: str, uid: str) -> str:
    safe = "".join(ch for ch in str(uid or "") if ch.isalnum())[:16] or "all"
    return os.path.join(_lock_dir(), ".cred-refresh-%s-%s.lock" % (seg_key, safe))


def lock_acquire(seg_key: str, uid: str, timeout: float = _LOCK_TIMEOUT):
    """拿跨进程续期锁；返回锁文件路径（拿到）或 ``None``（超时/不可用 → fail-open）。

    给**异步** provider 用（``await asyncio.to_thread(lock_acquire, …)``），
    免得同步等待把事件循环卡住；同步路径直接用 ``account_lock`` 更省事。
    """
    path = _lock_path(seg_key, uid)
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return path
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > _LOCK_STALE:
                    logger.warning("续期锁已陈旧，强制接管: %s", path)
                    os.remove(path)
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                logger.warning("等续期锁超时（%s），本次退化为无锁续期", path)
                return None
            time.sleep(0.15)
        except OSError as e:  # noqa: BLE001
            logger.warning("续期锁不可用（%s）: %s", path, e)
            return None


def lock_release(path) -> None:
    """释放续期锁（幂等）。"""
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


@contextlib.contextmanager
def account_lock(seg_key: str, uid: str, timeout: float = _LOCK_TIMEOUT):
    """跨进程续期互斥（同步版）；拿不到锁也照常继续（fail-open）。"""
    path = lock_acquire(seg_key, uid, timeout)
    try:
        yield path is not None
    finally:
        lock_release(path)
