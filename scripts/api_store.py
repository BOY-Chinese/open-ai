#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
API 密钥存储管理 (供网关与管理面 admin_api 共用)
================================================
config.json 中的 api_keys 结构:
  "api_keys": [
    {"name": "工作机", "key": "sk-xxxx...", "createdAt": 1789000000},
    ...
  ]

规则:
  - 创建 API 时自动生成强密钥 (secrets.token_urlsafe, 前缀 sk-)，并记录 createdAt
  - 未命名(空 name)的 API 自动命名为 "无名N" (N 为递增序号, 跳过已占用)
  - 改名: 更新 name 字段; 允许重名, 允许改回空(此时下次保存自动补"无名N")
  - 删除: 从 api_keys 移除
  - createdAt 单位为秒级时间戳; 老配置里没有该字段的条目返回 0，
    由前端显示为「—」而不是伪造时间（1970 是 bug，不是数据）

★ v3.0 变更：**取消顶层 api_key / api_key_name**，密钥统一只在 api_keys 里维护。

  旧结构（顶层 api_key + api_key_name）会带来两个实际问题：
    1. 安装包的空壳配置 config.shell.json 里是 `"api_key": "YOUR_API_KEY_HERE"`，
       管理面把它当成一条真实密钥列出来（虚拟机实测显示为「无名1 YOUR_API_KEY_HERE」），
       用户会以为这是可用密钥；
    2. 同一个概念有两个存放位置，改名/删除要分两套代码路径，且顶层那条
       「不可删除」，用户无法清理。
  `migrate_legacy_api_key()` 负责一次性迁移：把顶层密钥搬进 api_keys，
  占位符（没填过的空壳）直接换成新生成的强密钥，然后删掉两个顶层字段。

★ 2026-09-16 加固（config.json 被一次静默覆盖抹掉全部账号之后补的）：

  1. `load_config()` 在**文件存在但读不懂**时抛异常，不再静默返回 {}。
     返回 {} 的下一步就是「补一条密钥 → 整体覆盖写回」，配置里的账号/token
     就是这么没的 —— 静默的「空配置」比一次报错危险得多。
  2. `save_config()` 三道防线：防缩水守卫（原配置有 providers、新配置没有 →
     拒绝写入）、写入前滚动备份 `config.json.bak-auto`、临时文件 + os.replace
     原子替换（不再有半截 JSON）。
  3. 写盘失败一律**显式**（`_save_or_raise`）：调用方不能一边改着内存、
     一边把没落盘的密钥报成「创建成功」。
"""
import json
import logging
import os
import secrets
import shutil
import time

# ★ 打包态 __file__ 指向 _MEI 临时目录, config 必须钉死安装根 (见 app_paths.py)
try:
    import app_paths as _ap
    OPENAI_CFG = _ap.CONFIG_PATH
except Exception:  # 源码态: __file__ 是绝对路径, 兜底安全
    BASE = os.path.dirname(os.path.abspath(__file__))
    OPENAI_CFG = os.path.join(BASE, '..', 'config.json')

# 日志走标准 logging: 打包态（task/网关）里 print 会进黑洞, 而「拒绝覆盖配置」
# 这种事必须留下痕迹, 否则就成了又一次静默事故。
_log = logging.getLogger("openapi.api_store")

# 安装包空壳配置里的占位符：它从来不是真密钥，不该被当成密钥保留
LEGACY_PLACEHOLDER = 'YOUR_API_KEY_HERE'

# 名字都没给时的兜底名（迁移出来的那条）
DEFAULT_NAME = '默认密钥'


def load_config():
    """读取 config.json。

    ★ 文件存在但读不懂时**抛异常**，绝不静默返回 {}（2026-09-16 事故的一环）：
      调用方拿到 {} 只会以为「这是个空配置」，接着走「补一条密钥」的正常路径，
      顺手把整个配置覆盖成 {"api_keys": [...]} 的空壳 —— 4 个通道的账号、
      token、device_id 就是这么永久丢掉的（当时既无 git 也无归档可退）。
      读不懂就报错，让上层提示用户去修配置；静默的「空配置」比一次报错危险得多。
      文件**不存在**仍返回 {}：那是全新安装的合法状态。
    """
    if not os.path.exists(OPENAI_CFG):
        return {}
    with open(OPENAI_CFG, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError('config.json 顶层不是 JSON 对象，拒绝当作空配置处理')
    return data


def save_config(cfg):
    """把配置写回 config.json（原子替换 + 防缩水守卫 + 滚动备份）。

    三道防线，全部针对 2026-09-16「config.json 被覆灭」那次事故：
      1) **防缩水守卫**：磁盘上原配置有 providers（账号都在里面），而本次要写的
         配置没有 → 拒绝写入并返回 False。宁可少写一次密钥，也不接受
         「一次静默覆盖抹掉全部账号」——那次事故就是这么发生的，且无从追溯。
      2) **滚动备份**：写入前把上一版复制成 config.json.bak-auto，
         保证任何时候都能退回上一版（该文件已由 .gitignore 排除）。
      3) **原子替换**：先写同目录临时文件再 os.replace。中途崩溃/断电不会留下
         半截 JSON —— 半截 JSON 会让网关启动直接失败，比写失败更难查。

    返回 True 表示已落盘；False 表示被守卫拦下（原因写进日志）。
    """
    previous = None
    if os.path.exists(OPENAI_CFG):
        try:
            previous = load_config()
        except Exception as e:  # noqa: BLE001
            _log.error("config.json 无法解析，为避免覆盖掉可手工抢救的内容，"
                       "本次写入已拒绝: %s", e)
            return False

    if (previous or {}).get('providers') and not (cfg.get('providers') or {}):
        _log.error("拒绝写入 config.json：新配置不含 providers，"
                   "会把 %d 个通道的账号一起抹掉（如需清空请手工编辑文件）",
                   len(previous.get('providers') or {}))
        return False

    tmp = OPENAI_CFG + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())

    if previous is not None:
        try:
            shutil.copyfile(OPENAI_CFG, OPENAI_CFG + '.bak-auto')
        except Exception as e:  # noqa: BLE001
            _log.warning("滚动备份 config.json 失败（不影响本次写入）: %s", e)

    os.replace(tmp, OPENAI_CFG)
    return True


def _save_or_raise(cfg):
    """写入失败即抛错：静默失败会让界面显示一条**根本不存在**的密钥。"""
    if not save_config(cfg):
        raise RuntimeError(
            '写入 config.json 被拒绝：本次改动会覆盖掉现有通道配置，'
            '请检查 config.json 是否损坏（网关日志 openapi.api_store 有详情）')


def generate_key():
    """生成强随机 API 密钥。"""
    return 'sk-' + secrets.token_urlsafe(32)


def _next_unnamed_index(cfg):
    """返回下一个可用的"无名N"序号 (跳过已占用)。"""
    used = set()
    for a in cfg.get('api_keys') or []:
        n = a.get('name') or ''
        if n.startswith('无名') and n[2:].isdigit():
            used.add(int(n[2:]))
    i = 1
    while i in used:
        i += 1
    return i


def ensure_api_keys(cfg=None):
    """统一密钥结构：迁移旧版顶层 api_key，并保证至少有一条可用密钥（幂等）。

    为什么必须「保证至少一条」：安装包的空壳配置只有一个占位符，
    迁移时它会被丢弃；若就此留下空列表，网关将没有任何合法密钥，
    桌面端拿不到 Bearer → 整个管理面 401，用户连界面都用不了。
    所以这里补生成一条真密钥（名为 DEFAULT_NAME）。

    返回值: True 表示配置被改写并已落盘。
    """
    # ★ 在**副本**上改，落盘成功才算数：否则写入被守卫拦下时，
    #   调用方手里那份 cfg 会多出一条「只存在于内存里的密钥」，
    #   API 管理页会把它列出来，用户复制去做请求却 401 —— 又一种「看着正常」的假象。
    cfg = json.loads(json.dumps(cfg)) if cfg is not None else load_config()
    changed = False

    # ── 1) 旧版顶层 api_key / api_key_name → api_keys ──
    legacy = (cfg.get('api_key') or '').strip()
    legacy_name = (cfg.get('api_key_name') or '').strip()
    if legacy:
        existing = {a.get('key') for a in (cfg.get('api_keys') or [])}
        if legacy not in existing:
            # 空壳配置里的占位符不是真密钥：换成新生成的，而不是搬过去
            key = generate_key() if legacy == LEGACY_PLACEHOLDER else legacy
            cfg.setdefault('api_keys', []).append({
                'name': legacy_name or DEFAULT_NAME,
                'key': key,
                'createdAt': int(time.time()),
            })
        changed = True

    for field in ('api_key', 'api_key_name'):
        if field in cfg:
            cfg.pop(field, None)
            changed = True

    # ── 2) 至少保证一条可用密钥 ──
    if not (cfg.get('api_keys') or []):
        cfg.setdefault('api_keys', []).append({
            'name': DEFAULT_NAME,
            'key': generate_key(),
            'createdAt': int(time.time()),
        })
        changed = True

    if changed:
        # 写不进去就不算「已统一」：返回 False 让调用方（main 的启动日志 /
        # admin_api 的重读分支）知道配置还是旧样子，别报一条假成功。
        _save_or_raise(cfg)
    return changed


# 兼容旧调用名（迁移逻辑已并入 ensure_api_keys）
migrate_legacy_api_key = ensure_api_keys


def list_apis(cfg=None):
    """返回 API 列表: [(name, key, created_at), ...]（created_at 未知时为 0）。"""
    cfg = cfg if cfg is not None else load_config()
    apis = []
    for a in cfg.get('api_keys') or []:
        try:
            created = int(a.get('createdAt') or 0)
        except (TypeError, ValueError):
            created = 0
        apis.append((a.get('name', ''), a.get('key', ''), created))
    return apis


def create_api(cfg=None, name=''):
    """创建新 API, 返回 (name, key, createdAt)。未命名自动命名为 无名N。"""
    cfg = cfg if cfg is not None else load_config()
    key = generate_key()
    name = (name or '').strip() or ('无名' + str(_next_unnamed_index(cfg)))
    created = int(time.time())
    cfg.setdefault('api_keys', []).append(
        {'name': name, 'key': key, 'createdAt': created})
    _save_or_raise(cfg)
    return name, key, created


def rename_api(cfg, old_key, new_name):
    """按 key 改名。new_name 为空字符串则下次保存时自动补 无名N。"""
    new_name = (new_name or '').strip()
    for a in cfg.get('api_keys') or []:
        if a.get('key') == old_key:
            a['name'] = new_name
            _save_or_raise(cfg)
            return True
    return False


def delete_api(cfg, key):
    """按 key 删除。返回是否删除成功。"""
    apis = cfg.get('api_keys') or []
    for i, a in enumerate(apis):
        if a.get('key') == key:
            del apis[i]
            _save_or_raise(cfg)
            return True
    return False


def normalize_names(cfg):
    """把所有空 name 的 API 补成 无名N (保存)。"""
    changed = False
    for a in cfg.get('api_keys') or []:
        if not (a.get('name') or '').strip():
            a['name'] = '无名' + str(_next_unnamed_index(cfg))
            changed = True
    if changed:
        _save_or_raise(cfg)
    return cfg


def valid_keys(cfg=None):
    """返回所有合法 API key 集合。

    顶层 api_key 仅作为**兜底**保留（正常情况已被 migrate_legacy_api_key
    搬进 api_keys 并删除）；这样即使迁移因权限等原因没能落盘，
    网关也不会突然拒绝所有客户端。
    """
    cfg = cfg if cfg is not None else load_config()
    keys = set()
    if cfg.get('api_key'):
        keys.add(cfg['api_key'])
    for a in cfg.get('api_keys') or []:
        if a.get('key'):
            keys.add(a['key'])
    return keys
