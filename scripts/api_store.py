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
"""
import json
import os
import secrets
import time

BASE = os.path.dirname(os.path.abspath(__file__))
OPENAI_CFG = os.path.join(BASE, '..', 'config.json')

# 安装包空壳配置里的占位符：它从来不是真密钥，不该被当成密钥保留
LEGACY_PLACEHOLDER = 'YOUR_API_KEY_HERE'

# 名字都没给时的兜底名（迁移出来的那条）
DEFAULT_NAME = '默认密钥'


def load_config():
    try:
        with open(OPENAI_CFG, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    with open(OPENAI_CFG, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


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
    cfg = cfg if cfg is not None else load_config()
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
        save_config(cfg)
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
    save_config(cfg)
    return name, key, created


def rename_api(cfg, old_key, new_name):
    """按 key 改名。new_name 为空字符串则下次保存时自动补 无名N。"""
    new_name = (new_name or '').strip()
    for a in cfg.get('api_keys') or []:
        if a.get('key') == old_key:
            a['name'] = new_name
            save_config(cfg)
            return True
    return False


def delete_api(cfg, key):
    """按 key 删除。返回是否删除成功。"""
    apis = cfg.get('api_keys') or []
    for i, a in enumerate(apis):
        if a.get('key') == key:
            del apis[i]
            save_config(cfg)
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
        save_config(cfg)
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
