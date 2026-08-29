# -*- coding: utf-8 -*-
"""
测试: scripts/api_store.py API 密钥存储管理逻辑
================================================
用临时 config 隔离, 不触碰真实 config.json。
运行: python -m unittest tests.test_api_store -v
"""
import json
import os
import sys
import tempfile
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

    def tearDown(self):
        api_store.OPENAI_CFG = self._orig_cfg

    def _write(self, data):
        with open(self.tmp_cfg, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def test_create_auto_names_unnamed(self):
        self._write({'api_key': 'legacy'})
        name, key = api_store.create_api()
        self.assertTrue(key.startswith('sk-'))
        self.assertEqual(name, '无名1')

    def test_unnamed_index_increments_and_skips(self):
        self._write({'api_key': 'legacy',
                     'api_keys': [{'name': '无名1', 'key': 'sk-1'}]})
        name, _ = api_store.create_api()
        self.assertEqual(name, '无名2')  # 无名1 已占用, 跳到无名2

    def test_rename_and_delete(self):
        self._write({'api_key': 'legacy',
                     'api_keys': [{'name': 'a', 'key': 'sk-1'}]})
        cfg = api_store.load_config()
        self.assertTrue(api_store.rename_api(cfg, 'sk-1', '新名'))
        self.assertEqual(api_store.list_apis()[1][0], '新名')
        cfg = api_store.load_config()
        self.assertTrue(api_store.delete_api(cfg, 'sk-1'))
        self.assertEqual(len(api_store.list_apis()), 1)  # 只剩 legacy

    def test_legacy_rename(self):
        self._write({'api_key': 'legacy-key'})
        cfg = api_store.load_config()
        api_store.rename_legacy_api(cfg, '我的主密钥')
        rows = api_store.list_apis()
        self.assertEqual(rows[0][0], '我的主密钥')
        self.assertEqual(rows[0][1], 'legacy-key')

    def test_valid_keys_includes_legacy_and_all(self):
        self._write({'api_key': 'legacy-key',
                     'api_keys': [{'name': 'a', 'key': 'sk-1'},
                                  {'name': 'b', 'key': 'sk-2'}]})
        keys = api_store.valid_keys()
        self.assertIn('legacy-key', keys)
        self.assertIn('sk-1', keys)
        self.assertIn('sk-2', keys)

    def test_normalize_names_fills_unnamed(self):
        self._write({'api_key': 'legacy',
                     'api_keys': [{'name': '', 'key': 'sk-1'}]})
        cfg = api_store.load_config()
        api_store.normalize_names(cfg)
        rows = api_store.list_apis()
        # 找到 sk-1 的名字应为 无名1
        self.assertEqual(rows[1][0], '无名1')


if __name__ == '__main__':
    unittest.main()