"""测试辅助工具（纯函数，无副作用）。

放在独立模块而不是 conftest，方便各测试文件显式 ``import _helpers as H``，
避免依赖 pytest 的 fixture 注入魔法。
"""
from __future__ import annotations

import datetime
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
for _path in (_SRC, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)


# ---------------------------------------------------------------- Track B 替身

def llm_payload(content, usage=None):
    """构造一个 OpenAI 兼容的响应体。"""
    data = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        data["usage"] = usage
    return data


def make_request_returning(payload):
    """返回一个 async 假 ``_request``：固定返回 payload。"""

    async def fake_request(body, headers):  # noqa: ARG001 - 保持签名一致
        return payload

    return fake_request


def make_request_raising(exc):
    """返回一个 async 假 ``_request``：直接抛异常。"""

    async def fake_request(body, headers):  # noqa: ARG001
        raise exc

    return fake_request


def patch_request(engine, fake_request):
    """把 ``engine.track_b._request`` 换成假实现，并统计调用次数。

    Returns:
        ``{"n": 调用次数}``，用例断言"未发请求"时读 ``calls["n"]``。
    """
    calls = {"n": 0}

    async def wrapper(body, headers):
        calls["n"] += 1
        return await fake_request(body, headers)

    engine.track_b._request = wrapper
    return calls


# ---------------------------------------------------------------- DB 播种 / 读取

def seed_state(engine, user_id, emotion=50, affinity=0, level="L0", hours_ago=0.0):
    """直接往临时库里写一行状态（用于构造衰减场景）。

    Args:
        hours_ago: ``last_update`` 距离现在的小时数，正数表示过去。
    """
    from models import UserState, utcnow

    moment = utcnow() - datetime.timedelta(hours=float(hours_ago))
    with engine.Session() as session:
        row = session.get(UserState, user_id)
        if row is None:
            row = UserState(
                user_id=user_id,
                emotion_current=int(emotion),
                emotion_level=level,
                affinity_raw=int(affinity),
                last_update=moment,
            )
            session.add(row)
        else:
            row.emotion_current = int(emotion)
            row.emotion_level = level
            row.affinity_raw = int(affinity)
            row.last_update = moment
        session.commit()


def get_state(engine, user_id):
    """读回一行状态；不存在返回 None。"""
    from models import UserState

    with engine.Session() as session:
        row = session.get(UserState, user_id)
        if row is None:
            return None
        return {
            "emotion": int(row.emotion_current),
            "level": row.emotion_level,
            "affinity": int(row.affinity_raw),
            "last_update": row.last_update,
        }


def count_users(engine, user_id):
    """统计某 user_id 的行数（验证并发只建一行）。"""
    from sqlalchemy import func, select

    from models import UserState

    with engine.Session() as session:
        statement = select(func.count()).select_from(UserState).where(
            UserState.user_id == user_id
        )
        return int(session.execute(statement).scalar_one())


# ---------------------------------------------------------------- 状态机模拟

def simulate_serial(start, target, steps, factor=0.2, max_delta=5.0, low=0, high=100,
                    habituation=False):
    """在测试侧独立复现实现的平滑公式，用于"串行期望值"对照。

    公式（与 engine.update_emotion 一致）::

        delta   = clamp(alpha * (target - E), -max_delta, +max_delta)
        E_next  = int(round(clamp(E + delta, low, high)))

    注意：每一步都取整，避免浮点累积偏差与实现不一致。

    ``habituation=True`` 时复现习惯化的 alpha 调度（同极性第 3 次起降档）：
    0.2 / 0.2 / 0.1 / 0.05 / 0.05 ...
    """
    value = float(start)
    polarity = 0 if abs(int(target) - 50) < 5 else (1 if target > 50 else -1)
    for index in range(int(steps)):
        alpha = float(factor)
        if habituation and polarity != 0:
            same = index + 1  # 含本次
            if same == 3:
                alpha = 0.1
            elif same >= 4:
                alpha = 0.05
        delta = max(-max_delta, min(max_delta, alpha * (float(target) - value)))
        value = int(round(max(low, min(high, value + delta))))
    return value
