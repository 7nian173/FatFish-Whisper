"""FatFish-Whisper 宿主侧 Adapter 包。

为什么是包而不是一堆平铺模块
----------------------------
Core-Engine 里也有 ``models.py`` / ``main.py``。两者平铺在同一 ``sys.path`` 上会
互相遮蔽（``from models import ...`` 拿到谁全看插入顺序）。做成独立包
``fatfish_adapter`` 后，内部一律相对导入，永远不会和 Core 抢名字。
"""
from __future__ import annotations

from .config import AdapterConfig
from .core_client import CoreClient, NativeLLM
from .debounce import DebounceQueue, merge_events
from .executor import ActionExecutor, ExecutionResult
from .host import CallbackHost, HostAPI, MockHostAPI
from .models import Action, CoreReply, InboundEvent, Target
from .recall import RecallDecision, RecallRoller
from .sanitize import clean_cq, has_content
from .service import FatFishAdapter, TurnResult

__all__ = [
    "AdapterConfig",
    "CoreClient",
    "NativeLLM",
    "DebounceQueue",
    "merge_events",
    "ActionExecutor",
    "ExecutionResult",
    "CallbackHost",
    "HostAPI",
    "MockHostAPI",
    "Action",
    "CoreReply",
    "InboundEvent",
    "Target",
    "RecallDecision",
    "RecallRoller",
    "clean_cq",
    "has_content",
    "FatFishAdapter",
    "TurnResult",
]

__version__ = "0.4.0"
