# -*- coding: utf-8 -*-
"""测试: auto_router 多条自定义路由链（v3.2 重写后的核心行为）

覆盖:
  1. 旧单链配置 {enabled, timeout, models[]} 自动迁移为「无名1」;
  2. 多链归一化: 名称去重、id 补齐、sanitize_chains 校验;
  3. 链选择: 总名 / 历史别名 / 按链名; 禁用链与空链不参与; 链名与真实模型重名让位;
  4. auto_chat / auto_stream 按链故障转移, 逐模型独立超时;
  5. check_model 三态判定 (ok / busy / down)。

运行: pytest tests/test_auto_router.py
      或 python3 tests/test_auto_router.py（无 pytest 环境的直接跑法）
"""
import asyncio
import json
import sys
import tempfile
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto_router as ar  # noqa: E402


# ─────────────────────────── 测试工具 ───────────────────────────

def _with_cfg(tmp_cfg: dict, fn):
    """把 auto_router 的配置指到临时文件后执行 fn（不碰真实 config.json）。"""
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(tmp_cfg, f, ensure_ascii=False)
        old = ar.CONFIG_PATH
        ar.CONFIG_PATH = path
        try:
            return fn()
        finally:
            ar.CONFIG_PATH = old
    finally:
        os.unlink(path)


class FakeProvider:
    """可编程的假 provider: chat 行为按模型名查表。"""

    def __init__(self, behaviors: dict, models: list[str] | None = None):
        # behaviors: model -> callable(body) | 异常实例 | 值
        self.behaviors = behaviors
        self._models = models or list(behaviors.keys())

    def list_models(self):
        return [{"id": m, "object": "model"} for m in self._models]

    async def chat(self, body):
        b = self.behaviors.get(body.get("model"))
        if b is None:
            raise RuntimeError(f"HTTP 404: no model {body.get('model')}")
        if isinstance(b, Exception):
            raise b
        if callable(b):
            return b(body)
        return b

    async def stream(self, body):
        # 与真实 provider 同形: async generator (调用即返回, 不 await)
        b = self.behaviors.get(body.get("model"))
        if isinstance(b, Exception):
            raise b
        chunks = b(body) if callable(b) else (b or [])
        for chunk in chunks:
            yield chunk


def route_provider(model, providers):
    m = (model or "").lower()
    if m.startswith("lm-"):
        return providers.get("loomy")
    if m.startswith("wb-"):
        return providers.get("wb")
    return None


CHAIN = {"id": "c1", "name": "无名1", "enabled": True,
         "models": [{"model": "lm-a", "timeout": 0},
                    {"model": "wb-b", "timeout": 1}]}


# ─────────────────────────── 配置读取 / 迁移 ───────────────────────────

def test_legacy_config_migrates():
    """旧单链格式 → 一条名为「无名1」的链, 旧 timeout 逐模型继承。"""
    cfg = {"auto_chain": {"enabled": True, "timeout": 120,
                          "models": ["lm-a", "wb-b"]}}

    def run():
        chains = ar.load_auto_chains(cfg)
        assert len(chains) == 1
        c = chains[0]
        assert c["name"] == "无名1" and c["enabled"] is True
        assert c["models"] == [{"model": "lm-a", "timeout": 120.0},
                               {"model": "wb-b", "timeout": 120.0}]
        # 旧版兼容读取形状
        assert ar.load_auto_chain()["models"] == ["lm-a", "wb-b"]
    assert _with_cfg(cfg, run) is None


def test_multi_chain_normalization_and_dedupe():
    """多链格式: 名称重复自动补序号; 缺 id 自动补; 空链保留。"""
    cfg = {"auto_chain": {"chains": [
        {"name": "无名1", "enabled": True, "models": [{"model": "lm-a", "timeout": 5}]},
        {"name": "无名1", "models": ["wb-b"]},
        {"name": "", "models": []},
    ]}}

    def run():
        chains = ar.load_auto_chains(cfg)
        assert [c["name"] for c in chains] == ["无名1", "无名12", "无名3"]
        assert all(c["id"] for c in chains)
        assert chains[1]["models"] == [{"model": "wb-b", "timeout": 0.0}]
    assert _with_cfg(cfg, run) is None


def test_sanitize_chains_rejects_and_dedupes_ids():
    try:
        ar.sanitize_chains({"no": "pe"})
    except ValueError:
        pass
    else:
        raise AssertionError("非数组应抛 ValueError")
    out = ar.sanitize_chains([
        {"id": "x", "name": "A", "models": []},
        {"id": "x", "name": "B", "models": []},
    ])
    assert len({c["id"] for c in out}) == 2


# ─────────────────────────── 链选择 ───────────────────────────

def _select_cfg():
    return {"auto_chain": {"chains": [
        {"id": "c1", "name": "无名1", "enabled": True,
         "models": [{"model": "lm-a", "timeout": 0}]},
        {"id": "c2", "name": "备用链", "enabled": False,
         "models": [{"model": "wb-b", "timeout": 0}]},
        {"id": "c3", "name": "空链", "enabled": True, "models": []},
    ]}}


def test_resolve_by_total_name_and_alias():
    def run():
        c = ar.resolve_auto_chain("Auto路由链")
        assert c and c["name"] == "无名1"
        # 历史别名「Auto路由连」与 "Auto-mode" 仍指向第一条启用链
        assert ar.resolve_auto_chain("Auto路由连")["name"] == "无名1"
        assert ar.resolve_auto_chain("Auto-mode")["name"] == "无名1"
    assert _with_cfg(_select_cfg(), run) is None


def test_resolve_by_chain_name():
    def run():
        # 名称精确匹配 (忽略大小写与首尾空白)
        assert ar.resolve_auto_chain(" 无名1 ")["id"] == "c1"
        # 禁用链 / 空链不参与
        assert ar.resolve_auto_chain("备用链") is None
        assert ar.resolve_auto_chain("空链") is None
        assert ar.resolve_auto_chain("不存在的链") is None
    assert _with_cfg(_select_cfg(), run) is None


def test_chain_name_yields_to_real_model():
    """链名与 provider 已注册的真实模型重名 → 让位给真实模型。"""
    cfg = {"auto_chain": {"chains": [
        {"id": "c1", "name": "lm-a", "enabled": True,
         "models": [{"model": "wb-b", "timeout": 0}]},
    ]}}
    providers = {"loomy": FakeProvider({}, models=["lm-a"])}

    def run():
        assert ar.resolve_auto_chain("lm-a", providers) is None
        # 没注册该模型名的其它环境里, 链仍然命中
        assert ar.resolve_auto_chain("lm-a", {})["id"] == "c1"
    assert _with_cfg(cfg, run) is None


# ─────────────────────────── 链式故障转移 ───────────────────────────

def _providers_ok_second():
    return {"loomy": FakeProvider({"lm-a": RuntimeError("HTTP 500: busy upstream")}),
            "wb": FakeProvider({"wb-b": {"choices": [{"message": {"content": "hi"}}]}})}


def test_auto_chat_failover_and_per_model_timeout():
    providers = _providers_ok_second()

    async def run():
        out = await ar.auto_chat(route_provider, providers,
                                 {"messages": [{"role": "user", "content": "hi"}]}, CHAIN)
        assert out["model"] == "wb-b"
        # 第二个模型超时 1s → 全链失败, 报错包含两个模型的失败原因
        chain2 = {"id": "c2", "name": "全超时", "enabled": True,
                  "models": [{"model": "wb-b", "timeout": 1},
                             {"model": "lm-a", "timeout": 0}]}

        class Slow:
            async def chat(self, body):
                await asyncio.sleep(3)
        p2 = {"wb": Slow(), "loomy": FakeProvider({"lm-a": RuntimeError("HTTP 401")})}
        t0 = time.monotonic()
        try:
            await ar.auto_chat(route_provider, p2,
                               {"messages": []}, chain2)
        except ar.AutoExhausted as e:
            assert "超时" in str(e) and "401" in str(e)
        assert time.monotonic() - t0 < 2.5  # 第一个模型 1s 超时后即切换
    asyncio.run(run())


def test_auto_stream_skips_empty_first_model():
    """首个模型只产空 chunk → 静默切换到下一个有内容的模型。"""
    providers = {"loomy": FakeProvider({"lm-a": [
                    {"choices": [{"delta": {"role": "assistant"}}]}]}),
                 "wb": FakeProvider({"wb-b": [
                    {"choices": [{"delta": {"content": "ok"}}]},
                    {"choices": [{"delta": {}}]}]})}

    async def run():
        kinds = []
        async for kind, _payload in ar.auto_stream(route_provider, providers, {}, CHAIN):
            kinds.append(kind)
        assert "meta" in kinds and "chunk" in kinds
    asyncio.run(run())


# ─────────────────────────── check_model 三态 ───────────────────────────

def test_check_model_three_states():
    providers = {"loomy": FakeProvider({
        "lm-ok": {"choices": [{"message": {"content": "ok"}}]},
        "lm-busy": RuntimeError("HTTP 429 Too Many Requests"),
        "lm-busy2": RuntimeError("HTTP 502 Bad Gateway"),
        "lm-down": RuntimeError("HTTP 401 Unauthorized"),
    })}

    async def run():
        ok = await ar.check_model(route_provider, providers, "lm-ok")
        assert ok["status"] == "ok" and ok["latencyMs"] >= 0
        busy = await ar.check_model(route_provider, providers, "lm-busy")
        assert busy["status"] == "busy"
        busy2 = await ar.check_model(route_provider, providers, "lm-busy2")
        assert busy2["status"] == "busy"
        down = await ar.check_model(route_provider, providers, "lm-down")
        assert down["status"] == "down"
        # 无匹配通道 → 断连
        down2 = await ar.check_model(route_provider, providers, "wb-nowhere")
        assert down2["status"] == "down"
        # 超时 → 繁忙, 且检查等待被 cap 截断 (0.05s 配置 + cap 30 → 不应等 30s)
        class Slow:
            async def chat(self, body):
                await asyncio.sleep(2)
        p3 = {"wb": Slow()}
        t0 = time.monotonic()
        slow = await ar.check_model(route_provider, p3, "wb-b", 0.05)
        assert slow["status"] == "busy"
        assert time.monotonic() - t0 < 1.5
    asyncio.run(run())


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
