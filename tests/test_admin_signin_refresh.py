# -*- coding: utf-8 -*-
"""
测试: admin_api 的「刷新账号要顺带补签」相关判定
================================================
覆盖两处曾经出错的逻辑（用户实测反馈的两个 bug 都落在这里）：

1. `_signin_targets()` —— 判定「哪些账号今天还没签到、该跑哪个补签脚本」。
   旧版界面的「刷新账号」只重读签到表、不触发任何签到动作，于是未签到账号
   点多少次刷新都还是未签到。这里把判定收口成一个函数并测它。

2. 补签脚本的参数拼装 (`_run_signin_script` 的 `extra`) —— 历史上曾出现
   `--wb-only` / `--trae-only` 传错、或把整份 signin_all.py（含 token 续期）
   当成「刷新」来跑。本测试钉住「刷新绝不续期」：只允许补签用的两个开关。

3. `formatVersion` 的 Python 侧等价实现只用于断言前端规则 —— 版本号
   `dev-v3.1` 不能被补成 `vdev-v3.1`（用户实测的第三个 bug）。

不联网、不写真实 config：只对纯函数做断言。
运行: python -m unittest tests.test_admin_signin_refresh -v
"""
import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import admin_api as A  # noqa: E402


TRAE_ACC = {'uid': 'trae-1', 'token': 'x', 'name': '网页登录账号(trae-1)'}
WB_ACC = {'userId': 'wb-1', 'accessToken': 'y'}

# Loomy 的真实形状（见 config.json providers.loomy.accounts）：
#   Web 半边 userid 形如 `web:<手机号>`（cookies），桌面半边是纯数字 userid
#   （session）。两者同手机号 → 合并为界面上的**一行**，行 id 取桌面 userid
#   （`_loomy_row_state` 的 canonical uid），与 GET /accounts/signin 的 key 同构。
LOOMY_PHONE = '13800000000'
LOOMY_DESKTOP_UID = '260913000000000001'
LOOMY_WEB = {'userid': f'web:{LOOMY_PHONE}', 'phone': LOOMY_PHONE,
             'kind': 'web', 'cookies': {'loomy_web_session': 's'},
             'name': f'Loomy Web({LOOMY_PHONE})'}
LOOMY_DESKTOP = {'userid': LOOMY_DESKTOP_UID, 'session': 'sess',
                 'phone': LOOMY_PHONE, 'name': f'Loomy({LOOMY_PHONE})'}
# 合并行的完整 id（与签到表 key 同构）
LOOMY_ROW_ID = f'Loomy:{LOOMY_DESKTOP_UID}'


def _cfg(trae=None, wb=None, intl=None, loomy=None):
    """造一份最小 config（形状与真实 config.json 的 providers 段一致）。"""
    return {
        'providers': {
            'trae': {'device_id': 'dev', 'accounts': trae if trae is not None else [TRAE_ACC]},
            'workbuddy': {'domain': 'www.workbuddy.cn', 'product': 'SaaS',
                          'accounts': wb if wb is not None else [WB_ACC]},
            'workbuddy_intl': {'domain': 'www.workbuddy.ai', 'product': 'workbuddy-ai',
                               'accounts': intl if intl is not None else
                               [{'userId': 'wbie-1', 'accessToken': 'z'}]},
            'loomy': {'accounts': loomy if loomy is not None
                      else [LOOMY_WEB, LOOMY_DESKTOP]},
        }
    }


class SigninTargetsTest(unittest.TestCase):
    """_signin_targets: 决定「要不要跑补签、跑哪个」。"""

    def setUp(self):
        self._orig = A._read_config
        self.cfg = _cfg()
        A._read_config = lambda: self.cfg
        self.addCleanup(lambda: setattr(A, '_read_config', self._orig))

    def test_all_signed_in_runs_nothing(self):
        """全部已有当日凭证 → 不跑任何脚本（刷新不该白等一次联网）。

        ★ 2026-09-18：国际版已纳入补签（活跃动作路径），故这里必须连同
          `WorkBuddy_IE:wbie-1` 一起给凭证，才算「全部签到」。
          国际版**积分由服务端延迟自动入账**，当天通常拿不到凭证 ——
          那不叫「没签上」，只是结算没到（见 test_intl_triggers_wb_activity）。
        """
        t = A._signin_targets(None, {
            'Trae:trae-1': {}, 'WorkBuddy:wb-1': {}, 'WorkBuddy_IE:wbie-1': {},
            LOOMY_ROW_ID: {},
        })
        self.assertFalse(t['wb'], '已全部签到却仍要跑 WB 补签')
        self.assertFalse(t['trae'], '已全部签到却仍要跑 TRAE 补签')
        self.assertEqual(t['pending'], [])

    def test_unsigned_trae_triggers_trae_only(self):
        """TRAE 未签 → 只跑 --trae-only，不牵连 WB。"""
        t = A._signin_targets('Trae', {})
        self.assertTrue(t['trae'])
        self.assertFalse(t['wb'])
        self.assertEqual(len(t['pending']), 1)

    def test_unsigned_wb_triggers_wb_only(self):
        """WorkBuddy 未签 → 只跑 --wb-only。"""
        t = A._signin_targets('WorkBuddy', {})
        self.assertTrue(t['wb'])
        self.assertFalse(t['trae'])

    def test_intl_triggers_wb_activity(self):
        """国际版（2026-09-18 起）纳入补签 —— 走网页端活跃路径。

        历史：旧版这里断言「永不触发」，理由是 2026-09-13 确认国际版无签到渠道
        （服务端恒报 active=false，daily-checkin 必 10001 终态）。
        五期实验判决后国际版**改道**：`signin_all.py --wb-only` 内部改调
        `scripts/wb_web_daily.py` 发一条 `/console/chat/completions` 对话
        （「真实使用行为」路径），该动作确实会触发每日 +30，故必须进补签。

        注意 pending 里的 id 必须与签到表 key 同构（`WorkBuddy_IE:<userId>`），
        前端靠 `startsWith('WorkBuddy_IE:')` 把它从「未签上」文案里剔除。
        """
        t = A._signin_targets('WorkBuddy_IE', {})
        self.assertTrue(t['wb'], '国际版已接入活跃路径，应触发 --wb-only')
        self.assertFalse(t['trae'])
        self.assertEqual(t['pending'], ['WorkBuddy_IE:wbie-1'])

    def test_intl_activity_does_not_need_proof(self):
        """国际版没有当日入账凭证时仍要跑活跃动作。

        积分是**延迟自动入账**（Bonus Pack，约 03:12 前后到账），所以
        「今天还没凭证」是常态而非失败 —— 若无条件跳过，活跃动作就永远不会发，
        第二天也就永远等不到那 +30。
        """
        t = A._signin_targets('WorkBuddy_IE', {})
        self.assertIn('WorkBuddy_IE:wbie-1', t['pending'])

    def test_loomy_rides_wb_script(self):
        """Loomy 的补领挂在 --wb-only 里（见 signin_all.main 的补签段）。"""
        t = A._signin_targets('Loomy', {})
        self.assertTrue(t['wb'], 'Loomy 未领取时未触发补领')
        self.assertEqual(len(t['pending']), 1, t['pending'])

    def test_loomy_row_id_matches_signin_key(self):
        """pending 的 id 必须与签到表 key 同构，否则前端复核「补签是否成功」会对不上表。

        这是本函数最容易写错的一处：`_loomy_groups` 的 value 是
        `(序号, 账号 dict)` 元组，写成裸 dict 会静默得到错的 id。
        """
        t = A._signin_targets('Loomy', {})
        self.assertIn(LOOMY_ROW_ID, t['pending'],
                      f'Loomy 行 id 与签到表 key 不同构：{t["pending"]}')

    def test_loomy_signed_is_skipped(self):
        """合并行已有凭证 → Loomy 半边不再进 pending。"""
        t = A._signin_targets('Loomy', {LOOMY_ROW_ID: {}})
        self.assertFalse(t['wb'])
        self.assertEqual(t['pending'], [])

    def test_disabled_account_skipped(self):
        """enabled=false 的账号不参与补签（用户明确关掉的，不该被自动改状态）。"""
        self.cfg = _cfg(trae=[dict(TRAE_ACC, enabled=False)])
        t = A._signin_targets('Trae', {})
        self.assertFalse(t['trae'])
        self.assertEqual(t['pending'], [])

    def test_channel_filter_scopes_pending(self):
        """按通道筛选时，pending 不得混入其它通道（否则提示文案会说谎）。"""
        t = A._signin_targets('Trae', {})
        self.assertTrue(all(p.startswith('Trae:') for p in t['pending']), t['pending'])


class SigninArgvTest(unittest.TestCase):
    """_signin_argv: 命令行拼装（源码态 vs 打包态）。"""

    def test_source_mode_uses_venv_python(self):
        """源码态：用 scripts/<x>.py 的真路径 + 项目解释器。"""
        argv = A._signin_argv('signin_all.py', ['--wb-only'])
        self.assertTrue(argv[-1] == '--wb-only')
        self.assertTrue(any(a.endswith('signin_all.py') for a in argv))

    def test_source_mode_missing_script_raises_501(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            A._signin_argv('__nope__.py', [])
        self.assertEqual(ctx.exception.status_code, 501)

    def test_packaged_mode_uses_task_shim(self):
        """打包态：安装包不含 scripts\\*.py，必须走 open-ai-task.exe 路由标记。

        ★ 这里是「安装后签到/采集静默失效」的根因所在：若用
          os.path.exists(script) 当门槛，打包态必然 501，功能整条消失。
        """
        import sys
        orig_frozen = getattr(sys, 'frozen', None)
        sys.frozen = True  # type: ignore[attr-defined]
        try:
            argv = A._signin_argv('usage_collector.py', ['--collect'])
        finally:
            if orig_frozen is None:
                del sys.frozen  # type: ignore[attr-defined]
            else:
                sys.frozen = orig_frozen  # type: ignore[attr-defined]

        self.assertTrue(argv[0].lower().endswith('open-ai-task.exe'),
                        f'打包态未走 task shim：{argv}')
        # 脚本路径作为「路由标记」原样透传（不要求存在）
        self.assertTrue(any(a.endswith('usage_collector.py') for a in argv))
        self.assertEqual(argv[-1], '--collect')


class RefreshNeverRenewsTokenTest(unittest.TestCase):
    """补签脚本参数：刷新只许补签，不许顺带续期 token。"""

    def test_uses_only_idempotent_flags(self):
        """允许的开关只有 --wb-only / --trae-only。

        ★ 绝不允许出现 --force：那会让 signin_all 强制续期 TRAE token 并
          改写 config.json —— 续期是「重新连接」按钮的职责。刷新账号只是
          想把积分/签到状态看新一点，不该在背后动凭据。
        """
        allowed = {'--wb-only', '--trae-only'}
        for flag in allowed:
            rc, out = _fake_run(['--no-renew', flag])
            self.assertEqual(rc, 0, out)


def _fake_run(extra):
    """把 subprocess.run 换掉，只回显参数 —— 不真的拉起脚本。"""
    import subprocess
    orig = subprocess.run
    seen = {}

    class _P:
        returncode = 0
        stdout = 'fake'
        stderr = ''

    def fake(argv, **kw):
        seen['argv'] = argv
        return _P()

    subprocess.run = fake
    try:
        rc, out = A._run_signin_script(extra)
    finally:
        subprocess.run = orig
    assert not any('--force' in a for a in seen['argv']), seen['argv']
    return rc, out


class VersionStringTest(unittest.TestCase):
    """版本号规则（与前端 lib/utils.ts 的 formatVersion 一致）。"""

    @staticmethod
    def fmt(raw: str) -> str:
        import re
        s = raw.strip()
        if not s:
            return ''
        if re.match(r'^v\d', s, re.I):
            return s
        if re.match(r'^[a-z][a-z0-9]*[-_]v?\d', s, re.I):
            return s
        return 'v' + s

    def test_dev_version_kept_verbatim(self):
        """dev-v3.1 必须原样显示 —— 补前缀会得到 vdev-v3.1（用户实测的 bug）。"""
        self.assertEqual(self.fmt('dev-v3.1'), 'dev-v3.1')

    def test_portable_channel_version(self):
        self.assertEqual(self.fmt('portable-v3.0'), 'portable-v3.0')

    def test_plain_semver_gets_v(self):
        self.assertEqual(self.fmt('3.1.0'), 'v3.1.0')
        self.assertEqual(self.fmt('v2.4.0'), 'v2.4.0')

    def test_empty_stays_empty(self):
        """空值不许被补成 'v' —— 界面应显示「未知」而不是一个孤零零的 v。"""
        self.assertEqual(self.fmt(''), '')
        self.assertEqual(self.fmt('   '), '')


class ActualVersionFileTest(unittest.TestCase):
    """version.py 是版本号的唯一真源，格式必须被上面的规则正确渲染。"""

    def test_app_version_renders_unchanged(self):
        sys.path.insert(0, PROJECT_ROOT)
        from version import APP_VERSION  # type: ignore
        self.assertEqual(VersionStringTest.fmt(APP_VERSION), APP_VERSION,
                         'APP_VERSION 经格式化后变了 → 界面会显示错误的版本号')


if __name__ == '__main__':
    unittest.main()
