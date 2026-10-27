# -*- coding: utf-8 -*-
"""Qoder 国际版（Qoder Global, qoder.com）Provider —— 对外模型前缀 ``qd-``

与国内版同一套上游协议（见 ``providers/qoder_common.py``），差异全在区域配置：
openapi / 推理网关（api3 → api2 → api1 候选降级）/ device flow 参数 / 模型目录。

config.json::

    "providers": {
      "qoder_intl": {
        "enabled": true,
        "defaultModel": "auto",
        "timeout": 600,
        "accounts": [{"uid": "...", "realm": "intl", "accessToken": "...", ...}]
      }
    }

模型名：``qd-<上游 key>``，例如 ``qd-qmodel_38max``（Qwen3.8-Max）/ ``qd-auto``。
"""
from .qoder_base import QoderProvider


class QoderIntlProvider(QoderProvider):
    name = "qoder-intl"
    realm = "intl"
    PREFIX = "qd-"


__all__ = ["QoderIntlProvider"]
