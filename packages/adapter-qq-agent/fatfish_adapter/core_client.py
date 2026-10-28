"""Core-Engine 客户端：3 秒超时 + 立即降级。

铁律
----
1. **绝不长时间阻塞**：总超时 3s（可配），建连 1.5s 快速失败。
2. **超时 / 5xx / 解析失败一律降级**，不抛给宿主，不让用户干等。
3. 降级不特殊处理 —— 直接把兜底话术包成一条 ``text`` 动作，
   和正常动作走**同一条执行路径**，避免两套发送逻辑。

P0-9 幽灵状态补偿
-----------------
第 2 条铁律救的是「用户等待」，但 Core 可能只是**慢**、并没有失败——那条消息的
情绪账就这么凭空少了一笔。降级之后用**同一条原始消息 + 同一个 event_key**
异步补一次账，把漏掉的情绪补上；补账失败也绝不阻塞、绝不重试。
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, Awaitable, Callable, Optional

import httpx

from .config import AdapterConfig
from .models import CoreReply, InboundEvent

logger = logging.getLogger("ffw.adapter.core")

#: 原生 LLM 兜底回调：``async def (event) -> str``
NativeLLM = Callable[[InboundEvent], Awaitable[str]]

#: 补偿待发队列上限。满了自动丢最旧的一条——补账是尽力而为，不能反过来把
#: Adapter 自己的内存吃光。禁裸数字：这是运维要调的旋钮，得有个名字。
COMPENSATE_QUEUE_MAX = 100

#: 补账失败后的待补队列（仅供排查与后续人工重放，本模块不自动重发）。
compensate_backlog: deque[dict[str, Any]] = deque(maxlen=COMPENSATE_QUEUE_MAX)

#: 只有这些理由才补账。``bad_json`` / 4xx 是「Core 已处理」或「请求本身不合法」，
#: 补了也是错账（甚至是同一笔错账补第二遍）。
_COMPENSATE_REASONS = ("timeout", "network:", "http_5", "unexpected:")


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
        """生成降级回复：优先宿主原生 LLM，失败/未配置则用预设话术。

        降级回复生成后**不等待**地排一笔补偿：用户先拿到兜底话术，补账在后台跑。
        这里刻意用 ``create_task`` 而不是 ``await``——补账慢或挂都绝不能拖慢回复。
        """
        reply = await self._build_fallback_reply(event, reason)
        if any(reason.startswith(prefix) for prefix in _COMPENSATE_REASONS):
            # 先建 task 再返回：即使调用方立刻结束回合，事件循环也已持有它。
            try:
                asyncio.create_task(self._compensate(event, reason))
            except RuntimeError:
                # 没有运行中的事件循环（同步测试场景）：补账不是必须的，直接放弃
                logger.warning("[WARN] compensate failed: no running event loop")
                compensate_backlog.append(
                    {"payload": self._compensate_payload(event), "reason": reason}
                )
        return reply

    async def _build_fallback_reply(self, event: InboundEvent, reason: str) -> CoreReply:
        """真正决定降级话术：原生 LLM 优先，异常/空则回落预设文本。"""
        if self.native_llm is not None:
            try:
                text = await self.native_llm(event)
                if text:
                    return CoreReply.fallback(str(text), reason + "+native_llm")
            except Exception:
                logger.exception("原生 LLM 兜底也失败，改用预设话术")
        return CoreReply.fallback(self.config.fallback_text, reason)

    @staticmethod
    def _compensate_payload(event: InboundEvent) -> dict[str, Any]:
        """补偿请求体：**同一条原始消息、同一个 event_key**。

        ``original_event_key`` 必须是原请求真正用过的幂等键，否则 Core 那边会把
        这次补偿当成一条全新消息，同一个情绪被扣两遍——比不补更糟。
        """
        payload: dict[str, Any] = {
            "user_id": event.user_id,
            "message": event.message,
            "chat_scope": event.target.chat_scope,
            "trigger_type": event.trigger_type,
            # Adapter 侧没有独立生成 event_key，透传的是平台消息 ID——与
            # InboundEvent.to_payload() 的 event_key 优先级一致（Core 侧
            # 同样是 `req.event_key or req.platform_message_id`）。
            "original_event_key": event.platform_message_id,
        }
        if event.group_id is not None:
            payload["group_id"] = str(event.group_id)
        return payload

    def _compensate_url(self) -> str:
        """补偿端点 URL：与 ``config.core_api`` 同主机，只换路径。

        这样做的理由：配置里只有 ``core_api`` 一个旋钮，再加一个 URL 配置会让
        「两个地址配错一个」成为新的故障面。用 rsplit 保留可能存在的路径前缀。
        """
        base = self.config.core_api
        head, sep, _ = base.rpartition("/api/")
        if not sep:
            return base.rstrip("/") + "/api/v1/compensate"
        return head + "/api/v1/compensate"

    async def _compensate(self, event: InboundEvent, reason: str) -> None:
        """异步补账：成功只记 info，失败记 WARN 并进有界队列（不重试）。"""
        payload = self._compensate_payload(event)
        try:
            client = await self._get_client()
            response = await client.post(self._compensate_url(), json=payload)
            if response.status_code >= 400:
                raise httpx.HTTPStatusError(
                    "compensate http_" + str(response.status_code),
                    request=response.request,
                    response=response,
                )
            logger.info("compensate request sent")
        except Exception:
            logger.warning("[WARN] compensate failed")
            compensate_backlog.append({"payload": payload, "reason": reason})
