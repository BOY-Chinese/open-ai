# -*- coding: utf-8 -*-
"""
测试: scripts/api_store.py API 密钥存储管理逻辑
================================================
用临时 config 隔离, 不触碰真实 config.json。
运行: python -m unittest tests.test_api_store -v

v3.0 变更：取消顶层 api_key / api_key_name，统一只在 api_keys 里维护；
create_api 记录 createdAt。相应断言已更新，并新增迁移用例。
"""
import json
import os
import sys
import tempfile
import time
import unittest

# 让 scripts/ 可导入
SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts')
sys.path.insert(0, SCRIPTS_DIR)

import api_store  # noqa: E402


class ApiStoreTest(unittest.TestCase):
    def setUp(self):
        # 独立临时 config, 避免污染真实配置
        self.tmpdir = tempfile.mkdtemp()
        self.tmp_cfg = os.path.join(self.tmpdir, 'config.json')
        self._orig_cfg = api_store.OPENAI_CFG
        api_store.OPENAI_CFG = self.tmp_cfg
        self._write({'api_keys': [{'name': 'base', 'key': 'sk-base'}]})

    def tearDown(self):
        api_store.OPENAI_CFG = self._orig_cfg

    def _write(self, data):
        with open(self.tmp_cfg, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def test_create_auto_names_unnamed(self):
        name, key, created = api_store.create_api()
        self.assertTrue(key.startswith('sk-'))
        self.assertEqual(name, '无名1')
        # 创建时间必须真实记录（此前硬编码 0 → 界面显示 1970）
        self.assertGreater(created, 1_600_000_000)
        self.assertLessEqual(created, int(time.time()) + 5)

    def test_created_at_persisted_and_listed(self):
        _, key, created = api_store.create_api()
        rows = {r[1]: r for r in api_store.list_apis()}
        self.assertIn(key, rows)
        self.assertEqual(rows[key][2], created)

    def test_unnamed_index_increments_and_skips(self):
        self._write({'api_keys': [{'name': '无名1', 'key': 'sk-1'}]})
        name, _, _ = api_store.create_api()
        self.assertEqual(name, '无名2')  # 无名1 已占用, 跳到无名2

    def test_rename_and_delete(self):
        cfg = api_store.load_config()
        self.assertTrue(api_store.rename_api(cfg, 'sk-base', '新名'))
        rows = api_store.list_apis()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], '新名')
        cfg = api_store.load_config()
        self.assertTrue(api_store.delete_api(cfg, 'sk-base'))
        self.assertEqual(len(api_store.list_apis()), 0)

    def test_migrate_legacy_real_key(self):
        """旧版顶层真密钥 → 搬进 api_keys，顶层字段消失。"""
        self._write({'api_key': 'legacy-key', 'api_key_name': '我的主密钥'})
        self.assertTrue(api_store.ensure_api_keys())
        cfg = api_store.load_config()
        self.assertNotIn('api_key', cfg)
        self.assertNotIn('api_key_name', cfg)
        rows = api_store.list_apis()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], '我的主密钥')
        self.assertEqual(rows[0][1], 'legacy-key')

    def test_migrate_placeholder_becomes_real_key(self):
        """空壳配置里的 YOUR_API_KEY_HERE 不是密钥：换成新生成的强密钥。"""
        self._write({'api_key': api_store.LEGACY_PLACEHOLDER, 'api_key_name': ''})
        api_store.ensure_api_keys()
        cfg = api_store.load_config()
        rows = api_store.list_apis()
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0][1], api_store.LEGACY_PLACEHOLDER)
        self.assertTrue(rows[0][1].startswith('sk-'))
        self.assertGreater(rows[0][2], 0)      # createdAt 有值
        self.assertNotIn('api_key', cfg)

    def test_ensure_generates_key_when_list_empty(self):
        """api_keys 为空时必须补一条，否则网关没有任何合法密钥 → 全面 401。"""
        self._write({'api_keys': []})
        self.assertTrue(api_store.ensure_api_keys())
        rows = api_store.list_apis()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0][1].startswith('sk-'))

    def test_ensure_is_idempotent(self):
        self._write({'api_key': 'legacy-key'})
        self.assertTrue(api_store.ensure_api_keys())
        self.assertFalse(api_store.ensure_api_keys())   # 第二次不再改动

    def test_valid_keys_includes_all(self):
        self._write({'api_keys': [{'name': 'a', 'key': 'sk-1'},
                                  {'name': 'b', 'key': 'sk-2'}]})
        keys = api_store.valid_keys()
        self.assertIn('sk-1', keys)
        self.assertIn('sk-2', keys)

    def test_normalize_names_fills_unnamed(self):
        self._write({'api_keys': [{'name': '', 'key': 'sk-1'}]})
        cfg = api_store.load_config()
        api_store.normalize_names(cfg)
        rows = api_store.list_apis()
        self.assertEqual(rows[0][0], '无名1')


class SaveConfigGuardTest(unittest.TestCase):
    """config.json 覆灭防护（2026-09-16 事故：账号被一次静默覆盖全部抹掉）。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tmp_cfg = os.path.join(self.tmpdir, 'config.json')
        self._orig_cfg = api_store.OPENAI_CFG
        api_store.OPENAI_CFG = self.tmp_cfg

    def tearDown(self):
        api_store.OPENAI_CFG = self._orig_cfg

    def _write(self, data):
        with open(self.tmp_cfg, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _read(self):
        with open(self.tmp_cfg, encoding='utf-8') as f:
            return json.load(f)

    def test_drop_providers_is_refused(self):
        """要写的配置丢了 providers → 拒绝覆盖（宁可少写一次密钥）。"""
        self._write({'providers': {'trae': {'accounts': [{'uid': 'u1'}]}},
                     'api_keys': [{'name': 'a', 'key': 'sk-1'}]})
        self.assertFalse(api_store.save_config({'api_keys': [{'name': 'b', 'key': 'sk-2'}]}))
        on_disk = self._read()
        self.assertIn('trae', on_disk['providers'])          # 账号还在
        self.assertEqual(on_disk['api_keys'][0]['key'], 'sk-1')  # 也没被换掉

    def test_normal_write_keeps_providers_and_backups(self):
        """正常写入：providers 原样保留，并留下上一版滚动备份。"""
        self._write({'providers': {'trae': {'accounts': []}},
                     'api_keys': [{'name': 'a', 'key': 'sk-1'}]})
        cfg = api_store.load_config()
        cfg['api_keys'].append({'name': 'b', 'key': 'sk-2'})
        self.assertTrue(api_store.save_config(cfg))
        self.assertEqual(len(self._read()['api_keys']), 2)
        self.assertTrue(os.path.exists(self.tmp_cfg + '.bak-auto'))
        self.assertFalse(os.path.exists(self.tmp_cfg + '.tmp'))  # 临时文件已原子替换

    def test_corrupt_config_is_not_silently_treated_as_empty(self):
        """config.json 坏了 → 抛错，绝不返回 {}（返回 {} 的下一步就是覆盖成空壳）。"""
        with open(self.tmp_cfg, 'w', encoding='utf-8') as f:
            f.write('{"providers": {  <-- 手改坏了')
        with self.assertRaises(Exception):
            api_store.load_config()
        # 而且补密钥这条路径也必须被拦下，不得把坏文件换成空壳
        with self.assertRaises(Exception):
            api_store.ensure_api_keys()

    def test_ensure_keeps_other_top_level_keys(self):
        """迁移只动 api_key/api_key_name：port/host/providers 一律原样。"""
        self._write({'port': 8000, 'providers': {'trae': {}},
                     'api_key': 'legacy', 'api_key_name': '主密钥'})
        self.assertTrue(api_store.ensure_api_keys())
        cfg = self._read()
        self.assertEqual(cfg['port'], 8000)
        self.assertIn('trae', cfg['providers'])
        self.assertNotIn('api_key', cfg)
        self.assertEqual(cfg['api_keys'][0]['key'], 'legacy')


if __name__ == '__main__':
    unittest.main()
