"""7 日演化系统：夜间备份 → 记忆对比 → 只做加法的追加词 → OOC 探针 → 膨胀压缩。

完整链路
--------
```
每天 03:00
   ├─ 活跃度闸门：当日消息 > 20 条才备份（避免把"今天没人说话"也当素材）
   ├─ 备份：persona.txt -> backups/persona_YYYYMMDD.txt
   ├─ 对比：7 天前的备份 vs 最新备份 + 这 7 天的记忆摘要
   ├─ 生成：LLM 只做加法，输出 ≤80 字追加词
   ├─ OOC 探针：把追加词丢回 LLM 判"是否与核心人设冲突"，冲突就丢弃
   ├─ 写入：追加到人设卡末尾
   └─ 膨胀控制：人设卡 > 1000 字 -> 强制 LLM 合并压缩
```

设计约束
--------
- **永不抛异常**：任何一步失败都只记日志 + 写进报告，绝不影响主服务；
- **只做加法**：演化 Prompt 明令禁止修改/删除既有设定；
- **可注入**：LLM、时钟、记忆提供者、token 监控全部可替换，方便测试。
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from monitor import (
    MOD_COMPRESS,
    MOD_EVOLVE,
    MOD_OOC,
    TokenMonitor,
    parse_usage,
)

logger = logging.getLogger("ffw.evolution")

# ---------------------------------------------------------------- 常量

EVOLUTION_HOUR: int = 3                 # 每天凌晨 3 点跑
MIN_DAILY_MESSAGES: int = 20            # 当日消息 > 20 条才算活跃
LOOKBACK_DAYS: int = 7                  # 回看 7 天
MAX_APPEND_CHARS: int = 80              # 追加词上限（字）
MAX_PERSONA_CHARS: int = 1000           # 人设卡膨胀阈值
MAX_DIGEST_ITEMS: int = 20              # 记忆摘要最多几条
BACKUP_DATE_FORMAT: str = "%Y%m%d"

EVOLVE_SYSTEM_PROMPT = (
    "你是角色人设的演化助手。你会看到一份【旧人设】和【最近的用户记忆】。\n"
    "任务：只做加法——提炼出这段关系里新出现的、值得固化进人设的变化，"
    "写成一段可以直接追加到人设卡末尾的补充说明。\n"
    "硬性要求：\n"
    "1. 只能新增，不得修改、不得删除、不得否定任何既有设定；\n"
    "2. 不超过 80 个字，一句话或两短句，不要分点、不要标题、不要引号；\n"
    "3. 只描述关系与相处方式的变化，不要复述具体事件细节；\n"
    "4. 直接输出追加文本本身，不要任何解释、前后缀或 Markdown。"
)

OOC_SYSTEM_PROMPT = (
    "你是人设一致性审查员。你会看到【核心人设】和一段【待追加内容】。\n"
    "判断这段追加内容是否与核心人设冲突（性格、说话方式、世界观、身份定位）。\n"
    "只输出严格 JSON：{\"conflict\": true 或 false, \"reason\": \"一句话理由\"}\n"
    "判断标准：轻微深化 = 不冲突；性格反转、身份改变、语气崩坏 = 冲突。"
)

COMPRESS_SYSTEM_PROMPT = (
    "你是人设卡压缩器。请把给出的人设卡压缩到 1000 字以内，"
    "必须完整保留：身份、性格底色、说话风格、核心关系、禁忌。"
    "删除重复表述与琐碎细节，不要新增任何设定。直接输出压缩后的人设卡正文。"
)


@dataclass
class EvolutionReport:
    """一次演化的完整报告（写日志/看板/测试断言都用它）。"""

    day: str = ""
    started_at: str = ""
    active_users: list[str] = field(default_factory=list)
    backed_up: list[str] = field(default_factory=list)
    evolved: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    persona_chars: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "started_at": self.started_at,
            "active_users": self.active_users,
            "backed_up": self.backed_up,
            "evolved": self.evolved,
            "skipped": self.skipped,
            "errors": self.errors,
            "persona_chars": self.persona_chars,
        }


class EvolutionEngine:
    """7 日演化任务。

    Args:
        db_path: Core 的 SQLite（读 ``message_counts`` 判断活跃度）。
        persona_path: 人设卡路径（默认 ``persona.txt``，不存在会自动创建占位）。
        backups_dir: 备份目录。
        memory: 记忆提供者（需要 ``digest(user_id, limit)``）；一般传
            :class:`memory_router.LocalSQLiteProvider` 或 ``MemoryRouter``。
        env: 配置字典（LLM_API_BASE / LLM_API_KEY / LLM_MODEL / LLM_TIMEOUT）。
        monitor: Token 监控；为 None 时只打日志。
        clock: 可注入时钟（返回 naive UTC datetime）。
        session_factory: 可注入的 SQLAlchemy sessionmaker；不传则自建。
    """

    def __init__(
        self,
        db_path: str,
        persona_path: str,
        backups_dir: str,
        memory: Optional[Any] = None,
        env: Optional[dict[str, str]] = None,
        monitor: Optional[TokenMonitor] = None,
        clock: Optional[Any] = None,
        session_factory: Optional[Any] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.db_path = db_path
        self.persona_path = persona_path
        self.backups_dir = backups_dir
        self.memory = memory
        self.env = dict(env if env is not None else os.environ)
        self.monitor = monitor
        self._clock = clock or (
            lambda: datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        )
        self._transport = transport

        os.makedirs(self.backups_dir, exist_ok=True)
        if session_factory is None:
            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker

            from models import Base, ensure_schema

            engine = create_engine(
                "sqlite:///" + db_path,
                future=True,
                connect_args={"check_same_thread": False},
            )
            Base.metadata.create_all(engine)
            ensure_schema(engine)
            self._own_engine = engine
            self.Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        else:
            self._own_engine = None
            self.Session = session_factory

        self._ensure_persona()

    # ---------------------------------------------------------- 配置

    @property
    def llm_enabled(self) -> bool:
        return bool(
            self.env.get("LLM_API_BASE")
            and self.env.get("LLM_API_KEY")
            and self.env.get("LLM_MODEL")
            and not str(self.env.get("LLM_API_KEY", "")).startswith("sk-xxx")
        )

    def close(self) -> None:
        if self._own_engine is not None:
            self._own_engine.dispose()

    # ---------------------------------------------------------- 人设卡

    def _ensure_persona(self) -> None:
        if os.path.exists(self.persona_path):
            return
        directory = os.path.dirname(os.path.abspath(self.persona_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.persona_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("# 人设卡（核心设定，勿删）\n你是潜鱼潜语，一只嘴硬心软的鲸。\n")

    def read_persona(self) -> str:
        try:
            with open(self.persona_path, "r", encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            return ""

    def write_persona(self, text: str) -> None:
        with open(self.persona_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    def persona_chars(self) -> int:
        return len(self.read_persona())

    def _backup_name(self, day: datetime.date) -> str:
        stem = os.path.splitext(os.path.basename(self.persona_path))[0]
        return os.path.join(self.backups_dir, stem + "_" + day.strftime(BACKUP_DATE_FORMAT) + ".txt")

    # ---------------------------------------------------------- 活跃度

    def daily_message_count(self, user_id: str, day: datetime.date) -> int:
        from sqlalchemy import select

        from models import MessageCount

        key = day.isoformat()
        try:
            with self.Session() as session:
                row = session.get(MessageCount, (user_id, key))
                return int(row.count) if row is not None else 0
        except Exception:
            logger.warning("读取活跃度失败", exc_info=True)
            return 0

    def active_users(self, day: datetime.date, threshold: int = MIN_DAILY_MESSAGES) -> list[str]:
        from sqlalchemy import select

        from models import MessageCount

        try:
            with self.Session() as session:
                rows = session.execute(
                    select(MessageCount.user_id).where(
                        MessageCount.day == day.isoformat(),
                        MessageCount.count > threshold,
                    )
                ).all()
            return [str(row[0]) for row in rows]
        except Exception:
            logger.warning("读取活跃用户失败", exc_info=True)
            return []

    # ---------------------------------------------------------- 备份

    def backup(self, day: Optional[datetime.date] = None) -> Optional[str]:
        """把人设卡复制成 ``backups/persona_YYYYMMDD.txt``；已存在则跳过。"""
        target_day = day or self._clock().date()
        target = self._backup_name(target_day)
        if os.path.exists(target):
            return target
        try:
            shutil.copyfile(self.persona_path, target)
            logger.info("人设卡已备份：%s", target)
            return target
        except OSError:
            logger.warning("人设卡备份失败", exc_info=True)
            return None

    def backup_for_day(self, day: datetime.date) -> Optional[str]:
        return self._backup_name(day) if os.path.exists(self._backup_name(day)) else None

    def latest_backup(self) -> Optional[str]:
        """按文件名里的日期取最新备份。"""
        stem = os.path.splitext(os.path.basename(self.persona_path))[0] + "_"
        candidates: list[tuple[str, str]] = []
        try:
            for name in os.listdir(self.backups_dir):
                if name.startswith(stem) and name.endswith(".txt"):
                    candidates.append((name[len(stem):-4], os.path.join(self.backups_dir, name)))
        except OSError:
            return None
        if not candidates:
            return None
        candidates.sort()
        return candidates[-1][1]

    def _read(self, path: Optional[str]) -> str:
        if not path or not os.path.exists(path):
            return ""
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            return ""

    # ---------------------------------------------------------- 主流程

    async def run(
        self, day: Optional[datetime.date] = None, force: bool = False
    ) -> EvolutionReport:
        """跑一轮完整演化（每天 03:00 调一次）。

        Args:
            day: 目标日期，默认今天（UTC）。
            force: True 时跳过活跃度闸门（测试/手动触发用）。
        """
        target_day = day or self._clock().date()
        report = EvolutionReport(
            day=target_day.isoformat(),
            started_at=self._clock().isoformat(timespec="seconds"),
        )

        try:
            users = self.active_users(target_day) if not force else self._all_users()
            report.active_users = list(users)

            if not users:
                logger.info("今天没有活跃用户，跳过演化")
                report.persona_chars = self.persona_chars()
                return report

            backup_path = self.backup(target_day)
            if backup_path:
                report.backed_up.append(backup_path)

            for user_id in users:
                try:
                    outcome = await self.evolve_user(user_id, target_day, force=force)
                except Exception as exc:  # 单个用户失败不影响其他人
                    logger.exception("演化失败：%s", user_id)
                    report.errors.append(user_id + ":" + str(exc))
                    continue
                if outcome.get("appended"):
                    report.evolved.append(outcome)
                else:
                    report.skipped.append(outcome)

            report.persona_chars = self.persona_chars()
        except Exception as exc:  # 兜底：cron 绝不能把服务带崩
            logger.exception("演化任务异常")
            report.errors.append(str(exc))
        return report

    def _all_users(self) -> list[str]:
        """force 模式下的全量用户：状态表 ∪ 消息统计表（可能只写了一边）。"""
        from sqlalchemy import select

        from models import MessageCount, UserState

        users: set[str] = set()
        try:
            with self.Session() as session:
                for row in session.execute(select(UserState.user_id)).all():
                    users.add(str(row[0]))
                for row in session.execute(select(MessageCount.user_id)).all():
                    users.add(str(row[0]))
        except Exception:
            return sorted(users)
        return sorted(users)

    async def evolve_user(
        self,
        user_id: str,
        day: Optional[datetime.date] = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """对单个用户跑一次演化，返回**演化建议**（不写盘，死线 D11）。

        Returns:
            结果字典。顶层键：

            - ``suggested_persona_append``（**必存在**）：建议追加到人设卡末尾的
              文本，没有可用建议时是空串 ``""``（绝不是 ``None``）；是否采纳由
              宿主决定，本模块不再写盘。
            - ``appended``：旧键，语义已改为「产出了可用建议」，
              **不再表示写过盘**。
            - ``reason`` / ``append`` / ``ooc`` / ``compressed`` /
              ``persona_chars``：旧键，保留兼容。

        ``persona_chars`` 是「若采纳该建议后人设卡的字数」估算值，不是写盘结果。
        """
        target_day = day or self._clock().date()
        result: dict[str, Any] = {
            "user_id": user_id,
            "day": target_day.isoformat(),
            "appended": False,
            "suggested_persona_append": "",
            "reason": "",
        }

        old_path = self._backup_name(target_day - datetime.timedelta(days=LOOKBACK_DAYS))
        old_persona = self._read(old_path)
        if not old_persona:
            result["reason"] = "no_backup_7_days_ago"
            return result

        current = self.read_persona()
        if not current:
            result["reason"] = "empty_persona"
            return result

        digest = self._memory_digest(user_id)
        if not digest:
            result["reason"] = "no_memory"
            return result

        if not self.llm_enabled:
            result["reason"] = "llm_disabled"
            return result

        append_text = await self.generate_append(old_persona, digest, user_id)
        if not append_text:
            result["reason"] = "llm_failed"
            return result
        result["append"] = append_text

        ok, reason = await self.ooc_check(current, append_text, user_id)
        result["ooc"] = {"passed": ok, "reason": reason}
        if not ok:
            result["reason"] = "ooc_rejected"
            return result

        merged = current.rstrip("\n") + "\n" + append_text + "\n"
        compressed = False
        if len(merged) > MAX_PERSONA_CHARS:
            shorter = await self.compress(merged, user_id)
            if shorter and len(shorter) <= MAX_PERSONA_CHARS:
                merged = shorter
                compressed = True
            else:
                result["reason"] = "compress_failed_keep_original"
                return result

        # 死线 D11：演化只产出建议，**不写盘**（写 persona.txt 会污染性格向量）。
        # write_persona 方法保留给宿主/夹具显式调用，演化流程绝不触碰。
        result["appended"] = True  # 语义：产出了可用建议
        result["suggested_persona_append"] = append_text
        result["reason"] = "ok"
        result["compressed"] = compressed
        result["persona_chars"] = len(merged)
        logger.info("演化建议产出：user=%s append=%s", user_id, append_text)
        return result

    def _memory_digest(self, user_id: str) -> str:
        if self.memory is None:
            return ""
        try:
            digest = self.memory.digest(user_id, limit=MAX_DIGEST_ITEMS)
        except TypeError:
            digest = self.memory.digest(user_id)
        except Exception:
            logger.warning("记忆摘要失败", exc_info=True)
            return ""
        return str(digest or "")

    # ---------------------------------------------------------- LLM 三步

    async def generate_append(self, old_persona: str, digest: str, user_id: str = "") -> str:
        """生成 ≤80 字的追加词；失败返回空串。"""
        prompt = (
            "【旧人设】\n" + old_persona.strip() + "\n\n"
            "【最近 7 天的用户记忆】\n" + digest.strip() + "\n\n"
            "请输出不超过 80 字的追加文本。"
        )
        text = await self._chat(EVOLVE_SYSTEM_PROMPT, prompt, MOD_EVOLVE, user_id)
        return self._sanitize_append(text)

    async def ooc_check(self, persona: str, append_text: str, user_id: str = "") -> tuple[bool, str]:
        """OOC 探针：冲突返回 (False, reason)；探针本身失败时**放行**（宁可不写也不错杀？）。

        这里的选择是**失败放行**：探针是"防污染"而非"安全闸"，LLM 挂了就
        只保留"只做加法"的硬约束，不让演化永久卡死。若你想要更保守的策略，
        把下面的 ``return True, ...`` 改成 ``return False, ...`` 即可。
        """
        prompt = (
            "【核心人设】\n" + persona.strip() + "\n\n"
            "【待追加内容】\n" + append_text.strip()
        )
        raw = await self._chat(OOC_SYSTEM_PROMPT, prompt, MOD_OOC, user_id, json_mode=True)
        if not raw:
            return True, "ooc_probe_unavailable"
        parsed = self._parse_json(raw)
        if parsed is None:
            return True, "ooc_probe_unparsable"
        conflict = bool(parsed.get("conflict", False))
        reason = str(parsed.get("reason", ""))[:120]
        return (not conflict), reason

    async def compress(self, persona: str, user_id: str = "") -> str:
        """人设卡膨胀压缩；失败返回空串。"""
        text = await self._chat(COMPRESS_SYSTEM_PROMPT, persona, MOD_COMPRESS, user_id)
        return (text or "").strip()

    # ---------------------------------------------------------- LLM 底座

    async def _chat(
        self,
        system: str,
        user: str,
        module: str,
        user_id: str = "",
        json_mode: bool = False,
    ) -> str:
        """调一次廉价 LLM，返回正文；失败返回空串（永不抛异常）。"""
        if not self.llm_enabled:
            return ""
        base = str(self.env.get("LLM_API_BASE", "")).rstrip("/")
        body: dict[str, Any] = {
            "model": self.env.get("LLM_MODEL", ""),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.3,
            "max_tokens": 400,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": "Bearer " + str(self.env.get("LLM_API_KEY", "")),
            "Content-Type": "application/json",
        }
        timeout = float(self.env.get("LLM_TIMEOUT", "8") or 8)

        try:
            kwargs: dict[str, Any] = {"timeout": timeout}
            if self._transport is not None:
                kwargs["transport"] = self._transport
            async with httpx.AsyncClient(**kwargs) as client:
                response = await client.post(base + "/chat/completions", json=body, headers=headers)
                response.raise_for_status()
                data = response.json()
        except Exception:
            logger.warning("演化 LLM 调用失败（%s）", module, exc_info=True)
            return ""

        usage = parse_usage(data)
        if self.monitor is not None:
            await self.monitor.arecord(module, usage, user_id)

        try:
            return str(data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError):
            return ""

    # ---------------------------------------------------------- 工具

    @staticmethod
    def _sanitize_append(text: str) -> str:
        """清洗 + 硬截断到 80 字（去掉模型爱加的引号/前缀/Markdown）。"""
        if not text:
            return ""
        cleaned = text.strip()
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip().strip('"').strip("'").strip("“”")
        cleaned = re.sub(r"^(追加|补充|追加内容|追加文本)[:：]\s*", "", cleaned)
        cleaned = cleaned.strip().strip('"').strip("'").strip("“”")
        cleaned = cleaned.replace("\n", " ").strip()
        if len(cleaned) > MAX_APPEND_CHARS:
            cleaned = cleaned[:MAX_APPEND_CHARS].rstrip("，。、； ")
        return cleaned

    @staticmethod
    def _parse_json(text: str) -> Optional[dict[str, Any]]:
        text = (text or "").strip()
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.S)
            if not match:
                return None
            try:
                value = json.loads(match.group(0))
                return value if isinstance(value, dict) else None
            except json.JSONDecodeError:
                return None

    # ---------------------------------------------------------- 定时器

    def next_run_at(self, now: Optional[datetime.datetime] = None) -> datetime.datetime:
        """下一个 03:00（UTC，naive）。"""
        moment = now or self._clock()
        target = moment.replace(hour=EVOLUTION_HOUR, minute=0, second=0, microsecond=0)
        if target <= moment:
            target += datetime.timedelta(days=1)
        return target

    async def scheduler(self, stop_event: Optional[asyncio.Event] = None) -> None:
        """夜间定时循环；用 ``stop_event.set()`` 可优雅退出。"""
        stop = stop_event or asyncio.Event()
        while not stop.is_set():
            now = self._clock()
            target = self.next_run_at(now)
            wait = max(0.0, (target - now).total_seconds())
            logger.info("下次演化时间：%s（%.0f 秒后）", target.isoformat(), wait)
            try:
                await asyncio.wait_for(stop.wait(), timeout=wait)
                return  # 收到停止信号
            except asyncio.TimeoutError:
                pass
            try:
                report = await self.run()
                logger.info("演化完成：%s", report.as_dict())
            except Exception:
                logger.exception("演化任务异常（已忽略，等下一轮）")


# ---------------------------------------------------------------- CLI

def _main() -> None:  # pragma: no cover - 手动触发用
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="手动跑一次 7 日演化")
    parser.add_argument("--core-dir", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    parser.add_argument("--force", action="store_true", help="跳过活跃度闸门")
    args = parser.parse_args()

    from dotenv import load_dotenv

    core_dir = args.core_dir
    load_dotenv(os.path.join(core_dir, ".env"), override=True)

    from memory_router import LocalSQLiteProvider

    evolution = EvolutionEngine(
        db_path=os.path.join(core_dir, "app.db"),
        persona_path=os.path.join(core_dir, "persona.txt"),
        backups_dir=os.path.join(core_dir, "backups"),
        memory=LocalSQLiteProvider(os.path.join(core_dir, "memory.db")),
        env=dict(os.environ),
    )
    report = asyncio.run(evolution.run(force=args.force))
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":  # pragma: no cover
    _main()
