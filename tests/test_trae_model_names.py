# -*- coding: utf-8 -*-
"""
测试: providers/trae.py 模型名归一化 (无网络部分)
==================================================================
锁定 2026-09-15 上游 `llm_utils_chat` 协议变更后的命名规则:

  1. 模型名可分两段: config_name(基础名) + model_name(<基础名>__dev/__max 变体名)
  2. 查配置别名必须在「剥 tr-/trae- 前缀」与「拆 __dev/__max 后缀」之前,
     否则 "trae-v4-max" 这类带前缀的别名会被截成垃圾名 (历史 bug)
  3. 动态上游名大小写不敏感匹配
运行: python -m unittest tests.test_trae_model_names -v
"""
import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from providers.trae import TraeProvider  # noqa: E402

DEFAULT = 'DeepSeek-V4-Flash-Official__dev'


def _make(models=None, default_model=DEFAULT):
    return TraeProvider({
        'default_model': default_model,
        'models': models if models is not None else {
            'flash': 'DeepSeek-V4-Flash__dev',
            'v4-max': 'DeepSeek-V4-Pro__dev',
            'trae-flash-official': 'DeepSeek-V4-Flash-Official__dev',
            'deepseek-v4.1-flash': 'deepseek-v4.1-flash',
        },
    })


class TestTraeNormalizeModel(unittest.TestCase):
    def setUp(self):
        self.p = _make()
        # 模拟 Node 后端同步到的动态上游 config_name (基础名, 不带变体后缀)
        self.p._node_models = ['glm-5.3-flash', 'kimi-k3', 'DeepSeek-V4-Flash']

    # ---- 通道前缀 ----
    def test_strip_tr_prefix_then_dynamic_match(self):
        self.assertEqual(self.p.normalize_model('tr-glm-5.3-flash'), 'glm-5.3-flash')

    def test_strip_trae_prefix_then_dynamic_match(self):
        self.assertEqual(self.p.normalize_model('trae-kimi-k3'), 'kimi-k3')

    def test_dynamic_match_is_case_insensitive(self):
        self.assertEqual(self.p.normalize_model('tr-deepseek-v4-flash'), 'DeepSeek-V4-Flash')

    # ---- 别名表 (含前缀的 key 必须先于前缀剥离命中) ----
    def test_alias_key_without_prefix(self):
        self.assertEqual(self.p.normalize_model('flash'), 'DeepSeek-V4-Flash__dev')

    def test_alias_key_with_trae_prefix_wins_over_suffix_split(self):
        """回归: 'trae-v4-max' 曾因先剥前缀/拆后缀被截成 '-v4-max'。"""
        self.assertEqual(self.p.normalize_model('trae-v4-max'), 'DeepSeek-V4-Pro__dev')

    def test_alias_key_with_tr_prefix(self):
        self.assertEqual(self.p.normalize_model('tr-v4-max'), 'DeepSeek-V4-Pro__dev')

    def test_alias_key_with_trae_prefix_full(self):
        self.assertEqual(
            self.p.normalize_model('trae-flash-official'),
            'DeepSeek-V4-Flash-Official__dev')

    def test_new_deepseek_v41_flash_mapping(self):
        self.assertEqual(self.p.normalize_model('tr-deepseek-v4.1-flash'), 'deepseek-v4.1-flash')

    # ---- 变体后缀透传 (由 server.js 拆成 config_name + model_name) ----
    def test_variant_suffix_passes_through(self):
        self.assertEqual(
            self.p.normalize_model('tr-glm-5.3-flash__dev'), 'glm-5.3-flash__dev')
        self.assertEqual(
            self.p.normalize_model('tr-glm-5.3-flash__max'), 'glm-5.3-flash__max')

    def test_unknown_variant_keeps_full_name(self):
        self.assertEqual(
            self.p.normalize_model('DeepSeek-V4-Flash-Official__dev'),
            'DeepSeek-V4-Flash-Official__dev')

    # ---- 默认值 / 旧客户端格式 ----
    def test_empty_falls_back_to_default(self):
        self.assertEqual(self.p.normalize_model(''), DEFAULT)
        self.assertEqual(self.p.normalize_model(None), DEFAULT)

    def test_custom_local_prefix_compat(self):
        self.assertEqual(self.p.normalize_model('custom-local:flash'), 'DeepSeek-V4-Flash__dev')


class TestTraeModelList(unittest.TestCase):
    def test_no_usable_account_hides_all_models(self):
        p = TraeProvider({'models': {}, 'accounts': []})
        self.assertEqual(p.list_models(), [])

    def test_list_models_exposes_tr_prefix_and_variants(self):
        p = _make(models={'flash': 'DeepSeek-V4-Flash__dev'},
                  default_model='DeepSeek-V4-Flash-Official__dev')
        p.accounts = [{'token': 't', 'enabled': True}]
        p._node_models = ['glm-5.3-flash']
        ids = {m['id'] for m in p.list_models()}
        self.assertIn('tr-glm-5.3-flash', ids)            # 动态上游名
        self.assertIn('tr-flash', ids)                    # 别名 key
        self.assertIn('tr-DeepSeek-V4-Flash__dev', ids)   # 别名值(变体名)
        self.assertIn('tr-DeepSeek-V4-Flash-Official__dev', ids)  # 默认模型


if __name__ == '__main__':
    unittest.main()
