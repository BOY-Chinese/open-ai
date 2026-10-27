# -*- coding: utf-8 -*-
"""Open-AI 主网关 Provider 注册表

通道一览（对外模型前缀 → provider 注册键 → config.json 段名）::

    tr-       →  trae         →  trae         （内嵌 Node 后端 / Work 积分通道）
    wb-       →  workbuddy      →  workbuddy
    wbie-     →  workbuddy-intl →  workbuddy_intl （国际版）
    lm-       →  loomy          →  loomy
    qdc-      →  qoder-cn       →  qoder_cn       （Qoder 国内版，COSY 签名）
    qd-       →  qoder-intl     →  qoder_intl     （Qoder 国际版，COSY 签名）
    ca-       →  codearts       →  codearts       （华为云 CodeArts Agent）

★ 前缀路由存在包含关系，route_provider 里的判断顺序是**语义的一部分**：
  ``qdc-`` 必须在 ``qd-`` 之前（否则 qdc-* 会被当成 qd-*）；
  ``wbie-`` 必须在 ``wb-`` 之前；``lm-`` 必须在其它之前。
"""
import logging

logger = logging.getLogger("openapi.providers")


def build_providers(cfg: dict) -> dict:
    """根据 config.json 的 providers 段实例化 provider"""
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

    # WorkBuddy 国际版 (www.workbuddy.ai): 模型名 wbie-*
    intl_cfg = providers_cfg.get("workbuddy_intl", {})
    if intl_cfg.get("accessToken") or intl_cfg.get("accounts"):
        try:
            from .workbuddy_intl import WorkBuddyIntlProvider
            providers["workbuddy-intl"] = WorkBuddyIntlProvider(intl_cfg)
            logger.info("已注册 provider: workbuddy-intl (国际版)")
        except Exception as e:  # noqa: BLE001
            logger.error("workbuddy-intl provider 初始化失败: %s", e)

    # Trae 内嵌 Node 后端 (Work 积分通道): 模型名 trae-* / tr-*
    trae_cfg = providers_cfg.get("trae", {})
    if trae_cfg.get("enabled", True):
        try:
            from .trae import TraeProvider
            providers["trae"] = TraeProvider(trae_cfg)
            logger.info("已注册 provider: trae (本地代理, Work 积分通道)")
        except Exception as e:  # noqa: BLE001
            logger.error("trae provider 初始化失败: %s", e)

    # Loomy (讯飞 Loomy): 模型名 lm-*
    # ★ 与 workbuddy/intl 同规则: config 里已有账号才注册。此前缺省 enabled=True
    #   无条件注册, 令全新安装也把 DEFAULT_MODELS 静态目录挂上 /v1/models ——
    #   用户版「未登录却显示 Loomy 模型列表」即此因 (2026-09-14 VM 实测定位)。
    #   登录链路走 admin_api._loomy_client() 不依赖 provider 实例; 登录写入
    #   accounts 后 /v1/admin/reload-providers 热重建, 模型目录由上游实拉。
    loomy_cfg = providers_cfg.get("loomy", {})
    if loomy_cfg.get("enabled", True) and (loomy_cfg.get("accounts") or loomy_cfg.get("session")):
        try:
            from .loomy import LoomyProvider
            providers["loomy"] = LoomyProvider(loomy_cfg)
            logger.info("已注册 provider: loomy (讯飞 Loomy)")
        except Exception as e:  # noqa: BLE001
            logger.error("loomy provider 初始化失败: %s", e)

    # Qoder 国内版 (qoder.com.cn): 模型名 qdc-*
    # ★ 有账号才注册 —— 与 workbuddy 同规则，避免未登录也把静态模型目录挂出去
    qdc_cfg = dict(providers_cfg.get("qoder_cn") or {})
    if qdc_cfg.get("enabled", True) and qdc_cfg.get("accounts"):
        qdc_cfg["_cfg_key"] = "qoder_cn"
        try:
            from .qoder_cn import QoderCNProvider
            providers["qoder-cn"] = QoderCNProvider(qdc_cfg)
            logger.info("已注册 provider: qoder-cn (Qoder 国内版, qdc-*)")
        except Exception as e:  # noqa: BLE001
            logger.error("qoder-cn provider 初始化失败: %s", e)

    # Qoder 国际版 (qoder.com): 模型名 qd-*
    qd_cfg = dict(providers_cfg.get("qoder_intl") or {})
    if qd_cfg.get("enabled", True) and qd_cfg.get("accounts"):
        qd_cfg["_cfg_key"] = "qoder_intl"
        try:
            from .qoder_intl import QoderIntlProvider
            providers["qoder-intl"] = QoderIntlProvider(qd_cfg)
            logger.info("已注册 provider: qoder-intl (Qoder 国际版, qd-*)")
        except Exception as e:  # noqa: BLE001
            logger.error("qoder-intl provider 初始化失败: %s", e)

    # 华为云 CodeArts Agent (码道): 模型名 ca-*
    ca_cfg = dict(providers_cfg.get("codearts") or {})
    if ca_cfg.get("enabled", True) and ca_cfg.get("accounts"):
        try:
            from .codearts import CodeArtsProvider
            providers["codearts"] = CodeArtsProvider(ca_cfg)
            logger.info("已注册 provider: codearts (华为云码道, ca-*)")
        except Exception as e:  # noqa: BLE001
            logger.error("codearts provider 初始化失败: %s", e)

    return providers


def route_provider(model: str, providers: dict):
    """按模型名路由到对应 provider (前缀明确匹配)。

    ★ 判断顺序 = 语义, 见模块文档: qdc- 先于 qd-, wbie- 先于 wb-。
    """
    m = (model or "").lower()
    if not providers:
        return None
    # Loomy: 必须放在其它前缀判断之前 (子串匹配 "loomy" in m)
    # v3.1 起对外前缀是 lm- (loomy- 为历史前缀, 仍兼容)
    if m.startswith("lm-") or "loomy" in m:
        return providers.get("loomy") or next(iter(providers.values()))
    if "trae" in m or m.startswith("tr-"):
        return providers.get("trae")
    # Qoder 国内版: qdc- 必须在 qd- 之前 (qdc-* 也以 qd 开头)
    if m.startswith("qdc-") or m.startswith("qoder-cn-"):
        return providers.get("qoder-cn")
    # Qoder 国际版: qd-*（不含 qdc-*，上面已拦）
    if m.startswith("qd-") or m.startswith("qoder-"):
        return providers.get("qoder-intl") or providers.get("qoder-cn")
    # CodeArts: ca- 前缀（历史 codearts- 前缀同样兼容）
    if m.startswith("ca-") or m.startswith("codearts-"):
        return providers.get("codearts")
    # WorkBuddy 国际版: wbie- 前缀 / 历史 wbai- 前缀 / 名字含 intl
    # (必须排在 wb- 判断之前, 否则 wbie-xxx 会被误判给国内版 provider)
    if m.startswith("wbie") or m.startswith("wbai") or "intl" in m:
        return providers.get("workbuddy-intl") or providers.get("workbuddy")
    if "workbuddy" in m or m.startswith("wb-"):
        return providers.get("workbuddy")
    return next(iter(providers.values()))
