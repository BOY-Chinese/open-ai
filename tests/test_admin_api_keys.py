# -*- coding: utf-8 -*-
"""
测试: 管理面 API 密钥端点的「不许删空」守卫
==========================================
背景（2026-09-16 实测事故，用户报「拿不到后端数据」）：
  密钥是管理面唯一的凭据。用户在「API 管理」页把密钥依次删完的那一刻，
  正在发请求的客户端（桌面端自己）当场全部 401，而且**无法自救** ——
  「创建密钥」本身也要鉴权。界面从此只剩「无法连接后端网关 /
  界面已就绪，但拿不到后端数据」，唯一的出路是重启桌面端，
  而重启网关又会补一条**新**密钥，让还开着的客户端继续 401。

  故 `POST /v1/admin/api-keys/delete` 拒绝删掉最后一条。

不联网、不碰真实 config：把 api_store.OPENAI_CFG 指到临时文件后再调端点协程。
运行: python -m unittest tests.test_admin_api_keys -v
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'scripts'))

import admin_api as A  # noqa: E402
import api_store  # noqa: E402


class ApiKeyDeleteGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tmp_cfg = os.path.join(self.tmpdir, 'config.json')
        self._orig_cfg = api_store.OPENAI_CFG
        api_store.OPENAI_CFG = self.tmp_cfg

    def tearDown(self):
        api_store.OPENAI_CFG = self._orig_cfg

    def _write(self, keys):
        with open(self.tmp_cfg, 'w', encoding='utf-8') as f:
            json.dump({'providers': {'trae': {'accounts': []}}, 'api_keys': keys},
                      f, ensure_ascii=False, indent=2)

    def _keys_on_disk(self):
        with open(self.tmp_cfg, encoding='utf-8') as f:
            return json.load(f)['api_keys']

    def _delete(self, key):
        return asyncio.run(A.delete_api_key({'key': key}))

    def test_last_key_cannot_be_deleted(self):
        """只剩一条 → 400，且文件纹丝不动（否则所有客户端当场失联）。"""
        self._write([{'name': '默认密钥', 'key': 'sk-only'}])
        with self.assertRaises(A.HTTPException) as cm:
            self._delete('sk-only')
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn('至少需要保留一条', cm.exception.detail)
        self.assertEqual(len(self._keys_on_disk()), 1)

    def test_delete_when_more_than_one_is_allowed(self):
        self._write([{'name': 'a', 'key': 'sk-1'}, {'name': 'b', 'key': 'sk-2'}])
        self.assertTrue(self._delete('sk-1')['ok'])
        self.assertEqual([k['key'] for k in self._keys_on_disk()], ['sk-2'])
        # 剩下那条再删就被拦下（把「删到只剩一条」作为终点）
        with self.assertRaises(A.HTTPException):
            self._delete('sk-2')
        self.assertEqual(len(self._keys_on_disk()), 1)

    def test_unknown_key_is_404(self):
        self._write([{'name': 'a', 'key': 'sk-1'}, {'name': 'b', 'key': 'sk-2'}])
        with self.assertRaises(A.HTTPException) as cm:
            self._delete('sk-nope')
        self.assertEqual(cm.exception.status_code, 404)

    def test_empty_key_is_400(self):
        self._write([{'name': 'a', 'key': 'sk-1'}])
        with self.assertRaises(A.HTTPException) as cm:
            self._delete('')
        self.assertEqual(cm.exception.status_code, 400)


if __name__ == '__main__':
    unittest.main()
