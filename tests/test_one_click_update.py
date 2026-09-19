# -*- coding: utf-8 -*-
"""
测试: 「一键更新」的版本比较与安装包匹配
==========================================
起因 (2026-09-19 master 要求优化「一键更新」运行逻辑)
----------------------------------------------------
原实现只有两件事: 查 GitHub latest release, 然后 `has = latest != APP_VERSION`。
字符串不等判定带来两个实际故障:

  1. **同版本被当成有更新**: 发布 tag 是 `v3.1`, 本机 APP_VERSION 是
     `local-v3.1` → 恒判「有更新」, 用户点一次下载一次 (34MB), 装完毫无变化;
  2. **旧版本被当成有更新**: 上游比本机旧时同样判「有更新」, 一键更新会
     **降级覆盖**用户手上更新的版本。

而且原流程压根没有下载/安装的实现 —— 按钮点了只弹一句「开始下载...」,
没有任何下载动作。本次补上完整链路 (下载 → 进度 → UAC 提权安装),
本测试锁定其中的**纯逻辑**部分: 版本比较、通道匹配、下载状态机。

运行: python -m unittest tests.test_one_click_update -v
"""
import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import admin_api  # noqa: E402
from admin_api import (  # noqa: E402
    _is_newer,
    _pick_asset,
    _update_state_get,
    _update_state_set,
    _version_tuple,
)

# 真实 release v3.1 的资产清单 (2026-09-19 实测, 见 tests 说明)
REAL_ASSETS = [
    {"name": "open-ai-installer-dev.exe", "size": 34612819},
    {"name": "open-ai-installer-portable.exe", "size": 419092059},
]


class VersionTupleTest(unittest.TestCase):
    """版本串归一化: 前缀 (local-/dev-/portable-/v) 一律忽略, 只看数字段。"""

    def test_strips_prefixes(self):
        self.assertEqual(_version_tuple('v3.1'), (3, 1))
        self.assertEqual(_version_tuple('local-v3.1'), (3, 1))
        self.assertEqual(_version_tuple('dev-v3.1'), (3, 1))
        self.assertEqual(_version_tuple('portable-v3.1'), (3, 1))

    def test_drops_trailing_zero(self):
        # 3.1.0 与 3.1 是同一个版本, 不能因为多写一位就判成「有更新」
        self.assertEqual(_version_tuple('3.1.0'), _version_tuple('v3.1'))

    def test_multi_digit_ordering(self):
        # 字符串比较会把 'v3.10' 判成小于 'v3.9' —— 数字元组不会
        self.assertGreater(_version_tuple('v3.10'), _version_tuple('v3.9'))

    def test_garbage(self):
        self.assertEqual(_version_tuple(''), ())
        self.assertEqual(_version_tuple(None), ())
        self.assertEqual(_version_tuple('unknown'), ())


class IsNewerTest(unittest.TestCase):
    """只有**严格更新**才算有更新 —— 同版本与更旧版本都必须为 False。"""

    def test_same_version_across_channels(self):
        # 本次修复的核心: 发布 tag v3.1 vs 本机 local-v3.1 → 同版本, 无更新
        self.assertFalse(_is_newer('v3.1', 'local-v3.1'))
        self.assertFalse(_is_newer('dev-v3.1', 'v3.1'))
        self.assertFalse(_is_newer('portable-v3.1', 'local-v3.1'))

    def test_real_upgrade(self):
        self.assertTrue(_is_newer('v3.2', 'local-v3.1'))
        self.assertTrue(_is_newer('v4.0', 'v3.1'))

    def test_downgrade_is_not_update(self):
        # 上游更旧 → 绝不能提示更新 (否则一键更新会降级覆盖用户的新版本)
        self.assertFalse(_is_newer('v3.0', 'local-v3.1'))
        self.assertFalse(_is_newer('v3.9', 'v3.10'))

    def test_unparsable_is_not_update(self):
        # 解析不出数字时不敢断言「有更新」, 宁可不提示
        self.assertFalse(_is_newer('', 'local-v3.1'))
        self.assertFalse(_is_newer('v3.1', ''))
        self.assertFalse(_is_newer('unknown', 'unknown'))


class PickAssetTest(unittest.TestCase):
    """按 UPDATE_CHANNEL 精确匹配安装包, 匹配不到必须返回 None。"""

    def test_dev_channel(self):
        a = _pick_asset(REAL_ASSETS, 'dev')
        self.assertIsNotNone(a)
        self.assertEqual(a['name'], 'open-ai-installer-dev.exe')

    def test_portable_channel(self):
        a = _pick_asset(REAL_ASSETS, 'portable')
        self.assertIsNotNone(a)
        self.assertEqual(a['name'], 'open-ai-installer-portable.exe')

    def test_unknown_channel_returns_none(self):
        # 关键: 取不到必须返回 None, 由调用方明确报错;
        # 一旦回落到「随便挑一个」, portable 用户会装上跑不起来的 dev 包
        self.assertIsNone(_pick_asset(REAL_ASSETS, 'macos'))
        self.assertIsNone(_pick_asset(REAL_ASSETS, ''))

    def test_ignores_non_installer_files(self):
        assets = REAL_ASSETS + [
            {"name": 'open-ai-dev-notes.txt'},
            {"name": 'sha256sums-dev.txt'},
        ]
        self.assertEqual(_pick_asset(assets, 'dev')['name'],
                         'open-ai-installer-dev.exe')

    def test_empty_assets(self):
        self.assertIsNone(_pick_asset([], 'dev'))
        self.assertIsNone(_pick_asset(None, 'dev'))


class UpdateStateMachineTest(unittest.TestCase):
    """下载状态机: 相位与进度字段必须成对更新, 前端据此渲染。"""

    def tearDown(self):
        # 复位, 避免污染其它用例
        _update_state_set(phase='idle', percent=0.0, received=0, total=0,
                          version='', path='', error='')

    def test_initial_idle(self):
        _update_state_set(phase='idle', percent=0.0, received=0, total=0,
                          version='', path='', error='')
        st = _update_state_get()
        self.assertEqual(st['phase'], 'idle')
        self.assertEqual(st['percent'], 0.0)

    def test_downloading_progress(self):
        _update_state_set(phase='downloading', percent=42.5, received=425,
                          total=1000, version='v3.2')
        st = _update_state_get()
        self.assertEqual(st['phase'], 'downloading')
        self.assertAlmostEqual(st['percent'], 42.5)
        self.assertEqual(st['version'], 'v3.2')

    def test_get_returns_copy(self):
        # 返回的必须是副本: 前端读状态不能反过来改到内部字典
        st = _update_state_get()
        st['phase'] = 'hacked'
        self.assertNotEqual(_update_state_get()['phase'], 'hacked')

    def test_ts_updated_on_write(self):
        before = _update_state_get()['ts']
        _update_state_set(phase='ready')
        self.assertGreaterEqual(_update_state_get()['ts'], before)


class ConfigTest(unittest.TestCase):
    """仓库 / 通道 / 版本三者必须都能从 version.py 正确读出。"""

    def test_repo_is_configured(self):
        # 本机定制: 必须是真实发布仓库, 否则「一键更新」永远查不到 release
        self.assertEqual(admin_api.UPDATE_REPO, 'BOY-Chinese/open-ai')

    def test_channel_and_version(self):
        """通道与版本号必须自洽（勿写死 dev，portable 渠道取值为 'portable'）。

        原断言写死 `UPDATE_CHANNEL == 'dev'` —— 同一份文件在 portable 仓会让
        「改版本号即测试失败」；此处改为按渠道自洽断言，两仓通用。
        """
        version = admin_api._app_version()
        self.assertTrue(version)
        # 通道取值只能是两个已知渠道之一
        self.assertIn(admin_api.UPDATE_CHANNEL, ('dev', 'portable'))
        # 版本号前缀须与通道一致（dev-v3.2 / portable-v3.2）——
        # 「一键更新」按数字段比较，前缀用来标识渠道，二者不得漂移
        self.assertTrue(version.startswith(admin_api.UPDATE_CHANNEL + '-'),
                        f'版本号 {version!r} 与通道 {admin_api.UPDATE_CHANNEL!r} 不一致')


if __name__ == '__main__':
    unittest.main()
