"""出站代理 / CA 的统一读取（issue #7）。

读取顺序：.env > 标准环境变量 > 不设置（直连）。
bridge.py / send.py / channel_history.py 共用这一份实现，使它们在
cron / worker 等 stripped env 下行为一致。

约束：
- 仓库通用版不硬编码任何内网代理地址或秘密，部署值放在 .env。
- 不禁用 TLS 验证（CA_BUNDLE 仅在提供时覆盖默认系统 CA）。
"""

import os


def read_proxy_config(env):
    """从已加载的 .env 字典统读取代理与 CA 配置。

    env: load_env() 返回的 dict（键为字符串）。
    返回 (proxy_url, ca_bundle)，缺失时为 None。
    """
    env = env or {}
    proxy = (env.get("PROXY_URL")
             or os.environ.get("https_proxy")
             or os.environ.get("HTTPS_PROXY"))
    ca = (env.get("CA_BUNDLE")
          or os.environ.get("SSL_CERT_FILE"))
    return proxy, ca
