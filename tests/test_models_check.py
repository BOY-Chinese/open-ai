# -*- coding: utf-8 -*-
"""测试: 管理面 POST /models/check —— 模型列表右键「检查该模型」探活端点

与 test_auto_chain_admin.py 同做法（不起真实网关）：
  - 往 sys.modules 注入假 `main` 模块（端点内部 `import main as gateway`）；
  - 但路由用的是**真实** providers.route_provider（端点内部 from providers import
    route_provider），因此假 PROVIDERS 的注册键必须用真实键名
    （loomy / trae / workbuddy / workbuddy-intl），前缀 lm- / tr- / wb- 才能命中；
  - 不读写真实 config.json（本端点不碰配置，CONFIG_PATH 仅防误触）。

验证点：
  - 三态归因与 Auto 路由链「检查」一致（ok / busy / down）；
  - 发往上游的确实是极短探测请求（CHECK_PROMPT「回复ok」，非流式）；
  - 缺 model → 400；网关不可达 → 500。

运行: pytest tests/test_models_check.py
      或 .venv/Scripts/python.exe tests/test_models_check.py（无 pytest 环境的直接跑法）
"""
import asyncio
import os
import sys
import tempfile
import types

try:
    import pytest
except ImportError:  # 无 pytest 环境（如项目 .venv）走文件尾的独立跑法
    pytest = None

if pytest is not None:
    pytest.importorskip("fastapi", reason="需要 fastapi（项目 .venv 内可跑）")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import admin_api  # noqa: E402
import auto_router as ar  # noqa: E402


# ─────────────────────────── 假网关（替代 `import main as gateway`） ───────────────────────────

class FakeProvider:
    """chat 行为按模型名查表；记录最近一次请求体，供断言「探测请求形状」用。"""

    def __init__(self, behaviors):
        self.behaviors = behaviors
        self.last_body = None

    def list_models(self):
        return [{"id": m, "object": "model"} for m in self.behaviors]

    async def chat(self, body):
        self.last_body = dict(body)
        await asyncio.sleep(0.01)
        b = self.behaviors.get(body.get("model"))
        if b is None:
            raise RuntimeError(f"HTTP 404: no model {body.get('model')}")
        if isinstance(b, Exception):
            raise b
        return b


def _fake_gateway():
    gw = types.ModuleType("main")
    # 注册键用真实键名：端点内部走真实 providers.route_provider（按 lm-/tr-/wb- 前缀找键）
    gw.PROVIDERS = {
        "loomy": FakeProvider({
            "lm-ok": {"choices": [{"message": {"content": "ok"}}]},
            "lm-busy": RuntimeError("HTTP 429 Too Many Requests"),
        }),
        "trae": FakeProvider({}),  # 目录里没有 tr-ghost → 上游 404
        "workbuddy": FakeProvider({
            "wb-down": RuntimeError("HTTP 401 Unauthorized"),
        }),
    }
    return gw


# ─────────────────────────── 夹具 ───────────────────────────

if pytest is not None:

    @pytest.fixture()
    def client(monkeypatch):
        """临时 config + 假网关 + 裸 app 的 TestClient。"""
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")

        monkeypatch.setattr(admin_api, "CONFIG_PATH", path)
        monkeypatch.setattr(ar, "CONFIG_PATH", path)
        monkeypatch.setitem(sys.modules, "main", _fake_gateway())

        app = FastAPI()
        app.include_router(admin_api.router)
        yield TestClient(app)
        os.unlink(path)


# ─────────────────────────── 用例 ───────────────────────────

def _post(client, model):
    return client.post("/v1/admin/models/check", json={"model": model})


def test_check_model_ok(client):
    r = _post(client, "lm-ok")
    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) == 1
    row = results[0]
    assert row["model"] == "lm-ok"
    assert row["status"] == "ok"
    assert row["latencyMs"] >= 0
    assert row["detail"]  # 有回复摘要


def test_check_model_sends_short_probe(client):
    """发往上游的必须是极短探测请求（与 auto_router.check_model 同核）。"""
    _post(client, "lm-ok")
    body = sys.modules["main"].PROVIDERS["loomy"].last_body
    assert body["model"] == "lm-ok"
    assert body["messages"] == [{"role": "user", "content": ar.CHECK_PROMPT}]
    assert "stream" not in body


def test_check_model_three_states(client):
    ok = _post(client, "lm-ok").json()["results"][0]["status"]
    busy = _post(client, "lm-busy").json()["results"][0]["status"]
    down = _post(client, "wb-down").json()["results"][0]["status"]
    assert (ok, busy, down) == ("ok", "busy", "down")


def test_check_model_upstream_unknown_is_down(client):
    """通道活着但模型不存在（上游 404）→ down，与路由链「检查」同归因。"""
    r = _post(client, "tr-ghost")
    assert r.status_code == 200
    assert r.json()["results"][0]["status"] == "down"


def test_check_model_requires_model(client):
    assert client.post("/v1/admin/models/check", json={}).status_code == 400


def test_check_model_gateway_unreachable(client, monkeypatch):
    """sys.modules['main']=None → `import main` 抛 ImportError → 500。"""
    monkeypatch.setitem(sys.modules, "main", None)
    r = _post(client, "lm-ok")
    assert r.status_code == 500


if __name__ == "__main__":
    # 无 pytest 的直接跑法：逐用例手动搭「临时 config + 假网关 + 裸 app」
    def make_client():
        fd, p = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(p, "w", encoding="utf-8") as f:
            f.write("{}")
        old_admin_cfg = admin_api.CONFIG_PATH
        old_ar_cfg = ar.CONFIG_PATH
        had_main = "main" in sys.modules
        old_main = sys.modules.get("main")
        admin_api.CONFIG_PATH = p
        ar.CONFIG_PATH = p
        sys.modules["main"] = _fake_gateway()
        app = FastAPI()
        app.include_router(admin_api.router)

        class _Ctx:
            def __enter__(self):
                return TestClient(app)

            def __exit__(self, *exc):
                admin_api.CONFIG_PATH = old_admin_cfg
                ar.CONFIG_PATH = old_ar_cfg
                if had_main:
                    sys.modules["main"] = old_main
                else:
                    sys.modules.pop("main", None)
                os.unlink(p)
        return _Ctx()

    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        if fn.__code__.co_argcount > 1:  # 需要 monkeypatch 的用例跳过（仅 pytest 下跑）
            print(f"SKIP {fn.__name__} (需要 pytest 夹具)")
            continue
        with make_client() as c:
            fn(c)
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests done")
