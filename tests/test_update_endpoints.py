# -*- coding: utf-8 -*-
"""
测试: 「一键更新」三个端点的行为契约
=====================================
覆盖 POST /version/check、POST /version/download、GET /version/update-state
—— 重点不是 GitHub 真的连得上, 而是:

  1. **网络失败不谎报**: 查不到 release 时不能返回「已是最新」, 要带上 notes;
  2. **无更新就不下载**: 同版本调 /version/download 必须 409, 而不是照下 34MB;
  3. **通道缺包要 404**: release 里没有本通道安装包时明确报错, 不回落到别的包;
  4. **状态机只管下载**: download 置 downloading, 相位集合里没有「安装中」
     (安装必须经 Tauri 命令发起, 外部工具调不到, 故后端不设该相位)。

做法: 裸 FastAPI app 挂 admin router (不挂 require_auth), 并把
`_fetch_latest_release` / `_app_version` 换成可编程假实现 —— 测试绝不发真实网络
请求, 也不落任何真实文件 (UPDATE_DIR 指向临时目录)。

运行: python -m unittest tests.test_update_endpoints -v
"""
import os
import shutil
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

try:
    import fastapi  # noqa: F401
except ImportError:  # 无 fastapi 的环境跳过 (项目 .venv 内有)
    raise unittest.SkipTest('需要 fastapi（项目 .venv 内可跑）')

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import admin_api  # noqa: E402


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(admin_api.router)
    return TestClient(app)


class UpdateEndpointTest(unittest.TestCase):
    """四个端点的行为契约（GitHub 调用全部打桩）。"""

    def setUp(self):
        self.client = _make_client()
        self.tmp = tempfile.mkdtemp(prefix='openai-update-test-')
        self._orig = {
            'fetch': admin_api._fetch_latest_release,
            'ver': admin_api._app_version,
            'dir': admin_api.UPDATE_DIR,
            'repo': admin_api.UPDATE_REPO,
            'chan': admin_api.UPDATE_CHANNEL,
        }
        admin_api.UPDATE_DIR = self.tmp
        admin_api.UPDATE_CHANNEL = 'dev'
        admin_api.UPDATE_REPO = 'owner/open-ai'
        admin_api._update_state_set(phase='idle', percent=0.0, received=0,
                                    total=0, version='', path='', error='')

    def tearDown(self):
        admin_api._fetch_latest_release = self._orig['fetch']
        admin_api._app_version = self._orig['ver']
        admin_api.UPDATE_DIR = self._orig['dir']
        admin_api.UPDATE_REPO = self._orig['repo']
        admin_api.UPDATE_CHANNEL = self._orig['chan']
        admin_api._update_state_set(phase='idle', percent=0.0, received=0,
                                    total=0, version='', path='', error='')
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _stub_release(self, tag, assets=None, error=''):
        admin_api._fetch_latest_release = lambda: {
            'tag': tag, 'assets': assets or [], 'error': error}

    def _stub_version(self, v):
        admin_api._app_version = lambda: v

    # ── /version/check ──

    def test_check_has_update_with_asset(self):
        self._stub_version('local-v3.1')
        self._stub_release('v3.2', [{'name': 'open-ai-installer-dev.exe',
                                     'size': 123, 'browser_download_url': 'u'}])
        r = self.client.post('/v1/admin/version/check', json={})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertTrue(d['hasUpdate'])
        self.assertFalse(d['assetMissing'])
        self.assertEqual(d['assetName'], 'open-ai-installer-dev.exe')
        self.assertEqual(d['channel'], 'dev')

    def test_check_same_version_is_not_update(self):
        # 本次修复的核心: 同版本(不同前缀) 不再误报有更新
        self._stub_version('local-v3.1')
        self._stub_release('v3.1', [{'name': 'open-ai-installer-dev.exe',
                                     'size': 123}])
        d = self.client.post('/v1/admin/version/check', json={}).json()
        self.assertFalse(d['hasUpdate'])

    def test_check_downgrade_is_not_update(self):
        self._stub_version('local-v3.2')
        self._stub_release('v3.1', [{'name': 'open-ai-installer-dev.exe'}])
        d = self.client.post('/v1/admin/version/check', json={}).json()
        self.assertFalse(d['hasUpdate'])

    def test_check_reports_asset_missing(self):
        # 有新版本但没有 dev 包 → 必须显式上报, 不能静默当「无更新」
        self._stub_version('local-v3.1')
        self._stub_release('v3.2', [{'name': 'open-ai-installer-portable.exe'}])
        d = self.client.post('/v1/admin/version/check', json={}).json()
        self.assertTrue(d['hasUpdate'])
        self.assertTrue(d['assetMissing'])
        self.assertEqual(d['assetName'], '')

    def test_check_network_failure_is_not_reported_as_up_to_date(self):
        # 查不到 release: hasUpdate=False, 但必须带 notes, 前端据此提示「检查失败」
        self._stub_version('local-v3.1')
        self._stub_release('', [], error='timed out')
        d = self.client.post('/v1/admin/version/check', json={}).json()
        self.assertFalse(d['hasUpdate'])
        self.assertEqual(d['notes'], 'timed out')

    # ── /version/download ──

    def test_download_rejects_when_up_to_date(self):
        self._stub_version('local-v3.1')
        self._stub_release('v3.1', [{'name': 'open-ai-installer-dev.exe'}])
        r = self.client.post('/v1/admin/version/download', json={})
        self.assertEqual(r.status_code, 409)

    def test_download_404_when_channel_asset_missing(self):
        self._stub_version('local-v3.1')
        self._stub_release('v3.2', [{'name': 'open-ai-installer-portable.exe'}])
        r = self.client.post('/v1/admin/version/download', json={})
        self.assertEqual(r.status_code, 404)
        self.assertIn('dev', r.json()['detail'])

    def test_download_502_when_release_unavailable(self):
        self._stub_version('local-v3.1')
        self._stub_release('', [], error='network down')
        r = self.client.post('/v1/admin/version/download', json={})
        self.assertEqual(r.status_code, 502)

    def test_download_starts_and_state_is_downloading(self):
        # 起真实下载线程会发网络请求 → 用一个假的 URL 让它快速失败,
        # 断言的重点是「进入 downloading 相位」这一步
        self._stub_version('local-v3.1')
        self._stub_release('v3.2', [{
            'name': 'open-ai-installer-dev.exe', 'size': 10,
            'browser_download_url': 'http://127.0.0.1:1/nonexistent'}])
        r = self.client.post('/v1/admin/version/download', json={})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['phase'], 'downloading')
        st = self.client.get('/v1/admin/version/update-state').json()
        # 下载线程可能已经失败(error) —— 但绝不能还是 idle
        self.assertIn(st['phase'], ('downloading', 'error'))
        self.assertEqual(st['version'], 'v3.2')

    def test_download_reuses_completed_file(self):
        # 同一版本已下载完成 → 复用磁盘文件, 不再拉一遍
        self._stub_version('local-v3.1')
        self._stub_release('v3.2', [{
            'name': 'open-ai-installer-dev.exe', 'size': 5,
            'browser_download_url': 'http://127.0.0.1:1/nonexistent'}])
        dest = os.path.join(self.tmp, 'open-ai-installer-dev.exe')
        with open(dest, 'wb') as f:
            f.write(b'12345')
        admin_api._update_state_set(phase='ready', version='v3.2', path=dest)
        r = self.client.post('/v1/admin/version/download', json={})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get('reused'))

    # ── 相位集合 ──

    def test_phase_set_excludes_install_states(self):
        # 后端状态机只管下载: 「正在安装 / 已取消」由桌面端界面本地表达,
        # 因为安装必须经 Tauri 命令 install_update 发起, 外部工具调不到。
        # 这条断言防止有人日后又往网关里加一个永远为空的生产者。
        self.assertEqual(admin_api._update_state_get()['phase'], 'idle')
        for ep in ('/v1/admin/version/update-done',):
            r = self.client.post(ep, json={})
            self.assertEqual(r.status_code, 404, ep)


if __name__ == '__main__':
    unittest.main()