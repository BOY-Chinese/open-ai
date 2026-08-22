# -*- coding: utf-8 -*-
"""Open-AI 主网关 (WorkBuddy + Trae) Provider 注册表"""
import logging

logger = logging.getLogger("openapi.providers")


def build_providers(cfg: dict) -> dict:
    """根据 config.json 的 providers 段实例化 provider (仅 WorkBuddy + Trae)"""
    providers = {}
    providers_cfg = cfg.get("providers", {})

    # WorkBuddy 腾讯网关: 模型名 workbuddy-* / wb-*
    wb_cfg = providers_cfg.get("workbuddy", {})
    if wb_cfg.get("accessToken") or wb_cfg.get("accounts"):
        try:
            from .workbuddy import WorkBuddyProvider
            providers["workbuddy"] = WorkBuddyProvider(wb_cfg)
            logger.info("已注册 provider: workbuddy (腾讯网关)")
        except Exception as e:  # noqa: BLE001
            logger.error("workbuddy provider 初始化失败: %s", e)

    # Trae 内嵌 Node 后端 (Work 积分通道): 模型名 trae-* / tr-*
    trae_cfg = providers_cfg.get("trae", {})
    if trae_cfg.get("enabled", True):
        try:
            from .trae import TraeProvider
            providers["trae"] = TraeProvider(trae_cfg)
            logger.info("已注册 provider: trae (本地代理, Work 积分通道)")
        except Exception as e:  # noqa: BLE001
            logger.error("trae provider 初始化失败: %s", e)

    return providers


def route_provider(model: str, providers: dict):
    """按模型名路由到对应 provider (前缀明确匹配)"""
    m = (model or "").lower()
    if not providers:
        return None
    if "trae" in m or m.startswith("tr-"):
        return providers.get("trae")
    if "workbuddy" in m or m.startswith("wb-"):
        return providers.get("workbuddy")
    return next(iter(providers.values()))
