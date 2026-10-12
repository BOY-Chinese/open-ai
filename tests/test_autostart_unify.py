# -*- coding: utf-8 -*-
"""开机自启「开关关不掉」回归测试（v3.3 修复）。

## 事故（2026-10-12 master 反馈）

「portable 版的开机自启有 bug，不管开不开，都会开机自启。」

## 根因

同一个「开机自启」功能被**两套机制**各写一个文件到启动文件夹:

    installer/installer.py::_setup_autostart  → open-ai-autostart.bat
    admin_api.py::_autostart_target           → open-ai-autostart.vbs  (GUI 开关只认它)

用户在界面里关掉自启 → 只删掉 .vbs，安装器写的 .bat **原样留着继续生效**
→ 开机还是自启；而界面读的也是 .vbs，显示「已关闭」——两侧各说各话，
看日志/看界面都发现不了。

卸载器同病: 只删 .bat → 卸载后 .vbs 还在自启。

## 修法

以 `.vbs` 为唯一落地点（网关是唯一 owner，内容更完整: 先拉后端再起托盘）:
  * admin_api: 开关动作**连带清扫**遗留 .bat（老装机自愈）；GET 时若 .vbs 不在
    也顺手清遗留，避免界面显示「已关闭」但实际仍在自启。
  * installer: 改写 .vbs（与网关同构）+ 安装时清遗留 .bat。
  * uninstaller: 两个名字都删。

运行: python -m unittest tests.test_autostart_unify
"""
import importlib.util
import os
import sys
import tempfile
import types
import unittest

# 项目根 = 本文件上一级
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_admin_api():
    """加载 admin_api 模块，屏蔽其重量级依赖（只测纯函数/端点逻辑）。

    ⚠️ 隔离要求：本函数只在**真实的** httpx/fastapi 不存在时才注入桩模块，
    且执行完立即还原 sys.modules —— 否则会把假 httpx 永久留在 sys.modules 里,
    污染同一进程内其它测试 (实测: 全量跑时 test_stream_usage 因假 httpx 报
    "object() takes no arguments")。单跑通过、全量失败最难查, 故这里严格还原。
    """
    saved = {}
    stubbed = []
    for mod in ('httpx', 'fastapi', 'pydantic'):
        if mod not in sys.modules:
            sys.modules[mod] = types.ModuleType(mod)
            stubbed.append(mod)
    if 'fastapi' in stubbed:
        fa = sys.modules['fastapi']
        fa.APIRouter = lambda *a, **k: types.SimpleNamespace(
            get=lambda *a, **k: (lambda f: f),
            post=lambda *a, **k: (lambda f: f))
        fa.Body = lambda *a, **k: None
        fa.HTTPException = type('HTTPException', (Exception,), {
            '__init__': lambda self, status_code=None, detail=None:
                Exception.__init__(self, detail)})
    if 'httpx' in stubbed:
        sys.modules['httpx'].AsyncClient = object
        sys.modules['httpx'].AsyncHTTPTransport = object

    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    spec = importlib.util.spec_from_file_location(
        'admin_api_autostart_t', os.path.join(ROOT, 'admin_api.py'))
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    finally:
        # 无论成败都把桩模块摘掉, 不留副作用给后续测试
        for name in stubbed:
            saved[name] = sys.modules.pop(name, None)
    return mod


class AutostartUnifyTest(unittest.TestCase):
    """开关必须是唯一权威: 关闭后启动文件夹里不能再有任何 open-ai 自启文件。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_admin_api()

    def setUp(self):
        # 造一个假 APPDATA，让 _startup_dir() 指向临时「启动文件夹」
        self.appdata = tempfile.mkdtemp()
        self.startup = os.path.join(
            self.appdata, 'Microsoft', 'Windows', 'Start Menu', 'Programs', 'Startup')
        os.makedirs(self.startup)
        self._old = os.environ.get('APPDATA')
        os.environ['APPDATA'] = self.appdata

    def tearDown(self):
        if self._old is None:
            os.environ.pop('APPDATA', None)
        else:
            os.environ['APPDATA'] = self._old

    def _files(self):
        return sorted(os.listdir(self.startup))

    def _touch(self, name):
        with open(os.path.join(self.startup, name), 'w') as f:
            f.write('x')

    # ─────────────── 核心回归 ───────────────

    def test_disable_removes_legacy_bat(self):
        """★ 本次事故的回归点: 关开关后遗留 .bat 必须被清掉。

        修复前: 只剩 .vbs 被删, .bat 留着 → 开机仍自启。
        """
        self._touch('open-ai-autostart.vbs')
        self._touch('open-ai-autostart.bat')

        # POST enabled=False 的两个核心动作
        vbs = os.path.join(self.startup, 'open-ai-autostart.vbs')
        if os.path.isfile(vbs):
            os.remove(vbs)
        removed = self.mod._purge_legacy_autostart()

        self.assertEqual(removed, ['open-ai-autostart.bat'])
        self.assertEqual(self._files(), [],
                         '关闭自启后启动文件夹必须干净, 否则就是「关了还自启」')

    def test_legacy_name_list_covers_bat(self):
        """遗留名单必须真的包含安装器历史写入的 .bat。"""
        self.assertIn('open-ai-autostart.bat', self.mod.AUTOSTART_LEGACY_NAMES)

    def test_purge_idempotent(self):
        """没有遗留文件时清理不报错，且返回空列表。"""
        self.assertEqual(self.mod._purge_legacy_autostart(), [])

    def test_purge_only_targets_autostart_files(self):
        """清理不能误伤同目录的其它启动项。"""
        self._touch('open-ai-autostart.bat')
        self._touch('someone-else.lnk')
        self.mod._purge_legacy_autostart()
        self.assertEqual(self._files(), ['someone-else.lnk'])

    # ─────────────── 两侧文件名必须一致 ───────────────

    def test_target_is_vbs(self):
        """GUI 的落地点固定为 .vbs（installer 必须写同名，否则又会分叉）。"""
        tgt = self.mod._autostart_target()
        self.assertTrue(tgt.endswith('open-ai-autostart.vbs'), tgt)

    def test_installer_uses_same_filename(self):
        """安装器写出的文件名必须与网关一致 —— 这是分叉 bug 的直接原因。

        dev 仓走 start_hidden.ps1(VBS 包 powershell), portable 走品牌 exe;
        形态不同但**文件名必须都是 .vbs**, 否则 GUI 开关又管不到安装器的自启。
        """
        src = open(os.path.join(ROOT, 'installer', 'installer.py'),
                   encoding='utf-8').read()
        self.assertIn("'open-ai-autostart.vbs'", src)
        # 安装器必须顺手清掉历史 .bat, 否则老装机两份同时生效
        self.assertIn('open-ai-autostart.bat', src)

    def test_uninstaller_removes_both_names(self):
        """卸载器必须把两个名字都删，否则卸载后仍有自启残留。"""
        src = open(os.path.join(ROOT, 'installer', 'uninstaller.py'),
                   encoding='utf-8').read()
        self.assertIn('open-ai-autostart.vbs', src)
        self.assertIn('open-ai-autostart.bat', src)


if __name__ == '__main__':
    unittest.main(verbosity=2)
