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

from providers.trae import TraeProvider, is_usable_model  # noqa: E402

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


class TestTraeModelBlocklist(unittest.TestCase):
    """上游无效模型屏蔽 (2026-09-18 master 反馈: 通道返回一堆 custom_model_*)。

    上游 get_detail_param 的 config_info_list 里混着 Trae 客户端「自定义模型」
    槽位 (custom_model_*) 与 IDE 内部工具名 (summary/fast_apply/...), 它们不是
    可用的聊天模型 —— 列在 /v1/models 里会让人点了报错。
    """

    # 实测自上游的真实垃圾名 (2026-09-18, 18787 /v1/models 原样返回)
    JUNK = [
        'custom_model_placeholder', 'custom_model_200k', 'custom_model_200k_text',
        'custom_model_1M', 'custom_model_1M_text', 'custom_model_gemini',
        'custom_model_vercel', 'custom_model_vercel_gemini', 'custom_model_kimi',
        'custom_model_no-fc', 'custom_model_gpt-5', 'custom_model_gpt-6',
        'custom_model_deepseek_v4', 'custom_model_deepseek_chat',
        'custom_model_deepseek_reasoner',
        'summary', 'fast_apply', 'fast_apply_new', 'title_generation',
        'input_optimization',
    ]
    REAL = ['Doubao-Seed-Evolving', 'glm-5.3-flash', 'DeepSeek-V4-Flash-Official',
            'kimi-k3', 'minimax-m3', 'qwen3.8-max', 'deepseek-v4.1-flash']

    def test_is_usable_model_rejects_junk(self):
        for n in self.JUNK:
            self.assertFalse(is_usable_model(n), f'{n} 应被屏蔽')

    def test_is_usable_model_keeps_real_models(self):
        for n in self.REAL:
            self.assertTrue(is_usable_model(n), f'{n} 不应被屏蔽')

    def test_blocklist_is_case_insensitive(self):
        """上游 config_name 大小写不统一, 判定不能大小写敏感。"""
        self.assertFalse(is_usable_model('CUSTOM_MODEL_1M'))
        self.assertFalse(is_usable_model('Custom_Model_Gpt-5'))
        self.assertFalse(is_usable_model('Summary'))
        self.assertFalse(is_usable_model('  input_optimization  '))
        self.assertFalse(is_usable_model(''))

    def test_does_not_overmatch_similar_names(self):
        """前缀判定不能误伤 —— 只有 custom_model_ 开头的才屏蔽。"""
        for n in ('custom-1M', 'custom_model', 'mycustom_model_1M',
                  'summary-v2', 'fast_applyx', 'glm-5.3-flash'):
            self.assertTrue(is_usable_model(n), f'{n} 被误屏蔽了')

    def test_list_models_filters_stale_node_cache(self):
        """核心回归: Node 后端是**未更新的旧版 server.js** 时, 其 /v1/models
        原样透传上游, sync_models 会把垃圾名灌进 _node_models。此时 list_models
        的出口过滤必须兜住 —— 即「只换 Python 不换 JS」也不能漏。
        """
        p = _make(models={}, default_model='DeepSeek-V4-Flash-Official')
        p.accounts = [{'token': 't', 'enabled': True}]
        p._node_models = self.JUNK + self.REAL      # 模拟未过滤的旧版后端
        ids = {m['id'] for m in p.list_models()}
        self.assertFalse([i for i in ids if 'custom_model_' in i], ids)
        for n in ('tr-summary', 'tr-fast_apply', 'tr-fast_apply_new',
                  'tr-title_generation', 'tr-input_optimization'):
            self.assertNotIn(n, ids)
        for n in self.REAL:
            self.assertIn(f'tr-{n}', ids)

    def test_blocked_names_still_resolve_on_request(self):
        """屏蔽只针对「列表」, 请求侧仍要能解析 —— 否则客户端/auto_chain 里
        残留的旧模型名会从「列表看不到」变成「请求直接 500」。"""
        p = _make(models={}, default_model='DeepSeek-V4-Flash-Official')
        p._node_models = self.JUNK + self.REAL
        self.assertEqual(p.normalize_model('tr-custom_model_1M'), 'custom_model_1M')
        self.assertEqual(p.normalize_model('tr-summary'), 'summary')


if __name__ == '__main__':
    unittest.main()
