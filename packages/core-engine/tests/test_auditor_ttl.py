"""H-A2：刺激缓存 TTL + 容量上限 + 指标。

背景：``SubconsciousAuditor._stimuli`` 曾只增不减（V2 引入的债），长期运行 +
多用户 = 无界增长。修复：惰性清理（TTL 30 分钟）+ 容量上限 1000（按 last_at
淘汰最旧）+ 可观测指标；清理异常绝不影响判定。
"""
from __future__ import annotations

import random
import threading

from auditor import STIMULUS_CACHE_MAX, STIMULUS_PRUNE_INTERVAL, STIMULUS_TTL_SECONDS
from auditor import SubconsciousAuditor


def _clock_auditor(now: float = 1_000_000.0):
    """可操控时钟的 auditor + 推进函数。"""
    box = {"t": now}
    aud = SubconsciousAuditor(clock=lambda: box["t"], rng=random.Random(0))

    def advance(seconds: float) -> None:
        box["t"] += seconds

    return aud, advance


# ---------------------------------------------------------------- prune 直接行为


def test_过期条目被清理():
    aud, advance = _clock_auditor()
    aud._bump("u-a", 1)
    aud._bump("u-b", -1)
    assert len(aud._stimuli) == 2

    advance(STIMULUS_TTL_SECONDS + 1)
    pruned = aud.prune()

    assert pruned == 2
    assert aud._stimuli == {}


def test_窗口内条目保留():
    aud, advance = _clock_auditor()
    aud._bump("u-a", 1)

    advance(STIMULUS_TTL_SECONDS - 1)
    assert aud.prune() == 0
    # 分房改造后键是 (user_id, chat_scope) 元组；_bump 不传 scope 即空串分区
    assert ("u-a", "") in aud._stimuli


def test_恰好等于TTL不算过期():
    """边界：``> TTL`` 才清，等于 TTL 保留（与 _bump 的窗口判定一致）。"""
    aud, advance = _clock_auditor()
    aud._bump("u-a", 1)

    advance(STIMULUS_TTL_SECONDS)
    assert aud.prune() == 0
    assert ("u-a", "") in aud._stimuli


def test_容量上限淘汰最旧():
    aud, advance = _clock_auditor()
    for i in range(STIMULUS_CACHE_MAX + 1):
        aud._bump("user-%04d" % i, 1)
        advance(0.001)  # 让 last_at 有先后

    assert len(aud._stimuli) == STIMULUS_CACHE_MAX + 1
    pruned = aud.prune()

    assert pruned == 1
    assert len(aud._stimuli) == STIMULUS_CACHE_MAX
    # 键是 (user_id, chat_scope)：淘汰按「分区条目」计数，这里 scope 均为空串
    assert ("user-0000", "") not in aud._stimuli, "最旧的应先被淘汰"
    assert ("user-%04d" % STIMULUS_CACHE_MAX, "") in aud._stimuli


def test_reset立即移除与prune不重复():
    """reset 与 prune 是两条独立路径：reset 立即弹条目，prune 管过期。"""
    aud, advance = _clock_auditor()
    aud._bump("u-a", 1)
    aud.reset("u-a")
    assert aud.snapshot("u-a") == {"polarity": 0, "count": 0}
    assert aud._stimuli == {}, "reset 应立即移除条目"

    advance(STIMULUS_TTL_SECONDS + 1)
    assert aud.prune() == 0, "已 reset 的条目不应再被计入清理"
    assert aud.stats()["stimulus_pruned_total"] == 0


# ---------------------------------------------------------------- audit 前的惰性清理


def test_audit前自动惰性清理():
    aud, advance = _clock_auditor()
    aud._bump("u-stale", 1)

    advance(STIMULUS_TTL_SECONDS + 1)
    result = aud.audit("u-stale", "随便说说", target=75)

    assert result.action in ("pass", "unlock")
    # 旧条目已被清，随后 _bump 重建（计数从 1 开始，不会带着旧窗口累加）
    assert aud.snapshot("u-stale")["count"] == 1
    assert aud.stats()["stimulus_pruned_total"] >= 1


def test_清理间隔内不重复扫():
    aud, advance = _clock_auditor()
    aud.audit("u-1", "你好", target=75)  # 首次：_last_prune_at=0 → 立即扫一次
    first = aud.stats()["stimulus_last_prune_at"]

    advance(STIMULUS_PRUNE_INTERVAL - 1)
    aud.audit("u-2", "你好", target=75)
    assert aud.stats()["stimulus_last_prune_at"] == first, "间隔内不应再扫"

    advance(2)
    aud.audit("u-3", "你好", target=75)
    assert aud.stats()["stimulus_last_prune_at"] > first


def test_清理异常不影响判定():
    aud, _ = _clock_auditor()

    def boom(now=None):
        raise RuntimeError("prune boom")

    aud.prune = boom  # type: ignore[method-assign]
    result = aud.audit("u-x", "开心开心开心", target=75)

    assert result.action in ("pass", "unlock")
    assert aud.snapshot("u-x")["count"] == 1, "清理挂了也不能丢计数"


# ---------------------------------------------------------------- 指标


def test_stats指标():
    aud, advance = _clock_auditor()
    aud._bump("u-a", 1)
    stats = aud.stats()
    assert stats["stimulus_cache_size"] == 1
    assert stats["stimulus_pruned_total"] == 0

    advance(STIMULUS_TTL_SECONDS + 1)
    aud.prune()
    stats = aud.stats()
    assert stats["stimulus_cache_size"] == 0
    assert stats["stimulus_pruned_total"] == 1


# ---------------------------------------------------------------- 并发


def test_并发50线程audit无异常():
    aud = SubconsciousAuditor(rng=random.Random(0))
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            for _ in range(5):
                # allow_unlock=False：解禁会 reset() 弹条目，与线程安全无关，
                # 这里只验并发下维护路径（prune/锁/计数）不炸
                aud.audit("u-%02d" % index, "开心开心开心", target=75, allow_unlock=False)
        except BaseException as exc:  # noqa: BLE001 - 测试就是要抓住任何异常
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert aud.stats()["stimulus_cache_size"] == 50
    assert all(aud.snapshot("u-%02d" % i)["count"] == 5 for i in range(50))
