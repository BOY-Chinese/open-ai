# -*- coding: utf-8 -*-
"""
测试: scripts/gui_account_manager.py 中的账号解析逻辑 (无网络部分)
==================================================================
只测试纯解析函数 (load_account_groups / trae_row_name / wb_row_name),
不触发积分网络查询。
运行: python -m unittest tests.test_account_parse -v
"""
import json
import os
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(PROJECT_ROOT, 'scripts')
sys.path.insert(0, SCRIPTS_DIR)

import api_store  # noqa: E402
import gui_account_manager as g  # noqa: E402


class AccountParseTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tmp_cfg = os.path.join(self.tmpdir, 'config.json')
        # 重定向两个模块使用的 config 路径到临时文件
        self._orig_cfg_api = api_store.OPENAI_CFG
        self._orig_cfg_am = g.am.OPENAI_CFG
        self._orig_cfg_gui = g.OPENAI_CFG
        api_store.OPENAI_CFG = self.tmp_cfg
        g.am.OPENAI_CFG = self.tmp_cfg
        g.OPENAI_CFG = self.tmp_cfg
        # 关闭 server_invalid_map 网络调用
        g.am.server_invalid_map = lambda: {}

    def tearDown(self):
        api_store.OPENAI_CFG = self._orig_cfg_api
        g.am.OPENAI_CFG = self._orig_cfg_am
        g.OPENAI_CFG = self._orig_cfg_gui

    def _write(self, data):
        with open(self.tmp_cfg, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def test_load_groups_with_accounts(self):
        self._write({
            'providers': {
                'trae': {'device_id': 'd1',
                         'accounts': [{'name': 'T账号', 'uid': 'u1'}]},
                'workbuddy': {'domain': 'www.workbuddy.cn', 'product': 'SaaS',
                              'accounts': [{'userId': 'wb1'}]}
            }
        })
        t_info, w_info, err = g.load_account_groups()
        self.assertIsNone(err)
        t_dict, t_accs, device_id, inval = t_info
        self.assertEqual(len(t_accs), 1)
        self.assertEqual(t_accs[0]['uid'], 'u1')
        w_dict, w_accs, domain, product = w_info
        self.assertEqual(len(w_accs), 1)
        self.assertEqual(w_accs[0]['userId'], 'wb1')

    def test_load_groups_empty(self):
        self._write({'providers': {}})
        _t, _w, err = g.load_account_groups()
        self.assertEqual(err, '尚未添加任何账号')

    def test_row_names(self):
        self._write({
            'providers': {
                'trae': {'device_id': 'd1',
                         'accounts': [{'name': 'T账号', 'uid': 'u1'}]},
                'workbuddy': {'accounts': [{'userId': 'wb-uid-1'}]}
            }
        })
        t_info, w_info, _ = g.load_account_groups()
        t_dict, t_accs, did, inval = t_info
        name1 = g.trae_row_name(t_dict, t_accs[0], {}, 1)
        self.assertIn('T账号', name1)
        w_dict, w_accs, dom, prod = w_info
        n2 = g.wb_row_name(w_dict, w_accs[0], 1)
        self.assertEqual(n2, 'wb-uid-1')


if __name__ == '__main__':
    unittest.main()