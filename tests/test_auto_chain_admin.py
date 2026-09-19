# -*- coding: utf-8 -*-
"""测试: 管理面 Auto 路由链端点（GET/POST /auto-chain、create、check）

做法：不起真实网关 ——
  - 往 sys.modules 注入假 `main` 模块（admin_api 在端点内部 `import main as gateway`），
    只含 PROVIDERS（可编程假 provider）与 route_provider；
  - 把 admin_api / auto_router 的 CONFIG_PATH 指到临时文件，读写均不碰真实 config.json；
  - 裸 FastAPI app 挂 admin router（真实挂载时才带 require_auth 依赖）。

运行: pytest tests/test_auto_chain_admin.py
      或 .venv/Scripts/python.exe tests/test_auto_chain_admin.py（无 pytest 环境的直接跑法）
"""
import asyncio
import json
import os
import sys
import tempfile
import types

try:
    import pytest
except ImportError:  # 无 pytest 环境（如项目 .venv）走文件尾的独立跑法
    pytest = None

if pytest is not None:
    fastapi = pytest.importorskip("fastapi", reason="需要 fastapi（项目 .venv 内可跑）")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import auto_router as ar  # noqa: E402
import admin_api  # noqa: E402


# ─────────────────────────── 假网关（替代 `import main as gateway`） ───────────────────────────

class FakeProvider:
    """chat 行为按模型名查表; list_models 返回已注册模型（含重名让位测试用）。"""

    def __init__(self, behaviors, models=None):
        self.behaviors = behaviors
        self._models = models or list(behaviors.keys())

    def list_models(self):
        return [{"id": m, "object": "model"} for m in self._models]

    async def chat(self, body):
        await asyncio.sleep(0.01)
        b = self.behaviors.get(body.get("model"))
        if b is None:
            raise RuntimeError(f"HTTP 404: no model {body.get('model')}")
        if isinstance(b, Exception):
            raise b
        return b


def _route_provider(model, providers):
    m = (model or "").lower()
    if m.startswith("lm-"):
        return providers.get("loomy")
    if m.startswith("wb-"):
        return providers.get("wb")
    if m.startswith("tr-"):
        return providers.get("trae")
    return None


def _fake_gateway():
    gw = types.ModuleType("main")
    gw.PROVIDERS = {
        "loomy": FakeProvider({
            "lm-ok": {"choices": [{"message": {"content": "ok"}}]},
            "lm-busy": RuntimeError("HTTP 429 Too Many Requests"),
        }),
        "wb": FakeProvider({
            "wb-ok": {"choices": [{"message": {"content": "ok"}}]},
            "wb-down": RuntimeError("HTTP 401 Unauthorized"),
        }),
        # lm-chain-name 与下面的链名「lm-chain-name」重名 → 链应让位（网关侧逻辑）
        "trae": FakeProvider({}, models=["tr-real-model"]),
    }
    gw.route_provider = _route_provider
    return gw


# ─────────────────────────── 夹具 ───────────────────────────

if pytest is not None:

    @pytest.fixture()
    def client(monkeypatch):
        """临时 config + 假网关 + 裸 app 的 TestClient。"""
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            # 旧单链格式起步: 验证读取迁移
            json.dump({"auto_chain": {"enabled": True, "timeout": 120,
                                      "models": ["lm-ok", "wb-ok"]}},
                      f, ensure_ascii=False)

        monkeypatch.setattr(admin_api, "CONFIG_PATH", path)
        monkeypatch.setattr(ar, "CONFIG_PATH", path)
        monkeypatch.setitem(sys.modules, "main", _fake_gateway())

        app = FastAPI()
        app.include_router(admin_api.router)
        yield TestClient(app)
        os.unlink(path)


# ─────────────────────────── 用例 ───────────────────────────

def test_get_auto_chain_migrates_legacy(client):
    r = client.get("/v1/admin/auto-chain")
    assert r.status_code == 200
    body = r.json()
    assert len(body["chains"]) == 1
    assert body["chains"][0]["name"] == "无名1"
    assert body["chains"][0]["models"] == [{"model": "lm-ok", "timeout": 120.0},
                                           {"model": "wb-ok", "timeout": 120.0}]
    # availableModels 来自假网关 PROVIDERS
    assert "lm-ok" in body["availableModels"]


def test_create_auto_chain_names_and_persists(client):
    # 旧格式迁移出「无名1」→ 新链应为「无名2」, 且落盘为新格式
    r = client.post("/v1/admin/auto-chain/create")
    assert r.status_code == 200
    body = r.json()
    assert [c["name"] for c in body["chains"]] == ["无名1", "无名2"]
    assert body["created"]["name"] == "无名2" and body["created"]["enabled"] is True

    on_disk = json.load(open(admin_api.CONFIG_PATH, encoding="utf-8"))
    assert [c["name"] for c in on_disk["auto_chain"]["chains"]] == ["无名1", "无名2"]

    # 再建一条 → 无名3 (创建时会把已有链先 sanitize, id 不会重复)
    r2 = client.post("/v1/admin/auto-chain/create")
    assert [c["name"] for c in r2.json()["chains"]] == ["无名1", "无名2", "无名3"]


def test_save_auto_chain_roundtrip_and_validation(client):
    chains = [
        {"id": "c-a", "name": "主力链", "enabled": True,
         "models": [{"model": "lm-ok", "timeout": 60}, {"model": "wb-ok", "timeout": 0}]},
        {"id": "c-b", "name": "主力链", "enabled": False,
         "models": [{"model": "wb-down", "timeout": 30}]},
    ]
    r = client.post("/v1/admin/auto-chain", json={"chains": chains})
    assert r.status_code == 200
    out = r.json()["chains"]
    assert len(out) == 2
    assert out[0]["models"][1]["timeout"] == 0

    # 非法: chains 不是数组 → 400
    assert client.post("/v1/admin/auto-chain", json={"chains": {"x": 1}}).status_code == 400

    # GET 回读一致（重名链读取时补序号, 不丢链）
    got = client.get("/v1/admin/auto-chain").json()["chains"]
    assert {c["name"] for c in got} == {"主力链", "主力链2"}


def test_check_auto_chain_three_states(client):
    # 先落一条链: ok / busy / down 三个模型
    client.post("/v1/admin/auto-chain", json={"chains": [{
        "id": "c1", "name": "无名1", "enabled": True,
        "models": [{"model": "lm-ok", "timeout": 5},
                   {"model": "lm-busy", "timeout": 5},
                   {"model": "wb-down", "timeout": 5}],
    }]})

    r = client.post("/v1/admin/auto-chain/check", json={"chainId": "c1"})
    assert r.status_code == 200
    statuses = {x["model"]: x["status"] for x in r.json()["results"]}
    assert statuses == {"lm-ok": "ok", "lm-busy": "busy", "wb-down": "down"}

    # 单模型检查: 只返回该模型
    r2 = client.post("/v1/admin/auto-chain/check", json={"chainId": "c1", "model": "lm-ok"})
    results = r2.json()["results"]
    assert len(results) == 1 and results[0]["status"] == "ok"

    # 不存在的链 → 404; 不在链上的模型 → 400
    assert client.post("/v1/admin/auto-chain/check",
                       json={"chainId": "nope"}).status_code == 404
    assert client.post("/v1/admin/auto-chain/check",
                       json={"chainId": "c1", "model": "wb-ok"}).status_code == 400


if __name__ == "__main__":
    # 无 pytest 的直接跑法：逐用例手动搭「临时 config + 假网关 + 裸 app」
    def make_client(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"auto_chain": {"enabled": True, "timeout": 120,
                                      "models": ["lm-ok", "wb-ok"]}},
                      f, ensure_ascii=False)
        old_admin_cfg = admin_api.CONFIG_PATH
        old_ar_cfg = ar.CONFIG_PATH
        had_main = "main" in sys.modules
        old_main = sys.modules.get("main")
        admin_api.CONFIG_PATH = path
        ar.CONFIG_PATH = path
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
                os.unlink(path)
        return _Ctx()

    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fd, p = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with make_client(p) as c:
            fn(c)
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
