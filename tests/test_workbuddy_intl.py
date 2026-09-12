# -*- coding: utf-8 -*-
"""
测试: providers/workbuddy_intl.py 国际版反代 provider (无网络部分)
==================================================================
覆盖路由/模型命名/请求体适配等纯逻辑, 不发任何网络请求。
运行: python -m unittest tests.test_workbuddy_intl -v
"""
import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from providers import build_providers, route_provider  # noqa: E402
from providers.workbuddy_intl import (  # noqa: E402
    INTL_DEFAULT_MODEL, INTL_DOMAIN, INTL_PRODUCT, WorkBuddyIntlProvider,
    _intl_headers, _strip_intl_prefix,
)


def _make(**overrides):
    cfg = {
        'domain': INTL_DOMAIN,
        'product': INTL_PRODUCT,
        'defaultModel': INTL_DEFAULT_MODEL,
        'accounts': [{'userId': 'u1', 'accessToken': 'tok', 'refreshToken': 'rt'}],
    }
    cfg.update(overrides)
    return WorkBuddyIntlProvider(cfg)


class StripPrefixTest(unittest.TestCase):
    def test_wbai_and_custom_local(self):
        self.assertEqual(_strip_intl_prefix('wbai-deepseek-v4.1-flash'),
                         'deepseek-v4.1-flash')
        self.assertEqual(_strip_intl_prefix('custom-local:wbai-x'), 'x')
        self.assertEqual(_strip_intl_prefix('deepseek-v4.1-flash'),
                         'deepseek-v4.1-flash')
        self.assertEqual(_strip_intl_prefix(''), '')
        self.assertEqual(_strip_intl_prefix(None), '')


class NamingTest(unittest.TestCase):
    def test_normalize_model(self):
        p = _make()
        self.assertEqual(p.normalize_model('wbai-deepseek-v4.1-flash'),
                         'deepseek-v4.1-flash')
        self.assertEqual(p.normalize_model('wbai-auto'), 'deepseek-v4.1-flash')
        self.assertEqual(p.normalize_model('wbai-deepseek-chat'),
                         'deepseek-v4.1-flash')
        # 未知模型原样透传; 空模型回退默认
        self.assertEqual(p.normalize_model('wbai-some-new-model'), 'some-new-model')
        self.assertEqual(p.normalize_model(''), INTL_DEFAULT_MODEL)

    def test_config_models_merged(self):
        p = _make(models={'wbai-gpt-6-astra': 'gpt-6-astra'})
        self.assertEqual(p.normalize_model('wbai-gpt-6-astra'), 'gpt-6-astra')

    def test_list_models_only_intl(self):
        """对外模型列表只含 wbie-* —— 不能混入国内版 wb-* 模型。

        v3.0：通道前缀由 wbai- 改为 wbie-（与 tr- / wb- 形成统一三字母前缀）。
        旧 wbai- 仅作为**请求别名**兼容（见 NamingTest.normalize_*），
        不再出现在模型列表中。
        """
        p = _make()
        ids = {m['id'] for m in p.list_models()}
        self.assertIn('wbie-deepseek-v4.1-flash', ids)
        self.assertIn('wbie-auto', ids)
        self.assertTrue(all(i.startswith('wbie-') for i in ids), sorted(ids))
        self.assertFalse(any(i.startswith('wbai-') for i in ids), sorted(ids))
        self.assertFalse(any(i.startswith('wb-') and not i.startswith('wbie-')
                             for i in ids))
        # 国内版模型名不得泄漏进国际版列表
        self.assertNotIn('wb-glm-5.3', ids)
        self.assertNotIn('wb-hy3', ids)

    def test_list_models_includes_dynamic(self):
        p = _make(models={'wbie-gpt-6-astra': 'gpt-6-astra'})
        ids = {m['id'] for m in p.list_models()}
        self.assertIn('wbie-gpt-6-astra', ids)

    def test_owned_by(self):
        p = _make()
        self.assertTrue(all(m['owned_by'] == 'tencent-intl'
                            for m in p.list_models()))


class BodyAdaptTest(unittest.TestCase):
    def test_system_injected_when_missing(self):
        """国际版要求首条消息为 system (code=11128), 必须自动补一条。"""
        p = _make()
        body = {'model': 'wbai-deepseek-v4.1-flash',
                'messages': [{'role': 'user', 'content': 'hi'}]}
        up = p._to_body(body)
        self.assertEqual(up['messages'][0]['role'], 'system')
        self.assertEqual(len(up['messages']), 2)
        self.assertEqual(up['messages'][1]['role'], 'user')
        # 原请求体不被就地修改
        self.assertEqual(len(body['messages']), 1)

    def test_system_kept_when_present(self):
        p = _make()
        msgs = [{'role': 'system', 'content': 'S'},
                {'role': 'user', 'content': 'hi'}]
        up = p._to_body({'model': 'wbai-deepseek-v4.1-flash', 'messages': msgs})
        self.assertEqual(up['messages'], msgs)

    def test_force_stream_and_model_mapping(self):
        p = _make()
        up = p._to_body({'model': 'wbai-deepseek-v4.1-flash',
                         'messages': [{'role': 'system', 'content': 'S'}],
                         'stream': False, 'seed': 7})
        self.assertTrue(up['stream'])
        self.assertEqual(up['stream_options'], {'include_usage': True})
        self.assertEqual(up['model'], 'deepseek-v4.1-flash')
        self.assertNotIn('seed', up)

    def test_compat_alias_is_normalized(self):
        """回归: wbai-auto 必须归一化为上游模型名。

        基类 _to_upstream_body 用的是国内版前缀剥离规则, 不认 wbai-,
        曾把 wbai-auto 原样发给上游 → 400 code=11102
        (model [wbai-auto] service info not found)。
        """
        p = _make()
        up = p._to_body({'model': 'wbai-auto',
                         'messages': [{'role': 'system', 'content': 'S'}]})
        self.assertEqual(up['model'], 'deepseek-v4.1-flash')

    def test_unknown_prefixed_model_is_stripped(self):
        """回归: 未知模型也要剥掉 wbai- 前缀后再发上游。"""
        p = _make()
        up = p._to_body({'model': 'wbai-some-new-model',
                         'messages': [{'role': 'system', 'content': 'S'}]})
        self.assertEqual(up['model'], 'some-new-model')

    def test_compat_alias_listed_and_resolvable(self):
        """列表里暴露的每个 wbai-* 都必须能被归一化 (否则点选即 400)。"""
        p = _make(models={'wbai-gpt-6-astra': 'gpt-6-astra'})
        for mid in (m['id'] for m in p.list_models()):
            resolved = p.normalize_model(mid)
            self.assertFalse(resolved.startswith('wbai-'),
                             f'{mid} 未被归一化, 上游会 400: {resolved}')
            self.assertTrue(resolved, f'{mid} 解析为空')
        self.assertEqual(p.normalize_model('wbai-gpt-6-astra'), 'gpt-6-astra')


class HeaderTest(unittest.TestCase):
    def test_intl_headers(self):
        h = _intl_headers({'userId': 'u1', 'accessToken': 'tok'})
        self.assertEqual(h['X-Domain'], INTL_DOMAIN)
        self.assertEqual(h['X-Product'], INTL_PRODUCT)
        # 国际版专有: X-Product-Code (国内版没有)
        self.assertEqual(h['X-Product-Code'], INTL_PRODUCT)
        self.assertEqual(h['Authorization'], 'Bearer tok')


class RoutingTest(unittest.TestCase):
    def setUp(self):
        self.providers = {'workbuddy': object(), 'workbuddy-intl': object(),
                          'trae': object()}

    def test_wbai_routes_to_intl(self):
        self.assertIs(route_provider('wbai-deepseek-v4.1-flash', self.providers),
                      self.providers['workbuddy-intl'])
        self.assertIs(route_provider('wbai-auto', self.providers),
                      self.providers['workbuddy-intl'])

    def test_intl_keyword_routes_to_intl(self):
        self.assertIs(route_provider('workbuddy-intl', self.providers),
                      self.providers['workbuddy-intl'])

    def test_cn_models_still_route_to_cn(self):
        self.assertIs(route_provider('wb-glm-5.3', self.providers),
                      self.providers['workbuddy'])
        self.assertIs(route_provider('workbuddy-deepseek-v4-flash', self.providers),
                      self.providers['workbuddy'])

    def test_trae_still_routes_to_trae(self):
        self.assertIs(route_provider('tr-flash', self.providers),
                      self.providers['trae'])

    def test_intl_fallback_when_absent(self):
        """未注册国际版时, wbai-* 回退到国内版 provider, 不返回 None。"""
        only_cn = {'workbuddy': object()}
        self.assertIs(route_provider('wbai-x', only_cn), only_cn['workbuddy'])


class RegistrationTest(unittest.TestCase):
    def test_registered_when_accounts_present(self):
        cfg = {'providers': {
            'workbuddy_intl': {'domain': INTL_DOMAIN, 'product': INTL_PRODUCT,
                               'accounts': [{'userId': 'u1',
                                             'accessToken': 'tok'}]}}}
        provs = build_providers(cfg)
        self.assertIn('workbuddy-intl', provs)
        self.assertIsInstance(provs['workbuddy-intl'], WorkBuddyIntlProvider)

    def test_not_registered_without_accounts(self):
        cfg = {'providers': {
            'workbuddy_intl': {'domain': INTL_DOMAIN, 'product': INTL_PRODUCT,
                               'accounts': []},
            'trae': {'enabled': False}}}
        self.assertNotIn('workbuddy-intl', build_providers(cfg))


class DefaultsTest(unittest.TestCase):
    def test_defaults_applied(self):
        p = WorkBuddyIntlProvider({
            'accounts': [{'userId': 'u1', 'accessToken': 'tok'}]})
        self.assertEqual(p.domain, INTL_DOMAIN)
        self.assertEqual(p.product, INTL_PRODUCT)
        self.assertEqual(p.default_model, INTL_DEFAULT_MODEL)
        self.assertEqual(p.switch_every, 10)
        self.assertEqual(p.name, 'workbuddy-intl')


if __name__ == '__main__':
    unittest.main()
