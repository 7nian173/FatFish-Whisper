"""Core-Engine 客户端：3 秒超时 + 立即降级。

铁律
----
1. **绝不长时间阻塞**：总超时 3s（可配），建连 1.5s 快速失败。
2. **超时 / 5xx / 解析失败一律降级**，不抛给宿主，不让用户干等。
3. 降级不特殊处理 —— 直接把兜底话术包成一条 ``text`` 动作，
   和正常动作走**同一条执行路径**，避免两套发送逻辑。
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

import httpx

from .config import AdapterConfig
from .models import CoreReply, InboundEvent

logger = logging.getLogger("ffw.adapter.core")

#: 原生 LLM 兜底回调：``async def (event) -> str``
NativeLLM = Callable[[InboundEvent], Awaitable[str]]


class CoreClient:
    """请求 Core-Engine 的轻客户端。

    Args:
        config: Adapter 配置。
        native_llm: 可选的原生 LLM 兜底；不传就用 ``config.fallback_text``。
        transport: 测试注入用（如 ``httpx.ASGITransport(app=core_app)``）。
        client: 直接注入一个已配置好的 ``httpx.AsyncClient``（优先级最高）。
    """

    def __init__(
        self,
        config: Optional[AdapterConfig] = None,
        native_llm: Optional[NativeLLM] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.config = config or AdapterConfig()
        self.native_llm = native_llm
        self._transport = transport
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            kwargs: dict[str, Any] = {"timeout": self.config.as_timeout()}
            if self._transport is not None:
                kwargs["transport"] = self._transport
                kwargs["base_url"] = "http://core"
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def ask(self, event: InboundEvent) -> CoreReply:
        """向 Core 要一次判定；**永不抛异常**，失败返回降级回复。"""
        try:
            client = await self._get_client()
            response = await client.post(self.config.core_api, json=event.to_payload())
        except httpx.TimeoutException as exc:
            logger.warning("Core 超时（%s），立即降级", exc.__class__.__name__)
            return await self._fallback(event, "timeout")
        except httpx.HTTPError as exc:
            logger.warning("Core 网络异常（%s），立即降级", exc.__class__.__name__)
            return await self._fallback(event, "network:" + exc.__class__.__name__)
        except Exception as exc:  # 兜底：任何意外都不许拖挂宿主
            logger.exception("Core 请求未知异常，降级")
            return await self._fallback(event, "unexpected:" + str(exc))

        if response.status_code >= 500:
            logger.warning("Core 返回 %s，立即降级", response.status_code)
            return await self._fallback(event, "http_" + str(response.status_code))

        if response.status_code >= 400:
            logger.warning("Core 返回 %s（请求不合法），降级", response.status_code)
            return await self._fallback(event, "http_" + str(response.status_code))

        try:
            data = response.json()
        except ValueError:
            logger.warning("Core 响应不是合法 JSON，降级")
            return await self._fallback(event, "bad_json")

        if not isinstance(data, dict):
            return await self._fallback(event, "bad_shape")

        return CoreReply.from_raw(data)

    async def _fallback(self, event: InboundEvent, reason: str) -> CoreReply:
        """生成降级回复：优先宿主原生 LLM，失败/未配置则用预设话术。"""
        if self.native_llm is not None:
            try:
                text = await self.native_llm(event)
                if text:
                    return CoreReply.fallback(str(text), reason + "+native_llm")
            except Exception:
                logger.exception("原生 LLM 兜底也失败，改用预设话术")
        return CoreReply.fallback(self.config.fallback_text, reason)
