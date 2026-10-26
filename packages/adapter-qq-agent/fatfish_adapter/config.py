"""Adapter 配置：全部可用环境变量覆盖，方便宿主插件面板改。"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


@dataclass
class AdapterConfig:
    """Adapter 运行参数。

    Attributes:
        core_api: Core-Engine 的 /api/v1/chat 地址。
        timeout: 请求 Core 的超时秒数；超时立刻降级，不让用户干等。
        typing_delay: 多条文本动作之间的拟人打字延迟区间（秒）。
        recall_cooldown: 撤回冷却秒数；撤回后该用户/群在该时间内不再判定。
        fallback_text: 降级时直接发出的兜底话术。
        connect_timeout: 建连超时（比总超时更短，快速失败）。
    """

    core_api: str = os.environ.get(
        "FFW_CORE_API", "http://127.0.0.1:8000/api/v1/chat"
    )
    timeout: float = _env_float("FFW_TIMEOUT", 3.0)
    typing_delay: tuple[float, float] = (
        _env_float("FFW_TYPING_MIN", 0.5),
        _env_float("FFW_TYPING_MAX", 1.5),
    )
    recall_cooldown: float = _env_float("FFW_RECALL_COOLDOWN", 180.0)
    fallback_text: str = os.environ.get("FFW_FALLBACK_TEXT", "（网卡了……等我一下）")
    connect_timeout: float = _env_float("FFW_CONNECT_TIMEOUT", 1.5)

    def as_timeout(self) -> "object":
        """构造 httpx.Timeout：建连快失败，读超时用总超时。"""
        import httpx

        return httpx.Timeout(self.timeout, connect=min(self.connect_timeout, self.timeout))
