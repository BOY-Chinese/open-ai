# -*- coding: utf-8 -*-
"""Qoder 国内版（Qoder CN, qoder.com.cn）Provider —— 对外模型前缀 ``qdc-``

上游协议见 ``providers/qoder_common.py`` / ``providers/qoder_base.py``：
COSY 签名（RSA 包 AES 会话密钥 + MD5 请求签名）+ 自定义 Base64 请求体 +
SSE 信封。本文件只声明区域差异。

config.json::

    "providers": {
      "qoder_cn": {
        "enabled": true,
        "defaultModel": "auto",
        "timeout": 600,
        "switchEvery": 5,
        "accounts": [
          {"uid": "...", "nickname": "...", "realm": "cn",
           "accessToken": "...", "refreshToken": "...", "expiresAt": 0,
           "source": "oauth|pat", "enabled": true}
        ]
      }
    }

模型名：``qdc-<上游 key>``，例如 ``qdc-gmodel``（GLM-5.3）/ ``qdc-auto``。
"""
from .qoder_base import QoderProvider


class QoderCNProvider(QoderProvider):
    name = "qoder-cn"
    realm = "cn"
    PREFIX = "qdc-"


__all__ = ["QoderCNProvider"]
