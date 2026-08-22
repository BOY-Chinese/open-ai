#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
API 密钥存储管理 (供 GUI 与网关共用)
====================================
config.json 中的 api_keys 结构:
  "api_keys": [
    {"name": "无名1", "key": "sk-xxxx..."},
    ...
  ]

规则:
  - 创建 API 时自动生成强密钥 (secrets.token_urlsafe, 前缀 sk-)
  - 未命名(空 name)的 API 自动命名为 "无名N" (N 为递增序号, 跳过已占用)
  - 改名: 更新 name 字段; 允许重名, 允许改回空(此时下次保存自动补"无名N")
  - 删除: 从 api_keys 移除
  - 兼容旧配置: 顶层 api_key 作为第一个 API 展示 (不可删除, 可改名)
"""
import json
import os
import secrets

BASE = os.path.dirname(os.path.abspath(__file__))
OPENAI_CFG = os.path.join(BASE, '..', 'config.json')


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


def list_apis(cfg=None):
    """返回 API 列表: [(name, key, is_legacy), ...]。is_legacy 表示来自旧顶层 api_key。"""
    cfg = cfg if cfg is not None else load_config()
    apis = []
    legacy = cfg.get('api_key')
    if legacy:
        # 旧版 api_key 也可有自定义名字 (存于 api_key_name)
        apis.append((cfg.get('api_key_name', ''), legacy, True))
    for a in cfg.get('api_keys') or []:
        apis.append((a.get('name', ''), a.get('key', ''), False))
    return apis


def create_api(cfg=None):
    """创建新 API, 返回 (name, key)。未命名自动命名为 无名N。"""
    cfg = cfg if cfg is not None else load_config()
    key = generate_key()
    name = '无名' + str(_next_unnamed_index(cfg))
    cfg.setdefault('api_keys', []).append({'name': name, 'key': key})
    save_config(cfg)
    return name, key


def rename_api(cfg, old_key, new_name):
    """按 key 改名。new_name 为空字符串则下次保存时自动补 无名N。"""
    new_name = (new_name or '').strip()
    for a in cfg.get('api_keys') or []:
        if a.get('key') == old_key:
            a['name'] = new_name
            save_config(cfg)
            return True
    return False


def rename_legacy_api(cfg, new_name):
    """给旧版顶层 api_key 设置自定义名字 (存于 api_key_name)。"""
    cfg['api_key_name'] = (new_name or '').strip()
    save_config(cfg)
    return True


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
    """返回所有合法 API key 集合 (含旧顶层 api_key)。"""
    cfg = cfg if cfg is not None else load_config()
    keys = set()
    if cfg.get('api_key'):
        keys.add(cfg['api_key'])
    for a in cfg.get('api_keys') or []:
        if a.get('key'):
            keys.add(a['key'])
    return keys
