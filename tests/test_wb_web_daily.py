# -*- coding: utf-8 -*-
"""
测试: scripts/wb_web_daily.py —— WorkBuddy 国际版网页端每日活跃
================================================================
来源：2026-09-18 五期实验判决《WB网页端拿积分方案.md》。

这个脚本的**全部价值**在于把四个实测踩过的坑钉死在请求里，
所以本测试逐条钉住它们 —— 少任何一条，上游都会 4xx 而不是入账：

  * 11128 首条消息必须是 system 角色
  * 11101 不支持非流式 → stream 必须 true
  * 11102 缺 model 字段 → 必须带 model
  * 403    /console/as/* 是 edge-sync 专属通道 → 必须走 /console/chat/completions

另外钉住「成功判定不得兜底」这条历史教训：旧签到脚本曾因兜底
`{'code': 0}` 把 15s 超时记成签到成功（2026-09-12 两条假成功）。

不联网：`post_sse` 的 urlopen 被换掉，只检查真实发出的请求形状。
运行: python -m unittest tests.test_wb_web_daily -v
"""
import io
import json
import os
import sys
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'scripts'))

import wb_web_daily as W  # noqa: E402


ACC = {
    'userId': 'ae1c0d1a-bb22-4c80-92d6-05b46cd72318',
    'accessToken': 'tok-abc',
    'name': '国际版账号(linyanxi100@gmail.com)',
    'enabled': True,
}
DOMAIN = 'www.workbuddy.ai'
PRODUCT = 'workbuddy-ai'


class BuildBodyTest(unittest.TestCase):
    """请求体：三个上游错误码全在这里规避。"""

    def test_first_message_is_system(self):
        """11128 first message is not system prompt —— 首条必须是 system。"""
        body = W.build_body('早上好', 'hy3')
        self.assertEqual(body['messages'][0]['role'], 'system',
                         '首条不是 system → 上游必报 11128')
        self.assertTrue(body['messages'][0]['content'])

    def test_user_message_follows(self):
        body = W.build_body('早上好', 'hy3')
        self.assertEqual(body['messages'][1], {'role': 'user', 'content': '早上好'})

    def test_stream_must_be_true(self):
        """11101 Non-stream chat request is currently not supported."""
        self.assertIs(W.build_body('hi', 'hy3')['stream'], True)

    def test_model_present(self):
        """11102 model [] service info not found —— 缺 model 必报。"""
        self.assertEqual(W.build_body('hi', 'hy3')['model'], 'hy3')

    def test_default_model_is_free_hy3(self):
        """方案文档统一用 hy3（免费、已实测 200、不走思考链）。"""
        self.assertEqual(W.DEFAULT_MODEL, 'hy3')


class HeadersTest(unittest.TestCase):
    """请求头：鉴权 + 通道标识，且不许带企业头。"""

    def setUp(self):
        self.h = W.build_headers(ACC, DOMAIN, PRODUCT)

    def test_bearer_token(self):
        self.assertEqual(self.h['Authorization'], 'Bearer tok-abc')

    def test_intl_identity_headers(self):
        self.assertEqual(self.h['X-User-Id'], ACC['userId'])
        self.assertEqual(self.h['X-Domain'], DOMAIN)
        self.assertEqual(self.h['X-Product'], PRODUCT)
        self.assertEqual(self.h['X-Product-Code'], PRODUCT)

    def test_accept_is_sse(self):
        self.assertEqual(self.h['Accept'], 'text/event-stream')

    def test_no_enterprise_header(self):
        """X-Enterprise-Id 会把请求切到企业视角，个人资源包被隐藏（与取资源包同一个坑）。"""
        self.assertNotIn('X-Enterprise-Id', self.h)


class EndpointTest(unittest.TestCase):
    """端点：必须走 /console/chat/completions，不能碰 /console/as/*。"""

    def test_chat_path(self):
        self.assertEqual(W.CHAT_PATH, '/console/chat/completions')

    def test_base_is_intl_host(self):
        self.assertEqual(W.INTL_BASE, 'https://www.workbuddy.ai')

    def test_never_uses_edge_sync_channel(self):
        """403 access_denied on /console/as/* —— 那是 edge-sync 专属，普通 token 不通。"""
        self.assertNotIn('/console/as', W.CHAT_PATH)


class RunOneTest(unittest.TestCase):
    """run_one / post_sse：成功判定与请求形状。"""

    def test_dry_run_sends_nothing(self):
        with mock.patch.object(W.urllib.request, 'urlopen') as uo:
            ok, detail = W.run_one(ACC, DOMAIN, PRODUCT, '早上好', 'hy3',
                                   60, dry_run=True, quiet=True)
        self.assertTrue(ok)
        self.assertEqual(detail, 'dry-run')
        uo.assert_not_called()

    def test_disabled_account_skipped(self):
        ok, detail = W.run_one(dict(ACC, enabled=False), DOMAIN, PRODUCT,
                               'hi', 'hy3', 60, quiet=True)
        self.assertFalse(ok)
        self.assertTrue(detail.startswith('skipped'))

    def test_missing_token_skipped(self):
        ok, detail = W.run_one(dict(ACC, accessToken=''), DOMAIN, PRODUCT,
                               'hi', 'hy3', 60, quiet=True)
        self.assertFalse(ok)
        self.assertTrue(detail.startswith('skipped'))

    def test_post_sse_rejects_non_200(self):
        """业务错误码必须判失败 —— 不能因为「没抛异常」就记成功。"""
        err = io.BytesIO(json.dumps({'code': 11128,
                                     'message': 'first message is not system prompt'}
                                    ).encode('utf-8'))
        with mock.patch.object(W.urllib.request, 'urlopen',
                               side_effect=W.urllib.error.HTTPError(
                                   W.INTL_BASE + W.CHAT_PATH, 400, 'Bad Request',
                                   {}, err)):
            ok, code, detail = W.post_sse(W.INTL_BASE + W.CHAT_PATH, {}, {}, 5)
        self.assertFalse(ok)
        self.assertEqual(code, 400)
        self.assertIn('11128', detail)

    def test_post_sse_rejects_heartbeat_only(self):
        """HTTP 200 但只有 : heartbeat → 没有模型应答，不算成功。"""
        class _Resp:
            status = 200

            def read(self):
                return b': heartbeat\n\n'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(W.urllib.request, 'urlopen', return_value=_Resp()):
            ok, code, detail = W.post_sse(W.INTL_BASE + W.CHAT_PATH, {}, {}, 5)
        self.assertFalse(ok, '只有心跳却被判成功 → 假成功 bug 复发')
        self.assertEqual(code, 200)

    def test_post_sse_accepts_chunk(self):
        """真实应答：data: {...chat.completion.chunk...} → 成功。"""
        payload = json.dumps({
            'id': 'cmb-x', 'model': 'hy3', 'object': 'chat.completion.chunk',
            'choices': [{'index': 0, 'delta': {'role': 'assistant',
                                               'content': '你好'}}],
        }).encode('utf-8')

        class _Resp:
            status = 200

            def read(self):
                return b': heartbeat\n\ndata: ' + payload + b'\n\n'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(W.urllib.request, 'urlopen', return_value=_Resp()):
            ok, code, detail = W.post_sse(W.INTL_BASE + W.CHAT_PATH, {}, {}, 5)
        self.assertTrue(ok)
        # ★ detail 现在是**原始 SSE 正文**（不再是 'ok'）—— 上层要用它
        #   解析 assistant 回复来回写会话（第 3 步）。旧版丢正文会让回写变空。
        self.assertIn('data:', detail)
        self.assertIn('chat.completion.chunk', detail)

    def test_transport_failure_is_failure(self):
        """超时/DNS 失败必须判失败（历史教训：超时被记成成功）。"""
        with mock.patch.object(W.urllib.request, 'urlopen',
                               side_effect=TimeoutError('timed out')):
            ok, code, detail = W.post_sse(W.INTL_BASE + W.CHAT_PATH, {}, {}, 5)
        self.assertFalse(ok)
        self.assertEqual(code, 0)
        self.assertIn('TimeoutError', detail)

    def test_run_one_creates_cloud_agent_task(self):
        """★ 端到端: 必须走 /console/as/conversations/ (Cloud Agent)。

        这是 master 界面 `/app/task/<id>` 对应的**唯一**体系。
        此前两版打的是 `/console/webchat/*` (webchat 旧体系), 在里面怎么调
        都不会出现在 master 界面上 —— 本测试即为该缺陷的回归门。
        """
        class _Resp:
            def __init__(self, body, status=200):
                self._body, self.status = body, status

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        calls = []

        def fake_urlopen(req, **kw):
            url, method = req.full_url, req.get_method()
            body = json.loads(req.data.decode('utf-8')) if req.data else None
            hdrs = {k.lower(): v for k, v in req.headers.items()}
            calls.append({'url': url, 'method': method, 'body': body, 'headers': hdrs})
            if url.endswith('/console/as/conversations/') and method == 'POST':
                return _Resp(json.dumps({'code': 0, 'data': {
                    'id': '2101202034802855936', 'name': '你好',
                    'status': 'CREATING'}}).encode())
            if '/console/as/conversations/2101202034802855936' in url:
                return _Resp(json.dumps({'code': 0, 'data': {
                    'id': '2101202034802855936', 'status': 'working'}}).encode())
            return _Resp(b'{}')

        with mock.patch.object(W.urllib.request, 'urlopen', side_effect=fake_urlopen):
            ok, detail = W.run_one(ACC, DOMAIN, PRODUCT, '你好', 'hy3', 60, quiet=True)

        self.assertTrue(ok, detail)
        self.assertEqual(len(calls), 2, [c['url'] for c in calls])

        # 创建: 必须是 as 端点 + 带尾斜杠 (不带会 403)
        create = calls[0]
        self.assertTrue(create['url'].endswith('/console/as/conversations/'),
                        f"必须走 Cloud Agent 端点, 实际 {create['url']}")
        self.assertNotIn('/console/webchat/', create['url'],
                         '又打回 webchat 旧体系了 → master 界面看不到')
        self.assertNotIn('/console/chat/completions', create['url'])
        self.assertEqual(create['method'], 'POST')
        self.assertEqual(create['body']['prompt'], '你好')
        self.assertEqual(create['body']['model'], 'hy3')

        # 回读确认 (独立路径验收)
        self.assertIn('/console/as/conversations/2101202034802855936', calls[1]['url'])

    def test_create_failure_is_reported(self):
        """创建失败 → 判失败 (不能因 HTTP 200 就自认成功)。"""
        def fake_urlopen(req, **kw):
            raise W.urllib.error.HTTPError(req.full_url, 403, 'denied', {},
                                           io.BytesIO(b'access_denied'))

        with mock.patch.object(W.urllib.request, 'urlopen', side_effect=fake_urlopen):
            ok, detail = W.run_one(ACC, DOMAIN, PRODUCT, '你好', 'hy3', 60, quiet=True)
        self.assertFalse(ok)
        self.assertIn('create-failed', detail)


class CloudAgentProtocolTest(unittest.TestCase):
    """Cloud Agent 体系 (/console/as/) 的协议契约。"""

    def test_endpoint_has_trailing_slash(self):
        """★ 必须带尾斜杠 —— 不带会 403 access_denied (实测)。"""
        self.assertTrue(W.AS_CONVERSATIONS_PATH.endswith('/'),
                        '不带尾斜杠会 403 access_denied')
        self.assertEqual(W.AS_CONVERSATIONS_PATH, '/console/as/conversations/')

    def test_legacy_webchat_path_is_not_used(self):
        """旧 webchat 端点不得出现在**任何实际请求**里。

        ★ 用 AST 精确检查: 凡是传给 json_request / post_sse / post_sse_retry
          的路径实参, 都不许是 webchat 端点。比字符串搜索可靠 ——
          字符串搜索会被注释和常量定义误伤。
        """
        import ast as _ast
        src = io.open(os.path.join(PROJECT_ROOT, 'scripts', 'wb_web_daily.py'),
                      encoding='utf-8').read()
        tree = _ast.parse(src)
        bad = []
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Call):
                continue
            fn = node.func
            name = getattr(fn, 'id', None) or getattr(fn, 'attr', None)
            if name not in ('json_request', 'post_sse', 'post_sse_retry'):
                continue
            for a in list(node.args) + [k.value for k in node.keywords]:
                if isinstance(a, _ast.Constant) and isinstance(a.value, str):
                    if '/console/webchat' in a.value:
                        bad.append((name, a.value))
                if isinstance(a, _ast.JoinedStr):
                    for v in a.values:
                        if isinstance(v, _ast.Constant) and isinstance(v.value, str) \
                                and '/console/webchat' in v.value:
                            bad.append((name, v.value))
        self.assertEqual(bad, [], f'实际请求里仍在用 webchat 端点: {bad}')

    def test_cloud_agent_path_has_retry(self):
        """★ 改走 /console/as/ 后必须仍有退避重试。

        历史缺口: 三版把请求从 post_sse 换成 json_request 时,
        重试机制**静默失效**了 (只剩 SSE 旧路径在用 post_sse_retry)。
        本测试钉住「创建任务走的是带重试的入口」。
        """
        import ast as _ast
        src = io.open(os.path.join(PROJECT_ROOT, 'scripts', 'wb_web_daily.py'),
                      encoding='utf-8').read()
        tree = _ast.parse(src)
        fn = next((n for n in _ast.walk(tree)
                   if isinstance(n, _ast.FunctionDef)
                   and n.name == 'create_agent_task'), None)
        self.assertIsNotNone(fn, '找不到 create_agent_task')
        called = {getattr(c.func, 'id', None) or getattr(c.func, 'attr', None)
                  for c in _ast.walk(fn) if isinstance(c, _ast.Call)}
        self.assertIn('json_request_retry', called,
                      'create_agent_task 未走带重试的入口 → 瞬时抖动会整天漏打')

    def test_json_request_retry_retries_transient(self):
        """瞬时故障 (5xx) 会重试并最终成功。"""
        seq = [(False, 503, 'busy'), (True, 200, {'ok': 1})]
        with mock.patch.object(W, 'json_request', side_effect=seq), \
                mock.patch.object(W.time, 'sleep') as sleeper:
            ok, code, d = W.json_request_retry('POST', '/x', {}, {}, 5, quiet=True)
        self.assertTrue(ok)
        self.assertEqual(sleeper.call_count, 1)

    def test_json_request_retry_no_retry_on_4xx(self):
        """4xx 业务错误 (含 403 access_denied) 不重试。"""
        with mock.patch.object(W, 'json_request',
                               return_value=(False, 403, 'access_denied')) as jr, \
                mock.patch.object(W.time, 'sleep') as sleeper:
            ok, code, d = W.json_request_retry('POST', '/x', {}, {}, 5, quiet=True)
        self.assertFalse(ok)
        self.assertEqual(code, 403)
        self.assertEqual(jr.call_count, 1, '4xx 被重试了')
        sleeper.assert_not_called()

    def test_default_is_hy3_and_minimal_text(self):
        """master 指定: hy3 (免费) + 极简内容「你好」, 以免消耗积分。"""
        self.assertEqual(W.DEFAULT_MODEL, 'hy3')
        self.assertEqual(W.DEFAULT_TEXT, '你好')

    def test_headers_carry_agent_auth(self):
        h = W.build_headers(ACC, DOMAIN, PRODUCT)
        self.assertEqual(h['Authentication'], 'tok-abc')
        self.assertEqual(h['X-Username'], ACC['userId'])
        self.assertEqual(h['Origin'], W.WEB_ORIGIN)

    def test_origin_referer_point_to_app(self):
        """master 界面是 /app/, Referer 应对齐。"""
        self.assertTrue(W.WEB_REFERER.startswith('https://www.workbuddy.ai/'))

    def test_ua_is_browser_not_client(self):
        self.assertIn('Mozilla/5.0', W.UA_WEB_CHAT)
        self.assertNotIn('WorkBuddyAI', W.UA_WEB_CHAT)

    def test_list_agent_tasks_parses(self):
        class _Resp:
            status = 200

            def read(self):
                return json.dumps({'code': 0, 'data': {'conversations': [
                    {'id': '2100473510743687168', 'name': '你好'}]}}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(W.urllib.request, 'urlopen', return_value=_Resp()):
            ok, convs = W.list_agent_tasks(ACC, DOMAIN, PRODUCT)
        self.assertTrue(ok)
        self.assertEqual(convs[0]['id'], '2100473510743687168')


class ConsistencyWithSigninAllTest(unittest.TestCase):
    """与 signin_all 的契约：国际版活跃动作必须真的用本脚本。"""

    def test_signin_all_uses_wb_web_daily(self):
        """signin_all.wb_intl_checkin_one 必须转发到本脚本，且不再打 daily-checkin。"""
        sys.path.insert(0, PROJECT_ROOT)
        sys.path.insert(0, os.path.join(PROJECT_ROOT, 'scripts'))
        import signin_all as S  # noqa: E402

        src = io.open(os.path.join(PROJECT_ROOT, 'scripts', 'signin_all.py'),
                      encoding='utf-8').read()
        self.assertIn('wb_web_daily', src, '国际版未接入网页端活跃脚本')
        # 国际版不得再打签到接口（10001 终态，纯刷日志）。
        # ⚠️ 只检查**可执行代码**，不检查 docstring —— 注释里必须能写出这些
        #    路径来解释「为什么不再走它」，否则这段历史就无从追溯。
        body = src.split('def wb_intl_checkin_one')[1].split('\ndef ')[0]
        code = body.split('"""')[2] if body.count('"""') >= 2 else body
        self.assertNotIn('daily-checkin', code,
                         '国际版仍在打 daily-checkin（10001 终态，补领无效）')
        self.assertNotIn('checkin-activity-status', code,
                         '国际版应直接走活跃路径，不必再探活动状态')
        self.assertIn('_wd.run_one', code, '国际版活跃动作未真正调用本脚本')
        self.assertTrue(callable(S.wb_intl_checkin_one))

    def test_signin_all_tags_are_activity_not_checkin(self):
        """日志标签要说「活跃」，不要再说「国际签到」—— 否则排障时语义误导。"""
        src = io.open(os.path.join(PROJECT_ROOT, 'scripts', 'signin_all.py'),
                      encoding='utf-8').read()
        self.assertNotIn("'WB国际签到'", src)
        self.assertIn("'WB国际活跃'", src)


class MainTest(unittest.TestCase):
    """main: 账号筛选与退出码。"""

    def setUp(self):
        # ★ 必须把状态文件重定向到临时目录：默认路径是项目真实的
        #   data/.wb_intl_activity_state，跑测试会**污染当天真实状态**
        #   （把账号标记成"今天已完成" → 生产环境的补跑被挡掉）。
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self._orig = W.ACTIVITY_STATE
        W.ACTIVITY_STATE = os.path.join(self.tmp, '.wb_intl_activity_state')
        self.addCleanup(lambda: setattr(W, 'ACTIVITY_STATE', self._orig))

    def _cfg_file(self):
        return {'providers': {'workbuddy_intl': {
            'domain': DOMAIN, 'product': PRODUCT,
            'accounts': [ACC, dict(ACC, userId='other', enabled=False)],
        }}}

    def test_no_account_returns_2(self):
        with mock.patch.object(W, 'load_config',
                               return_value={'providers': {'workbuddy_intl': {}}}):
            self.assertEqual(W.main(['--quiet']), 2)

    def test_uid_filter_selects_one(self):
        posted = []
        with mock.patch.object(W, 'load_config', return_value=self._cfg_file()), \
                mock.patch.object(W, 'run_one',
                                  side_effect=lambda acc, *a, **k: (
                                      posted.append(acc['userId']), (True, 'ok'))[1]):
            rc = W.main(['--uid', ACC['userId'], '--quiet'])
        self.assertEqual(rc, 0)
        self.assertEqual(posted, [ACC['userId']])

    def test_all_failed_returns_1(self):
        with mock.patch.object(W, 'load_config', return_value=self._cfg_file()), \
                mock.patch.object(W, 'run_one', return_value=(False, 'HTTP 500 x')):
            self.assertEqual(W.main(['--quiet']), 1)


class RetryTest(unittest.TestCase):
    """网络波动补救：退避重试的边界（瞬时故障重试 / 业务错误不重试）。"""

    def test_transport_failure_is_retryable(self):
        """code==0 是传输层失败（超时/DNS/TLS）→ 必须重试。"""
        self.assertTrue(W._retryable(0))

    def test_5xx_and_429_retryable(self):
        for code in (429, 500, 502, 503, 504):
            self.assertTrue(W._retryable(code), f'{code} 应可重试')

    def test_4xx_business_errors_not_retryable(self):
        """★ 四条实测坑都是 4xx —— 请求本身不对，重试一万次也是同一个错。"""
        for code in (400, 401, 403, 404, 422):
            self.assertFalse(W._retryable(code), f'{code} 不该重试')

    def test_backoff_within_broker_timeout(self):
        """★ 重试总时长必须 < Broker 的 TASK_TIMEOUT(300s)。

        否则整个 signin 任务会被 Broker 强杀 —— 这比单次失败更糟：
        连本该成功的其它平台签到/续期也一起被砍掉。
        """
        worst = sum(W.RETRY_BACKOFF) + W.DEFAULT_TIMEOUT * (len(W.RETRY_BACKOFF) + 1)
        self.assertLess(worst, 300, f'最坏重试耗时 {worst}s 会撞上 Broker 300s 超时')

    def test_retry_succeeds_after_transient_failures(self):
        """前两次瞬时失败、第三次成功 → 整体判成功（这才是"补救"的意义）。"""
        seq = [(False, 0, 'TimeoutError: timed out'),
               (False, 503, 'code=None message=upstream busy'),
               (True, 200, 'ok')]
        with mock.patch.object(W, 'post_sse', side_effect=seq), \
                mock.patch.object(W.time, 'sleep') as sleeper:
            ok, code, detail = W.post_sse_retry('u', {}, {}, 5, quiet=True)
        self.assertTrue(ok, detail)
        self.assertEqual(code, 200)
        # 退避必须真的等过 —— 否则是"立刻重试", 对瞬时抖动没用
        self.assertEqual([c.args[0] for c in sleeper.call_args_list],
                         list(W.RETRY_BACKOFF[:2]))

    def test_no_sleep_on_business_error(self):
        """4xx 业务错误：立即返回，不 sleep、不重试。"""
        calls = []
        with mock.patch.object(W, 'post_sse',
                               side_effect=lambda *a, **k: (calls.append(1), (
                                   False, 400, 'code=11101 Non-stream chat request '
                                               'is currently not supported'))[1]), \
                mock.patch.object(W.time, 'sleep') as sleeper:
            ok, code, detail = W.post_sse_retry('u', {}, {}, 5, quiet=True)
        self.assertFalse(ok)
        self.assertEqual(code, 400)
        sleeper.assert_not_called()
        self.assertEqual(len(calls), 1, '业务错误被重试了（纯浪费 + 拖长任务）')

    def test_exhausted_retries_returns_last_error(self):
        """退避用尽 → 返回最后一次的错误（供上层记日志/决定下轮补跑）。"""
        with mock.patch.object(W, 'post_sse',
                               return_value=(False, 0, 'URLError: dns fail')), \
                mock.patch.object(W.time, 'sleep'):
            ok, code, detail = W.post_sse_retry('u', {}, {}, 5, quiet=True)
        self.assertFalse(ok)
        self.assertEqual(code, 0)
        self.assertIn('dns fail', detail)

    def test_attempt_count_is_backoff_plus_one(self):
        """总尝试次数 = 退避档数 + 1（首试 + 3 次重试 = 4）。"""
        calls = []
        with mock.patch.object(W, 'post_sse',
                               side_effect=lambda *a, **k: (calls.append(1),
                                                            (False, 500, 'x'))[1]), \
                mock.patch.object(W.time, 'sleep'):
            W.post_sse_retry('u', {}, {}, 5, quiet=True)
        self.assertEqual(len(calls), len(W.RETRY_BACKOFF) + 1)


class DedupStateTest(unittest.TestCase):
    """逐账号当日去重：避免 30 分钟一轮的 --wb-only 一天白发几十条。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self._orig = W.ACTIVITY_STATE
        W.ACTIVITY_STATE = os.path.join(self.tmp, '.wb_intl_activity_state')
        self.addCleanup(lambda: setattr(W, 'ACTIVITY_STATE', self._orig))

    def test_fresh_state_is_empty(self):
        day, done = W.get_activity_state()
        self.assertEqual((day, done), ('', set()))

    def test_mark_and_read_back(self):
        W.mark_account_done('u-1')
        self.assertTrue(W.account_done_today('u-1'))
        self.assertFalse(W.account_done_today('u-2'))

    def test_state_is_per_account(self):
        """★ 逐账号而非全局：一个账号成功不得掩盖其它账号的失败。

        全局标记会让 u-2 当天永久拿不到 +30 —— 这是本设计最关键的一点。
        """
        W.mark_account_done('u-1')
        self.assertFalse(W.account_done_today('u-2'),
                         'u-1 的成功把 u-2 也标成已完成了')

    def test_mark_is_cumulative(self):
        W.mark_account_done('u-1')
        W.mark_account_done('u-2')
        self.assertTrue(W.account_done_today('u-1'))
        self.assertTrue(W.account_done_today('u-2'))

    def test_stale_day_is_ignored(self):
        """跨天必须失效 —— 裸 done 残留会永久压制今天的补跑（TRAE 踩过的坑）。"""
        W.set_activity_state(['u-1'])
        with mock.patch.object(W, 'today_str', return_value='2099-01-01'):
            self.assertFalse(W.account_done_today('u-1'),
                             '昨天的状态压制了今天')

    def test_corrupt_state_file_fails_open(self):
        """状态文件损坏 → 当作"还没成功"（宁可多发一条，也不漏打一天）。"""
        os.makedirs(os.path.dirname(W.ACTIVITY_STATE), exist_ok=True)
        with io.open(W.ACTIVITY_STATE, 'w', encoding='utf-8') as f:
            f.write('{not json')
        self.assertFalse(W.account_done_today('u-1'))

    def test_state_written_via_atomic_replace(self):
        """原子写入：不留半截文件（config.json 覆写事故的教训）。"""
        W.set_activity_state(['u-1'])
        self.assertTrue(os.path.exists(W.ACTIVITY_STATE))
        self.assertFalse(os.path.exists(W.ACTIVITY_STATE + '.tmp'))


class MainDedupTest(unittest.TestCase):
    """main 的逐账号去重与 --force。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self._orig = W.ACTIVITY_STATE
        W.ACTIVITY_STATE = os.path.join(self.tmp, '.wb_intl_activity_state')
        self.addCleanup(lambda: setattr(W, 'ACTIVITY_STATE', self._orig))
        self.cfg = {'providers': {'workbuddy_intl': {
            'domain': DOMAIN, 'product': PRODUCT,
            'accounts': [ACC, dict(ACC, userId='other')],
        }}}

    def test_done_account_is_skipped(self):
        W.mark_account_done(ACC['userId'])
        posted = []
        with mock.patch.object(W, 'load_config', return_value=self.cfg), \
                mock.patch.object(W, 'run_one',
                                  side_effect=lambda acc, *a, **k: (
                                      posted.append(acc['userId']), (True, 'ok'))[1]):
            rc = W.main(['--quiet'])
        self.assertEqual(rc, 0)
        self.assertEqual(posted, ['other'], '已完成的账号被重复发送了')

    def test_force_resends(self):
        W.mark_account_done(ACC['userId'])
        posted = []
        with mock.patch.object(W, 'load_config', return_value=self.cfg), \
                mock.patch.object(W, 'run_one',
                                  side_effect=lambda acc, *a, **k: (
                                      posted.append(acc['userId']), (True, 'ok'))[1]):
            W.main(['--quiet', '--force'])
        self.assertEqual(sorted(posted), [ACC['userId'], 'other'])

    def test_failed_account_not_marked(self):
        """失败不写状态 → daemon 30 分钟一轮还会来补（补救机制的核心）。"""
        with mock.patch.object(W, 'load_config', return_value=self.cfg), \
                mock.patch.object(W, 'run_one', return_value=(False, 'HTTP 0 timeout')):
            rc = W.main(['--quiet'])
        self.assertEqual(rc, 1)
        self.assertFalse(W.account_done_today(ACC['userId']),
                         '失败的账号被标记成已完成 → 当天永不重试')

    def test_dry_run_does_not_pollute_state(self):
        """★ dry-run 绝不能写状态 —— 否则演练一次就把当天标记成"已完成"。"""
        with mock.patch.object(W, 'load_config', return_value=self.cfg), \
                mock.patch.object(W, 'run_one', return_value=(True, 'dry-run')):
            W.main(['--quiet', '--dry-run'])
        self.assertFalse(W.account_done_today(ACC['userId']),
                         'dry-run 污染了当日状态')


class RoutingTest(unittest.TestCase):
    """task_main 要能把脚本名路由到内置模块（打包态不靠文件存在）。"""

    def test_task_main_routes_wb_web_daily(self):
        src = io.open(os.path.join(PROJECT_ROOT, 'scripts', 'task_main.py'),
                      encoding='utf-8').read()
        self.assertIn('wb_web_daily', src,
                      'task_main 未路由 wb_web_daily.py → 打包态将无法运行')


if __name__ == '__main__':
    unittest.main()
